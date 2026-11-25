"""DEV-11 PR Guardian: strict HEAD-verified CI/conflict evaluation + Sentinel-routed pagination (P1-09 / R2-08).

Hardens P1-09 & R2-08:
- Refuses PASS if live PR head.sha != expected_head_sha (returns STALE_HEAD).
- Paginates check-runs and inspects GitHub commit statuses; returns UNKNOWN if pagination OR status API fails.
- Requires an authoritative non-empty `required_checks` set (from project policy and/or GitHub Branch Protection
  required_status_checks); refuses PASS when no authoritative required check policy is available.
- Treats unknown conclusion values, mergeable=None / unknown / blocked, neutral/skipped-only checks,
  or missing required_checks fail-closed as PENDING or UNKNOWN (never PASS).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ProjectScope, ScopeDenied, require_sha

_FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "cancelled", "stale"})
_KNOWN_CONCLUSIONS = _FAIL_CONCLUSIONS | {"success", "neutral", "skipped"}
_MAX_CHECK_PAGES = 10
_PER_PAGE = 100


@dataclass(frozen=True)
class GuardianEvaluation:
    ci_state: str  # PASS | PENDING | FAIL | CONFLICT | STALE_HEAD | UNKNOWN | ESCALATED_LIMIT_REACHED
    failing_checks: tuple[str, ...]
    missing_required_checks: tuple[str, ...]
    observed_head_sha: str | None
    mergeable_state: str
    should_repair: bool
    escalated: bool


def evaluate_pr_checks(  # noqa: PLR0911, PLR0912, PLR0915
    *,
    pr_payload: dict[str, Any],
    check_runs_payload: dict[str, Any],
    expected_head_sha: str | None = None,
    commit_status_payload: dict[str, Any] | None = None,
    commit_status_ok: bool = True,
    required_checks: Sequence[str] = (),
    repair_attempts: int,
    max_repair_attempts: int,
    pagination_complete: bool = True,
) -> GuardianEvaluation:
    """Pure fail-closed evaluation of GitHub PR, check-runs, and commit statuses."""
    head_obj = pr_payload.get("head")
    observed_sha = str(head_obj.get("sha") or "") if isinstance(head_obj, dict) else None
    if expected_head_sha is not None:
        require_sha(expected_head_sha)
        if not observed_sha or observed_sha != expected_head_sha:
            return GuardianEvaluation(
                ci_state="STALE_HEAD",
                failing_checks=(),
                missing_required_checks=tuple(required_checks),
                observed_head_sha=observed_sha,
                mergeable_state=str(pr_payload.get("mergeable_state") or "unknown").lower(),
                should_repair=False,
                escalated=False,
            )

    mergeable_state = str(pr_payload.get("mergeable_state") or "unknown").lower()
    mergeable = pr_payload.get("mergeable")
    if mergeable is False or mergeable_state == "dirty":
        exhausted = repair_attempts >= max_repair_attempts
        return GuardianEvaluation(
            ci_state="ESCALATED_LIMIT_REACHED" if exhausted else "CONFLICT",
            failing_checks=("merge_conflict",),
            missing_required_checks=(),
            observed_head_sha=observed_sha,
            mergeable_state=mergeable_state,
            should_repair=not exhausted,
            escalated=exhausted,
        )

    runs = check_runs_payload.get("check_runs")
    if not isinstance(runs, list):
        return GuardianEvaluation("UNKNOWN", (), tuple(required_checks), observed_sha, mergeable_state, False, False)

    total_count = check_runs_payload.get("total_count")
    if not pagination_complete or (isinstance(total_count, int) and total_count > len(runs)):
        return GuardianEvaluation("UNKNOWN", (), tuple(required_checks), observed_sha, mergeable_state, False, False)

    failing: list[str] = []
    pending = False
    unknown_conclusion = False
    succeeded_names: set[str] = set()

    for r in runs:
        if not isinstance(r, dict):
            unknown_conclusion = True
            continue
        name = str(r.get("name") or "").strip() or "check"
        status = str(r.get("status") or "").lower()
        conclusion = str(r.get("conclusion") or "").lower()
        if status != "completed":
            pending = True
        elif conclusion in _FAIL_CONCLUSIONS:
            failing.append(name)
        elif conclusion == "success":
            succeeded_names.add(name)
        elif conclusion in ("neutral", "skipped"):
            continue
        elif conclusion not in _KNOWN_CONCLUSIONS:
            unknown_conclusion = True

    if not commit_status_ok:
        unknown_conclusion = True
    elif commit_status_payload is not None:
        statuses = commit_status_payload.get("statuses")
        if not isinstance(statuses, list):
            unknown_conclusion = True
        else:
            for st_item in statuses:
                if not isinstance(st_item, dict):
                    unknown_conclusion = True
                    continue
                ctx = str(st_item.get("context") or "status")
                state = str(st_item.get("state") or "").lower()
                if state in ("failure", "error"):
                    failing.append(ctx)
                elif state == "pending":
                    pending = True
                elif state == "success":
                    succeeded_names.add(ctx)
                else:
                    unknown_conclusion = True

    if failing:
        exhausted = repair_attempts >= max_repair_attempts
        return GuardianEvaluation(
            ci_state="ESCALATED_LIMIT_REACHED" if exhausted else "FAIL",
            failing_checks=tuple(dict.fromkeys(failing)),
            missing_required_checks=(),
            observed_head_sha=observed_sha,
            mergeable_state=mergeable_state,
            should_repair=not exhausted,
            escalated=exhausted,
        )

    clean_required = tuple(dict.fromkeys(r.strip() for r in required_checks if r and r.strip()))
    missing_req = tuple(req for req in clean_required if req not in succeeded_names)
    if pending or mergeable is None or mergeable_state == "unknown":
        return GuardianEvaluation(
            ci_state="PENDING",
            failing_checks=(),
            missing_required_checks=missing_req,
            observed_head_sha=observed_sha,
            mergeable_state=mergeable_state,
            should_repair=False,
            escalated=False,
        )

    # R2-08: Never return PASS without a non-empty authoritative required check policy or with incomplete status API.
    if unknown_conclusion or not clean_required or missing_req or mergeable_state == "blocked" or not succeeded_names:
        return GuardianEvaluation(
            ci_state="UNKNOWN",
            failing_checks=(),
            missing_required_checks=missing_req,
            observed_head_sha=observed_sha,
            mergeable_state=mergeable_state,
            should_repair=False,
            escalated=False,
        )

    return GuardianEvaluation(
        ci_state="PASS",
        failing_checks=(),
        missing_required_checks=(),
        observed_head_sha=observed_sha,
        mergeable_state=mergeable_state,
        should_repair=False,
        escalated=False,
    )


class PrGuardian:
    """Inspects PR, paginated check-runs, commit statuses, and branch protection via Sentinel GET transport."""

    def __init__(self, read_transport: Callable[[ProjectScope, str, str], tuple[int, dict[str, Any]]]) -> None:
        self._read = read_transport

    def _fetch_branch_protection_checks(
        self, scope: ProjectScope, intent_ref: str, base_branch: str | None
    ) -> tuple[tuple[str, ...], bool]:
        if not base_branch:
            return (), True
        st, data = self._read(
            scope,
            intent_ref,
            f"/repos/{scope.repository}/branches/{base_branch}/protection/required_status_checks",
        )
        if st == 404:
            return (), True
        if st != 200 or not isinstance(data, dict):
            return (), False
        names: list[str] = []
        raw_contexts = data.get("contexts")
        if isinstance(raw_contexts, list):
            names.extend(str(x).strip() for x in raw_contexts if isinstance(x, str) and x.strip())
        raw_checks = data.get("checks")
        if isinstance(raw_checks, list):
            for item in raw_checks:
                if isinstance(item, dict) and isinstance(item.get("context"), str) and item["context"].strip():
                    names.append(item["context"].strip())
        return tuple(dict.fromkeys(names)), True

    def inspect_pr(
        self,
        *,
        scope: ProjectScope,
        intent_ref: str,
        pr_number: int,
        head_sha: str,
        repair_attempts: int,
        max_repair_attempts: int,
        required_checks: Sequence[str] = (),
        base_branch: str | None = None,
    ) -> GuardianEvaluation:
        if pr_number < 1:
            raise ScopeDenied("invalid pr_number")
        require_sha(head_sha)
        st_pr, pr_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/pulls/{pr_number}")
        if st_pr != 200:
            return GuardianEvaluation("UNKNOWN", (), tuple(required_checks), None, "unknown", False, False)

        resolved_base = base_branch
        if not resolved_base and isinstance(pr_data.get("base"), dict):
            raw_ref = pr_data["base"].get("ref")
            if isinstance(raw_ref, str) and raw_ref.strip():
                resolved_base = raw_ref.strip()

        bp_checks, bp_ok = self._fetch_branch_protection_checks(scope, intent_ref, resolved_base)
        merged_required = tuple(dict.fromkeys([*required_checks, *bp_checks]))

        all_runs: list[dict[str, Any]] = []
        total_count: int | None = None
        pagination_complete = bp_ok
        for page in range(1, _MAX_CHECK_PAGES + 1):
            st_chk, chk_data = self._read(
                scope,
                intent_ref,
                f"/repos/{scope.repository}/commits/{head_sha}/check-runs?per_page={_PER_PAGE}&page={page}",
            )
            if st_chk != 200:
                pagination_complete = False
                break
            page_runs = chk_data.get("check_runs")
            if not isinstance(page_runs, list):
                pagination_complete = False
                break
            if isinstance(chk_data.get("total_count"), int):
                total_count = chk_data["total_count"]
            all_runs.extend(page_runs)
            if len(page_runs) < _PER_PAGE or (total_count is not None and len(all_runs) >= total_count):
                break
        else:
            if total_count is None or len(all_runs) < total_count:
                pagination_complete = False

        st_status, status_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/commits/{head_sha}/status")
        status_ok = st_status == 200 and isinstance(status_data, dict)
        return evaluate_pr_checks(
            pr_payload=pr_data,
            check_runs_payload={
                "total_count": total_count if total_count is not None else len(all_runs),
                "check_runs": all_runs,
            },
            expected_head_sha=head_sha,
            commit_status_payload=status_data if status_ok else None,
            commit_status_ok=status_ok,
            required_checks=merged_required,
            repair_attempts=repair_attempts,
            max_repair_attempts=max_repair_attempts,
            pagination_complete=pagination_complete,
        )
