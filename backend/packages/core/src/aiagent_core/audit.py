"""Audit trail writer (01-05 Evidence / audit). Redacted detail only; never content."""

from __future__ import annotations

import re

import psycopg
from aiagent_domain import uuid7
from psycopg.types.json import Jsonb

from .db import Row

_FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "text",
        "rawtext",
        "content",
        "body",
        "prompt",
        "prompts",
        "output",
        "response",
        "completion",
        "message",
        "messages",
        "excerpt",
        "summary",
        "quote",
        "secret",
        "password",
        "passwd",
        "token",
        "apikey",
        "cookie",
        "cookies",
        "authorization",
        "authheader",
        "privatekey",
        "credential",
        "credentials",
    }
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{12,}|akia[0-9a-z]{16}|xox[baprs]-[a-z0-9-]{8,}"
    r"|bearer\s+[a-z0-9._~+/=-]{8,}|eyJ[a-z0-9_-]{8,}\.eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{4,}"
    r"|-----BEGIN\s+(?:[A-Z]+\s+)?PRIVATE\s+KEY-----"
    r"|(?:api_?key|password|passwd|secret|token|authorization|cookie)\s*[:=])"
)


def _validate_detail(value: object) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("AUDIT_DETAIL_FORBIDDEN")
            normalized = re.sub(r"[^a-z0-9]", "", k.lower())
            if normalized in _FORBIDDEN_DETAIL_KEYS:
                raise ValueError("AUDIT_DETAIL_FORBIDDEN")
            _validate_detail(v)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_detail(item)
        return
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise ValueError("AUDIT_DETAIL_FORBIDDEN")


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
    if detail is not None:
        _validate_detail(detail)
    audit_id = uuid7()
    c.execute(
        "INSERT INTO agent.audit_records(tenant_id, id, subject_ref, action_code, target_type, target_ref,"
        " redacted_detail_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (tenant_id, audit_id, subject_ref, action_code, target_type, target_ref, Jsonb(detail or {})),
    )
    return audit_id
