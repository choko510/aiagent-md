"""Restore reconciliation for Memory: replay external tombstones, then record a watermark.

A PITR-restored database can contain a Memory row whose revocation exists only in
the external tombstone log. This module must run before Memory reads are allowed
on the restored incarnation:

  1. verify the external chain (hash-linked, monotonic generations);
  2. apply every entry the restored database does not reflect (idempotent; re-revoking
     an already revoked row is a no-op) and add the matching revision history;
  3. record the verified generation in agent.memory_restore_state.

Reads stay closed until step 3 commits, and agent.memory_restore_ready only accepts
the incarnation that has a matching row. A later restore bumps the incarnation and
therefore requires a new reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from aiagent_core.db import Database, Row, connect

from .audit import record_audit
from .memory_tombstones import Tombstone, TombstoneLog, watermark_payload


class RestoreReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconcileReport:
    incarnation: int
    generation: int
    chain_head: str
    replayed: int
    reflected: int


def _apply(c: psycopg.Connection[Row], entries: list[Tombstone], *, reconciled_by: str) -> ReconcileReport:
    control = c.execute("SELECT restore_incarnation, dispatch_paused FROM agent.schema_control").fetchone()
    if control is None:
        raise RestoreReconcileError("schema_control is missing")
    if control["dispatch_paused"]:
        raise RestoreReconcileError("dispatch is paused; finish the operator restore steps first")
    incarnation = int(control["restore_incarnation"])
    c.execute("SELECT set_config('aiagent.memory_restore_reconcile', 'on', true)")
    replayed = 0
    reflected = 0
    tenants: set[str] = set()
    for entry in entries:
        tenants.add(entry.tenant_id)
        if entry.kind == "consent_revoked":
            consent = c.execute(
                "SELECT record_version,revoked_at,consent_basis FROM agent.memory_consents"
                " WHERE tenant_id=%s AND owner_user_id=%s AND recipient=%s FOR UPDATE",
                (entry.tenant_id, entry.owner_id, entry.memory_id),
            ).fetchone()
            if (
                consent is not None
                and consent["revoked_at"] is not None
                and int(consent["record_version"]) >= entry.revision
            ):
                reflected += 1
                continue
            if consent is None:
                revision = max(1, entry.revision)
                c.execute(
                    "INSERT INTO agent.memory_consents(tenant_id,owner_user_id,recipient,consent_basis,"
                    " granted_at,revoked_at,record_version) VALUES (%s,%s,%s,'user_explicit',now(),now(),%s)",
                    (entry.tenant_id, entry.owner_id, entry.memory_id, revision),
                )
                basis = "user_explicit"
            else:
                revision = max(int(consent["record_version"]), entry.revision) + 1
                basis = str(consent["consent_basis"])
                c.execute(
                    "UPDATE agent.memory_consents SET revoked_at=coalesce(revoked_at,now()),record_version=%s"
                    " WHERE tenant_id=%s AND owner_user_id=%s AND recipient=%s",
                    (revision, entry.tenant_id, entry.owner_id, entry.memory_id),
                )
            c.execute(
                "INSERT INTO agent.memory_consent_revisions(tenant_id,owner_user_id,recipient,revision,change_kind,"
                " consent_basis,actor_user_id,recorded_at) VALUES (%s,%s,%s,%s,'revoked',%s,%s,now())"
                " ON CONFLICT DO NOTHING",
                (entry.tenant_id, entry.owner_id, entry.memory_id, revision, basis, entry.owner_id),
            )
            record_audit(
                c,
                tenant_id=entry.tenant_id,
                action_code="memory.consent_restore_revoked",
                target_type="memory_consent",
                subject_ref="restore-reconciliation",
                target_ref=entry.memory_id,
                detail={"revision": revision, "externalGeneration": entry.generation},
            )
            replayed += 1
            continue
        row = c.execute(
            "SELECT revision,revoked_at,recipient,consent_basis,expires_at FROM agent.memory_records"
            " WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s FOR UPDATE",
            (entry.tenant_id, entry.owner_id, entry.task_id, entry.memory_id),
        ).fetchone()
        if row is None:
            reflected += 1
            continue
        if entry.kind in ("revoked", "updated"):
            # The external log records update identity/revision, not previous/new
            # recipient values. On PITR replay, conservatively revoke both updates
            # and explicit revocations rather than restoring stale consent metadata.
            if row["revoked_at"] is not None and int(row["revision"]) >= entry.revision:
                reflected += 1
                continue
            c.execute(
                "UPDATE agent.memory_records SET revoked_at=coalesce(revoked_at, now()),"
                " revision=greatest(revision, %s), updated_at=now() WHERE tenant_id=%s AND owner_user_id=%s"
                " AND task_id=%s AND id=%s",
                (entry.revision, entry.tenant_id, entry.owner_id, entry.task_id, entry.memory_id),
            )
        else:  # created: the row is either present or a later entry already changed it
            reflected += 1
            continue
        c.execute(
            "INSERT INTO agent.memory_record_revisions(tenant_id,owner_user_id,task_id,memory_id,revision,"
            " change_kind,recipient,consent_basis,expires_at,actor_user_id,recorded_at)"
            " SELECT tenant_id,owner_user_id,task_id,id,%s,%s,recipient,consent_basis,expires_at,owner_user_id,now()"
            " FROM agent.memory_records WHERE tenant_id=%s AND owner_user_id=%s AND task_id=%s AND id=%s"
            " ON CONFLICT DO NOTHING",
            (
                entry.revision,
                "revoked",
                entry.tenant_id,
                entry.owner_id,
                entry.task_id,
                entry.memory_id,
            ),
        )
        replayed += 1
    payload = watermark_payload(entries)
    generation = payload["generation"]
    c.execute(
        "INSERT INTO agent.memory_restore_state(restore_incarnation,external_generation,tombstone_chain_head,"
        " observed_at,reconciled_by) VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT (restore_incarnation) DO UPDATE SET external_generation=excluded.external_generation,"
        " tombstone_chain_head=excluded.tombstone_chain_head, observed_at=excluded.observed_at,"
        " reconciled_at=now(), reconciled_by=excluded.reconciled_by"
        " WHERE excluded.external_generation >= agent.memory_restore_state.external_generation",
        (
            incarnation,
            max(1, generation),
            payload["chain_head"],
            datetime.fromisoformat(str(payload["last_event_at"])) if generation > 0 else datetime.now(UTC),
            reconciled_by,
        ),
    )
    for tenant in tenants:
        record_audit(
            c,  # type: ignore[arg-type]
            tenant_id=tenant,
            action_code="memory.restore_reconciled",
            target_type="memory_restore",
            subject_ref=reconciled_by,
            target_ref=f"incarnation:{incarnation}",
            detail={"generation": generation, "replayed": replayed, "reflected": reflected},
        )
    return ReconcileReport(incarnation, generation, str(payload["chain_head"]), replayed, reflected)


def reconcile(db: Database, log: TombstoneLog, *, reconciled_by: str) -> ReconcileReport:
    if not reconciled_by or len(reconciled_by) > 200:
        raise RestoreReconcileError("a reconciling operator identity is required")
    entries = log.read()
    with db.tx() as c:
        return _apply(c, entries, reconciled_by=reconciled_by)


def reconcile_owner(owner_dsn: str, log: TombstoneLog, *, reconciled_by: str) -> ReconcileReport:
    """Operator CLI variant: migration-owner connection (no RLS restrictions)."""
    if not reconciled_by or len(reconciled_by) > 200:
        raise RestoreReconcileError("a reconciling operator identity is required")
    entries = log.read()
    conn = connect(owner_dsn)
    try:
        with conn.transaction():
            return _apply(conn, entries, reconciled_by=reconciled_by)
    finally:
        conn.close()
