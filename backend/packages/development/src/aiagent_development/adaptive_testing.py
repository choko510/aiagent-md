"""DEV-14 Adaptive Testing Gate: separates fast impacted feedback from mandatory security/regression gates.

Hardens P2-12:
- Conservative fallback on any non-COMPLETE coverage (including TEXT_ONLY by default), analysis issues,
  manifest/lock/SQL/UI/deleted changes, or empty impacted candidates.
- Separate command builders for test targets (`pytest -q`) vs static-check targets (`ruff check`).
- Authoritatively reads mandatory tests from TaskPlan.required_tests.
- Strictly verifies returned ExecutionEvidence (intent_ref, toolchain_digest, test_id, source_commit_sha,
  executed=True, timed_out=False, truncated=False, exit_code=0).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .code_intelligence.git_reader import GitSnapshot
from .code_intelligence.impact import impact
from .code_intelligence.inventory import LOCKS, MANIFESTS, RepositoryInventory
from .code_intelligence.models import Coverage
from .contracts import ExecutionEvidence, ExecutionRequest, ProjectScope, RunFence, TaskPlan
from .ports import SandboxExecutionPort

_MANDATORY_FALLBACK_SUFFIXES = frozenset({".sql", ".yml", ".yaml", ".toml", ".json", ".ts", ".tsx"})
ReceiptVerifier = Callable[[ExecutionRequest, ExecutionEvidence], bool]


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


def _evidence_matches_and_passed(
    req: ExecutionRequest,
    ev: ExecutionEvidence,
    *,
    expected_commit_sha: str | None,
    receipt_verifier: ReceiptVerifier | None = None,
) -> bool:
    basic_ok = (
        ev.executed
        and ev.exit_code == 0
        and ev.timed_out is False
        and ev.truncated is False
        and ev.run == req.run
        and ev.intent_ref == req.intent_ref
        and ev.toolchain_digest == req.toolchain_digest
        and ev.kind == req.kind
        and ev.test_id == req.test_id
        and (expected_commit_sha is None or ev.source_commit_sha == expected_commit_sha)
        and bool(ev.receipt_ref and ev.receipt_ref.strip())
    )
    return basic_ok and (receipt_verifier is None or receipt_verifier(req, ev))


def plan_adaptive_tests(
    reader: GitSnapshot,
    inv: RepositoryInventory,
    changed: tuple[str, ...],
    task_plan: TaskPlan,
    *,
    full_suite_tests: Sequence[str] | None = None,
    allow_text_only_narrowing: bool = False,
) -> AdaptiveTestPlan:
    res = impact(reader, inv, changed)
    inv_map = {item.entry.path: item for item in inv.files}
    discovered_tests = tuple(sorted(item.entry.path for item in inv.files if item.classification == "TEST"))
    fallback_suite = tuple(
        dict.fromkeys(full_suite_tests if full_suite_tests else discovered_tests or task_plan.required_tests)
    )

    allowed_cov = (Coverage.COMPLETE, Coverage.TEXT_ONLY) if allow_text_only_narrowing else (Coverage.COMPLETE,)
    reasons: list[str] = []
    if res.coverage not in allowed_cov:
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

    def __init__(self, sandbox: SandboxExecutionPort, *, receipt_verifier: ReceiptVerifier | None = None) -> None:
        self._sandbox = sandbox
        self._receipt_verifier = receipt_verifier

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
        snapshot_revision: int | None = None,
        test_command: Sequence[str] | None = None,
        static_command: Sequence[str] | None = None,
        run_mandatory: bool = True,
    ) -> AdaptiveGateResult:
        test_base = tuple(test_command or ("pytest", "-q"))
        static_base = tuple(static_command or ("ruff", "check"))
        # Always derive authoritative mandatory tests from TaskPlan, never trusting an altered adaptive_plan list.
        authoritative_mandatory = plan.required_tests
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
                argv=(*test_base, target),
                toolchain_digest=toolchain_digest,
                intent_ref=f"{intent_prefix}:imp:{idx}:{digest[:16]}",
                test_id=target,
                target_commit_sha=target_commit_sha,
                snapshot_revision=snapshot_revision,
            )
            ev = self._sandbox.execute(req)
            impacted_ev.append(ev)
            if not _evidence_matches_and_passed(
                req, ev, expected_commit_sha=target_commit_sha, receipt_verifier=self._receipt_verifier
            ):
                impacted_ok = False

        impacted_status = "PASSED" if (impacted_ev and impacted_ok) else "FAILED"

        mandatory_ok = True
        if run_mandatory:
            for idx, m_target in enumerate(authoritative_mandatory):
                digest = hashlib.sha256(f"{intent_prefix}:mandatory:{idx}:{m_target}".encode()).hexdigest()
                is_static = m_target.startswith("static:")
                static_arg = m_target.removeprefix("static:") or "."
                req = ExecutionRequest(
                    project=scope,
                    run=run,
                    plan_ref=plan.plan_ref,
                    kind="static-check" if is_static else "test",
                    argv=(*static_base, static_arg) if is_static else (*test_base, m_target),
                    toolchain_digest=toolchain_digest,
                    intent_ref=f"{intent_prefix}:man:{idx}:{digest[:16]}",
                    test_id=None if is_static else m_target,
                    target_commit_sha=target_commit_sha,
                    snapshot_revision=snapshot_revision,
                )
                ev = self._sandbox.execute(req)
                mandatory_ev.append(ev)
                if not _evidence_matches_and_passed(
                    req, ev, expected_commit_sha=target_commit_sha, receipt_verifier=self._receipt_verifier
                ):
                    mandatory_ok = False
            mandatory_status = "PASSED" if (mandatory_ev and mandatory_ok) else "FAILED"
        else:
            mandatory_status = "SKIPPED"

        overall_passed = impacted_status == "PASSED" and mandatory_status == "PASSED"
        resolved_plan = AdaptiveTestPlan(
            changed_paths=adaptive_plan.changed_paths,
            impacted_tests=adaptive_plan.impacted_tests,
            mandatory_tests=authoritative_mandatory,
            fallback_to_full=adaptive_plan.fallback_to_full,
            fallback_reasons=adaptive_plan.fallback_reasons,
        )
        return AdaptiveGateResult(
            plan=resolved_plan,
            impacted_status=impacted_status,
            mandatory_status=mandatory_status,
            overall_passed=overall_passed,
            impacted_evidence=tuple(impacted_ev),
            mandatory_evidence=tuple(mandatory_ev),
        )
