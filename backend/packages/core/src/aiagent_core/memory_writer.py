"""Task-scoped Memory writer. Authenticated Scope must be supplied by trusted API code.

No HTTP or agent Tool is exposed. Text remains in separately encrypted artifacts;
only references, consent, expiry and revision are stored here.
"""

from __future__ import annotations

import re
from datetime import datetime

import psycopg
from aiagent_domain import uuid7
from aiagent_domain.ids import is_uuid7

from . import control
from .commands import CommandError, Scope, _idem_begin, _idem_end
from .db import Database, Row

_RECIPIENT = re.compile(r"\A[a-zA-Z0-9_.:-]{1,256}\Z")


def _bind(c: psycopg.Connection[Row], tenant: str, owner: str, task: str) -> None:
    c.execute(
        "SELECT set_config('aiagent.memory_tenant',%s,true), set_config('aiagent.memory_owner',%s,true),"
        " set_config('aiagent.memory_task',%s,true)",
        (tenant, owner, task),
    )


def _owner_scope(c: psycopg.Connection[Row], scope: Scope, task_id: str, *, allow_revocation: bool = False) -> None:
    restore = c.execute("SELECT restore_incarnation, dispatch_paused FROM agent.schema_control").fetchone()
    if restore is None or (
        not allow_revocation and (restore["restore_incarnation"] != 1 or restore["dispatch_paused"])
    ):
        # There is no external tombstone high-watermark yet. Do not resurrect
        # Memory after a restore until an explicit reconciliation exists.
        raise CommandError("MEMORY_RESTORE_RECONCILIATION_REQUIRED", 409)
    if not is_uuid7(task_id) or not control.scope_active(c, scope.tenant_id, scope.user_id, scope.workspace_id):
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    task = c.execute(
        "SELECT id FROM agent.tasks WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND workspace_id=%s",
        (scope.tenant_id, task_id, scope.user_id, scope.workspace_id),
    ).fetchone()
    if task is None:
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    _bind(c, scope.tenant_id, scope.user_id, task_id)


def _lock_epoch(c: psycopg.Connection[Row], scope: Scope, task_id: str) -> int:
    c.execute(
        "INSERT INTO agent.memory_scope_epochs(tenant_id,owner_user_id,task_id) VALUES (%s,%s,%s)"
        " ON CONFLICT DO NOTHING",
        (scope.tenant_id, scope.user_id, task_id),
    )
    row = c.execute(
        "SELECT source_epoch FROM agent.memory_scope_epochs WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s FOR UPDATE",
        (scope.tenant_id, scope.user_id, task_id),
    ).fetchone()
    if row is None:
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    return int(row["source_epoch"])


def _epoch(c: psycopg.Connection[Row], scope: Scope, task_id: str) -> int:
    row = c.execute(
        "SELECT source_epoch FROM agent.memory_scope_epochs WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s",
        (scope.tenant_id, scope.user_id, task_id),
    ).fetchone()
    assert row is not None
    return int(row["source_epoch"])


def create_memory(
    db: Database,
    scope: Scope,
    *,
    task_id: str,
    source_ref: str,
    projection_ref: str,
    recipient: str,
    observed_at: datetime,
    expires_at: datetime,
    consent_basis: str,
    idempotency_key: str,
) -> tuple[str, int]:
    """Persist user-authorized references only; never auto-approve model-generated memory."""
    if (
        not is_uuid7(source_ref)
        or not is_uuid7(projection_ref)
        or not _RECIPIENT.fullmatch(recipient)
        or consent_basis not in {"user_explicit", "approved_setting", "reviewed_import"}
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or not 8 <= len(idempotency_key) <= 200
        or not idempotency_key.isascii()
    ):
        raise CommandError("INVALID_MEMORY", 422)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id)
        prior = _idem_begin(
            c,
            scope,
            "memory.create",
            idempotency_key,
            {
                "task": task_id,
                "source": source_ref,
                "projection": projection_ref,
                "recipient": recipient,
                "observed": observed_at.isoformat(),
                "expires": expires_at.isoformat(),
                "consent": consent_basis,
            },
        )
        if prior is not None:
            return str(prior["response_json"]["memory_id"]), int(prior["response_json"]["epoch"])
        _lock_epoch(c, scope, task_id)
        for artifact_id in (source_ref, projection_ref):
            row = c.execute(
                "SELECT agent.memory_artifact_usable(%s,%s,%s,%s) AS ok",
                (scope.tenant_id, scope.user_id, task_id, artifact_id),
            ).fetchone()
            if row is None or not row["ok"]:
                raise CommandError("MEMORY_SOURCE_UNAVAILABLE", 404)
        memory_id = uuid7()
        row = c.execute(
            "INSERT INTO agent.memory_records(tenant_id,owner_user_id,task_id,id,source_artifact_id,"
            "projection_artifact_id,recipient,consent_basis,observed_at,expires_at)"
            " SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s WHERE %s > clock_timestamp() RETURNING id",
            (
                scope.tenant_id,
                scope.user_id,
                task_id,
                memory_id,
                source_ref,
                projection_ref,
                recipient,
                consent_basis,
                observed_at,
                expires_at,
                expires_at,
            ),
        ).fetchone()
        if row is None:
            raise CommandError("MEMORY_EXPIRED", 422)
        epoch = _epoch(c, scope, task_id)
        _idem_end(c, scope, "memory.create", idempotency_key, {"memory_id": memory_id, "epoch": epoch})
    return memory_id, epoch


def revoke_memory(db: Database, scope: Scope, *, task_id: str, memory_id: str) -> int:
    """Tombstone and advance epoch atomically; repeated revocation is a no-op."""
    if not is_uuid7(memory_id):
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id, allow_revocation=True)
        epoch = _lock_epoch(c, scope, task_id)
        row = c.execute(
            "UPDATE agent.memory_records SET revoked_at=clock_timestamp(), revision=revision+1"
            " WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s AND revoked_at IS NULL RETURNING id",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchone()
        if row is not None:
            return _epoch(c, scope, task_id)
        row = c.execute(
            "SELECT id FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchone()
        if row is None:
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        return epoch
