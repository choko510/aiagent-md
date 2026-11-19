"""Opt-in scoped command dispatch; production stays disabled pending attestation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from aiagent_core import config, control
from aiagent_core.db import Database
from aiagent_development.code_intelligence.git_proof import GitTreeProof
from aiagent_development.code_intelligence.projection import ProjectionManifest
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt
from aiagent_development.workspace_authority import PreparedExecution, prepare_execution
from aiagent_sandbox.command_authority import issue_command_grant

from .execution_lifecycle import ExecutionLifecycle
from .execution_watch import pulse_source_authority
from .executors import ExecutorFailed, ExecutorUnknown
from .source_transfer import _complete_db_call, cancel_issued_source, register_issued_source, send_source_for_capture


class AuthorizedCommandExecutor:
    """Trusted, per-Run executor; never selected by model-controlled arguments."""

    def __init__(
        self,
        db: Database,
        claim: control.Claim,
        snapshot_id: str,
        recipe_id: str,
        lifecycle: ExecutionLifecycle,
        manifest: ProjectionManifest,
        receipt: ProjectionStreamReceipt,
        proof: GitTreeProof,
        chunks: Iterable[bytes],
        endpoint: str,
        rpc_key: bytes,
        ticket_key: bytes,
        grant_key: bytes,
        receipt_key: bytes,
        private_parent: Path,
        *,
        timeout_seconds: float = 40.0,
    ) -> None:
        if config.profile() == "production":
            raise RuntimeError("command execution requires production toolchain attestation")
        if not 0 < timeout_seconds <= 40 or len(receipt_key) < 32:
            raise ValueError("invalid command executor configuration")
        self._db, self._claim, self._snapshot_id, self._recipe_id = db, claim, snapshot_id, recipe_id
        self._lifecycle, self._manifest, self._receipt, self._proof, self._chunks = (
            lifecycle,
            manifest,
            receipt,
            proof,
            chunks,
        )
        self._endpoint, self._rpc_key, self._ticket_key, self._grant_key = endpoint, rpc_key, ticket_key, grant_key
        self._receipt_key, self._private_parent, self._timeout = receipt_key, private_parent, timeout_seconds
        self.context: dict[str, Any] = {}

    def destination(self) -> None:
        return None

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        ctx, claim = self.context.get(effect_key), self._claim
        expected = {
            "tenant_id": claim.tenant_id,
            "owner_user_id": claim.owner_user_id,
            "task_id": claim.task_id,
            "run_id": claim.run_id,
            "goal_revision": claim.goal_revision,
            "lease_epoch": claim.lease_epoch,
            "worker_ref": claim.worker_ref,
        }
        if not isinstance(ctx, dict) or any(ctx.get(k) != v for k, v in expected.items()):
            raise ExecutorFailed("command admission scope mismatch")
        attempt = ctx.get("attempt_id")
        if not isinstance(attempt, str) or not attempt:
            raise ExecutorFailed("command attempt missing")
        deadline = time.monotonic() + self._timeout
        try:
            prepared = cast(
                PreparedExecution,
                await _complete_db_call(
                    prepare_execution, self._db, claim, attempt, self._snapshot_id, self._recipe_id, deadline=deadline
                ),
            )
            if prepared.intent_id != ctx.get("intent_id"):
                raise ExecutorUnknown("command intent mismatch")
            receipt_id, payload, digest = await dispatch_command(
                self._lifecycle,
                prepared,
                self._manifest,
                self._receipt,
                self._proof,
                self._chunks,
                self._endpoint,
                self._rpc_key,
                self._ticket_key,
                self._grant_key,
                self._receipt_key,
                self._private_parent,
                deadline=deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ExecutorUnknown("command execution outcome unavailable") from exc
        return {
            "toolAttemptId": attempt,
            "executionId": prepared.id,
            "receiptId": receipt_id,
            "bodySha256": digest,
            "kind": payload["kind"],
            "testId": payload["test_id"],
        }

    async def query(self, effect_key: str, permit: str | None = None) -> None:
        # Positive command reconciliation requires a separate durable, scoped
        # receipt lookup contract. Never re-execute or infer a result here.
        return None


async def dispatch_command(
    lifecycle: ExecutionLifecycle,
    prepared: PreparedExecution,
    manifest: ProjectionManifest,
    receipt: ProjectionStreamReceipt,
    proof: GitTreeProof,
    chunks: Iterable[bytes],
    endpoint: str,
    rpc_key: bytes,
    ticket_key: bytes,
    grant_key: bytes,
    receipt_key: bytes,
    private_parent: Path,
    *,
    deadline: float,
) -> tuple[str, dict[str, object], str]:
    """Execute once and persist only an authenticated, output-redacted receipt."""
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
        binding, descriptor = await _complete_db_call(lifecycle.running_command_binding, prepared, deadline=deadline)
        grant = issue_command_grant(ticket_key, grant_key, ticket, binding, descriptor)
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
            command_descriptor=descriptor,
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
                raise TimeoutError("command authority expired")
            await asyncio.wait_for(
                asyncio.wait({wait_ready, wait_revoked}, return_when=asyncio.FIRST_COMPLETED), remaining
            )
            if not ready.is_set() or revoked.is_set():
                raise RuntimeError("command authority unavailable")
        finally:
            wait_ready.cancel()
            wait_revoked.cancel()
            await asyncio.gather(wait_ready, wait_revoked, return_exceptions=True)
        async with send_source_for_capture(
            endpoint,
            rpc_key,
            session_id,
            transfer_key,
            source_binding,
            chunks,
            private_parent,
            deadline=deadline,
            active=lambda: not revoked.is_set(),
        ) as (fd, signature):
            stop.set()
            assert pulse is not None
            await pulse
            if revoked.is_set():
                raise RuntimeError("command authority revoked")
            result = await _complete_db_call(
                lifecycle.finish_command, prepared, fd, signature, receipt_key, deadline=deadline
            )
        succeeded = True
        return cast(tuple[str, dict[str, object], str], result)
    except (Exception, asyncio.CancelledError) as exc:
        revoked.set()
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    lifecycle.mark_unknown, prepared.id, prepared.binding_sha256, deadline=time.monotonic() + 5
                )
            )
        except Exception as mark_exc:
            raise ExecutorUnknown("command outcome requires reconciliation") from mark_exc
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ExecutorUnknown("command execution outcome unknown") from exc
    finally:
        stop.set()
        if pulse is not None:
            await asyncio.gather(pulse, return_exceptions=True)
        if not succeeded and ticket is not None:
            with contextlib.suppress(Exception):
                await cancel_issued_source(
                    lifecycle, prepared, endpoint, rpc_key, ticket_key, ticket, deadline=time.monotonic() + 2
                )
