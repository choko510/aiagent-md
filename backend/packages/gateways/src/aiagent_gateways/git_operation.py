"""Reserve a Git operation after Gateway final admission, before worker dispatch.

Reservation alone never starts a worker or proves a Git effect. A duplicate
reservation is ambiguous and must be reconciled without another Git execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from aiagent_core import control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.workspace_authority import resolve_git_commit_binding
from aiagent_domain import is_uuid7, uuid7
from psycopg import DatabaseError, errors

from .context_keys import KeyRing
from .executors import ExecutorFailed, ExecutorUnknown
from .git_authority_watch import _check_git_operation
from .git_patch_artifacts import VerifiedGitPatch, fetch_git_patch


@dataclass(frozen=True)
class PreparedGitWorkerRequest:
    """Canonical request projection; it is not a worker grant or CAS authority."""

    payload: dict[str, Any]
    patch: VerifiedGitPatch


def prepare_git_worker_request(
    db: Database,
    claim: Claim,
    keys: KeyRing,
    operation_id: str,
    *,
    deadline: float,
) -> PreparedGitWorkerRequest:
    """Rehydrate a SENDING operation and encrypted patch from canonical DB rows."""
    if not is_uuid7(operation_id) or not time.monotonic() < deadline <= time.monotonic() + 60:
        raise ExecutorFailed("invalid Git operation")
    try:
        with db.tx(tenant=claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
            workspace = str(control.check_current(c, claim)["workspace_id"])
            row = c.execute(
                "SELECT o.*,i.safe_arguments_json,b.repository_ref FROM agent.git_operations o "
                "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                "JOIN agent.repository_bindings b ON b.tenant_id=o.tenant_id AND b.id=o.binding_id "
                "WHERE o.tenant_id=%s AND o.id=%s AND o.state='ADMITTED'",
                (claim.tenant_id, operation_id),
            ).fetchone()
        if row is None:
            raise ValueError("operation unavailable")
        expiry = _check_git_operation(db, claim, workspace, operation_id)
        binding = resolve_git_commit_binding(
            db,
            claim,
            str(row["binding_id"]),
            workspace_id=workspace,
            repository=str(row["repository_ref"]),
            generation=int(row["binding_generation"]),
            parent=str(row["parent_sha"]),
            deadline=deadline,
        )
        patch = fetch_git_patch(
            db,
            claim,
            keys,
            str(row["patch_id"]),
            workspace_id=workspace,
            binding_id=binding.id,
            binding_generation=binding.generation,
            cycle_id=str(row["cycle_id"]),
            plan_ref=str(row["plan_ref"]),
            deadline=deadline,
        )
        args = row["safe_arguments_json"]
        if (
            binding.branch != row["branch"]
            or patch.parent != row["parent_sha"]
            or patch.digest != row["patch_sha256"]
            or args.get("paths") != list(patch.paths)
            or not isinstance(args.get("message"), str)
            or not args["message"].strip()
        ):
            raise ValueError("operation scope changed")
        expires = int(min(time.time() + 30, expiry - 1))
        if expires <= time.time() + 1 or time.monotonic() >= deadline:
            raise ValueError("operation authority expired")
        payload = {
            "operation": operation_id,
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
            "binding": binding.id,
            "generation": binding.generation,
            "branch": binding.branch,
            "parent": patch.parent,
            "patch_id": patch.artifact_id,
            "patch_sha256": patch.digest,
            "patch_bytes": len(patch.data),
            "paths": list(patch.paths),
            "message": args["message"],
            "cycle_id": row["cycle_id"],
            "plan_ref": row["plan_ref"],
            "request_sha256": row["request_sha256"],
            "expires_at": expires,
        }
        return PreparedGitWorkerRequest(payload, patch)
    except Exception as exc:
        raise ExecutorUnknown("Git operation preparation requires reconciliation") from exc


def admit_git_operation(
    db: Database,
    claim: Claim,
    *,
    workspace_id: str,
    attempt_id: str,
    binding_id: str,
    patch_id: str,
    deadline: float,
) -> str:
    """Atomically reserve once against the existing approved intent and attempt."""
    now = time.monotonic()
    if not now < deadline <= now + 60 or not all(
        is_uuid7(value) for value in (workspace_id, attempt_id, binding_id, patch_id)
    ):
        raise ExecutorFailed("invalid Git operation request")
    operation_id = uuid7()
    try:
        with db.tx(tenant=claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
            row = c.execute(
                "SELECT agent.git_operation_admit(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS operation_id",
                (
                    claim.tenant_id,
                    claim.owner_user_id,
                    workspace_id,
                    claim.task_id,
                    claim.run_id,
                    claim.goal_revision,
                    claim.lease_epoch,
                    claim.worker_ref,
                    claim.restore_incarnation,
                    attempt_id,
                    binding_id,
                    patch_id,
                    operation_id,
                ),
            ).fetchone()
            if row is None or str(row["operation_id"]) != operation_id:
                raise ExecutorUnknown("Git operation reservation unavailable")
    except errors.UniqueViolation as exc:
        raise ExecutorUnknown("Git operation already exists; reconcile") from exc
    except errors.InsufficientPrivilege as exc:
        raise ExecutorFailed("Git operation admission denied") from exc
    except DatabaseError as exc:
        # Transaction outcome can be unknown if the database connection failed.
        raise ExecutorUnknown("Git operation reservation requires reconciliation") from exc
    return operation_id
