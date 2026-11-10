"""AI-03 development vertical-slice contract tests. All Git operations use temp fixtures."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from aiagent_development.contracts import (
    EffectUnknown,
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
)
from aiagent_development.mock import MemoryCycleStore, MockAuthority, MockGitProvider, MockSandbox
from aiagent_development.paths import safe_relative_path, validate_manifest_paths
from aiagent_development.workflow import DevelopmentWorkflow


def _git(*args: str, cwd: Path) -> str:
    # TEST-ONLY fixed argv, temporary repository; this is NOT production code execution.
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=10)
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "exclusive-fixture"
    root.mkdir()
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "Fixture Bot", cwd=root)
    _git("config", "user.email", "fixture@example.invalid", cwd=root)
    (root / "app.py").write_text("x = 1\n")
    _git("add", "app.py", cwd=root)
    _git("commit", "-m", "initial fixture", cwd=root)
    return root, _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def env(repo: tuple[Path, str]):
    root, head = repo
    scope = ProjectScope("tenant-A", "owner-A", "project-A", "workspace-A", "fixture/project", "feat/change", head, "revision-1")
    run = RunFence("builder-task", "run-1", 1, 1, 1)
    auth = MockAuthority(scope, run)
    scm = MockGitProvider(scope, auth)
    sandbox = MockSandbox()
    store = MemoryCycleStore()
    flow = DevelopmentWorkflow(runs=auth, reviewers=auth, workspaces=auth, gateway=auth, sandbox=sandbox, git=scm, store=store)
    return root, scope, run, auth, scm, sandbox, store, flow


def _request(env, *, kind="build", intent="build-1", plan_ref="plan-1"):
    _, scope, run, _, _, _, _, _ = env
    return ExecutionRequest(scope, run, plan_ref, kind, ("pytest", "-q"), "a" * 64, intent)


def _prepare(env, cycle="cycle-1", *, tests=("unit", "security")):
    root, scope, run, _, _, sandbox, _, flow = env
    flow.create(cycle, scope, run, "change-1")
    flow.plan(scope, cycle, TaskPlan("plan-1", "change-1", tests, run.goal_revision))
    flow.execute(scope, cycle, _request(env))
    patch = PatchArtifact("artifact-1", hashlib.sha256(b"patch-1").hexdigest(), ("app.py",), scope.base_sha, scope.workspace_revision, run)
    flow.submit_patch(scope, cycle, patch, root)
    flow.commit(scope, cycle, message="test-only commit", intent_ref="commit-1")
    committed = flow._load(scope, cycle)
    assert committed.commit is not None
    sandbox.source_sha = committed.commit.commit_sha
    return committed


def _test(env, test_id, *, executed=True, exit_code=0, cycle="cycle-1"):
    _, scope, _, _, _, sandbox, _, flow = env
    intent = f"test-{test_id}"
    sandbox.results[intent] = (executed, exit_code if executed else None, test_id)
    return flow.execute(scope, cycle, _request(env, kind="test", intent=intent))


def _review(env, role, *, passed=True, finding=None, sha=None):
    _, scope, _, _, scm, _, _, flow = env
    verdict = ReviewVerdict(f"task-{role}", role, sha or scm.head(scope), passed, f"evidence-{role}", (finding,) if finding else ())
    return flow.review(scope, "cycle-1", verdict)


def test_happy_path_code_ready_but_not_host_verified(env):
    _, scope, _, _, _, _, _, flow = env
    _prepare(env)
    _test(env, "unit")
    _test(env, "security")
    _review(env, "functional")
    _review(env, "security")
    c = _review(env, "ui-e2e")
    assert c.phase == Phase.CODE_READY
    assert flow.assert_code_ready(scope, "cycle-1") == c
    assert c.phase != "HOST_VERIFIED"


def test_branch_cas_race_blocks_commit(env):
    root, scope, run, _, scm, _, _, flow = env
    flow.create("cycle-1", scope, run, "change-1")
    flow.plan(scope, "cycle-1", TaskPlan("plan-1", "change-1", ("unit",), 1))
    flow.execute(scope, "cycle-1", _request(env))
    patch = PatchArtifact("patch-1", "f" * 64, ("app.py",), scope.base_sha, scope.workspace_revision, run)
    flow.submit_patch(scope, "cycle-1", patch, root)
    scm.heads[(scope.repository, scope.branch, scope.workspace_id)] = "1" * 40
    with pytest.raises(StaleReference):
        flow.commit(scope, "cycle-1", message="commit", intent_ref="commit-1")


def test_cas_checks_at_write_time_even_after_precheck(env):
    _prepare(env)
    _, scope, _, auth, scm, _, _, _ = env
    patch = PatchArtifact("second", "2" * 64, ("app.py",), scm.head(scope), scope.workspace_revision, auth.run)
    # Competing worker wins the branch between inspection and mutation.
    with pytest.raises(StaleReference):
        scm.commit_patch(scope, patch, expected_head_sha=scope.base_sha, intent_ref="other", message="other")


def test_stale_sha_review_and_branch_change_fail_closed(env):
    _, scope, _, _, scm, _, _, _flow = env
    _prepare(env)
    with pytest.raises(StaleReference):
        _review(env, "functional", sha=scope.base_sha)
    scm.heads[(scope.repository, scope.branch, scope.workspace_id)] = "9" * 40
    with pytest.raises(StaleReference):
        _review(env, "functional")


@pytest.mark.parametrize("path", ["../private", "a/../../b", "/etc/passwd", "C:/Windows/system32", "a\\b", "x//y", ".git/config", "a/.GIT/config", "a/./b", "~/.ssh/id_rsa", "a\x00b", "a\nfoo", "a/"])
def test_path_traversal_and_metadata_denied(path):
    with pytest.raises(InvalidPath):
        safe_relative_path(path)


def test_symlink_escape_denied(repo, tmp_path):
    root, _ = repo
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidPath):
        validate_manifest_paths(root, ("escape/x.py",))
    (root / "file-link").symlink_to(outside / "x")
    with pytest.raises(InvalidPath):
        validate_manifest_paths(root, ("file-link",))


def test_nonexistent_safe_target_preflight(repo):
    root, _ = repo
    assert validate_manifest_paths(root, ("src/new.py",)) == ("src/new.py",)


def test_push_requires_explicit_gateway_approval(env):
    _, scope, _, auth, scm, _, _, flow = env
    _prepare(env)
    _test(env, "unit")
    _test(env, "security")
    _review(env, "functional")
    _review(env, "security")
    _review(env, "ui-e2e")
    with pytest.raises(ScopeDenied):
        flow.push(scope, "cycle-1", intent_ref="push-1")
    assert scm.pushes == []
    auth.allowed.add("git.push")
    assert flow.push(scope, "cycle-1", intent_ref="push-2").startswith("mock-push:")
    with pytest.raises(ScopeDenied):
        flow.create_pr(scope, "cycle-1", intent_ref="pr-1")


def test_cancel_or_fence_after_admission_blocks_commit(env):
    root, scope, run, auth, scm, _, _, flow = env
    flow.create("cycle-1", scope, run, "change-1")
    flow.plan(scope, "cycle-1", TaskPlan("plan-1", "change-1", ("unit",), 1))
    flow.execute(scope, "cycle-1", _request(env))
    flow.submit_patch(scope, "cycle-1", PatchArtifact("patch", "f" * 64, ("app.py",), scope.base_sha, scope.workspace_revision, run), root)
    original = auth.require_operation

    def revoke_after_approval(*args):
        original(*args)
        auth.active = False

    auth.require_operation = revoke_after_approval  # type: ignore[method-assign]
    with pytest.raises(EffectUnknown):
        flow.commit(scope, "cycle-1", message="commit", intent_ref="commit-1")
    assert scm.head(scope) == scope.base_sha
    auth.active = True
    auth.run = replace(run, lease_epoch=run.lease_epoch + 1)
    with pytest.raises(ScopeDenied):
        scm.commit_patch(scope, PatchArtifact("patch", "b" * 64, ("app.py",), scope.base_sha, scope.workspace_revision, run), scope.base_sha, "i", "commit")


@pytest.mark.parametrize("executed,code", [(False, None), (True, 1)])
def test_failed_or_not_run_tests_cannot_be_fake_pass(env, executed, code):
    _, scope, _, _, _, _, _, flow = env
    _prepare(env)
    _test(env, "unit", executed=executed, exit_code=code)
    _test(env, "security")
    _review(env, "functional")
    _review(env, "security")
    assert _review(env, "ui-e2e").phase != Phase.CODE_READY
    with pytest.raises(StateConflict):
        flow.assert_code_ready(scope, "cycle-1")


def test_missing_required_test_never_ready(env):
    _prepare(env)
    _test(env, "unit")
    _review(env, "functional")
    _review(env, "security")
    assert _review(env, "ui-e2e").phase == Phase.REVIEWING


def test_open_p0_p1_and_false_pass_rejected(env):
    _prepare(env)
    _test(env, "unit")
    _test(env, "security")
    _review(env, "functional", finding=ReviewFinding("p0", "P0", "OPEN"))
    _review(env, "security", finding=ReviewFinding("p1", "P1", "UNVERIFIED"))
    assert _review(env, "ui-e2e").phase == Phase.REVIEWING


def test_duplicate_diff_rejected_across_cycles(env):
    root, scope, run, _, _, _, _, flow = env
    flow.create("cycle-1", scope, run, "change-1")
    flow.plan(scope, "cycle-1", TaskPlan("plan-1", "change-1", ("unit",), 1))
    flow.execute(scope, "cycle-1", _request(env))
    patch = PatchArtifact("p", "b" * 64, ("app.py",), scope.base_sha, scope.workspace_revision, run)
    flow.submit_patch(scope, "cycle-1", patch, root)
    flow.create("cycle-2", scope, run, "change-2")
    flow.plan(scope, "cycle-2", TaskPlan("plan-2", "change-2", ("unit",), 1))
    flow.execute(scope, "cycle-2", _request(env, intent="build-2", plan_ref="plan-2"))
    with pytest.raises(StateConflict):
        flow.submit_patch(scope, "cycle-2", patch, root)


def test_cross_tenant_and_workspace_denied(env):
    _, scope, _, _, _, _, store, flow = env
    flow.create("cycle-1", scope, flow.runs.run, "change-1")
    for foreign in (replace(scope, tenant_id="tenant-B"), replace(scope, workspace_id="workspace-B")):
        with pytest.raises(ScopeDenied):
            store.load(foreign, "cycle-1")
        with pytest.raises(ScopeDenied):
            flow.plan(foreign, "cycle-1", TaskPlan("plan-1", "change-1", ("unit",), 1))


def test_invalid_mock_evidence_and_duplicate_test_receipt(env):
    _prepare(env)
    _test(env, "unit")
    with pytest.raises(StateConflict):
        _test(env, "unit")


def test_review_unique_role_and_task_required(env):
    _prepare(env)
    _test(env, "unit")
    _test(env, "security")
    _review(env, "functional")
    with pytest.raises(InvalidEvidence):
        _review(env, "functional")


def test_independent_cycle_store_cas(env):
    _, scope, _, _, _, _, store, flow = env
    c = flow.create("cycle-1", scope, flow.runs.run, "change-1")
    store.save(c, replace(c, version=1))
    with pytest.raises(StateConflict):
        store.save(c, replace(c, version=1))


def test_atomic_diff_claim_does_not_leave_orphans(env):
    _, scope, run, _, _, _, store, flow = env
    c = flow.create("cycle-1", scope, run, "change-1")
    next_version = replace(c, version=1)
    store.save(c, next_version)
    digest = "f" * 64
    with pytest.raises(StateConflict):
        store.save_with_diff_claim(c, replace(c, version=1, phase=Phase.PATCH_READY), digest)
    assert (scope.tenant_id, scope.project_id, digest) not in store._diffs


def test_symlink_in_repository_root_ancestor_denied(repo, tmp_path):
    root, _ = repo
    shortcut = tmp_path / "shortcut"
    shortcut.symlink_to(root, target_is_directory=True)
    with pytest.raises(InvalidPath):
        validate_manifest_paths(shortcut, ("file.py",))
