"""Ports to existing authorities; no credentials, SQL or subprocess in this package.

AI-07 owns adapters: these protocols cannot issue authority or bypass the Gateway.
Each external Git action must create/settle a distinct Tool Intent and effect ledger
record, with Sentinel's decision and the current Run lease checked at dispatch.
"""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    CommitEvidence,
    Cycle,
    ExecutionEvidence,
    ExecutionRequest,
    PatchArtifact,
    ProjectScope,
    RunFence,
    ReviewVerdict,
)


class RunAuthorityPort(Protocol):
    def assert_current(self, scope: ProjectScope, run: RunFence) -> None:
        """Rehydrate Task/Run/tenant owner, goal revision, cancel and lease from DB."""


class WorkspacePort(Protocol):
    def assert_scope(self, scope: ProjectScope) -> None:
        """Rehydrate project/workspace owner, tenant, repository, branch and revision."""


class GatewayAuthorizationPort(Protocol):
    def require_operation(
        self, scope: ProjectScope, run: RunFence, operation: str, target_sha: str, intent_ref: str
    ) -> None:
        """Final Gateway+Sentinel+ledger admission; do not trust a bare approval string."""


class ReviewAttestationPort(Protocol):
    def assert_verified(self, scope: ProjectScope, builder_run: RunFence, verdict: ReviewVerdict) -> None:
        """Load independent reviewer Task/Run and evidence from canonical store.

        Must verify reviewer role, tenant, bound SHA, actual review execution and
        receipt provenance, not trust verdict's self-declared role/pass flag.
        """


class SandboxExecutionPort(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
        """Dispatch into approved Untrusted Cell; never execute generated code in Backend."""


class GitProviderPort(Protocol):
    def head(self, scope: ProjectScope) -> str:
        """Read the exact approved remote/fixture ref, not arbitrary model-supplied refs."""

    def commit_patch(
        self, scope: ProjectScope, patch: PatchArtifact, expected_head_sha: str, intent_ref: str, message: str
    ) -> CommitEvidence:
        """Atomic compare-and-swap of target branch, no force push or implicit merge.

        Adapter must verify the expected HEAD *at update*, not only before it.
        A timeout after possibly sending is EffectUnknown, not a safe retry.
        """

    def push(self, scope: ProjectScope, expected_head_sha: str, intent_ref: str) -> str:
        """Push explicitly approved branch only, with remote expected HEAD CAS."""

    def create_pr(self, scope: ProjectScope, head_sha: str, intent_ref: str) -> str:
        """Create a PR after a separate approved typed operation (never merge)."""


class CycleStorePort(Protocol):
    def create(self, cycle: Cycle) -> None:
        """Create scoped durable cycle; unique (tenant, project, cycle_id)."""

    def load(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        """Tenant-scoped read; missing or other tenant must be denied."""

    def save(self, previous: Cycle, updated: Cycle) -> None:
        """Atomic CAS by previous.version; outbox transaction is AI-07 owned."""

    def save_with_diff_claim(self, previous: Cycle, updated: Cycle, diff_sha256: str) -> None:
        """Atomically CAS the cycle AND claim tenant/project-scoped diff uniqueness.

        Claim must roll back if CAS fails; never reserve an orphan diff or let two
        concurrent workers commit the same digest.
        """
