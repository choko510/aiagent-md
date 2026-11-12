"""Audit trail writer (01-05 Evidence / audit). Redacted detail only; never content."""

from __future__ import annotations

import psycopg
from aiagent_domain import uuid7
from psycopg.types.json import Jsonb

from .db import Row


def record_audit(
    c: psycopg.Connection[Row],
    *,
    tenant_id: str,
    action_code: str,
    target_type: str,
    subject_ref: str | None = None,
    target_ref: str | None = None,
    detail: dict[str, object] | None = None,
) -> str:
    """Append one audit row inside the caller's transaction.

    `detail` must only contain identifiers, e.g. revision numbers and change kinds.
    Raw Memory text, provider output and secrets are forbidden here by construction.
    """
    audit_id = uuid7()
    c.execute(
        "INSERT INTO agent.audit_records(tenant_id, id, subject_ref, action_code, target_type, target_ref,"
        " redacted_detail_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (tenant_id, audit_id, subject_ref, action_code, target_type, target_ref, Jsonb(detail or {})),
    )
    return audit_id
