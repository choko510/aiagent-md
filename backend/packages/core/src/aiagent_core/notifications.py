"""Notification Bindings, Consent Validation, Transactional Outbox Delivery & Attempt Ledger (NOT-001..016).

Safety invariants:
1. Webhook URLs (including path/secret tokens) and Telegram botToken+chatId are AES-GCM encrypted at rest with
   binding-scoped AAD; plaintext secrets never enter database columns, logs, domain events, or API responses.
2. Enqueue validates consent + active owner scope and writes (domain_event, notification_intent, outbox_message)
   inside a SINGLE PostgreSQL transaction with semantic_event_key uniqueness (NOT-002 / NOT-003).
3. Delivery re-validates consent, binding_version, consent_version, opt-out/revocation, and active scope before
   issuing a Sentinel notification permit and again inside Trusted Egress Proxy at actual send time (NOT-008).
4. Post-send disconnects/timeouts record `UNKNOWN` (`DELIVERY_UNKNOWN`) and NEVER auto-retry (NOT-006).
5. Only explicit authenticated owner action transitions `PROVIDER_ACCEPTED` -> `READ` (NOT-016).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from aiagent_domain import sha256_hex, uuid7
from aiagent_domain.rpcwire import RpcError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.types.json import Jsonb

from . import config, control, events
from .db import Database, Row
from .egress_http import ForwardProxyError, send_via_forward_proxy
from .logging import redact as redact_secrets

MAX_ATTEMPTS = 5
LEASE_SECONDS = 30.0
_BOT_TOKEN_RE = re.compile(r"^[0-9]{4,16}:[A-Za-z0-9_-]{16,128}$")
_CHAT_ID_RE = re.compile(r"^-?[0-9A-Za-z_]{1,64}$")


def utcnow() -> datetime:
    """Single clock hook for current execution-time Quiet Hours evaluation."""
    return datetime.now(UTC)


DirectSendFn = Callable[[str, str, dict[str, str], bytes], Awaitable[tuple[int, str]]]


class NotificationError(Exception):
    """Domain/validation error mapped cleanly to HTTP error codes by the API layer."""

    def __init__(self, code: str, *, http_status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _parse_keyring_secret(raw_json: str) -> tuple[str, dict[str, bytes]]:
    try:
        doc = json.loads(raw_json)
        active = str(doc["active"])
        raw_keys = doc["keys"]
        if not isinstance(raw_keys, dict) or active not in raw_keys:
            raise ValueError("missing active key")
        keys_map: dict[str, bytes] = {}
        for kid, khex in raw_keys.items():
            kb = bytes.fromhex(str(khex))
            if not str(kid) or len(kb) != 32:
                raise ValueError("invalid key entry")
            keys_map[str(kid)] = kb
    except (ValueError, KeyError, TypeError) as exc:
        raise config.ConfigError("invalid notification secret key ring") from exc
    if not active or active not in keys_map:
        raise config.ConfigError("invalid notification secret key ring")
    return active, keys_map


def _load_keyring_from_env() -> tuple[str, dict[str, bytes]] | None:
    raw = config.secret("AIAGENT_NOTIFICATION_SECRET_KEYS", required=False)
    if raw is not None:
        return _parse_keyring_secret(raw.reveal())
    return None


def _binding_aad(tenant_id: str, user_id: str, binding_id: str) -> bytes:
    return f"notify-binding-v1|{tenant_id}|{user_id}|{binding_id}".encode()


def encrypt_binding_secret(
    keyring: Any,
    secret_payload: dict[str, str],
    *,
    tenant_id: str,
    user_id: str,
    binding_id: str,
) -> tuple[bytes, bytes, str, str]:
    plaintext = json.dumps(secret_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    aad = _binding_aad(tenant_id, user_id, binding_id)
    if keyring is not None and hasattr(keyring, "active") and hasattr(keyring, "cipher"):
        key_id = str(keyring.active)
        nonce = os.urandom(12)
        cipher_bytes = keyring.cipher(key_id).encrypt(nonce, plaintext, aad)
        return nonce, cipher_bytes, key_id, hashlib.sha256(plaintext).hexdigest()
    loaded = _load_keyring_from_env()
    if loaded is None:
        raise NotificationError("NOTIFICATION_KEY_UNAVAILABLE", http_status=503)
    key_id, keys_map = loaded
    nonce = os.urandom(12)
    cipher_bytes = AESGCM(keys_map[key_id]).encrypt(nonce, plaintext, aad)
    return nonce, cipher_bytes, key_id, hashlib.sha256(plaintext).hexdigest()


def decrypt_binding_secret(
    keyring: Any,
    *,
    nonce: bytes,
    ciphertext: bytes,
    key_id: str,
    expected_sha256: str,
    tenant_id: str,
    user_id: str,
    binding_id: str,
) -> dict[str, str]:
    aad = _binding_aad(tenant_id, user_id, binding_id)
    if keyring is not None and hasattr(keyring, "cipher"):
        try:
            cipher = keyring.cipher(key_id)
        except KeyError:
            raise NotificationError("NOTIFICATION_KEY_UNAVAILABLE", http_status=503) from None
        plain = cipher.decrypt(nonce, ciphertext, aad)
    else:
        loaded = _load_keyring_from_env()
        if loaded is None:
            raise NotificationError("NOTIFICATION_KEY_UNAVAILABLE", http_status=503)
        _, keys_map = loaded
        key_bytes = keys_map.get(key_id)
        if key_bytes is None:
            raise NotificationError("NOTIFICATION_KEY_UNAVAILABLE", http_status=503)
        plain = AESGCM(key_bytes).decrypt(nonce, ciphertext, aad)
    if hashlib.sha256(plain).hexdigest() != expected_sha256:
        raise NotificationError("BINDING_SECRET_TAMPERED", http_status=500)
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        raise NotificationError("BINDING_SECRET_CORRUPT", http_status=500)
    return {str(k): str(v) for k, v in data.items()}


def _validate_url(raw_url: str, *, production: bool) -> tuple[str, str, int, str]:
    try:
        u = urlsplit(raw_url.strip())
    except ValueError:
        raise NotificationError("INVALID_DESTINATION_URL") from None
    scheme = u.scheme.lower()
    host = (u.hostname or "").lower()
    if scheme not in ("http", "https") or not host or u.username is not None or u.password is not None:
        raise NotificationError("INVALID_DESTINATION_URL")
    if production and scheme != "https":
        raise NotificationError("HTTPS_REQUIRED")
    try:
        raw_port = u.port
    except ValueError:
        raise NotificationError("INVALID_DESTINATION_URL") from None
    port = (443 if scheme == "https" else 80) if raw_port is None else raw_port
    if not (1 <= port <= 65535):
        raise NotificationError("INVALID_DESTINATION_URL")
    if production and _is_private_or_loopback_ip(host):
        raise NotificationError("EGRESS_DENIED", http_status=403)
    path_q = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return scheme, host, port, path_q


def _is_private_or_loopback_ip(host: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(host.strip("[]"))
        return not ip_obj.is_global
    except ValueError:
        return host in ("localhost", "localhost.localdomain")


def compute_quiet_until(
    now_utc: datetime,
    *,
    timezone_id: str,
    quiet_start_hour: int | None,
    quiet_end_hour: int | None,
) -> datetime | None:
    """Return UTC datetime when quiet hours end if `now_utc` falls inside quiet hours, else None."""
    if quiet_start_hour is None or quiet_end_hour is None or quiet_start_hour == quiet_end_hour:
        return None
    tz = ZoneInfo(timezone_id)
    local_now = now_utc.astimezone(tz)
    hr = local_now.hour
    in_quiet = (
        quiet_start_hour <= hr < quiet_end_hour
        if quiet_start_hour < quiet_end_hour
        else (hr >= quiet_start_hour or hr < quiet_end_hour)
    )
    if not in_quiet:
        return None
    target_date = local_now.date()
    if quiet_start_hour > quiet_end_hour and hr >= quiet_start_hour:
        target_date = target_date + timedelta(days=1)
    end_local = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        quiet_end_hour,
        0,
        0,
        tzinfo=tz,
    )
    return end_local.astimezone(UTC)


def _format_binding_row(r: Row) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "channel": str(r["channel"]),
        "label": str(r["label"]),
        "destinationScheme": str(r["destination_scheme"]),
        "destinationHost": str(r["destination_host"]),
        "destinationPort": int(r["destination_port"]),
        "redactedTarget": str(r["redacted_target"]),
        "bindingVersion": int(r["binding_version"]),
        "consentGranted": bool(r["consent_granted"]),
        "consentVersion": int(r["consent_version"]),
        "quietStartHour": r["quiet_start_hour"],
        "quietEndHour": r["quiet_end_hour"],
        "timezoneId": str(r["timezone_id"]),
        "optedOutAt": r["opted_out_at"].isoformat() if r["opted_out_at"] else None,
        "revokedAt": r["revoked_at"].isoformat() if r["revoked_at"] else None,
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat(),
    }


def create_binding(
    db: Database,
    keyring: Any,
    *,
    tenant_id: str,
    user_id: str,
    workspace_id: str,
    channel: str,
    label: str,
    destination: dict[str, str],
    consent_granted: bool = False,
    quiet_start_hour: int | None = None,
    quiet_end_hour: int | None = None,
    timezone_id: str = "UTC",
) -> dict[str, Any]:
    if channel not in ("webhook", "telegram"):
        raise NotificationError("INVALID_CHANNEL")
    clean_label = label.strip()
    if not (1 <= len(clean_label) <= 120):
        raise NotificationError("INVALID_LABEL")
    try:
        ZoneInfo(timezone_id)
    except ZoneInfoNotFoundError:
        raise NotificationError("INVALID_TIMEZONE") from None
    if (quiet_start_hour is None) != (quiet_end_hour is None):
        raise NotificationError("INVALID_QUIET_HOURS")
    if (
        quiet_start_hour is not None
        and quiet_end_hour is not None
        and not (0 <= quiet_start_hour <= 23 and 0 <= quiet_end_hour <= 23)
    ):
        raise NotificationError("INVALID_QUIET_HOURS")

    production = config.profile() == "production"
    binding_id = uuid7()

    if channel == "webhook":
        raw_url = str(destination.get("url") or "").strip()
        scheme, host, port, _ = _validate_url(raw_url, production=production)
        default_port = 443 if scheme == "https" else 80
        port_suffix = "" if port == default_port else f":{port}"
        redacted_target = f"{scheme}://{host}{port_suffix}/***"
        secret_payload = {"url": raw_url}
    else:
        bot_token = str(destination.get("botToken") or "").strip()
        chat_id = str(destination.get("chatId") or "").strip()
        if not _BOT_TOKEN_RE.fullmatch(bot_token) or not _CHAT_ID_RE.fullmatch(chat_id):
            raise NotificationError("INVALID_TELEGRAM_DESTINATION")
        api_base = str(destination.get("apiBaseUrl") or "https://api.telegram.org").strip()
        scheme, host, port, _ = _validate_url(api_base, production=production)
        redacted_target = f"telegram:***{chat_id[-4:]}"
        secret_payload = {"botToken": bot_token, "chatId": chat_id, "apiBaseUrl": api_base.rstrip("/")}

    nonce, ciphertext, key_id, secret_sha = encrypt_binding_secret(
        keyring,
        secret_payload,
        tenant_id=tenant_id,
        user_id=user_id,
        binding_id=binding_id,
    )

    with db.tx(tenant=tenant_id) as c:
        if not control.scope_active(c, tenant_id, user_id, workspace_id):
            raise NotificationError("SCOPE_REVOKED", http_status=403)
        row = c.execute(
            "INSERT INTO agent.notification_bindings("
            " tenant_id, id, owner_user_id, workspace_id, channel, label,"
            " destination_scheme, destination_host, destination_port, redacted_target,"
            " secret_nonce, secret_ciphertext, secret_key_id, secret_sha256,"
            " consent_granted, quiet_start_hour, quiet_end_hour, timezone_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING *",
            (
                tenant_id,
                binding_id,
                user_id,
                workspace_id,
                channel,
                clean_label,
                scheme,
                host,
                port,
                redacted_target,
                nonce,
                ciphertext,
                key_id,
                secret_sha,
                bool(consent_granted),
                quiet_start_hour,
                quiet_end_hour,
                timezone_id,
            ),
        ).fetchone()
        assert row is not None
        return _format_binding_row(row)


def list_bindings(db: Database, *, tenant_id: str, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, 200))
    with db.tx(tenant=tenant_id) as c:
        rows = c.execute(
            "SELECT * FROM agent.notification_bindings"
            " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, user_id, bounded),
        ).fetchall()
        return [_format_binding_row(r) for r in rows]


def _suppress_pending_for_binding(c: Any, *, tenant_id: str, binding_id: str, reason: str) -> None:
    rows = c.execute(
        "WITH target AS ("
        "  SELECT id FROM agent.notification_intents"
        "  WHERE tenant_id=%s AND binding_id=%s AND status IN ('QUEUED','DEFERRED_QUIET_HOURS','SENDING')"
        "  ORDER BY id FOR UPDATE"
        ")"
        " UPDATE agent.notification_intents i"
        " SET status='SUPPRESSED', last_error_code=%s, lease_until=NULL, updated_at=now()"
        " FROM target WHERE i.tenant_id=%s AND i.id=target.id RETURNING i.id",
        (tenant_id, binding_id, reason, tenant_id),
    ).fetchall()
    for r in rows:
        iid = str(r["id"])
        c.execute(
            "UPDATE agent.outbox_messages"
            " SET dispatched_at=now(), relay_lease_until=NULL, last_error_code=%s"
            " WHERE tenant_id=%s AND topic='notification' AND dedupe_key=%s AND dispatched_at IS NULL",
            (reason, tenant_id, f"notify:{iid}"),
        )


def _run_consent_tx(db: Database, tenant_id: str, fn: Callable[[Any], Any]) -> Any:
    """Execute idempotent consent/opt-out TX; retry only on rolled-back PG deadlock, never fake success."""
    for attempt in range(3):
        try:
            with db.tx(tenant=tenant_id) as c:
                return fn(c)
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise
            time.sleep(0.01 * (attempt + 1))


def set_binding_consent(
    db: Database,
    *,
    tenant_id: str,
    user_id: str,
    binding_id: str,
    granted: bool,
) -> dict[str, Any] | None:
    def _op(c: Any) -> dict[str, Any] | None:
        if granted:
            row = c.execute(
                "UPDATE agent.notification_bindings"
                " SET consent_granted=true, consent_version=consent_version + 1, opted_out_at=NULL, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND revoked_at IS NULL"
                " RETURNING *",
                (tenant_id, binding_id, user_id),
            ).fetchone()
        else:
            row = c.execute(
                "UPDATE agent.notification_bindings"
                " SET consent_granted=false, consent_version=consent_version + 1, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND revoked_at IS NULL"
                " RETURNING *",
                (tenant_id, binding_id, user_id),
            ).fetchone()
            if row is not None:
                _suppress_pending_for_binding(c, tenant_id=tenant_id, binding_id=binding_id, reason="CONSENT_REVOKED")
        return _format_binding_row(row) if row is not None else None

    out: dict[str, Any] | None = _run_consent_tx(db, tenant_id, _op)
    return out


def opt_out_binding(
    db: Database,
    *,
    tenant_id: str,
    user_id: str,
    binding_id: str,
    revoke: bool = False,
) -> dict[str, Any] | None:
    def _op(c: Any) -> dict[str, Any] | None:
        if revoke:
            row = c.execute(
                "UPDATE agent.notification_bindings"
                " SET consent_granted=false, consent_version=consent_version + 1, binding_version=binding_version + 1,"
                " opted_out_at=COALESCE(opted_out_at, now()), revoked_at=COALESCE(revoked_at, now()), updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s RETURNING *",
                (tenant_id, binding_id, user_id),
            ).fetchone()
        else:
            row = c.execute(
                "UPDATE agent.notification_bindings"
                " SET consent_granted=false, consent_version=consent_version + 1,"
                " opted_out_at=COALESCE(opted_out_at, now()), updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND revoked_at IS NULL RETURNING *",
                (tenant_id, binding_id, user_id),
            ).fetchone()
        if row is not None:
            _suppress_pending_for_binding(
                c,
                tenant_id=tenant_id,
                binding_id=binding_id,
                reason="BINDING_REVOKED" if revoke else "OPTED_OUT",
            )
        return _format_binding_row(row) if row is not None else None

    out: dict[str, Any] | None = _run_consent_tx(db, tenant_id, _op)
    return out


def enqueue_notification_tx(
    conn: Any,
    *,
    tenant_id: str,
    user_id: str,
    workspace_id: str,
    binding_id: str,
    semantic_event_key: str,
    source_kind: str,
    source_ref: str,
    title: str,
    summary_text: str,
    evidence_urls: list[str] | None = None,
    observed_at: datetime | None = None,
    priority: str = "standard",
    delivery_purpose: str = "alert",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Commit immutable NotificationIntent + DomainEvent + Transactional Outbox in caller's TX."""
    if source_kind not in ("watch", "task", "campaign", "system"):
        raise NotificationError("INVALID_SOURCE_KIND")
    if priority not in ("critical", "standard", "low"):
        raise NotificationError("INVALID_PRIORITY")
    clean_key = semantic_event_key.strip()
    clean_title = redact_secrets(title.strip())[:200]
    clean_summary = redact_secrets(summary_text.strip())[:4000]
    if not clean_key or not clean_title or not clean_summary:
        raise NotificationError("INVALID_NOTIFICATION_CONTENT")

    b = conn.execute(
        "SELECT id, binding_version, consent_granted, consent_version, opted_out_at, revoked_at,"
        " quiet_start_hour, quiet_end_hour, timezone_id"
        " FROM agent.notification_bindings"
        " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND workspace_id=%s FOR SHARE",
        (tenant_id, binding_id, user_id, workspace_id),
    ).fetchone()
    if b is None:
        raise NotificationError("BINDING_NOT_FOUND", http_status=404)
    if not b["consent_granted"] or b["opted_out_at"] is not None or b["revoked_at"] is not None:
        raise NotificationError("NOTIFICATION_CONSENT_REQUIRED", http_status=403)

    inc_row = conn.execute("SELECT restore_incarnation FROM agent.schema_control").fetchone()
    assert inc_row is not None
    incarnation = int(inc_row["restore_incarnation"])

    effective_now = now_utc or utcnow()
    effective_obs = observed_at or effective_now
    safe_urls = [str(u).strip()[:1024] for u in (evidence_urls or []) if str(u).strip()][:16]

    quiet_until = (
        None
        if priority == "critical"
        else compute_quiet_until(
            effective_now,
            timezone_id=str(b["timezone_id"]),
            quiet_start_hour=b["quiet_start_hour"],
            quiet_end_hour=b["quiet_end_hour"],
        )
    )
    init_status = "DEFERRED_QUIET_HOURS" if quiet_until is not None else "QUEUED"
    not_before = quiet_until or effective_now

    intent_id = uuid7()
    row = conn.execute(
        "INSERT INTO agent.notification_intents("
        " tenant_id, id, owner_user_id, workspace_id, binding_id, binding_version, consent_version,"
        " semantic_event_key, delivery_purpose, source_kind, source_ref, evidence_urls_json, observed_at,"
        " priority, title, summary_text, status, not_before_at, next_attempt_at, restore_incarnation)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (tenant_id, owner_user_id, binding_id, semantic_event_key, delivery_purpose) DO NOTHING"
        " RETURNING *",
        (
            tenant_id,
            intent_id,
            user_id,
            workspace_id,
            binding_id,
            int(b["binding_version"]),
            int(b["consent_version"]),
            clean_key,
            delivery_purpose,
            source_kind,
            source_ref[:200],
            Jsonb(safe_urls),
            effective_obs,
            priority,
            clean_title,
            clean_summary,
            init_status,
            not_before,
            not_before,
            incarnation,
        ),
    ).fetchone()
    if row is None:
        existing = conn.execute(
            "SELECT * FROM agent.notification_intents"
            " WHERE tenant_id=%s AND owner_user_id=%s AND binding_id=%s"
            " AND semantic_event_key=%s AND delivery_purpose=%s",
            (tenant_id, user_id, binding_id, clean_key, delivery_purpose),
        ).fetchone()
        assert existing is not None
        return _format_intent_row(existing, deduplicated=True)

    outbox_payload = {
        "schemaVersion": "1.0",
        "intentId": intent_id,
        "bindingId": binding_id,
        "sourceKind": source_kind,
        "sourceRef": source_ref[:200],
        "status": init_status,
    }
    ev_id = events.append_event(
        conn,
        tenant_id=tenant_id,
        aggregate_type="notification",
        aggregate_id=intent_id,
        kind="notification.queued",
        payload={"intentId": intent_id, "bindingId": binding_id, "status": init_status, "priority": priority},
        actor={"type": "user", "id": user_id},
        owner_user_id=user_id,
        correlation_id=intent_id,
    )
    conn.execute(
        "INSERT INTO agent.outbox_messages(tenant_id, id, topic, dedupe_key, event_id, safe_payload_json,"
        " payload_sha256, restore_incarnation, next_attempt_at)"
        " VALUES (%s,%s,'notification',%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            tenant_id,
            uuid7(),
            f"notify:{intent_id}",
            ev_id,
            Jsonb(outbox_payload),
            sha256_hex(outbox_payload),
            incarnation,
            not_before,
        ),
    )
    return _format_intent_row(row, deduplicated=False)


