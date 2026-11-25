"""Minimal development state machine: patches and evidence, not a second Task/Run authority."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .contracts import (
    CommitEvidence,
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

    def _save(self, old: Cycle, updated: Cycle) -> Cycle:
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
        return self._save(c, replace(c, version=c.version + 1, phase=Phase.PLANNED, plan=plan))

    def execute(self, scope: ProjectScope, cycle_id: str, request: ExecutionRequest) -> Cycle:  # noqa: PLR0912
        """Execute only through Cell port; persist bounded, hash-addressed receipt."""
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.PLANNED, Phase.EXECUTING, Phase.COMMITTED, Phase.REVIEWING)
        if request.project != c.scope or request.run != c.run or c.plan is None or request.plan_ref != c.plan.plan_ref:
            raise ScopeDenied("execution does not belong to this plan/fence")
        if request.kind == "test" and request.test_id not in c.plan.required_tests:
            raise ScopeDenied("test ID is not approved by the current plan")
        if request.test_id is not None and any(receipt.test_id == request.test_id for receipt in c.tests):
            raise StateConflict("duplicate test receipt; start a new review round")
        if c.phase in (Phase.PLANNED, Phase.EXECUTING) and request.kind == "test":
            raise StateConflict("test must be bound to an immutable committed SHA")
        if c.commit is not None and c.phase in (Phase.COMMITTED, Phase.REVIEWING):
            target = c.commit.commit_sha
        else:
            target = c.commit.commit_sha if c.commit is not None else c.scope.base_sha
        if request.target_commit_sha is not None and request.target_commit_sha != target:
            raise StaleReference("execution target differs from current commit")
        if (
            request.snapshot_revision is not None
            and c.commit is not None
            and target != c.scope.base_sha
            and c.scope.workspace_revision.isascii()
            and c.scope.workspace_revision.isdecimal()
            and request.snapshot_revision <= int(c.scope.workspace_revision)
        ):
            raise StaleReference("committed execution requires a newer snapshot revision")
        self.gateway.require_operation(c.scope, c.run, f"sandbox.{request.kind}", target, request.intent_ref)
        receipt = self.sandbox.execute(request)
        if (
            receipt.run != c.run
            or receipt.intent_ref != request.intent_ref
            or receipt.kind != request.kind
            or receipt.toolchain_digest != request.toolchain_digest
            or receipt.test_id != request.test_id
            or (request.snapshot_revision is not None and receipt.snapshot_revision != request.snapshot_revision)
            or (receipt.executed and receipt.source_commit_sha is None)
            or (receipt.source_commit_sha is not None and receipt.source_commit_sha != target)
            or (
                c.commit is not None
                and c.phase in (Phase.COMMITTED, Phase.REVIEWING)
                and receipt.source_commit_sha is None
            )
        ):
            raise InvalidEvidence("sandbox receipt mismatches immutable request/SHA/fence")
        if receipt.test_id is not None:
            if request.kind != "test" or receipt.test_id not in c.plan.required_tests:
                raise InvalidEvidence("unknown or misleading test ID")
            if any(r.test_id == receipt.test_id for r in c.tests):
                raise StateConflict("duplicate test receipt; start a new review round")
        if c.phase in (Phase.COMMITTED, Phase.REVIEWING) and request.kind == "test" and not receipt.test_id:
            raise InvalidEvidence("test receipt missing required test ID")
        tests = (*c.tests, receipt) if receipt.test_id else c.tests
        phase = Phase.EXECUTING if c.phase in (Phase.PLANNED, Phase.EXECUTING) else Phase.REVIEWING
        return self._save(
            c,
            replace(
                c,
                version=c.version + 1,
                phase=phase,
                tests=tests,
                execution_receipts=(*c.execution_receipts, receipt),
            ),
        )

    def submit_patch(self, scope: ProjectScope, cycle_id: str, patch: PatchArtifact, repo_root: Path) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.EXECUTING)
        expected_parent = c.commit.commit_sha if c.commit is not None else scope.base_sha
        if (
            patch.run != c.run
            or patch.workspace_revision != scope.workspace_revision
            or patch.parent_sha != expected_parent
        ):
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
        pending = self._save(c, replace(c, version=c.version + 1, phase=Phase.COMMIT_PENDING))
        try:
            result = self.git.commit_patch(c.scope, c.patch, c.patch.parent_sha, intent_ref, message)
        except EffectUnknown:
            self._save(pending, replace(pending, version=pending.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise
        except Exception as exc:
            # Even a reported CAS conflict is not proof of a negative outcome:
            # the provider may have updated the ref before losing its response.
            self._save(pending, replace(pending, version=pending.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise EffectUnknown("provider exception; reconcile remote operation") from exc
        try:
            observed_head = self.git.head(scope)
        except Exception as exc:
            self._save(pending, replace(pending, version=pending.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise EffectUnknown("commit was dispatched but branch verification failed; reconcile") from exc
        # No blind retry if a network call may have been accepted but failed locally.
        if (
            not isinstance(result, CommitEvidence)
            or result.parent_sha != c.patch.parent_sha
            or result.diff_sha256 != c.patch.diff_sha256
            or result.artifact_ref != c.patch.artifact_ref
            or observed_head != result.commit_sha
        ):
            self._save(pending, replace(pending, version=pending.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise EffectUnknown("provider evidence cannot prove target commit; reconcile")
        return self._save(pending, replace(pending, version=pending.version + 1, phase=Phase.COMMITTED, commit=result))

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
            r.reviewer_task_id == verdict.reviewer_task_id or r.reviewer_role == verdict.reviewer_role
            for r in c.reviews
        ):
            raise InvalidEvidence("reviewers must be independent and roles unique")
        reviews = (*c.reviews, verdict)
        phase = Phase.CODE_READY if self._gate(c, reviews) else Phase.REVIEWING
        return self._save(c, replace(c, version=c.version + 1, reviews=reviews, phase=phase))

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
            or t.timed_out is not False
            or t.truncated is not False
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

    def start_repair_round(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        """Start a new P0/P1/test repair round on the current committed SHA without losing diff history."""
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.COMMITTED, Phase.REVIEWING)
        self._ensure_review_head(c)
        return self._save(
            c,
            replace(
                c,
                version=c.version + 1,
                phase=Phase.EXECUTING,
                tests=(),
                reviews=(),
                round_number=c.round_number + 1,
            ),
        )

    def reconcile_commit(
        self, scope: ProjectScope, cycle_id: str, observed_commit: CommitEvidence | None = None
    ) -> Cycle:
        """Inspect an unknown commit without inferring the outcome from the current Git head."""
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.UNKNOWN_EXTERNAL_EFFECT)
        if c.patch is None:
            raise StateConflict("missing patch for reconciliation")
        head = self.git.head(scope)
        if observed_commit is not None:
            if (
                head != observed_commit.commit_sha
                or observed_commit.parent_sha != c.patch.parent_sha
                or observed_commit.diff_sha256 != c.patch.diff_sha256
                or observed_commit.artifact_ref != c.patch.artifact_ref
            ):
                raise StaleReference("observed commit does not match remote HEAD or pending patch")
            # A matching Git HEAD and caller-supplied receipt reference do not
            # authenticate the original Intent or prove who updated the ref.
            # Positive recovery stays closed until a canonical Gateway receipt
            # verifier and the original Intent/Attempt binding are available.
            raise EffectUnknown("canonical commit receipt verification unavailable")
        if head == c.patch.parent_sha:
            # The ref may have advanced and later moved back. Its present value
            # cannot establish that the original operation did not take effect.
            raise EffectUnknown("canonical commit outcome unavailable; retain unknown")
        raise StaleReference("remote branch state does not match pre-commit parent SHA")

    def assert_code_ready(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        c = self._load(scope, cycle_id)
        self._phase(c, Phase.CODE_READY)
        self._ensure_review_head(c)
        if not self._gate(c, c.reviews):
            raise InvalidEvidence("review/test gate is no longer satisfied")
        return c

    def push(self, scope: ProjectScope, cycle_id: str, *, intent_ref: str) -> str:
        """Optional operation, NEVER called implicitly from commit/review."""
        if not intent_ref:
            raise StateConflict("missing typed intent reference")
        c = self.assert_code_ready(scope, cycle_id)
        if c.commit is None:
            raise StateConflict("commit evidence required")
        if c.push_receipt is not None:
            raise StateConflict("push already recorded for this commit")
        self.gateway.require_operation(c.scope, c.run, "git.push", c.commit.commit_sha, intent_ref)
        try:
            receipt = self.git.push(c.scope, c.commit.commit_sha, intent_ref)
        except (ScopeDenied, StaleReference, InvalidEvidence, StateConflict):
            raise
        except EffectUnknown:
            self._save(c, replace(c, version=c.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise
        except Exception as exc:
            self._save(c, replace(c, version=c.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise EffectUnknown("push outcome unknown; reconcile remote branch") from exc
        self._save(c, replace(c, version=c.version + 1, push_receipt=receipt))
        return receipt

    def create_pr(self, scope: ProjectScope, cycle_id: str, *, intent_ref: str) -> str:
        if not intent_ref:
            raise StateConflict("missing typed intent reference")
        c = self.assert_code_ready(scope, cycle_id)
        if c.commit is None:
            raise StateConflict("commit evidence required")
        if c.pr_receipt is not None:
            raise StateConflict("PR already recorded for this commit")
        self.gateway.require_operation(c.scope, c.run, "git.pull_request.create", c.commit.commit_sha, intent_ref)
        try:
            receipt = self.git.create_pr(c.scope, c.commit.commit_sha, intent_ref)
        except (ScopeDenied, StaleReference, InvalidEvidence, StateConflict):
            raise
        except EffectUnknown:
            self._save(c, replace(c, version=c.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise
        except Exception as exc:
            self._save(c, replace(c, version=c.version + 1, phase=Phase.UNKNOWN_EXTERNAL_EFFECT))
            raise EffectUnknown("PR creation outcome unknown; reconcile remote provider") from exc
        self._save(c, replace(c, version=c.version + 1, pr_receipt=receipt))
        return receipt
