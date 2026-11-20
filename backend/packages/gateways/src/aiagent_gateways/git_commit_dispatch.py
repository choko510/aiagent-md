"""Opt-in Development Git admission; execution remains fail closed.

The authenticated isolated Git worker and durable Git receipt are not yet
connected. A reserved operation therefore always remains UNKNOWN.
"""

from __future__ import annotations

import time
from typing import Any

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.workspace_authority import resolve_git_commit_binding
from aiagent_domain import is_uuid7

from .context_keys import KeyRing
from .executors import ExecutorFailed, ExecutorUnknown
from .git_patch_artifacts import fetch_git_patch


class GitCommitReservationExecutor:
    """Bind the owner's approved inputs; do not execute without an isolated worker."""

    def __init__(self, db: Database, claim: Claim, keys: KeyRing) -> None:
        if config.profile() == "production":
            raise RuntimeError("Git commit isolation not verified")
        self._db, self._claim, self._keys = db, claim, keys
        self.context: dict[str, Any] = {}

    def destination(self) -> None:
        return None

    def bind_request(self, tool: str, args: dict[str, Any], *, claim: Claim) -> dict[str, Any]:
        if tool != "git.commit" or claim != self._claim or config.profile() == "production":
            raise ExecutorFailed("Git authority unavailable")
        try:
            if not is_uuid7(args["bindingId"]) or not is_uuid7(args["patchId"]):
                raise ValueError("invalid identity")
            with self._db.tx(tenant=claim.tenant_id) as c:
                run = control.check_current(c, claim)
                workspace = str(run["workspace_id"])
                row = c.execute(
                    "SELECT repository_ref FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, args["bindingId"]),
                ).fetchone()
            if row is None:
                raise ValueError("binding unavailable")
            deadline = time.monotonic() + 10
            binding = resolve_git_commit_binding(
                self._db,
                claim,
                args["bindingId"],
                workspace_id=workspace,
                repository=str(row["repository_ref"]),
                generation=args["bindingGeneration"],
                parent=args["parent"],
                deadline=deadline,
            )
            patch = fetch_git_patch(
                self._db,
                claim,
                self._keys,
                args["patchId"],
                workspace_id=workspace,
                binding_id=binding.id,
                binding_generation=binding.generation,
                cycle_id=args["cycleId"],
                plan_ref=args["planRef"],
                deadline=deadline,
            )
            if (
                args["branch"] != binding.branch
                or args["parent"] != patch.parent
                or args["patchSha256"] != patch.digest
                or args["paths"] != list(patch.paths)
            ):
                raise ValueError("Git request does not match authority")
        except Exception as exc:
            raise ExecutorFailed("Git authority unavailable") from exc
        return {
            "method": "COMMIT",
            "action": binding.branch,
            "payloadDigest": patch.digest,
            "binding": binding.id,
            "generation": binding.generation,
            "artifact": patch.artifact_id,
            "parent": patch.parent,
            "paths": list(patch.paths),
            "workspace": workspace,
        }

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        del args, permit
        context = self.context.get(effect_key)
        if not isinstance(context, dict) or not is_uuid7(context.get("operation_id")):
            raise ExecutorFailed("Git operation unavailable")
        raise ExecutorUnknown("isolated Git worker unavailable; reconcile")

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        del effect_key, permit
        return None