def _format_intent_row(r: Row, *, deduplicated: bool = False) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "bindingId": str(r["binding_id"]),
        "bindingVersion": int(r["binding_version"]),
        "consentVersion": int(r["consent_version"]),
        "semanticEventKey": str(r["semantic_event_key"]),
        "deliveryPurpose": str(r["delivery_purpose"]),
        "sourceKind": str(r["source_kind"]),
        "sourceRef": str(r["source_ref"]),
        "evidenceUrls": list(r["evidence_urls_json"] or []),
        "observedAt": r["observed_at"].isoformat(),
        "priority": str(r["priority"]),
        "title": str(r["title"]),
        "summaryText": str(r["summary_text"]),
        "status": str(r["status"]),
        "attemptCount": int(r["attempt_count"]),
        "lastErrorCode": r["last_error_code"],
        "providerReceiptRef": r["provider_receipt_ref"],
        "notBeforeAt": r["not_before_at"].isoformat(),
        "deliveredAt": r["delivered_at"].isoformat() if r["delivered_at"] else None,
        "readAt": r["read_at"].isoformat() if r["read_at"] else None,
        "createdAt": r["created_at"].isoformat(),
        "deduplicated": deduplicated,
    }


def list_notifications(db: Database, *, tenant_id: str, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, 200))
    with db.tx(tenant=tenant_id) as c:
        rows = c.execute(
            "SELECT * FROM agent.notification_intents"
            " WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, user_id, bounded),
        ).fetchall()
        return [_format_intent_row(r) for r in rows]


