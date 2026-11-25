"""Test-only, read-only Git UNKNOWN inspection under original Run authority.

This never changes an effect, returns a receipt, or authorizes another CAS.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.rpc import RpcClient
from aiagent_domain import is_uuid7
from aiagent_sandbox.git_worker_rpc import sign_git_recovery_grant

from .context_keys import KeyRing
from .git_patch_artifacts import fetch_git_patch


class GitRecoveryUnavailable(RuntimeError):
    """Evidence cannot be inspected safely; UNKNOWN must be retained."""


@dataclass(frozen=True)
class GitRecoveryObservation:
    operation_id: str
    attempt_id: str
    commit: str
    tree: str
    journal_sha256: str


def _scope(db: Database, claim: Claim, workspace: str, operation: str, deadline: float) -> dict[str, Any]:
    with db.tx(tenant=claim.tenant_id) as c:
        ms = max(1, int((deadline - time.monotonic()) * 1000))
        c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
        c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
        row = c.execute(
            "SELECT * FROM agent.git_recovery_scope(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                claim.tenant_id,
                claim.owner_user_id,
                workspace,
                claim.task_id,
                claim.run_id,
                claim.goal_revision,
                claim.lease_epoch,
                claim.worker_ref,
                claim.restore_incarnation,
                operation,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("recovery scope unavailable")
        return dict(row)


async def inspect_unknown_git_operation(
    db: Database,
    claim: Claim,
    keys: KeyRing,
    operation: str,
    *,
    endpoint: str,
    rpc_key: bytes,
    grant_key: bytes,
    worker_id: str,
    deadline: float,
) -> GitRecoveryObservation:
    """Inspect only the original live Run's UNKNOWN operation; never resolve it."""
    if (
        config.profile() != "test"
        or not is_uuid7(operation)
        or not endpoint.startswith("unix:/")
        or len(rpc_key) < 32
        or len(grant_key) < 32
        or rpc_key == grant_key
        or not worker_id
        or not time.monotonic() < deadline <= time.monotonic() + 10
    ):
        raise GitRecoveryUnavailable("Git recovery inspection unavailable")

    def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        with db.tx(tenant=claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            current = control.check_current(c, claim)
            workspace = str(current["workspace_id"])
        row = _scope(db, claim, workspace, operation, deadline)
        with db.tx(tenant=claim.tenant_id) as c:
            intent = c.execute(
                "SELECT safe_arguments_json FROM agent.tool_intents WHERE tenant_id=%s AND id=%s",
                (claim.tenant_id, row["intent_id"]),
            ).fetchone()
        if intent is None:
            raise ValueError("Git intent unavailable")
        args = intent["safe_arguments_json"]
        patch = fetch_git_patch(
            db,
            claim,
            keys,
            str(row["patch_id"]),
            workspace_id=workspace,
            binding_id=str(row["binding_id"]),
            binding_generation=int(row["binding_generation"]),
            cycle_id=str(row["cycle_id"]),
            plan_ref=str(row["plan_ref"]),
            deadline=deadline,
            recovery_read=True,
        )
        payload = {
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
            "parent": patch.parent,
            "patch_id": patch.artifact_id,
            "patch_sha256": patch.digest,
            "patch_bytes": len(patch.data),
            "paths": list(patch.paths),
            "message": args["message"],
            "cycle_id": row["cycle_id"],
            "plan_ref": row["plan_ref"],
            "request_sha256": row["request_sha256"],
            "expires_at": int(time.time()) + min(5, max(1, int(deadline - time.monotonic()))),
        }
        return row, payload

    try:
        row, payload = await asyncio.to_thread(prepare)
        response = await RpcClient(
            endpoint, "tool_gateway", rpc_key, timeout_s=max(0.1, deadline - time.monotonic())
        ).call("git.recovery_inspect", {"grant": sign_git_recovery_grant(grant_key, payload)})
        if (
            set(response)
            != {
                "operation",
                "worker_id",
                "commit",
                "tree",
                "parent",
                "diff_sha256",
                "commit_object",
                "tree_object",
                "prepared_journal_sha256",
            }
            or response["operation"] != operation
            or response["worker_id"] != worker_id
            or response["commit"] != row["candidate_commit"]
            or response["tree"] != row["candidate_tree"]
            or response["parent"] != row["parent_sha"]
            or response["diff_sha256"] != row["patch_sha256"]
            or response["prepared_journal_sha256"] != row["prepared_journal_sha256"]
        ):
            raise ValueError("Git recovery evidence mismatch")
        commit = base64.b64decode(response["commit_object"], validate=True)
        tree = base64.b64decode(response["tree_object"], validate=True)
        if not 1 <= len(commit) <= 1024 * 1024 or not 1 <= len(tree) <= 1024 * 1024:
            raise ValueError("Git recovery objects too large")

        def oid(kind: bytes, content: bytes) -> str:
            return hashlib.sha1(
                kind + b" " + str(len(content)).encode("ascii") + b"\0" + content, usedforsecurity=False
            ).hexdigest()

        headers = commit.split(b"\n\n", 1)[0].splitlines()
        if (
            oid(b"commit", commit) != row["candidate_commit"]
            or oid(b"tree", tree) != row["candidate_tree"]
            or [line[5:] for line in headers if line.startswith(b"tree ")]
            != [str(row["candidate_tree"]).encode("ascii")]
            or [line[7:] for line in headers if line.startswith(b"parent ")] != [str(row["parent_sha"]).encode("ascii")]
        ):
            raise ValueError("Git recovery object mismatch")
        again = await asyncio.to_thread(_scope, db, claim, payload["workspace"], operation, deadline)
        if (
            any(
                again[k] != row[k]
                for k in (
                    "id",
                    "intent_id",
                    "attempt_id",
                    "binding_id",
                    "binding_generation",
                    "patch_id",
                    "request_sha256",
                    "candidate_commit",
                    "candidate_tree",
                    "prepared_journal_sha256",
                    "state",
                )
            )
            or time.monotonic() >= deadline
        ):
            raise ValueError("Git recovery scope changed")
        return GitRecoveryObservation(
            operation,
            str(row["attempt_id"]),
            str(row["candidate_commit"]),
            str(row["candidate_tree"]),
            str(row["prepared_journal_sha256"]),
        )
    except Exception as exc:
        raise GitRecoveryUnavailable("Git recovery inspection unavailable") from exc
