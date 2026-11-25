"""DEV-11 PR Guardian: pure CI/conflict evaluation + Sentinel-routed bounded repair escalation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .contracts import ProjectScope, ScopeDenied


@dataclass(frozen=True)
class GuardianEvaluation:
    ci_state: str  # PASS | PENDING | FAIL | CONFLICT | UNKNOWN | ESCALATED_LIMIT_REACHED
    failing_checks: tuple[str, ...]
    mergeable_state: str
    should_repair: bool
    escalated: bool


def evaluate_pr_checks(
    *,
    pr_payload: dict[str, Any],
    check_runs_payload: dict[str, Any],
    repair_attempts: int,
    max_repair_attempts: int,
) -> GuardianEvaluation:
    """Pure evaluation of GitHub PR & check-runs state with strict retry ceiling."""
    mergeable_state = str(pr_payload.get("mergeable_state") or "unknown").lower()
    mergeable = pr_payload.get("mergeable")
    if mergeable is False or mergeable_state == "dirty":
        exhausted = repair_attempts >= max_repair_attempts
        return GuardianEvaluation(
            ci_state="ESCALATED_LIMIT_REACHED" if exhausted else "CONFLICT",
            failing_checks=("merge_conflict",),
            mergeable_state=mergeable_state,
            should_repair=not exhausted,
            escalated=exhausted,
        )

    runs = check_runs_payload.get("check_runs") or []
    failing: list[str] = []
    pending = False
    passed = 0
    if isinstance(runs, list):
        for r in runs:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "check")
            status = str(r.get("status") or "").lower()
            conclusion = str(r.get("conclusion") or "").lower()
            if status != "completed":
                pending = True
            elif conclusion in ("failure", "timed_out", "action_required", "cancelled"):
                failing.append(name)
            elif conclusion in ("success", "neutral", "skipped"):
                passed += 1

    if failing:
        exhausted = repair_attempts >= max_repair_attempts
        return GuardianEvaluation(
            ci_state="ESCALATED_LIMIT_REACHED" if exhausted else "FAIL",
            failing_checks=tuple(failing),
            mergeable_state=mergeable_state,
            should_repair=not exhausted,
            escalated=exhausted,
        )
    if pending:
        return GuardianEvaluation(
            ci_state="PENDING",
            failing_checks=(),
            mergeable_state=mergeable_state,
            should_repair=False,
            escalated=False,
        )
    if passed > 0:
        return GuardianEvaluation(
            ci_state="PASS",
            failing_checks=(),
            mergeable_state=mergeable_state,
            should_repair=False,
            escalated=False,
        )
    return GuardianEvaluation(
        ci_state="UNKNOWN",
        failing_checks=(),
        mergeable_state=mergeable_state,
        should_repair=False,
        escalated=False,
    )


class PrGuardian:
    """Inspects PR & check-run state solely via a Sentinel-routed read-only GET transport."""

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
    ) -> GuardianEvaluation:
        if pr_number < 1:
            raise ScopeDenied("invalid pr_number")
        st_pr, pr_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/pulls/{pr_number}")
        if st_pr != 200:
            return GuardianEvaluation("UNKNOWN", (), "unknown", False, False)

        st_chk, chk_data = self._read(scope, intent_ref, f"/repos/{scope.repository}/commits/{head_sha}/check-runs")
        if st_chk != 200:
            chk_data = {"check_runs": []}

        return evaluate_pr_checks(
            pr_payload=pr_data,
            check_runs_payload=chk_data,
            repair_attempts=repair_attempts,
            max_repair_attempts=max_repair_attempts,
        )
