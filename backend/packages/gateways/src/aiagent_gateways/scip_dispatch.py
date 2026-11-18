"""Scoped SCIP dispatch over the existing Source and Artifact broker path.

This internal orchestrator still requires trusted configuration, a real
Supervisor endpoint and a caller-created canonical Tool Attempt. It does not
provide VM/image attestation or register itself with ToolGateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_development.code_intelligence.git_proof import GitTreeProof
from aiagent_development.code_intelligence.projection import ProjectionManifest
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt
from aiagent_development.workspace_authority import PreparedExecution, _prepared, prepare_execution
from aiagent_domain import is_uuid7
from aiagent_sandbox.scip_authority import issue_index_grant

from .execution_artifacts import ExecutionArtifactRef, PostgresExecutionArtifactStore
from .execution_lifecycle import ExecutionLifecycle
from .execution_watch import pulse_source_authority
from .executors import ExecutorFailed, ExecutorUnknown
from .source_transfer import _complete_db_call, cancel_issued_source, capture_finish_publish, register_issued_source


class ScipDispatchUnknown(RuntimeError):
    """An execution or publication outcome is uncertain and must be reconciled."""


class AuthorizedScipExecutor:
    """Tool Gateway executor configured only by a trusted workspace owner.

    The input snapshot, approved recipe, source producer and keys are supplied
    by trusted configuration, never by model-controlled tool arguments. This
    executor is opt-in and is not installed by the default environment loader.
    """

    def __init__(
        self,
        db: Database,
        claim: control.Claim,
        snapshot_id: str,
        recipe_id: str,
        lifecycle: ExecutionLifecycle,
        store_factory: Callable[[PreparedExecution], PostgresExecutionArtifactStore],
        manifest: ProjectionManifest,
        receipt: ProjectionStreamReceipt,
        proof: GitTreeProof,
        chunks: Iterable[bytes],
        endpoint: str,
        rpc_key: bytes,
        ticket_key: bytes,
        grant_key: bytes,
        private_parent: Path,
        *,
        timeout_seconds: float = 40.0,
    ) -> None:
        if not 0 < timeout_seconds <= 40:
            raise ValueError("invalid index timeout")
        self._db, self._claim, self._snapshot_id, self._recipe_id = db, claim, snapshot_id, recipe_id
        self._lifecycle, self._store_factory = lifecycle, store_factory
        self._manifest, self._receipt, self._proof, self._chunks = manifest, receipt, proof, chunks
        self._endpoint, self._rpc_key, self._ticket_key, self._grant_key = endpoint, rpc_key, ticket_key, grant_key
        self._private_parent, self._timeout = private_parent, timeout_seconds
        self.context: dict[str, Any] = {}

    def destination(self) -> None:
        return None

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        ctx, claim = self.context.get(effect_key), self._claim
        if not isinstance(ctx, dict) or any(
            ctx.get(k) != v
            for k, v in {
                "tenant_id": claim.tenant_id,
                "owner_user_id": claim.owner_user_id,
                "task_id": claim.task_id,
                "run_id": claim.run_id,
                "goal_revision": claim.goal_revision,
                "lease_epoch": claim.lease_epoch,
                "worker_ref": claim.worker_ref,
            }.items()
        ):
            raise ExecutorFailed("index admission scope mismatch")
        attempt = ctx.get("attempt_id")
        if not isinstance(attempt, str) or not attempt:
            raise ExecutorFailed("index attempt missing")
        deadline = time.monotonic() + self._timeout
        try:
            prepared = cast(
                PreparedExecution,
                await _complete_db_call(
                    prepare_execution, self._db, claim, attempt, self._snapshot_id, self._recipe_id, deadline=deadline
                ),
            )
            if prepared.intent_id != ctx.get("intent_id"):
                raise ExecutorUnknown("index intent mismatch")
            artifact = await dispatch_scip_index(
                self._lifecycle,
                self._store_factory(prepared),
                prepared,
                self._manifest,
                self._receipt,
                self._proof,
                self._chunks,
                self._endpoint,
                self._rpc_key,
                self._ticket_key,
                self._grant_key,
                self._private_parent,
                deadline=deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ExecutorUnknown("index execution outcome unavailable") from exc
        return {
            "toolAttemptId": attempt,
            "executionId": prepared.id,
            "artifactId": artifact.id,
            "artifactSha256": artifact.sha256,
            "artifactBytes": artifact.byte_length,
            "coverage": artifact.coverage,
        }

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        """Reconcile only a canonical published Artifact; fence everything else UNKNOWN."""
        tenant, separator, intent = effect_key.partition(":")
        claim = self._claim
        if separator != ":" or tenant != claim.tenant_id or not is_uuid7(intent):
            return None

        def reconcile() -> dict[str, Any] | None:
            with self._db.tx(tenant=claim.tenant_id) as c:
                c.execute("SELECT set_config('statement_timeout', '5000ms', true)")
                c.execute("SELECT set_config('lock_timeout', '5000ms', true)")
                row = c.execute(
                    "SELECT * FROM agent.reconcile_scip_execution(%s,%s,%s,%s,%s)",
                    (claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id, intent),
                ).fetchone()
                if row is None or row["result_state"] != "PUBLISHED" or row["artifact_id"] is None:
                    return None
                artifact = c.execute(
                    "SELECT * FROM agent.reconcile_scip_artifact(%s,%s,%s,%s,%s,%s)",
                    (claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id, intent, row["artifact_id"]),
                ).fetchone()
                if artifact is None:
                    return None
                execution = c.execute(
                    "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, artifact["execution_id"]),
                ).fetchone()
                if execution is None:
                    return None
                snapshot = c.execute(
                    "SELECT * FROM agent.workspace_snapshot_attestations WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, execution["snapshot_id"]),
                ).fetchone()
                recipe = c.execute(
                    "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, execution["recipe_id"]),
                ).fetchone()
                if snapshot is None or recipe is None:
                    return None
                prepared = _prepared(execution, snapshot, recipe)
            self._store_factory(prepared).verify_reconciled(artifact, deadline=time.monotonic() + 5)
            return {"sent": True, "receipt": "scip-artifact:" + str(artifact["artifact_id"])}

        try:
            return await asyncio.to_thread(reconcile)
        except Exception:
            return None


async def dispatch_scip_index(
    lifecycle: ExecutionLifecycle,
    store: PostgresExecutionArtifactStore,
    prepared: PreparedExecution,
    manifest: ProjectionManifest,
    receipt: ProjectionStreamReceipt,
    proof: GitTreeProof,
    chunks: Iterable[bytes],
    endpoint: str,
    rpc_key: bytes,
    ticket_key: bytes,
    grant_key: bytes,
    private_parent: Path,
    *,
    deadline: float,
) -> ExecutionArtifactRef:
    """Issue, register, heartbeat, execute, authenticate, finish and publish once."""
    ticket: dict[str, Any] | None = None
    pulse: asyncio.Task[None] | None = None
    stop, revoked, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    succeeded = False
    try:
        ticket = cast(
            dict[str, Any],
            await _complete_db_call(
                lifecycle.issue_source_session, prepared, manifest, receipt, ticket_key, deadline=deadline
            ),
        )
        binding = cast(
            dict[str, Any], await _complete_db_call(lifecycle.running_index_binding, prepared, deadline=deadline)
        )
        grant = issue_index_grant(ticket_key, grant_key, ticket, binding)
        session_id, transfer_key, source_binding = await register_issued_source(
            lifecycle,
            prepared,
            endpoint,
            rpc_key,
            ticket_key,
            ticket,
            manifest,
            proof,
            deadline=deadline,
            execution_binding=binding,
            grant=grant,
        )
        pulse = asyncio.create_task(
            pulse_source_authority(
                lifecycle,
                endpoint,
                rpc_key,
                revoked,
                stop,
                ready,
                tenant_id=ticket["payload"]["tenant"],
                session_id=session_id,
                execution_id=prepared.id,
                attempt_id=prepared.attempt_id,
                binding_sha256=prepared.binding_sha256,
            )
        )
        wait_ready, wait_revoked = asyncio.create_task(ready.wait()), asyncio.create_task(revoked.wait())
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("source authority expired")
            await asyncio.wait_for(
                asyncio.wait({wait_ready, wait_revoked}, return_when=asyncio.FIRST_COMPLETED), remaining
            )
            if not ready.is_set() or revoked.is_set():
                raise RuntimeError("source authority unavailable")
        finally:
            wait_ready.cancel()
            wait_revoked.cancel()
            await asyncio.gather(wait_ready, wait_revoked, return_exceptions=True)

        async def after_transfer() -> None:
            stop.set()
            assert pulse is not None
            await pulse
            if revoked.is_set():
                raise RuntimeError("source authority revoked")

        result = await capture_finish_publish(
            lifecycle,
            store,
            prepared,
            endpoint,
            rpc_key,
            ticket_key,
            ticket,
            session_id,
            transfer_key,
            source_binding,
            chunks,
            private_parent,
            deadline=deadline,
            active=lambda: not revoked.is_set(),
            after_transfer=after_transfer,
        )
        succeeded = True
        return result
    except (Exception, asyncio.CancelledError) as exc:
        revoked.set()
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    lifecycle.mark_unknown, prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 5
                )
            )
        except Exception as mark_exc:
            raise ScipDispatchUnknown("execution outcome requires reconciliation") from mark_exc
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ScipDispatchUnknown("execution outcome unknown") from exc
    finally:
        stop.set()
        if pulse is not None:
            await asyncio.gather(pulse, return_exceptions=True)
        if not succeeded and ticket is not None:
            with contextlib.suppress(Exception):
                await cancel_issued_source(
                    lifecycle, prepared, endpoint, rpc_key, ticket_key, ticket, deadline=time.monotonic() + 2
                )
