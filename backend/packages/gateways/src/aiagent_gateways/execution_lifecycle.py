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
from aiagent_core.db import Database, Row
from aiagent_development.code_intelligence.indexer_recipe import approved_recipe_digest, python_scip_recipe_digest
from aiagent_development.code_intelligence.models import Identity
from aiagent_development.code_intelligence.projection import ProjectionManifest, projection_manifest_digest
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt
from aiagent_development.workspace_authority import PreparedExecution, _binding_payload, _prepared
from aiagent_domain import require_uuid7, sha256_hex, uuid7
from aiagent_sandbox.command_authority import command_template_digest
from aiagent_sandbox.command_receipt import verify_command_receipt
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

    @staticmethod
    def _command_binding(
        execution: Row,
        snapshot: Row,
        recipe: Row,
        intent: Row,
        prepared: PreparedExecution,
    ) -> tuple[dict[str, object], dict[str, object]]:
        kind, args = recipe.get("kind"), intent.get("safe_arguments_json")
        if kind not in ("build", "static-check", "test") or not isinstance(args, dict):
            raise ExecutionArtifactError("COMMAND_RECIPE_DENIED")
        descriptor = {
            "kind": kind,
            "test_id": recipe.get("test_id"),
            "argv": args.get("argv"),
            "cwdRelative": args.get("cwdRelative"),
        }
        try:
            template = command_template_digest(descriptor)
        except ValueError as exc:
            raise ExecutionArtifactError("COMMAND_RECIPE_DENIED") from exc
        if (
            template != str(recipe["template_sha256"]).strip()
            or sha256_hex({"tool": "sandbox_exec", "args": args}) != str(recipe["request_sha256"]).strip()
        ):
            raise ExecutionArtifactError("COMMAND_RECIPE_DENIED")
        expected_recipe = approved_recipe_digest(
            kind=str(kind),
            test_id=str(recipe["test_id"]) if recipe.get("test_id") is not None else None,
            plan_sha256=str(recipe["plan_sha256"]).strip(),
            request_sha256=str(recipe["request_sha256"]).strip(),
            template_sha256=template,
            toolchain_sha256=str(recipe["toolchain_sha256"]).strip(),
            image_digest=str(recipe["image_digest"]),
        )
        if expected_recipe != str(recipe["recipe_sha256"]).strip():
            raise ExecutionArtifactError("COMMAND_RECIPE_DENIED")
        binding = _binding_payload(execution, snapshot, recipe)
        if sha256_hex(binding) != prepared.binding_sha256 or str(intent.get("id")) != prepared.intent_id:
            raise ExecutionArtifactError("COMMAND_BINDING_DENIED")
        return binding, descriptor

    def _command_rows(
        self, c: psycopg.Connection[Row], prepared: PreparedExecution
    ) -> tuple[dict[str, object], dict[str, object]]:
        # Callers first hold the canonical execution lock in this transaction.
        connection = c
        tenant = self._claim.tenant_id
        execution = connection.execute(
            "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND id=%s", (tenant, prepared.id)
        ).fetchone()
        snapshot = connection.execute(
            "SELECT * FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s AND id=%s",
            (tenant, prepared.snapshot_id),
        ).fetchone()
        recipe = connection.execute(
            "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s", (tenant, prepared.recipe_id)
        ).fetchone()
        intent = connection.execute(
            "SELECT * FROM agent.tool_intents WHERE tenant_id=%s AND id=%s", (tenant, prepared.intent_id)
        ).fetchone()
        if execution is None or snapshot is None or recipe is None or intent is None:
            raise ExecutionArtifactError("COMMAND_BINDING_DENIED")
        return self._command_binding(execution, snapshot, recipe, intent, prepared)

    def running_command_binding(
        self, prepared: PreparedExecution, *, deadline: float
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Read exact approved command inputs after a fresh canonical RUNNING check."""
        _deadline(deadline)
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                c.execute(
                    "SELECT agent.execution_check_running(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*self._scope(workspace, prepared.id), prepared.binding_sha256),
                )
                return self._command_rows(c, prepared)
        except psycopg.Error as exc:
            raise ExecutionArtifactError("EXECUTION_AUTHORITY_DENIED") from exc

    def finish_command(
        self,
        prepared: PreparedExecution,
        body_fd: int,
        proof: bytes,
        receipt_key: bytes,
        *,
        deadline: float,
    ) -> tuple[str, dict[str, object], str]:
        """Authenticate real process outcome and atomically persist its canonical receipt."""
        binding, descriptor = self.running_command_binding(prepared, deadline=deadline)
        body = _read_private_fd(body_fd, deadline)
        try:
            payload = verify_command_receipt(receipt_key, body, proof, binding, descriptor)
        except ValueError as exc:
            raise ExecutionArtifactError("COMMAND_RECEIPT_INVALID") from exc
        digest, proof_digest = hashlib.sha256(body).hexdigest(), hashlib.sha256(proof).hexdigest()
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                row = c.execute(
                    "SELECT agent.execution_finish_command(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS id",
                    (
                        *self._scope(workspace, prepared.id),
                        prepared.binding_sha256,
                        uuid7(),
                        descriptor["kind"],
                        descriptor["test_id"] or "",
                        body,
                        proof,
                        digest,
                        proof_digest,
                    ),
                ).fetchone()
                if row is None or row["id"] is None:
                    raise ExecutionArtifactError("COMMAND_RECEIPT_UNAVAILABLE")
                return str(row["id"]), payload, digest
        except psycopg.Error as exc:
            raise ExecutionArtifactError("COMMAND_FINISH_DENIED") from exc

    def fetch_command_receipt(
        self,
        execution_id: str,
        receipt_id: str,
        receipt_key: bytes,
        *,
        deadline: float,
    ) -> tuple[dict[str, object], str]:
        """Rehydrate and authenticate a scoped durable command receipt."""
        _deadline(deadline)
        require_uuid7(execution_id, "execution id")
        require_uuid7(receipt_id, "receipt id")
        workspace = self._workspace(deadline)
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                _db_deadline(c, deadline)
                row = c.execute(
                    "SELECT * FROM agent.execution_fetch_command(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    self._scope(workspace, execution_id),
                ).fetchone()
                if row is None or str(row["receipt_id"]) != receipt_id:
                    raise ExecutionArtifactError("COMMAND_RECEIPT_UNAVAILABLE")
                execution = c.execute(
                    "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
                    (self._claim.tenant_id, execution_id),
                ).fetchone()
                if execution is None:
                    raise ExecutionArtifactError("COMMAND_RECEIPT_UNAVAILABLE")
                snapshot = c.execute(
                    "SELECT * FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s AND id=%s",
                    (self._claim.tenant_id, execution["snapshot_id"]),
                ).fetchone()
                recipe = c.execute(
                    "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s",
                    (self._claim.tenant_id, execution["recipe_id"]),
                ).fetchone()
                if snapshot is None or recipe is None:
                    raise ExecutionArtifactError("COMMAND_RECEIPT_UNAVAILABLE")
                prepared = _prepared(execution, snapshot, recipe)
                binding, descriptor = self._command_rows(c, prepared)
                body, proof = bytes(row["body"]), bytes(row["proof"])
                digest = hashlib.sha256(body).hexdigest()
                if (
                    digest != str(row["body_sha256"]).strip()
                    or hashlib.sha256(proof).hexdigest() != str(row["proof_sha256"]).strip()
                ):
                    raise ExecutionArtifactError("COMMAND_RECEIPT_INVALID")
                try:
                    payload = verify_command_receipt(
                        receipt_key, body, proof, binding, descriptor, max_age_seconds=None
                    )
                except ValueError as exc:
                    raise ExecutionArtifactError("COMMAND_RECEIPT_INVALID") from exc
                return payload, digest
        except psycopg.Error as exc:
            raise ExecutionArtifactError("COMMAND_RECEIPT_UNAVAILABLE") from exc
