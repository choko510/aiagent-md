"""Trusted Gateway transitions for a prepared execution and authenticated capture.

These operations do not dispatch a Cell or transfer bytes. A caller must use the
same scoped execution, authenticated transport, and live authority watchdog.
"""

from __future__ import annotations

import hashlib

import psycopg
from aiagent_core import control
from aiagent_core.db import Database
from aiagent_domain import require_uuid7

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
