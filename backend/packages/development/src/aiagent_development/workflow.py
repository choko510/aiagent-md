"""Minimal development state machine: patches and evidence, not a second Task/Run authority."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .contracts import (
    Cycle,
    EffectUnknown,
    ExecutionRequest,
    InvalidEvidence,
    PatchArtifact,
    Phase,
    ProjectScope,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    StaleReference,
    StateConflict,
    TaskPlan,
    require_sha,
)
from .paths import validate_manifest_paths
from .ports import (
    CycleStorePort,
    GatewayAuthorizationPort,
    GitProviderPort,
    ReviewAttestationPort,
    RunAuthorityPort,
    SandboxExecutionPort,
    WorkspacePort,
)


class DevelopmentWorkflow:
    """Server orchestration facade. Ports MUST be bound to real trusted adapters.

    In particular, this class cannot self-approve a write. All returned states
    are projections for the canonical Task/Run and its persisted ledger.
    """

    def __init__(
        self,
        *,
        runs: RunAuthorityPort,
        reviewers: ReviewAttestationPort,
        workspaces: WorkspacePort,
        gateway: GatewayAuthorizationPort,
        sandbox: SandboxExecutionPort,
        git: GitProviderPort,
        store: CycleStorePort,
    ) -> None:
        self.runs = runs
        self.reviewers = reviewers
        self.workspaces = workspaces
        self.gateway = gateway
        self.sandbox = sandbox
        self.git = git
        self.store = store

    def _current(self, scope: ProjectScope, run: RunFence) -> None:
        self.workspaces.assert_scope(scope)
        self.runs.assert_current(scope, run)

    def _load(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        current = self.store.load(scope, cycle_id)
        if current.scope != scope:
            raise ScopeDenied("different workspace or tenant")
        self._current(current.scope, current.run)
        return current

    def _save(self, old: Cycle, **changes: object) -> Cycle:
        updated = replace(old, version=old.version + 1, **changes)
        self.store.save(old, updated)
        return updated

    @staticmethod
    def _phase(cycle: Cycle, *allowed: Phase) -> None:
        if cycle.phase not in allowed:
            raise StateConflict(f"phase {cycle.phase} cannot perform this transition")

    def create(self, cycle_id: str, scope: ProjectScope, run: RunFence, change_request_ref: str) -> Cycle:
        self._current(scope, run)
        if not change_request_ref or require_sha(self.git.head(scope)) != scope.base_sha:
            raise StaleReference("base SHA changed or missing request")
        c = Cycle(cycle_id, scope, run, Phase.REQUESTED, change_request_ref)
        self.store.create(c)
        return c

    def plan(self, scope: ProjectScope, cycle_id: str, plan: TaskPlan) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.REQUESTED)
        if plan.goal_revision != c.run.goal_revision or plan.change_request_ref != c.change_request_ref:
            raise StaleReference("plan does not match canonical goal")
        return self._save(c, phase=Phase.PLANNED, plan=plan)

    def execute(self, scope: ProjectScope, cycle_id: str, request: ExecutionRequest) -> Cycle:
        """Execute only through Cell port; persist bounded, hash-addressed receipt."""
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.PLANNED, Phase.EXECUTING, Phase.COMMITTED, Phase.REVIEWING)
        if request.project != c.scope or request.run != c.run or c.plan is None or request.plan_ref != c.plan.plan_ref:
            raise ScopeDenied("execution does not belong to this plan/fence")
        if c.commit is None and request.kind == "test":
            raise StateConflict("test must be bound to an immutable committed SHA")
        target = c.commit.commit_sha if c.commit else c.scope.base_sha
        self.gateway.require_operation(c.scope, c.run, f"sandbox.{request.kind}", target, request.intent_ref)
        receipt = self.sandbox.execute(request)
        if (
            receipt.run != c.run
            or receipt.intent_ref != request.intent_ref
            or receipt.kind != request.kind
            or receipt.toolchain_digest != request.toolchain_digest
            or (c.commit is not None and receipt.source_commit_sha != c.commit.commit_sha)
        ):
            raise InvalidEvidence("sandbox receipt mismatches immutable request/SHA/fence")
        if receipt.test_id is not None:
            if request.kind != "test" or receipt.test_id not in c.plan.required_tests:
                raise InvalidEvidence("unknown or misleading test ID")
            if any(r.test_id == receipt.test_id for r in c.tests):
                raise StateConflict("duplicate test receipt; start a new review round")
        if c.commit is not None and request.kind == "test" and not receipt.test_id:
            raise InvalidEvidence("test receipt missing required test ID")
        tests = (*c.tests, receipt) if receipt.test_id else c.tests
        phase = Phase.EXECUTING if c.commit is None else Phase.REVIEWING
        return self._save(c, phase=phase, tests=tests, execution_receipts=(*c.execution_receipts, receipt))

    def submit_patch(self, scope: ProjectScope, cycle_id: str, patch: PatchArtifact, repo_root: Path) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.EXECUTING)
        if patch.run != c.run or patch.workspace_revision != scope.workspace_revision or patch.parent_sha != scope.base_sha:
            raise StaleReference("patch lineage/workspace/base differs")
        validate_manifest_paths(repo_root, patch.paths)
        if patch.diff_sha256 in c.submitted_digests:
            raise StateConflict("duplicate diff submitted")
        if self.git.head(scope) != patch.parent_sha:
            raise StaleReference("branch moved before patch submission")
        # The claim and cycle CAS are ONE transaction in the real Store adapter.
        # An orphan reservation must never suppress a valid retry after a crash.
        updated = replace(
            c,
            version=c.version + 1,
            phase=Phase.PATCH_READY,
            patch=patch,
            submitted_digests=c.submitted_digests | {patch.diff_sha256},
        )
        self.store.save_with_diff_claim(c, updated, patch.diff_sha256)
        return updated

    def commit(self, scope: ProjectScope, cycle_id: str, *, message: str, intent_ref: str) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.PATCH_READY)
        if c.patch is None or not message.strip() or len(message) > 256 or not intent_ref:
            raise StateConflict("missing patch/commit message/typed intent")
        if self.git.head(scope) != c.patch.parent_sha:
            raise StaleReference("branch HEAD moved: CAS conflict")
        # Gateway does fresh authoritative checks immediately before dispatch.
        self.gateway.require_operation(c.scope, c.run, "git.commit", c.patch.parent_sha, intent_ref)
        pending = self._save(c, phase=Phase.COMMIT_PENDING)
        try:
            result = self.git.commit_patch(c.scope, c.patch, c.patch.parent_sha, intent_ref, message)
        except EffectUnknown:
            self._save(pending, phase=Phase.UNKNOWN_EXTERNAL_EFFECT)
            raise
        except StaleReference:
            self._save(pending, phase=Phase.BLOCKED)
            raise
        except Exception as exc:
            # A provider exception after dispatch may conceal a completed write.
            self._save(pending, phase=Phase.UNKNOWN_EXTERNAL_EFFECT)
            raise EffectUnknown("provider exception; reconcile remote operation") from exc
        # No blind retry if a network call may have been accepted but failed locally.
        if (
            result.parent_sha != c.patch.parent_sha
            or result.diff_sha256 != c.patch.diff_sha256
            or result.artifact_ref != c.patch.artifact_ref
            or self.git.head(scope) != result.commit_sha
        ):
            self._save(pending, phase=Phase.UNKNOWN_EXTERNAL_EFFECT)
            raise EffectUnknown("provider evidence cannot prove target commit; reconcile")
        return self._save(pending, phase=Phase.COMMITTED, commit=result)

    def review(self, scope: ProjectScope, cycle_id: str, verdict: ReviewVerdict) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.COMMITTED, Phase.REVIEWING)
        if c.commit is None or c.plan is None:
            raise StateConflict("review requires immutable commit and plan")
        self._ensure_review_head(c)
        if verdict.target_sha != c.commit.commit_sha:
            raise StaleReference("review is for an obsolete SHA")
        self.reviewers.assert_verified(c.scope, c.run, verdict)
        if verdict.reviewer_task_id == c.run.task_id or any(
            r.reviewer_task_id == verdict.reviewer_task_id or r.reviewer_role == verdict.reviewer_role for r in c.reviews
        ):
            raise InvalidEvidence("reviewers must be independent and roles unique")
        reviews = (*c.reviews, verdict)
        phase = Phase.CODE_READY if self._gate(c, reviews) else Phase.REVIEWING
        return self._save(c, reviews=reviews, phase=phase)

    def _ensure_review_head(self, c: Cycle) -> None:
        if c.commit is None or self.git.head(c.scope) != c.commit.commit_sha:
            raise StaleReference("branch changed since commit; re-test and re-review")

    @staticmethod
    def _gate(c: Cycle, reviews: tuple[ReviewVerdict, ...]) -> bool:
        if c.plan is None or c.commit is None:
            return False
        required = set(c.plan.required_tests)
        tested = {t.test_id for t in c.tests if t.test_id is not None}
        if tested != required or len(c.tests) != len(required):
            return False
        if any(
            not t.executed
            or t.exit_code != 0
            or t.kind != "test"
            or t.source_commit_sha != c.commit.commit_sha
            for t in c.tests
        ):
            return False
        if {v.reviewer_role for v in reviews} != {"functional", "security", "ui-e2e"}:
            return False
        return all(
            v.passed
            and v.target_sha == c.commit.commit_sha
            and not any(f.severity in ("P0", "P1") and f.status != "RESOLVED" for f in v.findings)
            for v in reviews
        )

    def assert_code_ready(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.CODE_READY)
        self._ensure_review_head(c)
        if not self._gate(c, c.reviews):
            raise InvalidEvidence("review/test gate is no longer satisfied")
        return c

    def push(self, scope: ProjectScope, cycle_id: str, *, intent_ref: str) -> str:
        """Optional operation, NEVER called implicitly from commit/review."""
        c = self.assert_code_ready(scope, cycle_id)
        assert c.commit is not None
        self.gateway.require_operation(c.scope, c.run, "git.push", c.commit.commit_sha, intent_ref)
        return self.git.push(c.scope, c.commit.commit_sha, intent_ref)

    def create_pr(self, scope: ProjectScope, cycle_id: str, *, intent_ref: str) -> str:
        c = self.assert_code_ready(scope, cycle_id)
        assert c.commit is not None
        self.gateway.require_operation(c.scope, c.run, "git.pull_request.create", c.commit.commit_sha, intent_ref)
        return self.git.create_pr(c.scope, c.commit.commit_sha, intent_ref)
