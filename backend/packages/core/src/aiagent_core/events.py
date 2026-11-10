"""Domain Event + Transactional Outbox writer (01-02 / 10-02). Must be called inside the caller's transaction so the
domain mutation, the event and the outbox row commit (or roll back) together. Lock order: domain row (caller) ->
aggregate counter -> tenant counter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from aiagent_domain import sha256_hex, utc_instant, uuid7
from aiagent_domain.generated import RunJobPointerV1
from psycopg.types.json import Jsonb

from .db import Row

EVENT_KINDS = frozenset(
    {
        "task.accepted",
        "task.state_changed",
        "task.goal_revised",
        "run.created",
        "run.claimed",
        "run.state_changed",
        "run.checkpointed",
        "approval.requested",
        "approval.decided",
        "approval.revoked",
        "tool.intent_recorded",
        "tool.effect_recorded",
        "tool.effect_unknown",
        "result.published",
    }
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset({"arguments", "args", "content", "secret", "token", "cookie", "password", "prompt"})


def append_event(
    conn: psycopg.Connection[Row],
    *,
    tenant_id: str,
    aggregate_type: str,
    aggregate_id: str,
    kind: str,
    payload: dict[str, Any],
    actor: dict[str, str],
    owner_user_id: str | None,
    task_id: str | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
    causation_event_id: str | None = None,
) -> str:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind}")
    if _FORBIDDEN_PAYLOAD_KEYS & set(payload):
        raise ValueError("event payload must not carry raw content, tool arguments or credentials")
    conn.execute(
        "INSERT INTO agent.event_aggregate_counters(tenant_id, aggregate_type, aggregate_id) VALUES (%s,%s,%s)"
        " ON CONFLICT DO NOTHING",
        (tenant_id, aggregate_type, aggregate_id),
    )
    seq = conn.execute(
        "UPDATE agent.event_aggregate_counters SET next_aggregate_sequence = next_aggregate_sequence + 1"
        " WHERE tenant_id=%s AND aggregate_type=%s AND aggregate_id=%s RETURNING next_aggregate_sequence - 1 AS s",
        (tenant_id, aggregate_type, aggregate_id),
    ).fetchone()
    conn.execute("INSERT INTO agent.tenant_event_counters(tenant_id) VALUES (%s) ON CONFLICT DO NOTHING", (tenant_id,))
    off = conn.execute(
        "UPDATE agent.tenant_event_counters SET next_delivery_offset = next_delivery_offset + 1 WHERE tenant_id=%s"
        " RETURNING next_delivery_offset - 1 AS o",
        (tenant_id,),
    ).fetchone()
    assert seq is not None and off is not None
    event_id = uuid7()
    conn.execute(
        "INSERT INTO agent.domain_events(tenant_id, id, aggregate_type, aggregate_id, aggregate_sequence,"
        " delivery_offset, event_kind, payload_json, actor_json, audience_code, owner_user_id, task_id, run_id,"
        " causation_event_id, correlation_id, occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())",
        (
            tenant_id,
            event_id,
            aggregate_type,
            aggregate_id,
            seq["s"],
            off["o"],
            kind,
            Jsonb(payload),
            Jsonb(actor),
            "owner_private" if owner_user_id else "operator_audit",
            owner_user_id,
            task_id,
            run_id,
            causation_event_id,
            correlation_id or (task_id or aggregate_id),
        ),
    )
    return event_id


def enqueue_run_job(
    conn: psycopg.Connection[Row],
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    kind: str,
    goal_revision: int,
    dedupe_key: str,
    event_id: str,
    schedule_occurrence_id: str | None = None,
    ttl_s: int = 7 * 24 * 3600,
) -> bool:
    """Outbox row carrying a RunJobPointerV1 (validated by the generated contract). Same TX as the mutation."""
    incarnation = conn.execute("SELECT restore_incarnation FROM agent.schema_control").fetchone()
    assert incarnation is not None

    pointer = RunJobPointerV1(
        schema_version="1.0",
        kind=kind,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        expected_goal_revision=str(goal_revision),  # type: ignore[arg-type]
        restore_incarnation=str(incarnation["restore_incarnation"]),  # type: ignore[arg-type]
        schedule_occurrence_id=schedule_occurrence_id,
        expires_at=utc_instant(datetime.now(UTC) + timedelta(seconds=ttl_s)),
    )
    body = pointer.model_dump(mode="json")
    cur = conn.execute(
        "INSERT INTO agent.outbox_messages(tenant_id, id, topic, dedupe_key, event_id, safe_payload_json,"
        " payload_sha256, restore_incarnation) VALUES (%s,%s,'run',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (tenant_id, uuid7(), dedupe_key, event_id, Jsonb(body), sha256_hex(body), incarnation["restore_incarnation"]),
    )
    return cur.rowcount == 1
