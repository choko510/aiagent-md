"""Explicitly test-only deterministic ports. NEVER connect these to production users.

The synthetic SHA returned here is not a real Git commit object, no network is used.
Local actual Git fixture setup is confined to tests/ and never in Trusted Backend.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from .contracts import (
    CommitEvidence,
    Cycle,
    ExecutionEvidence,
    ExecutionRequest,
    InvalidEvidence,
    PatchArtifact,
    ProjectScope,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    StaleReference,
    StateConflict,
)


@dataclass
class MockAuthority:
    scope: ProjectScope
    run: RunFence
    allowed: set[str] = field(default_factory=lambda: {"sandbox.build", "sandbox.test", "sandbox.static-check", "git.commit"})
    active: bool = True
    calls: list[str] = field(default_factory=list)

    def assert_current(self, scope: ProjectScope, run: RunFence) -> None:
        if not self.active or scope != self.scope or run != self.run:
            raise ScopeDenied("inactive, cancelled or different Run/tenant/fence")

    def assert_verified(self, scope: ProjectScope, builder_run: RunFence, verdict: ReviewVerdict) -> None:
        self.assert_current(scope, builder_run)
        if verdict.reviewer_task_id == builder_run.task_id or verdict.reviewer_task_id != f"task-{verdict.reviewer_role}":
            raise ScopeDenied("reviewer task is not independently verified")

    def assert_scope(self, scope: ProjectScope) -> None:
        if not self.active or scope != self.scope:
            raise ScopeDenied("workspace ownership or revision mismatched")

    def require_operation(self, scope: ProjectScope, run: RunFence, operation: str, target_sha: str, intent_ref: str) -> None:
        self.assert_current(scope, run)
        if operation not in self.allowed or not target_sha or not intent_ref:
            raise ScopeDenied(f"unapproved {operation} action")
        self.calls.append(operation)


class MemoryCycleStore:
    """CAS is used even by the mock. No cross-tenant fallback lookups."""

    def __init__(self) -> None:
        self._cycles: dict[tuple[str, str, str], Cycle] = {}
        self._diffs: set[tuple[str, str, str]] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _key(scope: ProjectScope, cycle_id: str) -> tuple[str, str, str]:
        return (scope.tenant_id, scope.project_id, cycle_id)

    def create(self, cycle: Cycle) -> None:
        with self._lock:
            key = self._key(cycle.scope, cycle.cycle_id)
            if key in self._cycles:
                raise StateConflict("cycle already exists")
            self._cycles[key] = cycle

    def load(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        with self._lock:
            c = self._cycles.get(self._key(scope, cycle_id))
            if c is None or c.scope != scope:
                raise ScopeDenied("unknown or other tenant/workspace cycle")
            return c

    def save(self, previous: Cycle, updated: Cycle) -> None:
        with self._lock:
            key = self._key(previous.scope, previous.cycle_id)
            if self._cycles.get(key) != previous or updated.version != previous.version + 1:
                raise StateConflict("cycle version CAS conflict")
            if updated.scope != previous.scope or updated.run != previous.run:
                raise ScopeDenied("immutable identity changed")
            self._cycles[key] = updated

    def save_with_diff_claim(self, previous: Cycle, updated: Cycle, diff_sha256: str) -> None:
        with self._lock:
            key = (previous.scope.tenant_id, previous.scope.project_id, diff_sha256)
            cycle_key = self._key(previous.scope, previous.cycle_id)
            if key in self._diffs:
                raise StateConflict("same diff already claimed")
            if self._cycles.get(cycle_key) != previous or updated.version != previous.version + 1:
                raise StateConflict("cycle CAS failed; diff not claimed")
            if updated.scope != previous.scope or updated.run != previous.run:
                raise ScopeDenied("immutable identity changed")
            self._cycles[cycle_key] = updated
            self._diffs.add(key)


class MockGitProvider:
    """Atomically update a fixture branch pointer, checking the current trusted lease again."""

    def __init__(self, scope: ProjectScope, authority: MockAuthority) -> None:
        self.authority = authority
        self.heads = {(scope.repository, scope.branch, scope.workspace_id): scope.base_sha}
        self._lock = threading.RLock()
        self.pushes: list[tuple[str, str]] = []
        self.prs: list[tuple[str, str]] = []

    def head(self, scope: ProjectScope) -> str:
        with self._lock:
            key = (scope.repository, scope.branch, scope.workspace_id)
            if key not in self.heads:
                raise ScopeDenied("unapproved repository or branch")
            return self.heads[key]

    def commit_patch(self, scope: ProjectScope, patch: PatchArtifact, expected_head_sha: str, intent_ref: str, message: str) -> CommitEvidence:
        with self._lock:
            self.authority.assert_current(scope, patch.run)
            if "git.commit" not in self.authority.allowed or not intent_ref or not message.strip():
                raise ScopeDenied("commit not authorized")
            if self.head(scope) != expected_head_sha or patch.parent_sha != expected_head_sha:
                raise StaleReference("branch CAS conflict")
            sha = hashlib.sha256((expected_head_sha + patch.diff_sha256 + patch.artifact_ref).encode()).hexdigest()[:40]
            if sha == expected_head_sha:
                raise InvalidEvidence("unexpected synthetic SHA collision")
            self.heads[(scope.repository, scope.branch, scope.workspace_id)] = sha
            return CommitEvidence(expected_head_sha, sha, patch.diff_sha256, patch.artifact_ref, "mock-commit:" + intent_ref)

    def push(self, scope: ProjectScope, expected_head_sha: str, intent_ref: str) -> str:
        with self._lock:
            self.authority.assert_current(scope, self.authority.run)
            if "git.push" not in self.authority.allowed:
                raise ScopeDenied("push not approved")
            if self.head(scope) != expected_head_sha:
                raise StaleReference("push CAS conflict")
            self.pushes.append((expected_head_sha, intent_ref))
            return "mock-push:" + intent_ref

    def create_pr(self, scope: ProjectScope, head_sha: str, intent_ref: str) -> str:
        with self._lock:
            self.authority.assert_current(scope, self.authority.run)
            if "git.pull_request.create" not in self.authority.allowed:
                raise ScopeDenied("PR creation not approved")
            if self.head(scope) != head_sha:
                raise StaleReference("PR source changed")
            self.prs.append((head_sha, intent_ref))
            return "mock-pr:" + intent_ref


class MockSandbox:
    """Only creates bounded receipts. Does not execute argv at all."""

    def __init__(self) -> None:
        self.results: dict[str, tuple[bool, int | None, str | None]] = {}
        self.source_sha: str | None = None
        self.invocations: list[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
        self.invocations.append(request)
        executed, exit_code, test_id = self.results.get(request.intent_ref, (True, 0, None))
        return ExecutionEvidence(
            intent_ref=request.intent_ref,
            run=request.run,
            kind=request.kind,
            exit_code=exit_code,
            executed=executed,
            artifact_sha256=hashlib.sha256((request.intent_ref + repr(request.argv)).encode()).hexdigest(),
            toolchain_digest=request.toolchain_digest,
            stdout="mock only, no execution",
            stderr="",
            source_commit_sha=self.source_sha,
            test_id=test_id,
            receipt_ref="mock-receipt:" + request.intent_ref,
        )
