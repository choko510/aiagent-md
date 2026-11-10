"""Isolated Mock Gateway/SCM and finite fixture tests. No real GitHub or host code execution."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from aiagent_development import (
    CommitEvidence,
    DevelopmentWorkflow,
    EffectUnknown,
    ExecutionEvidence,
    ExecutionRequest,
    InvalidEvidence,
    InvalidPath,
    PatchArtifact,
    Phase,
    ProjectScope,
    ReviewFinding,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    StaleReference,
    StateConflict,
    TaskPlan,
    safe_relative_path,
    validate_manifest_paths,
)

BASE = "a" * 40
DIGEST = "b" * 64
TOOLCHAIN = "c" * 64


class FakeRunAuthority:
    def __init__(self) -> None:
        self.current = True

    def assert_current(self, scope: ProjectScope, run: RunFence) -> None:
        if not self.current:
            raise ScopeDenied("run cancelled, lease lost, or restored")


class FakeWorkspace:
    def __init__(self, scope: ProjectScope) -> None:
        self.bound = scope

    def assert_scope(self, scope: ProjectScope) -> None:
        if scope != self.bound:
            raise ScopeDenied("tenant/branch/workspace/revision mismatch")


class FakeGateway:
    def __init__(self) -> None:
        self.allowed = {"sandbox.build", "sandbox.static-check", "sandbox.test", "git.commit"}
        self.calls: list[tuple[str, str, str]] = []

    def require_operation(self, scope: ProjectScope, run: RunFence, operation: str, target_sha: str, intent_ref: str) -> None:
        if operation not in self.allowed or not intent_ref or not target_sha:
            raise ScopeDenied("no matching trusted Tool Intent/approval")
        self.calls.append((operation, target_sha, intent_ref))


class FakeReviewAttestation:
    def __init__(self) -> None:
        self.valid = True

    def assert_verified(self, scope: ProjectScope, builder_run: RunFence, verdict: ReviewVerdict) -> None:
        if not self.valid or verdict.reviewer_task_id != f"review-task-{verdict.reviewer_role}":
            raise ScopeDenied("review task/receipt not independently attested")


class FakeGit:
    def __init__(self, scope: ProjectScope) -> None:
        self.head_sha = scope.base_sha
        self.calls: list[str] = []
        self.unknown = False

    def head(self, scope: ProjectScope) -> str:
        return self.head_sha

    def commit_patch(
        self, scope: ProjectScope, patch: PatchArtifact, expected_head_sha: str, intent_ref: str, message: str
    ) -> CommitEvidence:
        self.calls.append("commit")
        if self.unknown:
            raise EffectUnknown("ack lost; may have written")
        if self.head_sha != expected_head_sha:
            raise StaleReference("git compare-and-swap rejected")
        sha = hashlib.sha1((expected_head_sha + patch.diff_sha256).encode()).hexdigest()
        self.head_sha = sha
        return CommitEvidence(expected_head_sha, sha, patch.diff_sha256, patch.artifact_ref, "git-receipt")

    def push(self, scope: ProjectScope, expected_head_sha: str, intent_ref: str) -> str:
        self.calls.append("push")
        if expected_head_sha != self.head_sha:
            raise StaleReference("branch moved")
        return "push-receipt"

    def create_pr(self, scope: ProjectScope, head_sha: str, intent_ref: str) -> str:
        self.calls.append("pr")
        return "test-only-pr-reference"


class FakeCell:
    def __init__(self, git: FakeGit) -> None:
        self.git = git
        self.execute_calls = 0
        self.test_executed = True
        self.test_exit = 0
        self.wrong_fence = False

    def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
        self.execute_calls += 1
        run = replace(request.run, lease_epoch=request.run.lease_epoch + 1) if self.wrong_fence else request.run
        is_test = request.kind == "test"
        return ExecutionEvidence(
            request.intent_ref,
            run,
            request.kind,
            self.test_exit if is_test and self.test_executed else (0 if not is_test else None),
            self.test_executed if is_test else True,
            DIGEST,
            request.toolchain_digest,
            "bounded stdout",
            "",
            self.git.head_sha if is_test else None,
            request.argv[-1] if is_test else None,
            f"cell-receipt-{self.execute_calls}",
        )


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], object] = {}
        self.diffs: set[tuple[str, str, str]] = set()

    @staticmethod
    def key(scope: ProjectScope, cycle_id: str) -> tuple[str, str, str]:
        return scope.tenant_id, scope.project_id, cycle_id

    def create(self, cycle) -> None:
        key = self.key(cycle.scope, cycle.cycle_id)
        if key in self.rows:
            raise StateConflict("duplicate cycle")
        self.rows[key] = cycle

    def load(self, scope: ProjectScope, cycle_id: str):
        key = self.key(scope, cycle_id)
        if key not in self.rows:
            raise ScopeDenied("unknown/other-tenant cycle")
        return self.rows[key]

    def save(self, previous, updated) -> None:
        key = self.key(previous.scope, previous.cycle_id)
        if self.rows.get(key) != previous or updated.version != previous.version + 1:
            raise StateConflict("lost update")
        self.rows[key] = updated

    def save_with_diff_claim(self, previous, updated, diff_sha256: str) -> None:
        key = (previous.scope.tenant_id, previous.scope.project_id, diff_sha256)
        row_key = self.key(previous.scope, previous.cycle_id)
        if key in self.diffs:
            raise StateConflict("duplicate semantic diff")
        if self.rows.get(row_key) != previous or updated.version != previous.version + 1:
            raise StateConflict("lost update")
        self.rows[row_key] = updated
        self.diffs.add(key)



@pytest.fixture
def harness(tmp_path: Path):
    scope = ProjectScope("tenant-a", "owner-a", "project-a", "workspace-a", "choko/test-fixture", "feat/test", BASE, "r1")
    fence = RunFence("task-builder", "run-1", 1, 3, 0)
    runs = FakeRunAuthority()
    workspace = FakeWorkspace(scope)
    gateway = FakeGateway()
    reviewers = FakeReviewAttestation()
    git = FakeGit(scope)
    cell = FakeCell(git)
    store = FakeStore()
    root = tmp_path / "project"
    root.mkdir()
    flow = DevelopmentWorkflow(runs=runs, reviewers=reviewers, workspaces=workspace, gateway=gateway, sandbox=cell, git=git, store=store)
    return flow, scope, fence, runs, workspace, gateway, git, cell, store, root, reviewers


def stage(h, cycle_id="cycle-1", digest=DIGEST):
    flow, scope, fence = h[:3]
    root = h[9]
    flow.create(cycle_id, scope, fence, f"request-{cycle_id}")
    flow.plan(scope, cycle_id, TaskPlan(f"plan-{cycle_id}", f"request-{cycle_id}", ("unit", "security"), 1))
    flow.execute(
        scope,
        cycle_id,
        ExecutionRequest(scope, fence, f"plan-{cycle_id}", "build", ("offline-static-build",), TOOLCHAIN, "build-intent"),
    )
    flow.submit_patch(scope, cycle_id, PatchArtifact(f"artifact-{cycle_id}", digest, ("src/main.py",), BASE, "r1", fence), root)
    return flow, scope, fence


def commit(h, cycle_id="cycle-1", digest=DIGEST):
    flow, scope, fence = stage(h, cycle_id, digest)
    flow.commit(scope, cycle_id, message="test-only commit", intent_ref="commit-intent")
    return flow, scope, fence


def run_tests(h, cycle_id="cycle-1", names=("unit", "security")):
    flow, scope, fence = h[:3]
    for test in names:
        flow.execute(
            scope,
            cycle_id,
            ExecutionRequest(scope, fence, f"plan-{cycle_id}", "test", ("fixture-test", test), TOOLCHAIN, f"test-{test}"),
        )


def verdict(target_sha: str, role: str, findings=(), passed=True) -> ReviewVerdict:
    return ReviewVerdict(f"review-task-{role}", role, target_sha, passed, f"evidence-{role}", tuple(findings))


def review_all(h):
    flow, scope, *_ = h
    sha = h[6].head_sha
    for role in ("functional", "security", "ui-e2e"):
        flow.review(scope, "cycle-1", verdict(sha, role))


def test_complete_code_ready_but_never_auto_push(harness):
    commit(harness)
    run_tests(harness)
    review_all(harness)
    flow, scope, _, _, _, _, git, *_ = harness
    assert flow.assert_code_ready(scope, "cycle-1").phase == Phase.CODE_READY
    assert git.calls == ["commit"]


def test_branch_cas_conflict_no_silent_overwrite(harness):
    flow, scope, _, _, _, _, git, *_ = harness
    stage(harness, "one", "d" * 64)
    stage(harness, "two", "e" * 64)
    flow.commit(scope, "one", message="first", intent_ref="git-one")
    with pytest.raises(StaleReference):
        flow.commit(scope, "two", message="second", intent_ref="git-two")
    assert git.calls == ["commit"]


def test_obsolete_review_and_changed_head_rejected(harness):
    commit(harness)
    run_tests(harness)
    flow, scope, _, _, _, _, git, *_ = harness
    with pytest.raises(StaleReference):
        flow.review(scope, "cycle-1", verdict(BASE, "functional"))
    git.head_sha = "d" * 40
    with pytest.raises(StaleReference):
        flow.review(scope, "cycle-1", verdict(git.head_sha, "security"))


@pytest.mark.parametrize("path", ["../secrets", "/etc/passwd", "src/../../tmp", "C:/Users/a", "..\\secret", ".git/config", "a//b", "a/./b", "a/", "~/.ssh/id_rsa"])
def test_path_traversal_rejected(path):
    with pytest.raises(InvalidPath):
        safe_relative_path(path)


def test_symlink_escape_is_rejected(harness, tmp_path):
    root = harness[9]
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InvalidPath):
        validate_manifest_paths(root, ("escape/secret.txt",))
    with pytest.raises(InvalidPath):
        validate_manifest_paths(root, (".git/objects/payload",))


def test_unapproved_push_and_pr_refused(harness):
    commit(harness)
    run_tests(harness)
    review_all(harness)
    flow, scope, _, _, _, _, git, *_ = harness
    with pytest.raises(ScopeDenied):
        flow.push(scope, "cycle-1", intent_ref="no-push-approval")
    with pytest.raises(ScopeDenied):
        flow.create_pr(scope, "cycle-1", intent_ref="no-pr-approval")
    assert git.calls == ["commit"]


def test_cancel_or_lost_fence_cannot_commit(harness):
    flow, scope, _, runs, _, _, git, *_ = harness
    stage(harness)
    runs.current = False
    with pytest.raises(ScopeDenied):
        flow.commit(scope, "cycle-1", message="stale", intent_ref="git-approval")
    assert git.calls == []


def test_failed_or_unexecuted_tests_cannot_fake_code_ready(harness):
    commit(harness)
    hcell = harness[7]
    hcell.test_exit = 1
    run_tests(harness, names=("unit",))
    hcell.test_exit = 0
    hcell.test_executed = False
    run_tests(harness, names=("security",))
    review_all(harness)
    flow, scope = harness[:2]
    assert flow._load(scope, "cycle-1").phase == Phase.REVIEWING
    with pytest.raises(StateConflict):
        flow.assert_code_ready(scope, "cycle-1")


def test_missing_required_test_cannot_fake_code_ready(harness):
    commit(harness)
    run_tests(harness, names=("unit",))
    review_all(harness)
    assert harness[0]._load(harness[1], "cycle-1").phase != Phase.CODE_READY


def test_p0_p1_open_and_unverified_cannot_fake_pass(harness):
    commit(harness)
    run_tests(harness)
    flow, scope = harness[:2]
    sha = harness[6].head_sha
    flow.review(scope, "cycle-1", verdict(sha, "functional", [ReviewFinding("x", "P0", "OPEN")]))
    flow.review(scope, "cycle-1", verdict(sha, "security", [ReviewFinding("y", "P1", "UNVERIFIED")]))
    flow.review(scope, "cycle-1", verdict(sha, "ui-e2e"))
    assert flow._load(scope, "cycle-1").phase != Phase.CODE_READY


def test_duplicate_diff_across_cycles_rejected(harness):
    stage(harness, "first", DIGEST)
    flow, scope, fence = harness[:3]
    flow.create("second", scope, fence, "request-second")
    flow.plan(scope, "second", TaskPlan("plan-second", "request-second", ("unit",), 1))
    flow.execute(scope, "second", ExecutionRequest(scope, fence, "plan-second", "build", ("offline-build",), TOOLCHAIN, "build-second"))
    with pytest.raises(StateConflict):
        flow.submit_patch(scope, "second", PatchArtifact("second-patch", DIGEST, ("src/other.py",), BASE, "r1", fence), harness[9])


def test_other_tenant_workspace_rejected(harness):
    flow, scope, fence = harness[:3]
    other = replace(scope, tenant_id="tenant-b")
    with pytest.raises(ScopeDenied):
        flow.create("evil", other, fence, "ref")
    flow.create("legit", scope, fence, "ref")
    with pytest.raises(ScopeDenied):
        flow.plan(other, "legit", TaskPlan("plan", "ref", ("unit",), 1))


def test_cell_fence_mismatch_rejected(harness):
    flow, scope, fence, *_ = harness
    flow.create("cycle-1", scope, fence, "change")
    flow.plan(scope, "cycle-1", TaskPlan("plan", "change", ("unit",), 1))
    harness[7].wrong_fence = True
    with pytest.raises(InvalidEvidence):
        flow.execute(scope, "cycle-1", ExecutionRequest(scope, fence, "plan", "build", ("fixed-test",), TOOLCHAIN, "intent"))


def test_commit_ack_unknown_remains_blocked(harness):
    stage(harness)
    flow, scope, _, _, _, _, git, *_ = harness
    git.unknown = True
    with pytest.raises(EffectUnknown):
        flow.commit(scope, "cycle-1", message="uncertain write", intent_ref="commit")
    assert flow._load(scope, "cycle-1").phase == Phase.UNKNOWN_EXTERNAL_EFFECT
    with pytest.raises(StateConflict):
        flow.commit(scope, "cycle-1", message="blind retry", intent_ref="commit")


def test_reviewers_independent_and_sha_bound(harness):
    commit(harness)
    run_tests(harness)
    flow, scope = harness[:2]
    sha = harness[6].head_sha
    flow.review(scope, "cycle-1", verdict(sha, "functional"))
    with pytest.raises(InvalidEvidence):
        flow.review(scope, "cycle-1", verdict(sha, "functional"))
    with pytest.raises(ScopeDenied):
        flow.review(scope, "cycle-1", ReviewVerdict("task-builder", "security", sha, True, "receipt"))


def test_output_bounds_and_unexecuted_exit_conflict(harness):
    fence = harness[2]
    with pytest.raises(InvalidEvidence):
        ExecutionEvidence("intent", fence, "test", 0, True, DIGEST, TOOLCHAIN, "x" * 4097, "", BASE, "unit", "receipt")
    with pytest.raises(InvalidEvidence):
        ExecutionEvidence("intent", fence, "test", 0, False, DIGEST, TOOLCHAIN, "", "", BASE, "unit", "receipt")


def test_unverified_reviewer_receipt_rejected(harness):
    commit(harness)
    run_tests(harness)
    flow, scope = harness[:2]
    harness[10].valid = False
    with pytest.raises(ScopeDenied):
        flow.review(scope, "cycle-1", verdict(harness[6].head_sha, "functional"))
