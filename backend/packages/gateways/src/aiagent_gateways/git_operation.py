"""Reserve a Git operation after Gateway final admission, before worker dispatch.

Reservation alone never starts a worker or proves a Git effect. A duplicate
reservation is ambiguous and must be reconciled without another Git execution.
"""

from __future__ import annotations

import time

from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_domain import is_uuid7, uuid7
from psycopg import DatabaseError, errors

from .executors import ExecutorFailed, ExecutorUnknown


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
