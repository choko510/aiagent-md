"""Task-scoped Memory writer. Authenticated Scope must be supplied by trusted API code.

No HTTP or agent Tool is exposed. Text remains in separately encrypted artifacts;
only references, consent, expiry and revision are stored here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import psycopg
from aiagent_domain import uuid7
from aiagent_domain.ids import is_uuid7

from . import config, control
from .audit import record_audit
from .commands import CommandError, Scope, _idem_begin, _idem_end
from .db import Database, Row

ALLOWED_UPDATE_FIELDS = frozenset({"recipient", "expires_at"})

_RECIPIENT = re.compile(r"\A[a-zA-Z0-9_.:-]{1,256}\Z")


def _bind(c: psycopg.Connection[Row], tenant: str, owner: str, task: str) -> None:
    c.execute(
        "SELECT set_config('aiagent.memory_tenant',%s,true), set_config('aiagent.memory_owner',%s,true),"
        " set_config('aiagent.memory_task',%s,true)",
        (tenant, owner, task),
    )


def _restore_ready(c: psycopg.Connection[Row]) -> bool:
    row = c.execute("SELECT restore_incarnation, dispatch_paused FROM agent.schema_control").fetchone()
    if row is None:
        return False
    if row["restore_incarnation"] == 1:
        return not row["dispatch_paused"]
    return (
        c.execute(
            "SELECT 1 FROM agent.memory_restore_state WHERE restore_incarnation=%s",
            (row["restore_incarnation"],),
        ).fetchone()
        is not None
    )


def _owner_scope(c: psycopg.Connection[Row], scope: Scope, task_id: str, *, allow_revocation: bool = False) -> None:
    # A restored database may contain old consent/tombstone state. Reads stay
    # closed until a reconciliation row exists for the current incarnation;
    # user revocation is always allowed so data can be withdrawn after a restore.
    if not allow_revocation and not _restore_ready(c):
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


def _insert_revision(
    c: psycopg.Connection[Row],
    scope: Scope,
    task_id: str,
    memory_id: str,
    *,
    revision: int,
    change_kind: str,
    recipient: str,
    consent_basis: str,
    expires_at: datetime,
) -> None:
    c.execute(
        "INSERT INTO agent.memory_record_revisions(tenant_id,owner_user_id,task_id,memory_id,revision,change_kind,"
        " recipient,consent_basis,expires_at,actor_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            scope.tenant_id,
            scope.user_id,
            task_id,
            memory_id,
            revision,
            change_kind,
            recipient,
            consent_basis,
            expires_at,
            scope.user_id,
        ),
    )


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
        _insert_revision(
            c,
            scope,
            task_id,
            str(memory_id),
            revision=1,
            change_kind="created",
            recipient=recipient,
            consent_basis=consent_basis,
            expires_at=expires_at,
        )
        record_audit(
            c,
            tenant_id=scope.tenant_id,
            action_code="memory.created",
            target_type="memory",
            subject_ref=scope.user_id,
            target_ref=str(memory_id),
            detail={"taskId": task_id, "revision": 1, "consentBasis": consent_basis, "recipient": recipient},
        )
        epoch = _epoch(c, scope, task_id)
        _idem_end(c, scope, "memory.create", idempotency_key, {"memory_id": memory_id, "epoch": epoch})
    return memory_id, epoch


def revoke_memory(
    db: Database,
    scope: Scope,
    *,
    task_id: str,
    memory_id: str,
    tombstone_log: Any = None,
) -> int:
    """Tombstone and advance epoch atomically; repeated revocation is a no-op.

    The external tombstone log is appended *before* the database commit so a PITR
    restore cannot resurrect the record. Over-revocation (log append, rollback) is
    safe and reconciled later.
    """
    if not is_uuid7(memory_id):
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    if tombstone_log is None and config.profile() != "test":
        # Without the external append-only record a PITR restore could resurrect
        # this revocation, so production refuses to revoke.
        raise CommandError("MEMORY_TOMBSTONE_UNAVAILABLE", 503)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id, allow_revocation=True)
        epoch = _lock_epoch(c, scope, task_id)
        row = c.execute(
            "UPDATE agent.memory_records SET revoked_at=clock_timestamp(), revision=revision+1, updated_at=now()"
            " WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s AND revoked_at IS NULL"
            " RETURNING id,revision,recipient,consent_basis,expires_at",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchone()
        if row is not None:
            if tombstone_log is not None:
                tombstone_log.append(
                    kind="revoked",
                    tenant_id=scope.tenant_id,
                    owner_id=scope.user_id,
                    task_id=task_id,
                    memory_id=memory_id,
                    revision=int(row["revision"]),
                    recorded_at=datetime.now(UTC).isoformat(),
                )
            _insert_revision(
                c,
                scope,
                task_id,
                memory_id,
                revision=int(row["revision"]),
                change_kind="revoked",
                recipient=str(row["recipient"]),
                consent_basis=str(row["consent_basis"]),
                expires_at=row["expires_at"],
            )
            record_audit(
                c,
                tenant_id=scope.tenant_id,
                action_code="memory.revoked",
                target_type="memory",
                subject_ref=scope.user_id,
                target_ref=memory_id,
                detail={"taskId": task_id, "revision": int(row["revision"])},
            )
            return _epoch(c, scope, task_id)
        row = c.execute(
            "SELECT id FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchone()
        if row is None:
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        return epoch


def update_memory(
    db: Database,
    scope: Scope,
    *,
    task_id: str,
    memory_id: str,
    recipient: str | None = None,
    expires_at: datetime | None = None,
    expected_revision: int,
    tombstone_log: Any = None,
) -> int:
    """Update only recipient/expiry of an active Memory row; identity and content refs never change."""
    if (
        not is_uuid7(memory_id)
        or (recipient is None and expires_at is None)
        or (recipient is not None and not _RECIPIENT.fullmatch(recipient))
        or (expires_at is not None and (expires_at.tzinfo is None or expires_at.utcoffset() is None))
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise CommandError("INVALID_MEMORY", 422)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id)
        _lock_epoch(c, scope, task_id)
        current = c.execute(
            "SELECT revision,recipient,consent_basis,expires_at FROM agent.memory_records WHERE tenant_id=%s"
            " AND owner_user_id=%s AND task_id=%s AND id=%s AND revoked_at IS NULL FOR UPDATE",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchone()
        if current is None:
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        if int(current["revision"]) != expected_revision:
            raise CommandError("MEMORY_REVISION_CONFLICT", 409)
        new_recipient = recipient if recipient is not None else str(current["recipient"])
        new_expiry = expires_at if expires_at is not None else current["expires_at"]
        revision = int(current["revision"]) + 1
        c.execute(
            "UPDATE agent.memory_records SET recipient=%s, expires_at=%s, revision=%s, updated_at=now()"
            " WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s",
            (new_recipient, new_expiry, revision, scope.tenant_id, scope.user_id, task_id, memory_id),
        )
        if tombstone_log is not None:
            tombstone_log.append(
                kind="updated",
                tenant_id=scope.tenant_id,
                owner_id=scope.user_id,
                task_id=task_id,
                memory_id=memory_id,
                revision=revision,
                recorded_at=datetime.now(UTC).isoformat(),
            )
        _insert_revision(
            c,
            scope,
            task_id,
            memory_id,
            revision=revision,
            change_kind="updated",
            recipient=new_recipient,
            consent_basis=str(current["consent_basis"]),
            expires_at=new_expiry,
        )
        record_audit(
            c,
            tenant_id=scope.tenant_id,
            action_code="memory.updated",
            target_type="memory",
            subject_ref=scope.user_id,
            target_ref=memory_id,
            detail={"taskId": task_id, "revision": revision},
        )
        return revision


def list_memories(db: Database, scope: Scope, *, task_id: str) -> list[dict[str, Any]]:
    """Owner-scoped metadata list; never returns Memory plaintext (that stays in the Gateway)."""
    if not is_uuid7(task_id):
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id)
        rows = c.execute(
            "SELECT id,recipient,consent_basis,observed_at,expires_at,revision,revoked_at,source_artifact_id,"
            " projection_artifact_id FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
            " ORDER BY created_at DESC, id",
            (scope.tenant_id, scope.user_id, task_id),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "recipient": r["recipient"],
            "consentBasis": r["consent_basis"],
            "observedAt": r["observed_at"].isoformat(),
            "expiresAt": r["expires_at"].isoformat(),
            "revision": int(r["revision"]),
            "revoked": r["revoked_at"] is not None,
            "sourceArtifactRef": r["source_artifact_id"],
            "projectionArtifactRef": r["projection_artifact_id"],
        }
        for r in rows
    ]


def memory_revisions(db: Database, scope: Scope, *, task_id: str, memory_id: str) -> list[dict[str, Any]]:
    if not is_uuid7(task_id) or not is_uuid7(memory_id):
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id)
        owned = c.execute(
            "SELECT 1 FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchone()
        if owned is None:
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        rows = c.execute(
            "SELECT revision,change_kind,recipient,consent_basis,expires_at,actor_user_id,recorded_at"
            " FROM agent.memory_record_revisions WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s"
            " AND memory_id=%s ORDER BY revision",
            (scope.tenant_id, scope.user_id, task_id, memory_id),
        ).fetchall()
    return [
        {
            "revision": int(r["revision"]),
            "changeKind": r["change_kind"],
            "recipient": r["recipient"],
            "consentBasis": r["consent_basis"],
            "expiresAt": r["expires_at"].isoformat(),
            "actorUserId": r["actor_user_id"],
            "recordedAt": r["recorded_at"].isoformat(),
        }
        for r in rows
    ]


def grant_memory_consent(db: Database, scope: Scope, *, recipient: str, consent_basis: str = "user_explicit") -> int:
    """Durable owner grant for one recipient with immutable revision history."""
    if not _RECIPIENT.fullmatch(recipient) or consent_basis not in {
        "user_explicit",
        "approved_setting",
        "reviewed_import",
    }:
        raise CommandError("INVALID_MEMORY", 422)
    with db.tx(tenant=scope.tenant_id) as c:
        if not control.scope_active(c, scope.tenant_id, scope.user_id, scope.workspace_id):
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        if not _restore_ready(c):
            raise CommandError("MEMORY_RESTORE_RECONCILIATION_REQUIRED", 409)
        row = c.execute(
            "INSERT INTO agent.memory_consents(tenant_id,owner_user_id,recipient,consent_basis)"
            " VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,owner_user_id,recipient) DO UPDATE"
            " SET consent_basis=excluded.consent_basis, revoked_at=NULL, granted_at=now(),"
            " record_version=agent.memory_consents.record_version + 1 RETURNING record_version",
            (scope.tenant_id, scope.user_id, recipient, consent_basis),
        ).fetchone()
        assert row is not None
        revision = int(row["record_version"])
        c.execute(
            "INSERT INTO agent.memory_consent_revisions(tenant_id,owner_user_id,recipient,revision,change_kind,"
            " consent_basis,actor_user_id) VALUES (%s,%s,%s,%s,'granted',%s,%s)",
            (scope.tenant_id, scope.user_id, recipient, revision, consent_basis, scope.user_id),
        )
        record_audit(
            c,
            tenant_id=scope.tenant_id,
            action_code="memory.consent_granted",
            target_type="memory_consent",
            subject_ref=scope.user_id,
            target_ref=recipient,
            detail={"consentBasis": consent_basis, "revision": revision},
        )
        return revision


def revoke_memory_consent(db: Database, scope: Scope, *, recipient: str, tombstone_log: Any = None) -> int:
    """Withdraw one recipient grant, persisting the tombstone before the DB update."""
    if not _RECIPIENT.fullmatch(recipient):
        raise CommandError("INVALID_MEMORY", 422)
    if tombstone_log is None and config.profile() != "test":
        raise CommandError("MEMORY_TOMBSTONE_UNAVAILABLE", 503)
    with db.tx(tenant=scope.tenant_id) as c:
        if not control.scope_active(c, scope.tenant_id, scope.user_id, scope.workspace_id):
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        row = c.execute(
            "SELECT record_version,consent_basis,revoked_at FROM agent.memory_consents"
            " WHERE tenant_id=%s AND owner_user_id=%s AND recipient=%s FOR UPDATE",
            (scope.tenant_id, scope.user_id, recipient),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise CommandError("MEMORY_CONSENT_NOT_FOUND", 404)
        revision = int(row["record_version"]) + 1
        if tombstone_log is not None:
            tombstone_log.append(
                kind="consent_revoked",
                tenant_id=scope.tenant_id,
                owner_id=scope.user_id,
                task_id="__owner_consent__",
                memory_id=recipient,
                revision=revision,
                recorded_at=datetime.now(UTC).isoformat(),
            )
        c.execute(
            "UPDATE agent.memory_consents SET revoked_at=now(), record_version=%s"
            " WHERE tenant_id=%s AND owner_user_id=%s AND recipient=%s",
            (revision, scope.tenant_id, scope.user_id, recipient),
        )
        c.execute(
            "INSERT INTO agent.memory_consent_revisions(tenant_id,owner_user_id,recipient,revision,change_kind,"
            " consent_basis,actor_user_id) VALUES (%s,%s,%s,%s,'revoked',%s,%s)",
            (scope.tenant_id, scope.user_id, recipient, revision, row["consent_basis"], scope.user_id),
        )
        record_audit(
            c,
            tenant_id=scope.tenant_id,
            action_code="memory.consent_revoked",
            target_type="memory_consent",
            subject_ref=scope.user_id,
            target_ref=recipient,
            detail={"revision": revision},
        )
        return revision


def memory_consents(db: Database, scope: Scope) -> list[dict[str, Any]]:
    with db.tx(tenant=scope.tenant_id) as c:
        if not control.scope_active(c, scope.tenant_id, scope.user_id, scope.workspace_id):
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        if not _restore_ready(c):
            raise CommandError("MEMORY_RESTORE_RECONCILIATION_REQUIRED", 409)
        rows = c.execute(
            "SELECT recipient,consent_basis,revoked_at,record_version,granted_at FROM agent.memory_consents"
            " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY recipient",
            (scope.tenant_id, scope.user_id),
        ).fetchall()
    return [
        {
            "recipient": r["recipient"],
            "consentBasis": r["consent_basis"],
            "granted": r["revoked_at"] is None,
            "recordVersion": int(r["record_version"]),
            "grantedAt": r["granted_at"].isoformat(),
        }
        for r in rows
    ]


def memory_consent_revisions(db: Database, scope: Scope, *, recipient: str) -> list[dict[str, Any]]:
    if not _RECIPIENT.fullmatch(recipient):
        raise CommandError("INVALID_MEMORY", 422)
    with db.tx(tenant=scope.tenant_id) as c:
        if not control.scope_active(c, scope.tenant_id, scope.user_id, scope.workspace_id):
            raise CommandError("MEMORY_SCOPE_DENIED", 404)
        if not _restore_ready(c):
            raise CommandError("MEMORY_RESTORE_RECONCILIATION_REQUIRED", 409)
        rows = c.execute(
            "SELECT revision,change_kind,consent_basis,actor_user_id,recorded_at"
            " FROM agent.memory_consent_revisions WHERE tenant_id=%s AND owner_user_id=%s AND recipient=%s"
            " ORDER BY revision",
            (scope.tenant_id, scope.user_id, recipient),
        ).fetchall()
    return [
        {
            "revision": int(r["revision"]),
            "changeKind": r["change_kind"],
            "consentBasis": r["consent_basis"],
            "actorUserId": r["actor_user_id"],
            "recordedAt": r["recorded_at"].isoformat(),
        }
        for r in rows
    ]


def set_context_policy(
    db: Database,
    scope: Scope,
    *,
    task_id: str,
    recipient: str,
    model: str,
    enabled: bool,
    expected_record_version: int | None = None,
) -> int:
    """Owner-authored switch for the Context path. This is the only way to enable it."""
    if (
        not is_uuid7(task_id)
        or not _RECIPIENT.fullmatch(recipient)
        or not 1 <= len(model) <= 128
        or not isinstance(enabled, bool)
    ):
        raise CommandError("INVALID_CONTEXT_POLICY", 422)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id, allow_revocation=True)
        if enabled and not _restore_ready(c):
            raise CommandError("MEMORY_RESTORE_RECONCILIATION_REQUIRED", 409)
        current = c.execute(
            "SELECT record_version, recipient, model, enabled FROM agent.task_context_policies"
            " WHERE tenant_id=%s AND task_id=%s FOR UPDATE",
            (scope.tenant_id, task_id),
        ).fetchone()
        if current is None:
            if expected_record_version not in (None, 0):
                raise CommandError("CONTEXT_POLICY_CONFLICT", 409)
            c.execute(
                "INSERT INTO agent.task_context_policies(tenant_id,task_id,owner_user_id,recipient,model,enabled)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (scope.tenant_id, task_id, scope.user_id, recipient, model, enabled),
            )
            version = 1
        else:
            if expected_record_version is not None and int(current["record_version"]) != expected_record_version:
                raise CommandError("CONTEXT_POLICY_CONFLICT", 409)
            c.execute(
                "UPDATE agent.task_context_policies SET recipient=%s, model=%s, enabled=%s,"
                " record_version=record_version+1, updated_at=now() WHERE tenant_id=%s AND task_id=%s",
                (recipient, model, enabled, scope.tenant_id, task_id),
            )
            version = int(current["record_version"]) + 1
        record_audit(
            c,
            tenant_id=scope.tenant_id,
            action_code="context.policy_set",
            target_type="task",
            subject_ref=scope.user_id,
            target_ref=task_id,
            detail={"recipient": recipient, "model": model, "enabled": enabled, "recordVersion": version},
        )
        return version


def context_policy(db: Database, scope: Scope, *, task_id: str) -> dict[str, Any] | None:
    if not is_uuid7(task_id):
        raise CommandError("MEMORY_SCOPE_DENIED", 404)
    with db.tx(tenant=scope.tenant_id) as c:
        _owner_scope(c, scope, task_id, allow_revocation=True)
        row = c.execute(
            "SELECT recipient,model,enabled,record_version,updated_at FROM agent.task_context_policies"
            " WHERE tenant_id=%s AND task_id=%s AND owner_user_id=%s",
            (scope.tenant_id, task_id, scope.user_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "recipient": row["recipient"],
        "model": row["model"],
        "enabled": bool(row["enabled"]),
        "recordVersion": int(row["record_version"]),
        "updatedAt": row["updated_at"].isoformat(),
    }
