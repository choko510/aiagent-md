"""DEV-14 Adaptive Testing Gate: separates fast impacted feedback from mandatory security/regression gates.

Never mislabels an impacted-only pass as full verification. Falls back conservatively to the full
discovered test suite whenever coverage is incomplete, deletions/config/migrations change, or
impact analysis hits limits or yields empty candidates. Enforces PR #110 strict completion flags
(executed=True, timed_out=False, truncated=False).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .code_intelligence.git_reader import GitSnapshot
from .code_intelligence.impact import impact
from .code_intelligence.inventory import LOCKS, MANIFESTS, RepositoryInventory
from .code_intelligence.models import Coverage
from .contracts import ExecutionEvidence, ExecutionRequest, ProjectScope, RunFence, TaskPlan
from .ports import SandboxExecutionPort

_MANDATORY_FALLBACK_SUFFIXES = frozenset({".sql", ".yml", ".yaml", ".toml", ".json", ".ts", ".tsx"})


@dataclass(frozen=True)
class AdaptiveTestPlan:
    changed_paths: tuple[str, ...]
    impacted_tests: tuple[str, ...]
    mandatory_tests: tuple[str, ...]
    fallback_to_full: bool
    fallback_reasons: tuple[str, ...]


@dataclass(frozen=True)
class AdaptiveGateResult:
    plan: AdaptiveTestPlan
    impacted_status: str  # PASSED | FAILED
    mandatory_status: str  # PASSED | FAILED | SKIPPED
    overall_passed: bool
    impacted_evidence: tuple[ExecutionEvidence, ...]
    mandatory_evidence: tuple[ExecutionEvidence, ...]


def _evidence_passed(ev: ExecutionEvidence) -> bool:
    return ev.executed and ev.exit_code == 0 and ev.timed_out is False and ev.truncated is False


def plan_adaptive_tests(
    reader: GitSnapshot,
    inv: RepositoryInventory,
    changed: tuple[str, ...],
    task_plan: TaskPlan,
    *,
    full_suite_tests: Sequence[str] | None = None,
) -> AdaptiveTestPlan:
    res = impact(reader, inv, changed)
    inv_map = {item.entry.path: item for item in inv.files}
    discovered_tests = tuple(sorted(item.entry.path for item in inv.files if item.classification == "TEST"))
    fallback_suite = tuple(
        dict.fromkeys(full_suite_tests if full_suite_tests else discovered_tests or task_plan.required_tests)
    )

    reasons: list[str] = []
    if res.coverage not in (Coverage.COMPLETE, Coverage.TEXT_ONLY):
        reasons.append(f"coverage:{res.coverage.value}")
    if res.issues:
        reasons.append("analysis_issues:" + ";".join(res.issues[:3]))

    for path in res.changed:
        p = PurePosixPath(path)
        if path not in inv_map:
            reasons.append(f"deleted_or_missing:{path}")
        if p.name in MANIFESTS or p.name in LOCKS or p.suffix.lower() in _MANDATORY_FALLBACK_SUFFIXES:
            reasons.append(f"high_impact_file:{path}")

    impacted_test_files = tuple(
        sorted(c.path for c in res.candidates if c.path in inv_map and inv_map[c.path].classification == "TEST")
    )
    if not impacted_test_files:
        reasons.append("no_specific_impacted_tests")

    fallback_to_full = bool(reasons)
    selected_impacted = fallback_suite if fallback_to_full else impacted_test_files
    return AdaptiveTestPlan(
        changed_paths=res.changed,
        impacted_tests=selected_impacted,
        mandatory_tests=task_plan.required_tests,
        fallback_to_full=fallback_to_full,
        fallback_reasons=tuple(dict.fromkeys(reasons)),
    )


class AdaptiveTestingGate:
    """Runs fast impacted checks and mandatory checks separately inside an isolated Sandbox Cell."""

    def __init__(self, sandbox: SandboxExecutionPort) -> None:
        self._sandbox = sandbox

    def run_gate(
        self,
        *,
        scope: ProjectScope,
        run: RunFence,
        plan: TaskPlan,
        adaptive_plan: AdaptiveTestPlan,
        toolchain_digest: str,
        intent_prefix: str,
        target_commit_sha: str | None = None,
        command_builder: Sequence[str] | None = None,
        run_mandatory: bool = True,
    ) -> AdaptiveGateResult:
        cmd_base = tuple(command_builder or ("pytest", "-q"))
        impacted_ev: list[ExecutionEvidence] = []
        mandatory_ev: list[ExecutionEvidence] = []

        impacted_ok = True
        for idx, target in enumerate(adaptive_plan.impacted_tests):
            digest = hashlib.sha256(f"{intent_prefix}:impacted:{idx}:{target}".encode()).hexdigest()
            req = ExecutionRequest(
                project=scope,
                run=run,
                plan_ref=plan.plan_ref,
                kind="test",
                argv=(*cmd_base, target),
                toolchain_digest=toolchain_digest,
                intent_ref=f"{intent_prefix}:imp:{idx}:{digest[:16]}",
                test_id=target,
                target_commit_sha=target_commit_sha,
            )
            ev = self._sandbox.execute(req)
            impacted_ev.append(ev)
            if not _evidence_passed(ev):
                impacted_ok = False

        impacted_status = "PASSED" if (impacted_ev and impacted_ok) else "FAILED"

        mandatory_ok = True
        if run_mandatory:
            for idx, m_target in enumerate(adaptive_plan.mandatory_tests):
                digest = hashlib.sha256(f"{intent_prefix}:mandatory:{idx}:{m_target}".encode()).hexdigest()
                is_static = m_target.startswith("static:")
                req = ExecutionRequest(
                    project=scope,
                    run=run,
                    plan_ref=plan.plan_ref,
                    kind="static-check" if is_static else "test",
                    argv=(*cmd_base, m_target),
                    toolchain_digest=toolchain_digest,
                    intent_ref=f"{intent_prefix}:man:{idx}:{digest[:16]}",
                    test_id=None if is_static else m_target,
                    target_commit_sha=target_commit_sha,
                )
                ev = self._sandbox.execute(req)
                mandatory_ev.append(ev)
                if not _evidence_passed(ev):
                    mandatory_ok = False
            mandatory_status = "PASSED" if (mandatory_ev and mandatory_ok) else "FAILED"
        else:
            mandatory_status = "SKIPPED"

        overall_passed = impacted_status == "PASSED" and mandatory_status == "PASSED"
        return AdaptiveGateResult(
            plan=adaptive_plan,
            impacted_status=impacted_status,
            mandatory_status=mandatory_status,
            overall_passed=overall_passed,
            impacted_evidence=tuple(impacted_ev),
            mandatory_evidence=tuple(mandatory_ev),
        )