class GitCommitStagingExecutor(GitCommitReservationExecutor):
    """Opt-in authenticated staging after real Gateway admission; CAS stays closed."""

    def __init__(
        self,
        db: Database,
        claim: Claim,
        keys: KeyRing,
        *,
        endpoint: str,
        rpc_key: bytes,
        grant_key: bytes,
        transfer_key: bytes,
    ) -> None:
        super().__init__(db, claim, keys)
        if (
            not endpoint.startswith("unix:/")
            or any(len(k) < 32 for k in (rpc_key, grant_key, transfer_key))
            or len({rpc_key, grant_key, transfer_key}) != 3
        ):
            raise ValueError("Git worker staging configuration unavailable")
        self._endpoint, self._rpc_key, self._grant_key, self._transfer_key = endpoint, rpc_key, grant_key, transfer_key

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        from .git_worker_staging import stage_git_worker

        del args, permit
        context = self.context.get(effect_key)
        if not isinstance(context, dict) or not is_uuid7(context.get("operation_id")):
            raise ExecutorFailed("Git operation unavailable")
        if (
            context.get("tenant_id") != self._claim.tenant_id
            or context.get("run_id") != self._claim.run_id
            or context.get("attempt_id") is None
            or effect_key != self._claim.tenant_id + ":" + str(context.get("intent_id"))
        ):
            raise ExecutorFailed("Git operation scope mismatch")
        try:
            staged = await stage_git_worker(
                self._db,
                self._claim,
                self._keys,
                context["operation_id"],
                endpoint=self._endpoint,
                rpc_key=self._rpc_key,
                grant_key=self._grant_key,
                transfer_key=self._transfer_key,
                deadline=time.monotonic() + 20,
            )
            # This path intentionally cannot call git.execute: the cross-process
            # CAS/revocation fence has not yet been proven.
            await staged.cancel()
        except Exception as exc:
            raise ExecutorUnknown("Git staging requires reconciliation") from exc
        raise ExecutorUnknown("Git CAS fence unavailable; reconcile")


class GitCommitTestExecutor(GitCommitStagingExecutor):
    """Real Git execution for test profile only; no production fence is claimed."""

    def __init__(self, *args: Any, result_key: bytes, worker_id: str, **kwargs: Any) -> None:
        if config.profile() != "test":
            raise RuntimeError("unfenced Git execution is test-only")
        super().__init__(*args, **kwargs)
        if len(result_key) < 32 or result_key in (self._rpc_key, self._grant_key, self._transfer_key) or not worker_id:
            raise ValueError("Git worker verification unavailable")
        self._result_key, self._worker_id = result_key, worker_id

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        import asyncio

        from aiagent_core.rpc import RpcClient

        from .git_object_verifier import RpcGitObjectVerifier
        from .git_receipts import GitReceiptStore
        from .git_worker_staging import stage_git_worker

        del args, permit
        if config.profile() != "test":
            raise ExecutorFailed("unfenced Git execution is test-only")
        context = self.context.get(effect_key)
        if (
            not isinstance(context, dict)
            or not is_uuid7(context.get("operation_id"))
            or context.get("tenant_id") != self._claim.tenant_id
            or context.get("run_id") != self._claim.run_id
            or not is_uuid7(context.get("attempt_id"))
            or effect_key != self._claim.tenant_id + ":" + str(context.get("intent_id"))
        ):
            raise ExecutorFailed("Git operation scope mismatch")
        staged = None
        deadline = time.monotonic() + 25
        try:
            staged = await stage_git_worker(
                self._db,
                self._claim,
                self._keys,
                context["operation_id"],
                endpoint=self._endpoint,
                rpc_key=self._rpc_key,
                grant_key=self._grant_key,
                transfer_key=self._transfer_key,
                deadline=deadline,
            )
            response = await RpcClient(
                self._endpoint, "tool_gateway", self._rpc_key, timeout_s=max(0.1, deadline - time.monotonic())
            ).call("git.execute", {"operation": context["operation_id"], "end": staged.terminator})
            if staged.revoked.is_set():
                raise ValueError("Git authority revoked")
            staged.stop.set()
            await asyncio.gather(staged.pulse)
            verifier = RpcGitObjectVerifier(self._endpoint, self._rpc_key, self._grant_key, self._worker_id)
            receipts = GitReceiptStore(
                self._db,
                self._claim,
                self._keys,
                result_key=self._result_key,
                worker_id=self._worker_id,
                object_verifier=verifier,
            )
            receipt_id = await asyncio.to_thread(receipts.finish, context["operation_id"], response, deadline=deadline)
            payload, digest = await asyncio.to_thread(
                receipts.fetch, context["operation_id"], receipt_id, deadline=deadline
            )
            return {
                "receipt": "git:" + receipt_id,
                "operationId": context["operation_id"],
                "toolAttemptId": context["attempt_id"],
                "receiptId": receipt_id,
                "bodySha256": digest,
                "commitSha": payload["commit"],
            }
        except Exception as exc:
            raise ExecutorUnknown("Git result requires reconciliation") from exc
        finally:
            if staged is not None:
                await staged.cancel()