def mark_notification_read(db: Database, *, tenant_id: str, user_id: str, notification_id: str) -> dict[str, Any]:
    """Only PROVIDER_ACCEPTED (or idempotent READ) may transition to READ by authenticated owner (NOT-016)."""
    with db.tx(tenant=tenant_id) as c:
        row = c.execute(
            "SELECT * FROM agent.notification_intents WHERE tenant_id=%s AND id=%s AND owner_user_id=%s FOR UPDATE",
            (tenant_id, notification_id, user_id),
        ).fetchone()
        if row is None:
            raise NotificationError("NOTIFICATION_NOT_FOUND", http_status=404)
        st = str(row["status"])
        if st == "READ":
            return _format_intent_row(row)
        if st != "PROVIDER_ACCEPTED":
            raise NotificationError("NOTIFICATION_NOT_DELIVERED", http_status=409)
        upd = c.execute(
            "UPDATE agent.notification_intents SET status='READ', read_at=now(), updated_at=now()"
            " WHERE tenant_id=%s AND id=%s RETURNING *",
            (tenant_id, notification_id),
        ).fetchone()
        assert upd is not None
        return _format_intent_row(upd)


def preview_notification_dry_run(
    db: Database,
    *,
    tenant_id: str,
    user_id: str,
    workspace_id: str,
    binding_ids: list[str] | None = None,
    all_consented_bindings: bool = False,
    priority: str = "standard",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Read-only preview of notification delivery targets, consent, and quiet-hours without any DB mutation or send."""
    if priority not in ("critical", "standard", "low"):
        raise NotificationError("INVALID_PRIORITY")
    effective_now = now_utc or utcnow()
    with db.tx(tenant=tenant_id) as c:
        if binding_ids:
            rows = c.execute(
                "SELECT * FROM agent.notification_bindings"
                " WHERE tenant_id=%s AND owner_user_id=%s AND workspace_id=%s AND id = ANY(%s)"
                " ORDER BY created_at DESC",
                (tenant_id, user_id, workspace_id, binding_ids),
            ).fetchall()
            found = {str(r["id"]) for r in rows}
            for bid in binding_ids:
                if bid not in found:
                    raise NotificationError("BINDING_NOT_FOUND", http_status=404)
        elif all_consented_bindings:
            rows = c.execute(
                "SELECT * FROM agent.notification_bindings"
                " WHERE tenant_id=%s AND owner_user_id=%s AND workspace_id=%s"
                " AND consent_granted=true AND opted_out_at IS NULL AND revoked_at IS NULL"
                " ORDER BY created_at DESC",
                (tenant_id, user_id, workspace_id),
            ).fetchall()
        else:
            raise NotificationError("BINDING_SELECTION_REQUIRED", http_status=422)

    recipients: list[dict[str, Any]] = []
    sendable_count = 0
    for r in rows:
        blocked_reason: str | None = None
        if r["revoked_at"] is not None:
            blocked_reason = "BINDING_REVOKED"
        elif r["opted_out_at"] is not None:
            blocked_reason = "BINDING_OPTED_OUT"
        elif not bool(r["consent_granted"]):
            blocked_reason = "CONSENT_NOT_GRANTED"
        quiet_until = (
            None
            if priority == "critical"
            else compute_quiet_until(
                effective_now,
                timezone_id=str(r["timezone_id"]),
                quiet_start_hour=r["quiet_start_hour"],
                quiet_end_hour=r["quiet_end_hour"],
            )
        )
        eligible = blocked_reason is None
        if eligible:
            sendable_count += 1
        recipients.append(
            {
                "bindingId": str(r["id"]),
                "label": str(r["label"]),
                "channel": str(r["channel"]),
                "redactedTarget": str(r["redacted_target"]),
                "eligible": eligible,
                "blockedReason": blocked_reason,
                "wouldDeferUntil": quiet_until.isoformat() if quiet_until is not None else None,
            }
        )
    return {
        "dryRun": True,
        "evaluatedAt": effective_now.isoformat(),
        "recipientCount": len(recipients),
        "sendableCount": sendable_count,
        "recipients": recipients,
    }


def preview_pending_dispatch(
    db: Database,
    *,
    tenant_id: str,
    user_id: str,
    workspace_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Read-only preview of pending QUEUED/DEFERRED notifications scoped strictly to owner+workspace (P2-3)."""
    bounded = max(1, min(limit, 50))
    with db.tx(tenant=tenant_id) as c:
        rows = c.execute(
            "SELECT i.id AS intent_id, i.title, i.priority, i.status, i.not_before_at, i.binding_id,"
            " b.label, b.channel, b.redacted_target, b.consent_granted, b.opted_out_at, b.revoked_at"
            " FROM agent.notification_intents i"
            " JOIN agent.notification_bindings b ON b.tenant_id=i.tenant_id AND b.id=i.binding_id"
            " WHERE i.tenant_id=%s AND i.owner_user_id=%s AND i.workspace_id=%s"
            " AND i.status IN ('QUEUED','DEFERRED_QUIET_HOURS')"
            " ORDER BY i.next_attempt_at, i.id LIMIT %s",
            (tenant_id, user_id, workspace_id, bounded),
        ).fetchall()
    items = [
        {
            "intentId": str(r["intent_id"]),
            "bindingId": str(r["binding_id"]),
            "label": str(r["label"]),
            "channel": str(r["channel"]),
            "redactedTarget": str(r["redacted_target"]),
            "title": str(r["title"]),
            "priority": str(r["priority"]),
            "status": str(r["status"]),
            "notBeforeAt": r["not_before_at"].isoformat(),
            "consentActive": bool(r["consent_granted"]) and r["opted_out_at"] is None and r["revoked_at"] is None,
        }
        for r in rows
    ]
    return {"dryRun": True, "pendingCount": len(items), "items": items}


@dataclass(frozen=True)
class NotificationClaim:
    tenant_id: str
    intent_id: str
    owner_user_id: str
    workspace_id: str
    binding_id: str
    channel: str
    destination_scheme: str
    destination_host: str
    destination_port: int
    secret_nonce: bytes
    secret_ciphertext: bytes
    secret_key_id: str
    secret_sha256: str
    source_kind: str
    source_ref: str
    evidence_urls: list[str]
    observed_at_iso: str
    priority: str
    title: str
    summary_text: str
    attempt_id: str
    attempt_no: int
    lease_epoch: int
    lease_owner_ref: str
    send_token: str


class NotificationDeliveryWorker:
    """Claims due NotificationIntents, revalidates consent/quiet-hours/incarnation, and delivers via Sentinel."""

    def __init__(
        self,
        db: Database,
        keyring: Any = None,
        *,
        owner: str = "notify-worker",
        sentinel: Any = None,
        egress_proxy: str | None = None,
        direct_send_fn: DirectSendFn | None = None,
        allow_private_addrs: frozenset[str] = frozenset(),
        resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
        verify_ssl: Any = True,
    ) -> None:
        self._db = db
        self._keyring = keyring
        self._owner = owner
        self._sentinel = sentinel
        self._egress_proxy = egress_proxy
        self._direct_send_fn = direct_send_fn
        self._allow_private = allow_private_addrs
        self._resolver = resolver
        self._verify_ssl = verify_ssl

    def recover_abandoned(
        self,
        *,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> int:
        """Any expired SENDING row (with or without a NOT_STARTED attempt row) transitions to DELIVERY_UNKNOWN."""
        if (owner_user_id is not None or workspace_id is not None) and not tenant_id:
            raise NotificationError("TENANT_REQUIRED_FOR_OWNER_SCOPE", http_status=400)
        clauses = ["i.status='SENDING'", "i.lease_until IS NOT NULL", "i.lease_until <= now()"]
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("i.tenant_id=%s")
            params.append(tenant_id)
        if owner_user_id is not None:
            clauses.append("i.owner_user_id=%s")
            params.append(owner_user_id)
        if workspace_id is not None:
            clauses.append("i.workspace_id=%s")
            params.append(workspace_id)
        where_sql = " AND ".join(clauses)
        with self._db.tx(tenant=tenant_id) as c:
            rows = c.execute(
                "UPDATE agent.notification_intents i"
                " SET status='DELIVERY_UNKNOWN', last_error_code='WORKER_LEASE_EXPIRED', lease_until=NULL, updated_at=now()"
                f" WHERE {where_sql}"
                " RETURNING i.tenant_id, i.id",
                tuple(params),
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE agent.notification_attempts"
                    " SET disposition='UNKNOWN', error_code='WORKER_LEASE_EXPIRED', finished_at=now()"
                    " WHERE tenant_id=%s AND intent_id=%s AND disposition='NOT_STARTED'",
                    (r["tenant_id"], r["id"]),
                )
                c.execute(
                    "UPDATE agent.outbox_messages"
                    " SET dispatched_at=now(), relay_lease_until=NULL, last_error_code='DELIVERY_UNKNOWN'"
                    " WHERE tenant_id=%s AND topic='notification' AND dedupe_key=%s AND dispatched_at IS NULL",
                    (r["tenant_id"], f"notify:{r['id']}"),
                )
            return len(rows)

    def _claim_batch(
        self,
        *,
        batch: int = 20,
        tenant_filter: str | None = None,
        owner_user_id: str | None = None,
        workspace_id: str | None = None,
        intent_ids: Sequence[str] | None = None,
    ) -> list[NotificationClaim]:
        if (owner_user_id is not None or workspace_id is not None) and not tenant_filter:
            raise NotificationError("TENANT_REQUIRED_FOR_OWNER_SCOPE", http_status=400)
        if batch <= 0:
            return []
        if intent_ids is not None and len(intent_ids) == 0:
            return []
        claims: list[NotificationClaim] = []
        with self._db.tx(tenant=tenant_filter) as c:
            inc_row = c.execute("SELECT restore_incarnation FROM agent.schema_control").fetchone()
            assert inc_row is not None
            inc = int(inc_row["restore_incarnation"])
            now_row = c.execute("SELECT now() AS n").fetchone()
            assert now_row is not None
            db_now: datetime = now_row["n"]

            clauses = [
                "i.status IN ('QUEUED','DEFERRED_QUIET_HOURS')",
                "i.not_before_at <= now()",
                "i.next_attempt_at <= now()",
            ]
            params: list[Any] = []
            if tenant_filter is not None:
                clauses.append("i.tenant_id = %s")
                params.append(tenant_filter)
            if owner_user_id is not None:
                clauses.append("i.owner_user_id = %s")
                params.append(owner_user_id)
            if workspace_id is not None:
                clauses.append("i.workspace_id = %s")
                params.append(workspace_id)
            if intent_ids is not None:
                clauses.append("i.id = ANY(%s)")
                params.append([str(x) for x in intent_ids])
            scan_limit = max(batch, 20)
            # Step 1 (Strict lock hierarchy: Binding -> Intent -> Attempt -> Outbox):
            # Discover candidate bindings without locks, lock parent bindings FOR SHARE in deterministic order first,
            # then lock child ready intents ONLY within those already-locked bindings.
            cand_rows = c.execute(
                "SELECT DISTINCT i.tenant_id, i.binding_id FROM agent.notification_intents i"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY i.tenant_id, i.binding_id LIMIT %s",
                (*params, scan_limit),
            ).fetchall()
            if not cand_rows:
                return []
            b_rows = c.execute(
                "SELECT b.* FROM agent.notification_bindings b"
                " JOIN unnest(%s::uuid[], %s::uuid[]) AS k(tid, bid) ON b.tenant_id=k.tid AND b.id=k.bid"
                " ORDER BY b.tenant_id, b.id FOR SHARE OF b",
                ([r["tenant_id"] for r in cand_rows], [r["binding_id"] for r in cand_rows]),
            ).fetchall()
            locked_bindings: dict[tuple[str, str], Row] = {(str(b["tenant_id"]), str(b["id"])): b for b in b_rows}
            if not locked_bindings:
                return []

            rows = c.execute(
                "SELECT i.* FROM agent.notification_intents i"
                f" WHERE {' AND '.join(clauses)} AND i.binding_id = ANY(%s::uuid[])"
                " ORDER BY i.next_attempt_at, i.id LIMIT %s FOR UPDATE OF i SKIP LOCKED",
                (*params, [str(r["binding_id"]) for r in cand_rows], scan_limit),
            ).fetchall()
            for r in rows:
                if len(claims) >= batch:
                    break
                tid = str(r["tenant_id"])
                iid = str(r["id"])
                b = locked_bindings.get((tid, str(r["binding_id"])))
                if b is None:
                    continue
                if int(r["restore_incarnation"]) != inc:
                    c.execute(
                        "UPDATE agent.notification_intents SET status='SUPPRESSED', last_error_code='RESTORE_STALE',"
                        " updated_at=now() WHERE tenant_id=%s AND id=%s",
                        (tid, iid),
                    )
                    c.execute(
                        "UPDATE agent.outbox_messages SET dispatched_at=now(), last_error_code='RESTORE_STALE'"
                        " WHERE tenant_id=%s AND topic='notification' AND dedupe_key=%s AND dispatched_at IS NULL",
                        (tid, f"notify:{iid}"),
                    )
                    continue
                valid_binding = (
                    bool(b["consent_granted"])
                    and b["opted_out_at"] is None
                    and b["revoked_at"] is None
                    and int(b["binding_version"]) == int(r["binding_version"])
                    and int(b["consent_version"]) == int(r["consent_version"])
                    and control.scope_active(c, tid, str(r["owner_user_id"]), str(r["workspace_id"]))
                )
                if not valid_binding:
                    c.execute(
                        "UPDATE agent.notification_intents SET status='SUPPRESSED', last_error_code='CONSENT_OR_SCOPE_INVALID',"
                        " updated_at=now() WHERE tenant_id=%s AND id=%s",
                        (tid, iid),
                    )
                    c.execute(
                        "UPDATE agent.outbox_messages SET dispatched_at=now(), last_error_code='SUPPRESSED'"
                        " WHERE tenant_id=%s AND topic='notification' AND dedupe_key=%s AND dispatched_at IS NULL",
                        (tid, f"notify:{iid}"),
                    )
                    continue
                if str(r["priority"]) != "critical":
                    quiet_until = compute_quiet_until(
                        db_now,
                        timezone_id=str(b["timezone_id"]),
                        quiet_start_hour=b["quiet_start_hour"],
                        quiet_end_hour=b["quiet_end_hour"],
                    )
                    if quiet_until is not None and quiet_until > db_now:
                        c.execute(
                            "UPDATE agent.notification_intents"
                            " SET status='DEFERRED_QUIET_HOURS', not_before_at=%s, next_attempt_at=%s, updated_at=now()"
                            " WHERE tenant_id=%s AND id=%s",
                            (quiet_until, quiet_until, tid, iid),
                        )
                        continue
                new_attempt = int(r["attempt_count"]) + 1
                new_lease = int(r["lease_epoch"]) + 1
                send_token = uuid7()
                attempt_id = uuid7()
                c.execute(
                    "UPDATE agent.notification_intents"
                    " SET status='SENDING', attempt_count=%s, lease_epoch=%s, lease_owner_ref=%s,"
                    " lease_until=now() + make_interval(secs => %s), updated_at=now()"
                    " WHERE tenant_id=%s AND id=%s",
                    (new_attempt, new_lease, self._owner, LEASE_SECONDS, tid, iid),
                )
                c.execute(
                    "INSERT INTO agent.notification_attempts("
                    " tenant_id, id, intent_id, binding_id, attempt_no, send_token, claimed_lease_epoch, disposition)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,'NOT_STARTED')",
                    (tid, attempt_id, iid, str(r["binding_id"]), new_attempt, send_token, new_lease),
                )
                claims.append(
                    NotificationClaim(
                        tenant_id=tid,
                        intent_id=iid,
                        owner_user_id=str(r["owner_user_id"]),
                        workspace_id=str(r["workspace_id"]),
                        binding_id=str(r["binding_id"]),
                        channel=str(b["channel"]),
                        destination_scheme=str(b["destination_scheme"]),
                        destination_host=str(b["destination_host"]),
                        destination_port=int(b["destination_port"]),
                        secret_nonce=bytes(b["secret_nonce"]),
                        secret_ciphertext=bytes(b["secret_ciphertext"]),
                        secret_key_id=str(b["secret_key_id"]),
                        secret_sha256=str(b["secret_sha256"]),
                        source_kind=str(r["source_kind"]),
                        source_ref=str(r["source_ref"]),
                        evidence_urls=list(r["evidence_urls_json"] or []),
                        observed_at_iso=r["observed_at"].isoformat(),
                        priority=str(r["priority"]),
                        title=str(r["title"]),
                        summary_text=str(r["summary_text"]),
                        attempt_id=attempt_id,
                        attempt_no=new_attempt,
                        lease_epoch=new_lease,
                        lease_owner_ref=self._owner,
                        send_token=send_token,
                    )
                )
                if len(claims) >= batch:
                    break
        return claims

    def _verify_claim_lease_active(
        self,
        claim: NotificationClaim,
        *,
        bind_path_sha256: str | None = None,
        bind_body_sha256: str | None = None,
    ) -> bool:
        """Enforce Binding -> Intent -> Attempt lock order, re-verify consent/lease, and optionally bind digests once."""
        with self._db.tx(tenant=claim.tenant_id) as c:
            b = c.execute(
                "SELECT binding_version, consent_granted, consent_version, opted_out_at, revoked_at"
                " FROM agent.notification_bindings WHERE tenant_id=%s AND id=%s FOR SHARE",
                (claim.tenant_id, claim.binding_id),
            ).fetchone()
            row = c.execute(
                "SELECT owner_user_id, workspace_id, binding_id, binding_version, consent_version,"
                " status, lease_epoch, lease_owner_ref,"
                " (lease_until IS NOT NULL AND lease_until > now()) AS lease_valid"
                " FROM agent.notification_intents WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (claim.tenant_id, claim.intent_id),
            ).fetchone()
            valid_scope_and_consent = (
                row is not None
                and str(row["owner_user_id"]) == claim.owner_user_id
                and str(row["workspace_id"]) == claim.workspace_id
                and b is not None
                and bool(b["consent_granted"])
                and b["opted_out_at"] is None
                and b["revoked_at"] is None
                and int(b["binding_version"]) == int(row["binding_version"])
                and int(b["consent_version"]) == int(row["consent_version"])
                and control.scope_active(c, claim.tenant_id, claim.owner_user_id, claim.workspace_id)
            )
            lease_active = (
                row is not None
                and valid_scope_and_consent
                and str(row["status"]) == "SENDING"
                and int(row["lease_epoch"]) == claim.lease_epoch
                and str(row["lease_owner_ref"] or "") == self._owner
                and bool(row["lease_valid"])
            )
            if lease_active:
                if bind_path_sha256 is not None and bind_body_sha256 is not None:
                    bound = c.execute(
                        "UPDATE agent.notification_attempts"
                        " SET request_path_sha256=%s, request_body_sha256=%s"
                        " WHERE tenant_id=%s AND id=%s AND intent_id=%s AND attempt_no=%s"
                        " AND disposition='NOT_STARTED' AND request_path_sha256 IS NULL AND request_body_sha256 IS NULL"
                        " RETURNING id",
                        (
                            bind_path_sha256,
                            bind_body_sha256,
                            claim.tenant_id,
                            claim.attempt_id,
                            claim.intent_id,
                            claim.attempt_no,
                        ),
                    ).fetchone()
                    return bound is not None
                return True
            if not valid_scope_and_consent:
                reason = "CONSENT_OR_SCOPE_INVALID_PRE_SEND"
            elif row is not None and str(row["status"]) == "SENDING" and not bool(row["lease_valid"]):
                reason = "CLAIM_LEASE_EXPIRED_PRE_SEND"
            else:
                reason = f"CLAIM_STALE_{row['status'] if row is not None else 'MISSING'}"
            if (
                row is not None
                and str(row["status"]) == "SENDING"
                and int(row["lease_epoch"]) == claim.lease_epoch
                and str(row["lease_owner_ref"] or "") == self._owner
            ):
                if not valid_scope_and_consent:
                    c.execute(
                        "UPDATE agent.notification_intents"
                        " SET status='SUPPRESSED', last_error_code=%s, lease_until=NULL, updated_at=now()"
                        " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s AND status='SENDING'",
                        (reason, claim.tenant_id, claim.intent_id, claim.lease_epoch),
                    )
                    c.execute(
                        "UPDATE agent.outbox_messages"
                        " SET dispatched_at=now(), relay_lease_until=NULL, last_error_code='SUPPRESSED'"
                        " WHERE tenant_id=%s AND topic='notification' AND dedupe_key=%s AND dispatched_at IS NULL",
                        (claim.tenant_id, f"notify:{claim.intent_id}"),
                    )
                else:
                    # Proven pre-send lease expiry: return cleanly to QUEUED instead of stranding as DELIVERY_UNKNOWN.
                    c.execute(
                        "UPDATE agent.notification_intents"
                        " SET status='QUEUED', last_error_code=%s, lease_until=NULL, next_attempt_at=now(), updated_at=now()"
                        " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s AND status='SENDING'",
                        (reason, claim.tenant_id, claim.intent_id, claim.lease_epoch),
                    )
            c.execute(
                "UPDATE agent.notification_attempts"
                " SET disposition='TRANSPORT_REJECTED', error_code=%s, finished_at=now()"
                " WHERE tenant_id=%s AND intent_id=%s AND attempt_no=%s AND disposition='NOT_STARTED'",
                (reason, claim.tenant_id, claim.intent_id, claim.attempt_no),
            )
            return False

    def _finish_attempt(
        self,
        claim: NotificationClaim,
        *,
        disposition: str,
        intent_status: str,
        receipt_ref: str | None = None,
        error_code: str | None = None,
        retry_delay_s: int | None = None,
    ) -> None:
        with self._db.tx(tenant=claim.tenant_id) as c:
            if intent_status == "PROVIDER_ACCEPTED":
                updated = c.execute(
                    "UPDATE agent.notification_intents"
                    " SET status='PROVIDER_ACCEPTED', provider_receipt_ref=%s, delivered_at=now(),"
                    " last_error_code=NULL, lease_until=NULL, updated_at=now()"
                    " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s AND status='SENDING'"
                    " RETURNING id",
                    (receipt_ref, claim.tenant_id, claim.intent_id, claim.lease_epoch),
                ).fetchone()
            elif intent_status == "QUEUED" and retry_delay_s is not None:
                updated = c.execute(
                    "UPDATE agent.notification_intents"
                    " SET status='QUEUED', last_error_code=%s, lease_until=NULL,"
                    " next_attempt_at=now() + make_interval(secs => %s), updated_at=now()"
                    " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s AND status='SENDING'"
                    " RETURNING id",
                    (error_code, retry_delay_s, claim.tenant_id, claim.intent_id, claim.lease_epoch),
                ).fetchone()
            else:
                updated = c.execute(
                    "UPDATE agent.notification_intents"
                    " SET status=%s, last_error_code=%s, lease_until=NULL, updated_at=now()"
                    " WHERE tenant_id=%s AND id=%s AND lease_epoch=%s AND status='SENDING'"
                    " RETURNING id",
                    (intent_status, error_code, claim.tenant_id, claim.intent_id, claim.lease_epoch),
                ).fetchone()
            c.execute(
                "UPDATE agent.notification_attempts"
                " SET disposition=%s, provider_receipt_ref=%s, error_code=%s, finished_at=now()"
                " WHERE tenant_id=%s AND intent_id=%s AND attempt_no=%s",
                (disposition, receipt_ref, error_code, claim.tenant_id, claim.intent_id, claim.attempt_no),
            )
            if updated is not None and intent_status != "QUEUED":
                c.execute(
                    "UPDATE agent.outbox_messages"
                    " SET dispatched_at=now(), relay_lease_until=NULL, last_error_code=%s"
                    " WHERE tenant_id=%s AND topic='notification' AND dedupe_key=%s AND dispatched_at IS NULL",
                    (
                        None if intent_status == "PROVIDER_ACCEPTED" else (error_code or intent_status),
                        claim.tenant_id,
                        f"notify:{claim.intent_id}",
                    ),
                )

    def _build_request(
        self, claim: NotificationClaim, send_token: str
    ) -> tuple[str, str, str, int, dict[str, str], bytes]:
        sec = decrypt_binding_secret(
            self._keyring,
            nonce=claim.secret_nonce,
            ciphertext=claim.secret_ciphertext,
            key_id=claim.secret_key_id,
            expected_sha256=claim.secret_sha256,
            tenant_id=claim.tenant_id,
            user_id=claim.owner_user_id,
            binding_id=claim.binding_id,
        )
        if claim.channel == "telegram":
            bot_token = sec["botToken"]
            chat_id = sec["chatId"]
            base_url = sec.get("apiBaseUrl") or "https://api.telegram.org"
            lines = [
                f"[{claim.priority.upper()}] {claim.title}",
                claim.summary_text,
                f"Source: {claim.source_kind}:{claim.source_ref}",
                f"ObservedAt: {claim.observed_at_iso}",
            ]
            if claim.evidence_urls:
                lines.append("Evidence: " + ", ".join(claim.evidence_urls[:5]))
            payload = {
                "chat_id": chat_id,
                "text": "\n".join(lines)[:4096],
                "disable_web_page_preview": True,
            }
            target_url = f"{base_url.rstrip('/')}/bot{bot_token}/sendMessage"
            hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            target_url = sec["url"]
            payload = {
                "schemaVersion": "1.0",
                "notificationId": claim.intent_id,
                "sourceKind": claim.source_kind,
                "sourceRef": claim.source_ref,
                "priority": claim.priority,
                "title": claim.title,
                "summary": claim.summary_text,
                "observedAt": claim.observed_at_iso,
                "evidenceUrls": claim.evidence_urls,
            }
            hdrs = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Aiagent-Notification-Id": claim.intent_id,
                "Idempotency-Key": send_token,
            }
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        u = urlsplit(target_url)
        scheme = u.scheme.lower()
        host = (u.hostname or "").lower()
        port = u.port or (443 if scheme == "https" else 80)
        return target_url, scheme, host, port, hdrs, body

    async def _authorize_permit(
        self,
        claim: NotificationClaim,
        *,
        scheme: str,
        host: str,
        port: int,
        target_path: str,
        body_sha256: str,
    ) -> str:
        sentinel = self._sentinel
        if sentinel is None:
            endpoint = os.environ.get("AIAGENT_SENTINEL_ENDPOINT")
            key = config.secret("AIAGENT_API_SENTINEL_RPC_KEY", required=False)
            if endpoint and key is not None:
                from .rpc import RpcClient

                sentinel = RpcClient(endpoint, "api", bytes.fromhex(key.reveal()))
        if sentinel is None:
            raise NotificationError("SENTINEL_REQUIRED", http_status=503)
        if hasattr(sentinel, "authorize_notification"):
            return str(
                await asyncio.to_thread(
                    sentinel.authorize_notification,
                    claim.tenant_id,
                    claim.owner_user_id,
                    claim.workspace_id,
                    claim.intent_id,
                    claim.binding_id,
                    scheme,
                    host,
                    port,
                    "POST",
                    target_path=target_path,
                    body_sha256=body_sha256,
                    attempt_id=claim.attempt_id,
                    attempt_no=claim.attempt_no,
                    lease_epoch=claim.lease_epoch,
                    lease_owner_ref=claim.lease_owner_ref,
                    send_token=claim.send_token,
                )
            )
        res = await sentinel.call(
            "sentinel.authorize_notification",
            {
                "tenantId": claim.tenant_id,
                "userId": claim.owner_user_id,
                "workspaceId": claim.workspace_id,
                "intentId": claim.intent_id,
                "bindingId": claim.binding_id,
                "scheme": scheme,
                "host": host,
                "port": port,
                "method": "POST",
                "targetPath": target_path,
                "bodySha256": body_sha256,
                "attemptId": claim.attempt_id,
                "attemptNo": claim.attempt_no,
                "leaseEpoch": claim.lease_epoch,
                "leaseOwnerRef": claim.lease_owner_ref,
                "sendToken": claim.send_token,
            },
        )
        return str(res["permit"])

    async def _direct_pinned_post(
        self,
        target_url: str,
        scheme: str,
        host: str,
        port: int,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, str]:
        if self._direct_send_fn is not None:
            return await self._direct_send_fn(target_url, "POST", headers, body)
        if config.profile() == "production":
            raise NotificationError("EGRESS_PROXY_REQUIRED", http_status=503)
        u = urlsplit(target_url)
        if self._resolver is not None:
            addrs = await self._resolver(host, port)
        else:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
            addrs = [str(sa[0]).split("%", 1)[0] for *_, sa in infos]
        if not addrs:
            raise ForwardProxyError("EGRESS_DENIED", http_status=403, post_send=False)
        for addr in addrs:
            ip_obj = ipaddress.ip_address(addr.split("%", 1)[0])
            if not ip_obj.is_global and str(ip_obj) not in self._allow_private:
                raise ForwardProxyError("EGRESS_DENIED", http_status=403, post_send=False)
        ip = addrs[0]
        default_port = 443 if scheme == "https" else 80
        host_hdr = host if port == default_port else f"{host}:{port}"
        path_q = (u.path or "/") + (f"?{u.query}" if u.query else "")
        lines = [
            f"POST {path_q} HTTP/1.1",
            f"Host: {host_hdr}",
            "Connection: close",
            f"Content-Length: {len(body)}",
        ]
        for k, v in headers.items():
            if k.lower() not in ("host", "connection", "content-length"):
                lines.append(f"{k}: {v}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        ssl_ctx = self._verify_ssl if isinstance(self._verify_ssl, ssl.SSLContext) else ssl.create_default_context()
        try:
            if scheme == "https":
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=host), timeout=10.0
                )
            else:
                r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=10.0)
        except OSError:
            raise ForwardProxyError("EGRESS_PROXY_UNREACHABLE", http_status=502, post_send=False) from None

        sent = False
        try:
            w.write(raw)
            sent = True
            await asyncio.wait_for(w.drain(), timeout=10.0)
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=10.0)
            status_parts = head[:-4].decode("latin-1").split("\r\n")[0].split(" ", 2)
            code = int(status_parts[1])
            resp_body = await asyncio.wait_for(r.read(262_144), timeout=10.0)
            return code, resp_body.decode("utf-8", "replace")
        except TimeoutError:
            raise ForwardProxyError("DIRECT_TIMEOUT", http_status=504, post_send=sent) from None
        except (OSError, asyncio.IncompleteReadError, ValueError):
            raise ForwardProxyError("DIRECT_TRANSPORT_ERROR", http_status=502, post_send=sent) from None
        finally:
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()

    async def deliver_claim(self, claim: NotificationClaim) -> str:
        send_token = claim.send_token
        try:
            target_url, scheme, host, port, headers, body = self._build_request(claim, send_token)
        except NotificationError as exc:
            self._finish_attempt(claim, disposition="PERMANENT_FAILURE", intent_status="FAILED", error_code=exc.code)
            return "FAILED"

        u = urlsplit(target_url)
        target_path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        path_sha = hashlib.sha256(target_path.encode("utf-8")).hexdigest()
        body_sha = hashlib.sha256(body).hexdigest()

        if not await asyncio.to_thread(
            self._verify_claim_lease_active,
            claim,
            bind_path_sha256=path_sha,
            bind_body_sha256=body_sha,
        ):
            return "LEASE_LOST"

        proxy_url = (
            self._egress_proxy if self._egress_proxy is not None else os.environ.get("AIAGENT_EGRESS_PROXY", "").strip()
        )
        use_proxy = bool(proxy_url) or config.profile() == "production"

        if use_proxy:
            try:
                permit = await self._authorize_permit(
                    claim,
                    scheme=scheme,
                    host=host,
                    port=port,
                    target_path=target_path,
                    body_sha256=body_sha,
                )
            except (RpcError, NotificationError) as exc:
                err_code = exc.code if hasattr(exc, "code") else type(exc).__name__
                retryable = err_code == "SENTINEL_UNAVAILABLE" and claim.attempt_no < MAX_ATTEMPTS
                self._finish_attempt(
                    claim,
                    disposition="TRANSPORT_REJECTED" if retryable else "PERMANENT_FAILURE",
                    intent_status="QUEUED" if retryable else "FAILED",
                    error_code=str(err_code),
                    retry_delay_s=min(60, 2**claim.attempt_no) if retryable else None,
                )
                return "QUEUED" if retryable else "FAILED"
        else:
            permit = ""

        if not await asyncio.to_thread(self._verify_claim_lease_active, claim):
            return "LEASE_LOST"

        if os.environ.get("AIAGENT_NOTIFY_FAULT") == "crash_after_begin":
            raise RuntimeError("simulated crash after transmission began")

        try:
            if use_proxy:
                default_port = 443 if scheme == "https" else 80
                host_hdr = host if port == default_port else f"{host}:{port}"
                status_code, _, resp_text = await send_via_forward_proxy(
                    proxy_url,
                    identity="notification",
                    permit=permit,
                    method="POST",
                    target_url=target_url,
                    host_header=host_hdr,
                    headers=headers,
                    body=body,
                    timeout_s=15.0,
                )
            else:
                status_code, resp_text = await self._direct_pinned_post(target_url, scheme, host, port, headers, body)
        except ForwardProxyError as exc:
            if exc.post_send:
                # ACK lost after sending: NEVER blindly retry (NOT-006).
                self._finish_attempt(
                    claim,
                    disposition="UNKNOWN",
                    intent_status="DELIVERY_UNKNOWN",
                    error_code=exc.code,
                )
                return "DELIVERY_UNKNOWN"
            # Pre-send rejection (egress denied, sentinel denied, or proxy unreachable before dialing)
            retryable = (
                exc.code in ("EGRESS_PROXY_UNREACHABLE", "EGRESS_PROXY_TIMEOUT") and claim.attempt_no < MAX_ATTEMPTS
            )
            self._finish_attempt(
                claim,
                disposition="TRANSPORT_REJECTED" if retryable else "PERMANENT_FAILURE",
                intent_status="QUEUED" if retryable else "FAILED",
                error_code=exc.code,
                retry_delay_s=min(60, 2**claim.attempt_no) if retryable else None,
            )
            return "QUEUED" if retryable else "FAILED"

        if 200 <= status_code < 300:
            receipt = f"http:{status_code}"
            if claim.channel == "telegram":
                try:
                    parsed_tg = json.loads(resp_text)
                    if not parsed_tg.get("ok"):
                        self._finish_attempt(
                            claim,
                            disposition="PERMANENT_FAILURE",
                            intent_status="FAILED",
                            error_code="TELEGRAM_API_ERROR",
                        )
                        return "FAILED"
                    msg_id = (parsed_tg.get("result") or {}).get("message_id")
                    if msg_id is not None:
                        receipt = f"tg:{msg_id}"
                except ValueError:
                    self._finish_attempt(
                        claim,
                        disposition="UNKNOWN",
                        intent_status="DELIVERY_UNKNOWN",
                        error_code="TELEGRAM_INVALID_RESPONSE",
                    )
                    return "DELIVERY_UNKNOWN"
            else:
                with contextlib.suppress(ValueError):
                    parsed_wh = json.loads(resp_text)
                    if isinstance(parsed_wh, dict) and parsed_wh.get("receiptId"):
                        receipt = str(parsed_wh["receiptId"])[:128]
            self._finish_attempt(
                claim,
                disposition="PROVIDER_ACCEPTED",
                intent_status="PROVIDER_ACCEPTED",
                receipt_ref=receipt,
            )
            return "PROVIDER_ACCEPTED"

        # Any post-send non-2xx upstream HTTP response (400/409/429/500/503 etc.) occurred AFTER request
        # transmission; remote side effect is ambiguous, so transition to DELIVERY_UNKNOWN with ZERO auto-retry (BE-P1-1).
        self._finish_attempt(
            claim,
            disposition="UNKNOWN",
            intent_status="DELIVERY_UNKNOWN",
            error_code=f"HTTP_{status_code}",
        )
        return "DELIVERY_UNKNOWN"

    async def run_once(
        self,
        *,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
        workspace_id: str | None = None,
        intent_ids: Sequence[str] | None = None,
        batch: int = 20,
    ) -> dict[str, int]:
        recovered = await asyncio.to_thread(
            self.recover_abandoned,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
        )
        counts: dict[str, int] = {"recoveredUnknown": recovered, "claimed": 0}
        if intent_ids is not None and len(intent_ids) == 0:
            return counts
        bounded = max(1, min(batch, 100))
        for _ in range(bounded):
            claims = await asyncio.to_thread(
                self._claim_batch,
                batch=1,
                tenant_filter=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                intent_ids=intent_ids,
            )
            if not claims:
                break
            if os.environ.get("AIAGENT_NOTIFY_FAULT") == "crash_after_claim":
                raise RuntimeError("simulated crash immediately after batch claim")
            counts["claimed"] += 1
            outcome = await self.deliver_claim(claims[0])
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts
