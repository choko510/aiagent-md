"""DEV-11 PR Guardian: strict HEAD-verified CI/conflict evaluation + Sentinel-routed pagination.

Hardens P1-09:
- Refuses PASS if live PR head.sha != expected_head_sha (returns STALE_HEAD).
- Paginates check-runs and inspects GitHub commit statuses; returns UNKNOWN if pagination is incomplete.
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

    if commit_status_payload is not None:
        statuses = commit_status_payload.get("statuses") or []
        if isinstance(statuses, list):
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

    missing_req = tuple(req for req in required_checks if req not in succeeded_names)
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

    if unknown_conclusion or missing_req or mergeable_state == "blocked" or not succeeded_names:
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
    """Inspects PR, paginated check-runs, and commit statuses via a Sentinel-routed read-only GET transport."""

    def __init__(self, read_transport: Callable[[ProjectScope, str, str], tuple[int, dict[str, Any]]]) -> None:
        self._read = read_transport

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
    ) -> GuardianEvaluation:
        if pr_number < 1:
            raise ScopeDenied("invalid pr_number")
        require_sha(head_sha)
        st_pr, pr_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/pulls/{pr_number}")
        if st_pr != 200:
            return GuardianEvaluation("UNKNOWN", (), tuple(required_checks), None, "unknown", False, False)

        all_runs: list[dict[str, Any]] = []
        total_count: int | None = None
        pagination_complete = True
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
        commit_status = status_data if st_status == 200 else None

        return evaluate_pr_checks(
            pr_payload=pr_data,
            check_runs_payload={
                "total_count": total_count if total_count is not None else len(all_runs),
                "check_runs": all_runs,
            },
            expected_head_sha=head_sha,
            commit_status_payload=commit_status,
            required_checks=required_checks,
            repair_attempts=repair_attempts,
            max_repair_attempts=max_repair_attempts,
            pagination_complete=pagination_complete,
        )
