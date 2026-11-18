"""Trusted Gateway transitions for a prepared execution and authenticated capture.

These operations do not dispatch a Cell or transfer bytes. A caller must use the
same scoped execution, authenticated transport, and live authority watchdog.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime

import psycopg
from aiagent_core import control
from aiagent_core.db import Database
from aiagent_development.code_intelligence.indexer_recipe import python_scip_recipe_digest
from aiagent_development.code_intelligence.models import Identity
from aiagent_development.code_intelligence.projection import ProjectionManifest, projection_manifest_digest
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt
from aiagent_development.workspace_authority import PreparedExecution, _binding_payload
from aiagent_domain import require_uuid7, sha256_hex, uuid7
from aiagent_sandbox.source_ticket import sign_source_ticket

from .execution_artifacts import ExecutionArtifactError, _db_deadline, _deadline, _read_private_fd
from .execution_capture_proof import HmacCaptureVerifier


class ExecutionLifecycle:
    def __init__(self, db: Database, claim: control.Claim, verifier: HmacCaptureVerifier) -> None:
        self._db, self._claim, self._verifier = db, claim, verifier

    def _scope(self, workspace: str, execution_id: str) -> tuple[object, ...]:
        claim = self._claim
        return (
            claim.tenant_id,
            claim.owner_user_id,
            workspace,
            claim.task_id,
            claim.run_id,
            claim.goal_revision,
            claim.lease_epoch,
            claim.worker_ref,
            claim.restore_incarnation,
            execution_id,
        )

    def _workspace(self, deadline: float) -> str:
        with self._db.tx(tenant=self._claim.tenant_id) as c:
            _db_deadline(c, deadline)
            return str(control.check_current(c, self._claim)["workspace_id"])

    def start(self, execution_id: str, binding_sha256: str, *, deadline: float) -> None:
        """Fence PREPARED -> RUNNING before dispatch; a retry cannot start twice."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                c.execute(
                    "SELECT agent.execution_start(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*self._scope(workspace, execution_id), binding_sha256),
                )
        except psycopg.Error as exc:
            raise ExecutionArtifactError("EXECUTION_START_DENIED") from exc

    def issue_source_session(
        self,
        prepared: PreparedExecution,
        manifest: ProjectionManifest,
        receipt: ProjectionStreamReceipt,
        ticket_key: bytes,
        *,
        deadline: float,
    ) -> dict[str, object]:
        """Atomically fence dispatch and issue one source ticket for the DB snapshot.

        The result is not a live lease: a remote watchdog and authenticated
        registration are still required before it may drive execution.
        """
        _deadline(deadline)
        if len(ticket_key) < 32:
            raise ExecutionArtifactError("SOURCE_SESSION_INVALID")
        workspace = self._workspace(deadline)
        session_id = uuid7()
        expires = int(time.time()) + min(45, max(1, int(deadline - time.monotonic())))
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                row = c.execute(
                    "SELECT e.*,s.binding_id,s.workspace_revision,s.commit_sha,s.tree_sha,s.manifest_sha256,"
                    "s.source_sha256,s.source_bytes FROM agent.execution_instances e "
                    "JOIN agent.workspace_snapshot_attestations s ON s.tenant_id=e.tenant_id AND s.id=e.snapshot_id "
                    "WHERE e.tenant_id=%s AND e.id=%s",
                    (self._claim.tenant_id, prepared.id),
                ).fetchone()
                if row is None:
                    raise ExecutionArtifactError("SOURCE_SESSION_DENIED")
                identity = Identity(
                    self._claim.tenant_id,
                    self._claim.owner_user_id,
                    str(row["binding_id"]),
                    str(row["workspace_revision"]),
                    str(row["commit_sha"]),
                )
                if (
                    manifest.identity != identity
                    or manifest.tree != str(row["tree_sha"])
                    or not hmac.compare_digest(
                        projection_manifest_digest(manifest), str(row["manifest_sha256"]).strip()
                    )
                    or not hmac.compare_digest(receipt.sha256, str(row["source_sha256"]).strip())
                    or receipt.byte_length != int(row["source_bytes"])
                    or receipt.file_count != len(manifest.files)
                    or prepared.attempt_id != str(row["tool_attempt_id"])
                    or prepared.snapshot_id != str(row["snapshot_id"])
                    or prepared.recipe_id != str(row["recipe_id"])
                    or not hmac.compare_digest(prepared.binding_sha256, str(row["binding_sha256"]).strip())
                ):
                    raise ExecutionArtifactError("SOURCE_SESSION_DENIED")
                payload = {
                    "version": 1,
                    "direction": "gateway-to-supervisor",
                    "session": session_id,
                    "tenant": self._claim.tenant_id,
                    "owner": self._claim.owner_user_id,
                    "task": self._claim.task_id,
                    "run": self._claim.run_id,
                    "goal": self._claim.goal_revision,
                    "lease": self._claim.lease_epoch,
                    "execution": prepared.id,
                    "attempt": prepared.attempt_id,
                    "snapshot": prepared.snapshot_id,
                    "recipe": prepared.recipe_id,
                    "binding_sha256": prepared.binding_sha256,
                    "repository_binding": identity.binding_id,
                    "revision": identity.revision,
                    "commit": identity.commit,
                    "tree": manifest.tree,
                    "manifest_sha256": str(row["manifest_sha256"]).strip(),
                    "source_sha256": receipt.sha256,
                    "source_bytes": receipt.byte_length,
                    "expires_at": expires,
                }
                c.execute(
                    "SELECT agent.execution_issue_source_session(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        *self._scope(workspace, prepared.id),
                        prepared.binding_sha256,
                        session_id,
                        datetime.fromtimestamp(expires, UTC),
                    ),
                )
        except psycopg.Error as exc:
            raise ExecutionArtifactError("SOURCE_SESSION_DENIED") from exc
        return sign_source_ticket(ticket_key, payload)

    def finish(self, execution_id: str, capture_fd: int, proof: bytes, *, deadline: float) -> None:
        """Record only the bytes and binding authenticated by the capture verifier."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        workspace = self._workspace(deadline)
        data = _read_private_fd(capture_fd, deadline)
        digest = hashlib.sha256(data).hexdigest()
        binding = self._verifier.verify_running(self._claim, execution_id, digest, len(data), proof)
        _deadline(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                c.execute(
                    "SELECT agent.execution_finish(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        *self._scope(workspace, execution_id),
                        binding,
                        digest,
                        len(data),
                        hashlib.sha256(proof).hexdigest(),
                    ),
                )
        except psycopg.Error as exc:
            raise ExecutionArtifactError("EXECUTION_FINISH_DENIED") from exc

    def mark_unknown(self, execution_id: str, binding_sha256: str, *, deadline: float) -> None:
        """Fence an ambiguous execution even after its original lease is lost."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                c.execute(
                    "SELECT agent.execution_mark_unknown(%s,%s,%s,%s,%s,%s)",
                    (
                        self._claim.tenant_id,
                        self._claim.owner_user_id,
                        self._claim.task_id,
                        self._claim.run_id,
                        execution_id,
                        binding_sha256,
                    ),
                )
        except psycopg.Error as exc:
            raise ExecutionArtifactError("EXECUTION_UNKNOWN_DENIED") from exc

    def check_running(self, execution_id: str, binding_sha256: str, *, deadline: float) -> None:
        """Recheck canonical Run, lease, snapshot, recipe and running Attempt."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                c.execute(
                    "SELECT agent.execution_check_running(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*self._scope(workspace, execution_id), binding_sha256),
                )
        except psycopg.Error as exc:
            raise ExecutionArtifactError("EXECUTION_AUTHORITY_DENIED") from exc

    def running_index_binding(self, prepared: PreparedExecution, *, deadline: float) -> dict[str, object]:
        """Return the complete canonical binding only for an approved RUNNING SCIP recipe."""
        _deadline(deadline)
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                c.execute(
                    "SELECT agent.execution_check_running(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*self._scope(workspace, prepared.id), prepared.binding_sha256),
                )
                execution = c.execute(
                    "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
                    (self._claim.tenant_id, prepared.id),
                ).fetchone()
                snapshot = c.execute(
                    "SELECT * FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s AND id=%s",
                    (self._claim.tenant_id, prepared.snapshot_id),
                ).fetchone()
                recipe = c.execute(
                    "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s",
                    (self._claim.tenant_id, prepared.recipe_id),
                ).fetchone()
                if execution is None or snapshot is None or recipe is None or recipe["kind"] != "index":
                    raise ExecutionArtifactError("INDEX_RECIPE_DENIED")
                if str(recipe["template_sha256"]).strip() != python_scip_recipe_digest():
                    raise ExecutionArtifactError("INDEX_RECIPE_DENIED")
                binding = _binding_payload(execution, snapshot, recipe)
                if sha256_hex(binding) != prepared.binding_sha256:
                    raise ExecutionArtifactError("INDEX_BINDING_DENIED")
                return binding
        except psycopg.Error as exc:
            raise ExecutionArtifactError("EXECUTION_AUTHORITY_DENIED") from exc
