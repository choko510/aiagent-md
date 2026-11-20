"""Validate captured SCIP against a trusted immutable snapshot with the B1 decoder."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from aiagent_core import config, control
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.projection import ProjectionManifest, projection_manifest_digest
from aiagent_development.code_intelligence.scip_navigation import (
    IndexManifest,
    IndexStatus,
    SourceProvenance,
    load_index,
)
from aiagent_development.workspace_authority import PreparedExecution

from .execution_artifacts import ExecutionArtifactError


class SnapshotScipVerifier:
    """Local test/development verifier; production requires isolated validation."""

    def __init__(
        self,
        claim: control.Claim,
        prepared: PreparedExecution,
        manifest: ProjectionManifest,
        snapshot_factory: Callable[[], GitSnapshot],
    ) -> None:
        if config.profile() == "production":
            raise ExecutionArtifactError("SCIP_VALIDATION_ISOLATION_REQUIRED")
        self._claim, self._prepared, self._manifest, self._snapshot_factory = (
            claim,
            prepared,
            manifest,
            snapshot_factory,
        )

    def verify(self, claim: control.Claim, execution_id: str, content: bytes) -> str:
        if config.profile() == "production":
            raise ExecutionArtifactError("SCIP_VALIDATION_ISOLATION_REQUIRED")
        if (
            claim != self._claim
            or execution_id != self._prepared.id
            or self._manifest.identity.tenant_id != claim.tenant_id
            or self._manifest.identity.owner_id != claim.owner_user_id
            or self._manifest.identity.commit != self._prepared.commit
            or self._manifest.tree != self._prepared.tree
            or self._manifest.identity.revision != str(self._prepared.revision)
            or projection_manifest_digest(self._manifest) != self._prepared.manifest_sha256
        ):
            raise ExecutionArtifactError("SCIP_SCOPE_INVALID")
        snapshot = self._snapshot_factory()
        if snapshot.snapshot.identity != self._manifest.identity or snapshot.tree != self._manifest.tree:
            raise ExecutionArtifactError("SCIP_SNAPSHOT_STALE")
        index = IndexManifest(
            self._manifest.identity,
            self._manifest.tree,
            hashlib.sha256(content).hexdigest(),
            "scip-python",
            "0.6.8",
            0,
            tuple(SourceProvenance(f.path, f.blob_id, f.sha256) for f in self._manifest.files),
            toolchain_digest=self._prepared.toolchain_sha256,
        )
        loaded = load_index(snapshot, index, content)
        if loaded.status not in (IndexStatus.VALIDATED, IndexStatus.PARTIAL) or loaded.graph is None:
            raise ExecutionArtifactError("SCIP_INDEX_INVALID")
        return str(loaded.status.value)
