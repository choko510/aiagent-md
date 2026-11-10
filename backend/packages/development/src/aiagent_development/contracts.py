"""Immutable development projections. They are NOT a replacement for canonical Task/Run/Approval.

All principal, goal, workspace and fence values must be rehydrated from the trusted
control plane; model-supplied values never grant permissions by constructing these types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class DevelopmentError(RuntimeError):
    """Fail closed: caller must not treat an error as an approval or successful effect."""


class ScopeDenied(DevelopmentError):
    pass


class StaleReference(DevelopmentError):
    pass


class InvalidPath(DevelopmentError):
    pass


class StateConflict(DevelopmentError):
    pass


class InvalidEvidence(DevelopmentError):
    pass


class EffectUnknown(DevelopmentError):
    """May have reached a remote provider; reconcile; never blindly re-dispatch."""


def require_sha(value: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InvalidEvidence("expected a full lowercase Git SHA")
    return value


def require_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise InvalidEvidence("expected a full lowercase sha256 digest")
    return value


@dataclass(frozen=True)
class ProjectScope:
    tenant_id: str
    owner_user_id: str
    project_id: str
    workspace_id: str
    repository: str
    branch: str
    base_sha: str
    workspace_revision: str

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.owner_user_id, self.project_id, self.workspace_id, self.workspace_revision)):
            raise ScopeDenied("missing server-scoped project identity")
        if not _REPO.fullmatch(self.repository) or any(part in (".", "..") for part in self.repository.split("/")):
            raise ScopeDenied("invalid repository identity")
        # Reject git ref options, pseudo-refs, and ambiguous branch names up front.
        if (
            not self.branch
            or self.branch.startswith(("-", "/", "."))
            or self.branch.endswith(("/", ".", ".lock"))
            or self.branch in ("HEAD", "@")
            or self.branch.startswith("refs/")
            or any(part.startswith(".") or part.endswith((".", ".lock")) for part in self.branch.split("/"))
            or any(c in self.branch for c in ("\\", " ", "~", "^", ":", "?", "*", "[", "@{", "..", "//"))
            or any(ord(c) < 33 or ord(c) == 127 for c in self.branch)
        ):
            raise ScopeDenied("invalid approved branch")
        require_sha(self.base_sha)


@dataclass(frozen=True)
class RunFence:
    task_id: str
    run_id: str
    goal_revision: int
    lease_epoch: int
    restore_incarnation: int

    def __post_init__(self) -> None:
        if not self.task_id or not self.run_id or min(self.goal_revision, self.lease_epoch, self.restore_incarnation) < 0:
            raise ScopeDenied("invalid Run fence")


class Phase(StrEnum):
    REQUESTED = "REQUESTED"
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    PATCH_READY = "PATCH_READY"
    COMMIT_PENDING = "COMMIT_PENDING"
    COMMITTED = "COMMITTED"
    REVIEWING = "REVIEWING"
    CODE_READY = "CODE_READY"
    BLOCKED = "BLOCKED"
    UNKNOWN_EXTERNAL_EFFECT = "UNKNOWN_EXTERNAL_EFFECT"


@dataclass(frozen=True)
class TaskPlan:
    plan_ref: str
    change_request_ref: str
    required_tests: tuple[str, ...]
    goal_revision: int

    def __post_init__(self) -> None:
        if not self.plan_ref or not self.change_request_ref or not self.required_tests:
            raise InvalidEvidence("plan needs artifact refs and required tests")
        if any(not x for x in self.required_tests) or len(set(self.required_tests)) != len(self.required_tests):
            raise InvalidEvidence("required test IDs must be unique")


@dataclass(frozen=True)
class ExecutionRequest:
    project: ProjectScope
    run: RunFence
    plan_ref: str
    kind: str  # build, test, static-check; never host execution
    argv: tuple[str, ...]
    toolchain_digest: str
    intent_ref: str

    def __post_init__(self) -> None:
        if self.kind not in ("build", "test", "static-check") or not self.argv or not self.intent_ref:
            raise ScopeDenied("invalid scoped sandbox request")
        if any(not a or "\x00" in a for a in self.argv):
            raise ScopeDenied("invalid argv")
        require_digest(self.toolchain_digest)


@dataclass(frozen=True)
class ExecutionEvidence:
    intent_ref: str
    run: RunFence
    kind: str
    exit_code: int | None
    executed: bool
    artifact_sha256: str
    toolchain_digest: str
    stdout: str
    stderr: str
    source_commit_sha: str | None = None
    test_id: str | None = None
    # Required: this is the actual dispatch claim; not a self-reported LLM fence.
    receipt_ref: str = ""

    def __post_init__(self) -> None:
        require_digest(self.artifact_sha256)
        require_digest(self.toolchain_digest)
        if self.source_commit_sha is not None:
            require_sha(self.source_commit_sha)
        if len(self.stdout.encode()) > 4096 or len(self.stderr.encode()) > 4096:
            raise InvalidEvidence("bounded output exceeded")
        if not self.receipt_ref or not self.intent_ref or self.kind not in ("build", "test", "static-check"):
            raise InvalidEvidence("missing execution evidence identity")
        if self.executed and self.exit_code is None:
            raise InvalidEvidence("executed receipt needs exit code")
        if not self.executed and self.exit_code is not None:
            raise InvalidEvidence("unexecuted receipt cannot report exit code")


@dataclass(frozen=True)
class PatchArtifact:
    artifact_ref: str
    diff_sha256: str
    paths: tuple[str, ...]
    parent_sha: str
    workspace_revision: str
    run: RunFence

    def __post_init__(self) -> None:
        if not self.artifact_ref or not self.paths or not self.workspace_revision:
            raise InvalidEvidence("patch artifact lacks provenance")
        require_digest(self.diff_sha256)
        require_sha(self.parent_sha)


@dataclass(frozen=True)
class CommitEvidence:
    parent_sha: str
    commit_sha: str
    diff_sha256: str
    artifact_ref: str
    receipt_ref: str

    def __post_init__(self) -> None:
        require_sha(self.parent_sha)
        require_sha(self.commit_sha)
        require_digest(self.diff_sha256)
        if self.commit_sha == self.parent_sha or not self.artifact_ref or not self.receipt_ref:
            raise InvalidEvidence("invalid commit evidence")


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    severity: str
    status: str  # OPEN, UNVERIFIED, RESOLVED

    def __post_init__(self) -> None:
        if not self.finding_id or self.severity not in ("P0", "P1", "P2", "P3"):
            raise InvalidEvidence("invalid finding")
        if self.status not in ("OPEN", "UNVERIFIED", "RESOLVED"):
            raise InvalidEvidence("invalid finding status")


@dataclass(frozen=True)
class ReviewVerdict:
    reviewer_task_id: str
    reviewer_role: str
    target_sha: str
    passed: bool
    evidence_ref: str
    findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        require_sha(self.target_sha)
        if not self.reviewer_task_id or not self.evidence_ref or self.reviewer_role not in (
            "functional", "security", "ui-e2e"
        ):
            raise InvalidEvidence("reviewer identity/role/evidence required")


@dataclass(frozen=True)
class Cycle:
    cycle_id: str
    scope: ProjectScope
    run: RunFence
    phase: Phase
    change_request_ref: str
    version: int = 0
    plan: TaskPlan | None = None
    patch: PatchArtifact | None = None
    commit: CommitEvidence | None = None
    tests: tuple[ExecutionEvidence, ...] = ()
    reviews: tuple[ReviewVerdict, ...] = ()
    execution_receipts: tuple[ExecutionEvidence, ...] = ()
    submitted_digests: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.cycle_id or not self.change_request_ref or self.version < 0:
            raise StateConflict("invalid development cycle")
