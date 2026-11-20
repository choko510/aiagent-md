"""Verify a committed Git projection and atomically advance workspace authority."""

from __future__ import annotations

import time
from pathlib import Path
from typing import BinaryIO

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.code_intelligence.git_proof import GitTreeProof
from aiagent_development.code_intelligence.models import Identity
from aiagent_development.code_intelligence.projection import ProjectionManifest, projection_manifest_digest
from aiagent_development.code_intelligence.projection_stage import stage_git_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt
from aiagent_development.workspace_authority import SnapshotRef
from aiagent_domain import is_uuid7, uuid7

from .git_receipts import GitReceiptStore


class GitSnapshotPromotionUnknown(RuntimeError):
    """Promotion failed or its transaction result must be reconciled."""


def promote_git_snapshot(
    db: Database,
    claim: Claim,
    receipts: GitReceiptStore,
    operation: str,
    receipt_id: str,
    manifest: ProjectionManifest,
    proof: GitTreeProof,
    source_receipt: ProjectionStreamReceipt,
    stream: BinaryIO,
    private_parent: Path,
    *,
    deadline: float,
) -> SnapshotRef:
    """Authenticate receipt, stage the new projection, then atomically advance its binding and head.

    Current support is test-profile only. A lost DB response is UNKNOWN; this
    function must not be blindly retried or used to grant Git authority.
    """
    if (
        config.profile() != "test"
        or not is_uuid7(operation)
        or not is_uuid7(receipt_id)
        or not time.monotonic() < deadline <= time.monotonic() + 60
    ):
        raise GitSnapshotPromotionUnknown("Git snapshot promotion unavailable")
    try:
        signed, body_sha = receipts.fetch(operation, receipt_id, deadline=deadline)
        with db.tx(tenant=claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
            current = control.check_current(c, claim)
            row = c.execute(
                "SELECT b.id,b.generation,b.workspace_revision,b.approved_commit,b.workspace_id "
                "FROM agent.git_operations o JOIN agent.repository_bindings b ON b.tenant_id=o.tenant_id AND b.id=o.binding_id "
                "WHERE o.tenant_id=%s AND o.id=%s",
                (claim.tenant_id, operation),
            ).fetchone()
        if (
            row is None
            or str(row["workspace_id"]) != str(current["workspace_id"])
            or str(row["id"]) != signed["binding"]
            or int(row["generation"]) != signed["generation"]
            or str(row["approved_commit"]) != signed["parent"]
        ):
            raise ValueError("Git binding changed")
        identity = Identity(
            claim.tenant_id,
            claim.owner_user_id,
            str(row["id"]),
            str(int(row["workspace_revision"]) + 1),
            str(signed["commit"]),
        )
        if manifest.identity != identity or manifest.tree != signed["tree"]:
            raise ValueError("Git snapshot does not match receipt")
        with stage_git_projection(
            private_parent, stream, manifest, source_receipt, proof, identity, deadline=deadline
        ) as staged:
            strength = staged.check.collision_resistance
        if time.monotonic() >= deadline:
            raise ValueError("Git snapshot promotion deadline exceeded")
        snapshot_id = uuid7()
        with db.tx(tenant=claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
            result = c.execute(
                "SELECT agent.git_promote_verified_snapshot(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s,%s) AS id",
                (
                    claim.tenant_id,
                    claim.owner_user_id,
                    str(row["workspace_id"]),
                    claim.task_id,
                    claim.run_id,
                    claim.goal_revision,
                    claim.lease_epoch,
                    claim.worker_ref,
                    claim.restore_incarnation,
                    operation,
                    receipt_id,
                    body_sha,
                    snapshot_id,
                    manifest.tree,
                    projection_manifest_digest(manifest),
                    source_receipt.sha256,
                    source_receipt.byte_length,
                    strength,
                ),
            ).fetchone()
            if result is None or str(result["id"]) != snapshot_id:
                raise ValueError("Git snapshot promotion unavailable")
        return SnapshotRef(
            snapshot_id,
            str(row["id"]),
            str(row["workspace_id"]),
            int(identity.revision),
            identity.commit,
            manifest.tree,
            projection_manifest_digest(manifest),
            source_receipt.sha256,
            strength,
        )
    except Exception as exc:
        raise GitSnapshotPromotionUnknown("Git snapshot promotion requires reconciliation") from exc
