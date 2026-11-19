"""Persist only an authenticated Git worker result under canonical authority.

No production object verifier or worker provisioning is supplied here.  The
injected verifier must independently verify the Git objects and approved diff;
without it a receipt cannot be stored or returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Protocol

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_domain import canonical_json, is_uuid7, uuid7
from aiagent_sandbox.git_worker_rpc import verify_git_worker_result

from .context_keys import KeyRing
from .git_patch_artifacts import VerifiedGitPatch, fetch_git_patch


class GitReceiptError(RuntimeError):
    """Receipt unavailable, invalid, revoked, or outcome requires reconciliation."""


class GitObjectVerifier(Protocol):
    def verify(self, result: dict[str, Any], patch: VerifiedGitPatch, *, deadline: float) -> None:
        """Verify authenticated Git commit/tree/parent and exact approved diff in isolation."""


class GitReceiptStore:
    def __init__(
        self,
        db: Database,
        claim: Claim,
        keys: KeyRing,
        *,
        result_key: bytes,
        worker_id: str,
        object_verifier: GitObjectVerifier,
    ) -> None:
        if config.profile() == "production" or len(result_key) < 32 or not worker_id or object_verifier is None:
            raise ValueError("Git receipt verification unavailable")
        self._db, self._claim, self._keys = db, claim, keys
        self._result_key, self._worker_id, self._objects = result_key, worker_id, object_verifier

    def _scope(self, workspace: str, operation: str) -> tuple[object, ...]:
        c = self._claim
        return (
            c.tenant_id,
            c.owner_user_id,
            workspace,
            c.task_id,
            c.run_id,
            c.goal_revision,
            c.lease_epoch,
            c.worker_ref,
            c.restore_incarnation,
            operation,
        )

    def _verify(
        self, operation: str, envelope: dict[str, Any], *, deadline: float, fresh: bool
    ) -> tuple[str, bytes, bytes]:
        claim = self._claim
        if not is_uuid7(operation) or not time.monotonic() < deadline <= time.monotonic() + 60:
            raise GitReceiptError("Git receipt unavailable")
        try:
            result, proof = envelope["payload"], envelope["mac"]
            if not isinstance(result, dict) or not isinstance(proof, str):
                raise ValueError
            expected = {k: v for k, v in result.items() if k not in {"commit", "tree", "cas", "worker_id"}}
            verified = verify_git_worker_result(self._result_key, envelope, expected, self._worker_id)
            if fresh and (type(verified["expires_at"]) is not int or time.time() >= verified["expires_at"]):
                raise ValueError
            with self._db.tx(tenant=claim.tenant_id) as c:
                run = control.check_current(c, claim)
                workspace = str(run["workspace_id"])
                row = c.execute(
                    "SELECT o.*,i.safe_arguments_json,b.repository_ref FROM agent.git_operations o "
                    "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                    "JOIN agent.repository_bindings b ON b.tenant_id=o.tenant_id AND b.id=o.binding_id "
                    "WHERE o.tenant_id=%s AND o.id=%s",
                    (claim.tenant_id, operation),
                ).fetchone()
            if row is None:
                raise ValueError
            args = row["safe_arguments_json"]
            canonical = {
                "operation": operation,
                "tenant": claim.tenant_id,
                "owner": claim.owner_user_id,
                "task": claim.task_id,
                "run": claim.run_id,
                "workspace": workspace,
                "goal": claim.goal_revision,
                "lease": claim.lease_epoch,
                "restore": claim.restore_incarnation,
                "worker_ref": claim.worker_ref,
                "intent": str(row["intent_id"]),
                "attempt": str(row["attempt_id"]),
                "effect_key": row["effect_key"],
                "binding": str(row["binding_id"]),
                "generation": int(row["binding_generation"]),
                "branch": row["branch"],
                "parent": row["parent_sha"],
                "patch_id": str(row["patch_id"]),
                "patch_sha256": row["patch_sha256"],
                "cycle_id": row["cycle_id"],
                "plan_ref": row["plan_ref"],
                "request_sha256": row["request_sha256"],
                "message": args["message"],
                "paths": args["paths"],
            }
            if any(verified.get(k) != v for k, v in canonical.items()):
                raise ValueError
            patch = fetch_git_patch(
                self._db,
                claim,
                self._keys,
                str(row["patch_id"]),
                workspace_id=workspace,
                binding_id=str(row["binding_id"]),
                binding_generation=int(row["binding_generation"]),
                cycle_id=str(row["cycle_id"]),
                plan_ref=str(row["plan_ref"]),
                deadline=deadline,
            )
            if verified["patch_bytes"] != len(patch.data) or verified["paths"] != list(patch.paths):
                raise ValueError
            self._objects.verify(verified, patch, deadline=deadline)
            body = canonical_json(verified).encode("utf-8")
            return workspace, body, proof.encode("ascii")
        except Exception as exc:
            raise GitReceiptError("Git receipt verification failed") from exc

    def finish(self, operation: str, envelope: dict[str, Any], *, deadline: float) -> str:
        workspace, body, proof = self._verify(operation, envelope, deadline=deadline, fresh=True)
        receipt_id = uuid7()
        try:
            if time.monotonic() >= deadline:
                raise ValueError
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                ms = max(1, int((deadline - time.monotonic()) * 1000))
                c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
                c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
                row = c.execute(
                    "SELECT agent.git_operation_finish(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS receipt_id",
                    (
                        *self._scope(workspace, operation),
                        receipt_id,
                        body,
                        proof,
                        hashlib.sha256(body).hexdigest(),
                        hashlib.sha256(proof).hexdigest(),
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError
                return str(row["receipt_id"])
        except Exception as exc:
            raise GitReceiptError("Git receipt persistence requires reconciliation") from exc

    def fetch(self, operation: str, receipt_id: str, *, deadline: float) -> tuple[dict[str, Any], str]:
        if not is_uuid7(receipt_id) or not time.monotonic() < deadline <= time.monotonic() + 60:
            raise GitReceiptError("Git receipt unavailable")
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                ms = max(1, int((deadline - time.monotonic()) * 1000))
                c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
                c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
                workspace = str(control.check_current(c, self._claim)["workspace_id"])
                row = c.execute(
                    "SELECT * FROM agent.git_operation_fetch(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    self._scope(workspace, operation),
                ).fetchone()
                if row is None or str(row["receipt_id"]) != receipt_id:
                    raise ValueError
                body, proof = bytes(row["body"]), bytes(row["proof"])
                if not hmac.compare_digest(
                    hashlib.sha256(body).hexdigest(), str(row["body_sha256"])
                ) or not hmac.compare_digest(hashlib.sha256(proof).hexdigest(), str(row["proof_sha256"])):
                    raise ValueError
            result = json.loads(body)
            envelope = {"payload": result, "mac": proof.decode("ascii")}
            _, verified_body, verified_proof = self._verify(operation, envelope, deadline=deadline, fresh=False)
            if (
                time.monotonic() >= deadline
                or not hmac.compare_digest(body, verified_body)
                or not hmac.compare_digest(proof, verified_proof)
            ):
                raise ValueError
            return result, hashlib.sha256(body).hexdigest()
        except Exception as exc:
            raise GitReceiptError("Git receipt unavailable or invalid") from exc
