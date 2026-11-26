"""DEV-11 PR Guardian: strict HEAD-verified CI/conflict evaluation + Branch Protection/Ruleset/App ID (R2-08 / R3-10).

Hardens P1-09, R2-08, and R3-10:
- Refuses PASS if live PR head.sha != expected_head_sha (returns STALE_HEAD).
- Paginates check-runs and inspects GitHub commit statuses; returns UNKNOWN if pagination OR status API fails.
- Inspects `/branches/{base}` (`protected` flag), `/branches/{base}/protection/required_status_checks` (preserving
  `app_id`), AND `/rules/branches/{base}` (GitHub Rulesets `required_status_checks`, preserving `integration_id`).
  If `protected=True` and protection/ruleset reads return 403/404 (insufficient permissions), fails closed as UNKNOWN.
- Enforces `app_id` matching on required checks so a forged check from a different GitHub App cannot satisfy a rule.
- Refuses PASS when no authoritative required check policy is available, or on unknown conclusion / mergeable=blocked.
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
class RequiredCheckRule:
    """Authoritative required CI check rule, optionally pinned to a specific GitHub App ID (R3-10)."""

    context: str
    app_id: int | None = None


@dataclass(frozen=True)
class GuardianEvaluation:
    ci_state: str  # PASS | PENDING | FAIL | CONFLICT | STALE_HEAD | UNKNOWN | ESCALATED_LIMIT_REACHED
    failing_checks: tuple[str, ...]
    missing_required_checks: tuple[str, ...]
    observed_head_sha: str | None
    mergeable_state: str
    should_repair: bool
    escalated: bool


def _merge_required_rules(
    required_checks: Sequence[str],
    required_check_rules: Sequence[RequiredCheckRule],
) -> tuple[RequiredCheckRule, ...]:
    merged: list[RequiredCheckRule] = []
    seen: set[tuple[str, int | None]] = set()
    for rule in required_check_rules:
        ctx = rule.context.strip()
        if not ctx:
            continue
        key = (ctx, rule.app_id)
        if key not in seen:
            seen.add(key)
            merged.append(RequiredCheckRule(ctx, rule.app_id))
    for name in required_checks:
        ctx = name.strip()
        if not ctx:
            continue
        # If an app-pinned rule already covers this context, keep the stricter app-pinned rule.
        if any(r.context == ctx for r in merged):
            continue
        key = (ctx, None)
        if key not in seen:
            seen.add(key)
            merged.append(RequiredCheckRule(ctx, None))
    return tuple(merged)


def evaluate_pr_checks(  # noqa: PLR0911, PLR0912, PLR0915
    *,
    pr_payload: dict[str, Any],
    check_runs_payload: dict[str, Any],
    expected_head_sha: str | None = None,
    commit_status_payload: dict[str, Any] | None = None,
    commit_status_ok: bool = True,
    required_checks: Sequence[str] = (),
    required_check_rules: Sequence[RequiredCheckRule] = (),
    repair_attempts: int,
    max_repair_attempts: int,
    pagination_complete: bool = True,
) -> GuardianEvaluation:
    """Pure fail-closed evaluation of GitHub PR, check-runs (with App ID), and commit statuses."""
    all_rules = _merge_required_rules(required_checks, required_check_rules)
    rule_names = tuple(r.context for r in all_rules)

    head_obj = pr_payload.get("head")
    observed_sha = str(head_obj.get("sha") or "") if isinstance(head_obj, dict) else None
    if expected_head_sha is not None:
        require_sha(expected_head_sha)
        if not observed_sha or observed_sha != expected_head_sha:
            return GuardianEvaluation(
                ci_state="STALE_HEAD",
                failing_checks=(),
                missing_required_checks=rule_names,
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
        return GuardianEvaluation("UNKNOWN", (), rule_names, observed_sha, mergeable_state, False, False)

    total_count = check_runs_payload.get("total_count")
    if not pagination_complete or (isinstance(total_count, int) and total_count > len(runs)):
        return GuardianEvaluation("UNKNOWN", (), rule_names, observed_sha, mergeable_state, False, False)

    failing: list[str] = []
    pending = False
    unknown_conclusion = False
    succeeded_names: set[str] = set()
    succeeded_app_checks: set[tuple[str, int]] = set()

    for r in runs:
        if not isinstance(r, dict):
            unknown_conclusion = True
            continue
        name = str(r.get("name") or "").strip() or "check"
        status = str(r.get("status") or "").lower()
        conclusion = str(r.get("conclusion") or "").lower()
        app_obj = r.get("app")
        app_id = app_obj.get("id") if isinstance(app_obj, dict) and type(app_obj.get("id")) is int else None
        if status != "completed":
            pending = True
        elif conclusion in _FAIL_CONCLUSIONS:
            failing.append(name)
        elif conclusion == "success":
            succeeded_names.add(name)
            if app_id is not None:
                succeeded_app_checks.add((name, app_id))
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

    missing_list: list[str] = []
    for rule in all_rules:
        if rule.app_id is not None:
            if (rule.context, rule.app_id) not in succeeded_app_checks:
                missing_list.append(f"{rule.context}(app:{rule.app_id})")
        elif rule.context not in succeeded_names:
            missing_list.append(rule.context)
    missing_req = tuple(missing_list)

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

    # R2-08 / R3-10: Never return PASS without a non-empty authoritative required check policy, exact App ID match,
    # or complete branch protection / ruleset / commit status visibility.
    if unknown_conclusion or not all_rules or missing_req or mergeable_state == "blocked" or not succeeded_names:
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
    """Inspects PR, paginated check-runs, commit statuses, branch protection, and rulesets via Sentinel GET transport."""

    def __init__(self, read_transport: Callable[[ProjectScope, str, str], tuple[int, Any]]) -> None:
        self._read = read_transport

    def _fetch_branch_protection_checks(  # noqa: PLR0912
        self, scope: ProjectScope, intent_ref: str, base_branch: str | None
    ) -> tuple[tuple[RequiredCheckRule, ...], bool]:
        """Fetch required status check rules from BOTH Branch Protection and GitHub Rulesets, preserving App IDs (R3-10)."""
        if not base_branch:
            return (), True

        # 1. Inspect branch metadata to distinguish genuinely unprotected branches from 404 due to missing permissions.
        st_br, br_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/branches/{base_branch}")
        if st_br != 200 or not isinstance(br_data, dict):
            return (), False
        is_protected = bool(br_data.get("protected", False))

        rules: list[RequiredCheckRule] = []

        # 2. Classic Branch Protection required_status_checks (preserving app_id per check)
        st_bp, bp_data = self._read(
            scope,
            intent_ref,
            f"/repos/{scope.repository}/branches/{base_branch}/protection/required_status_checks",
        )
        if st_bp == 200 and isinstance(bp_data, dict):
            raw_checks = bp_data.get("checks")
            if isinstance(raw_checks, list):
                for item in raw_checks:
                    if isinstance(item, dict) and isinstance(item.get("context"), str) and item["context"].strip():
                        raw_app = item.get("app_id")
                        app_id = raw_app if type(raw_app) is int and raw_app > 0 else None
                        rules.append(RequiredCheckRule(item["context"].strip(), app_id))
            raw_contexts = bp_data.get("contexts")
            if isinstance(raw_contexts, list):
                for ctx in raw_contexts:
                    if isinstance(ctx, str) and ctx.strip() and not any(r.context == ctx.strip() for r in rules):
                        rules.append(RequiredCheckRule(ctx.strip(), None))
        elif st_bp == 404:
            if is_protected:
                # Branch is protected, yet protection endpoint returned 404 -> could be missing permission OR ruleset-only.
                # Verify rulesets below; if rulesets also yield no rule or fail, fail closed.
                pass
        else:
            return (), False

        # 3. GitHub Rulesets (`/repos/{repo}/rules/branches/{base}`) preserving integration_id
        st_rs, rs_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/rules/branches/{base_branch}")
        ruleset_rules_found = False
        rs_entries: list[Any] | None = None
        if st_rs == 200:
            if isinstance(rs_data, list):
                rs_entries = rs_data
            elif isinstance(rs_data, dict) and isinstance(rs_data.get("items"), list):
                rs_entries = rs_data["items"]
        if rs_entries is not None:
            for entry in rs_entries:
                if not isinstance(entry, dict) or entry.get("type") != "required_status_checks":
                    continue
                params = entry.get("parameters")
                req_list = params.get("required_status_checks") if isinstance(params, dict) else None
                if isinstance(req_list, list):
                    for item in req_list:
                        if isinstance(item, dict) and isinstance(item.get("context"), str) and item["context"].strip():
                            raw_int = item.get("integration_id")
                            app_id = raw_int if type(raw_int) is int and raw_int > 0 else None
                            rules.append(RequiredCheckRule(item["context"].strip(), app_id))
                            ruleset_rules_found = True
        elif st_rs == 404 and not is_protected:
            pass
        else:
            return (), False

        if is_protected and st_bp == 404 and not ruleset_rules_found:
            # Protected branch whose protection rules could not be read (e.g. token lacks administration:read).
            return (), False

        return _merge_required_rules((), rules), True

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
        if st_pr != 200 or not isinstance(pr_data, dict):
            return GuardianEvaluation("UNKNOWN", (), tuple(required_checks), None, "unknown", False, False)

        resolved_base = base_branch
        if not resolved_base and isinstance(pr_data.get("base"), dict):
            raw_ref = pr_data["base"].get("ref")
            if isinstance(raw_ref, str) and raw_ref.strip():
                resolved_base = raw_ref.strip()

        bp_rules, bp_ok = self._fetch_branch_protection_checks(scope, intent_ref, resolved_base)

        all_runs: list[dict[str, Any]] = []
        total_count: int | None = None
        pagination_complete = bp_ok
        for page in range(1, _MAX_CHECK_PAGES + 1):
            st_chk, chk_data = self._read(
                scope,
                intent_ref,
                f"/repos/{scope.repository}/commits/{head_sha}/check-runs?per_page={_PER_PAGE}&page={page}",
            )
            if st_chk != 200 or not isinstance(chk_data, dict):
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
            required_checks=required_checks,
            required_check_rules=bp_rules,
            repair_attempts=repair_attempts,
            max_repair_attempts=max_repair_attempts,
            pagination_complete=pagination_complete,
        )
