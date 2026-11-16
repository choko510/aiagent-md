"""Sentinel final authorization (09 sentinel-authority). Issues short-lived egress permits bound to ONE tool intent and
ONE destination (scheme/host/port/method), after re-checking the ledger, approval, run fence/goal/cancel and restore
incarnation. The Trusted Egress proxy verifies the permit again at send time (approval revoked in between -> denied).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from aiagent_core import control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_domain import uuid7
from aiagent_domain.rpcwire import RpcError
from aiagent_domain.tools import TOOLS

from .netpolicy import EgressDenied, normalize_host

PERMIT_TTL_S = 60
_PG_PERMIT_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")


@dataclass(frozen=True)
class DestinationPolicy:
    name: str
    scheme: str
    host: str
    port: int
    methods: frozenset[str]
    allow_private: frozenset[str] = frozenset()
    identities: frozenset[str] = frozenset({"tool_gateway"})  # who may use it
    needs_permit: bool = True  # False only for standing destinations such as model providers


@dataclass
class EgressConfig:
    destinations: dict[str, DestinationPolicy] = field(default_factory=dict)
    static_tokens: dict[str, str] = field(default_factory=dict)  # identity -> token (non-permit identities)

    @classmethod
    def from_json(cls, text: str, *, production: bool) -> EgressConfig:
        raw = json.loads(text)
        out = cls()
        for name, d in raw.get("destinations", {}).items():
            allow_private = frozenset(d.get("allow_private", []))
            if allow_private and production:
                raise ValueError(f"destination {name}: allow_private is forbidden in production")
            if not bool(d.get("needs_permit", True)) and frozenset(d.get("identities", [])) != frozenset(
                {"model_gateway"}
            ):
                raise ValueError(f"destination {name}: static egress is reserved for model_gateway")
            raw_host = str(d["host"]).strip()
            if raw_host == "*":
                if (
                    name != "web-fetch"
                    or not bool(d.get("needs_permit", True))
                    or not set(d["methods"]) <= {"GET", "HEAD"}
                ):
                    raise ValueError(f"destination {name}: wildcard host is restricted to read-only permit web-fetch")
                norm_host = "*"
            else:
                norm_host = normalize_host(raw_host)
            out.destinations[name] = DestinationPolicy(
                name,
                d["scheme"],
                norm_host,
                int(d["port"]),
                frozenset(d["methods"]),
                allow_private,
                frozenset(d.get("identities", ["tool_gateway"])),
                bool(d.get("needs_permit", True)),
            )
        out.static_tokens = dict(raw.get("static_tokens", {}))
        return out

    def match(self, scheme: str, host: str, port: int) -> DestinationPolicy | None:
        for d in self.destinations.values():
            if (d.scheme, d.host, d.port) == (scheme, host, port):
                return d
        for d in self.destinations.values():
            if d.host == "*" and d.scheme in (scheme, "http*") and d.port in (0, port):
                return d
        return None


def _mac(key: bytes, *parts: Any) -> str:
    return hmac.new(key, "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


class Sentinel:
    def __init__(self, db: Database, config: EgressConfig, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("sentinel key must be >= 32 bytes")
        self.db, self.config, self.key = db, config, key
        self._human_lock = threading.Lock()
        self._used_human_nonces: dict[str, int] = {}

    def authorize(self, claim: Claim, intent_id: str, scheme: str, host: str, port: int, method: str) -> str:
        try:
            host = normalize_host(host)
        except EgressDenied as e:
            raise RpcError(e.code) from None
        if not _PG_PERMIT_HOST.fullmatch(host):
            raise RpcError("DESTINATION_NOT_ALLOWED")
        dest = self.config.match(scheme, host, port)
        if dest is None or not dest.needs_permit or method not in dest.methods or "tool_gateway" not in dest.identities:
            raise RpcError("DESTINATION_NOT_ALLOWED")
        with self.db.tx() as c:
            try:
                control.check_current(c, claim)
            except control.ControlError as e:
                raise RpcError(e.code) from None
            it = c.execute(
                "SELECT i.tool_key, i.status_code, i.goal_revision, a.decision_code, a.goal_revision AS apr_goal"
                " FROM agent.tool_intents i LEFT JOIN agent.approval_records a ON a.tenant_id=i.tenant_id"
                " AND a.tool_intent_id=i.id WHERE i.tenant_id=%s AND i.id=%s AND i.run_id=%s",
                (claim.tenant_id, intent_id, claim.run_id),
            ).fetchone()
            if it is None:
                raise RpcError("INTENT_NOT_FOUND")
            spec = TOOLS.get(it["tool_key"])
            if spec is None or dest.name not in spec.egress:
                raise RpcError("DESTINATION_NOT_BOUND_TO_TOOL")
            allowed_states = ("SENDING", "UNKNOWN") if method == "GET" else ("SENDING",)
            if it["status_code"] not in allowed_states:
                raise RpcError("INTENT_NOT_ADMITTED")
            if (
                spec.approval_required
                and method != "GET"
                and (it["decision_code"] != "APPROVED" or it["apr_goal"] != claim.goal_revision)
            ):
                raise RpcError("APPROVAL_INVALID")
            pid = uuid7()
            expires = int(time.time()) + PERMIT_TTL_S
            c.execute(
                "INSERT INTO agent.egress_permits(tenant_id, id, tool_intent_id, run_id, run_lease_epoch, scheme, host,"
                " port, method, max_uses, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1, to_timestamp(%s))",
                (claim.tenant_id, pid, intent_id, claim.run_id, claim.lease_epoch, scheme, host, port, method, expires),
            )
        return f"{claim.tenant_id}.{pid}.{expires}.{_mac(self.key, claim.tenant_id, pid, scheme, host, port, method, expires)}"

    def authorize_reconcile(self, tenant_id: str, intent_id: str, scheme: str, host: str, port: int) -> str:
        """Read-only receipt query for an UNKNOWN effect (operator reconciliation). GET only, never a send."""
        host = normalize_host(host)
        dest = self.config.match(scheme, host, port)
        if dest is None or "GET" not in dest.methods:
            raise RpcError("DESTINATION_NOT_ALLOWED")
        with self.db.tx() as c:
            it = c.execute(
                "SELECT i.tool_key, i.status_code, i.run_id, t.owner_user_id, t.workspace_id"
                " FROM agent.tool_intents i JOIN agent.tasks t ON t.tenant_id=i.tenant_id AND t.id=i.task_id"
                " WHERE i.tenant_id=%s AND i.id=%s",
                (tenant_id, intent_id),
            ).fetchone()
            if it is None or it["status_code"] not in ("UNKNOWN", "SENDING"):
                raise RpcError("INTENT_NOT_RECONCILABLE")
            if not control.scope_active(c, tenant_id, str(it["owner_user_id"]), str(it["workspace_id"])):
                raise RpcError("SCOPE_REVOKED")
            spec = TOOLS.get(it["tool_key"])
            if spec is None or dest.name not in spec.egress:
                raise RpcError("DESTINATION_NOT_BOUND_TO_TOOL")
            pid = uuid7()
            expires = int(time.time()) + PERMIT_TTL_S
            c.execute(
                "INSERT INTO agent.egress_permits(tenant_id, id, tool_intent_id, run_id, run_lease_epoch, scheme, host,"
                " port, method, max_uses, expires_at) SELECT %s,%s,%s,%s, greatest(1, r.lease_epoch), %s,%s,%s,'GET',1,"
                " to_timestamp(%s) FROM agent.runs r WHERE r.tenant_id=%s AND r.id=%s",
                (tenant_id, pid, intent_id, it["run_id"], scheme, host, port, expires, tenant_id, it["run_id"]),
            )
        return f"{tenant_id}.{pid}.{expires}.{_mac(self.key, tenant_id, pid, scheme, host, port, 'GET', expires)}"

    def authorize_human_takeover(
        self,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        scheme: str,
        host: str,
        port: int,
        method: str,
    ) -> str:
        """Issue single-use Human Takeover permit bound to a PAUSED/HUMAN browser session & destination."""
        try:
            host = normalize_host(host)
        except EgressDenied as e:
            raise RpcError(e.code) from None
        if not _PG_PERMIT_HOST.fullmatch(host) or method not in ("GET", "POST"):
            raise RpcError("DESTINATION_NOT_ALLOWED")
        dest = self.config.match(scheme, host, port)
        if dest is None or not dest.needs_permit or method not in dest.methods:
            raise RpcError("DESTINATION_NOT_ALLOWED")
        with self.db.tx() as c:
            row = c.execute(
                "SELECT s.id FROM agent.browser_sessions s"
                " JOIN agent.tasks t ON t.tenant_id=s.tenant_id AND t.id=s.task_id"
                " JOIN agent.runs r ON r.tenant_id=s.tenant_id AND r.id=s.run_id"
                " JOIN agent.schema_control sc ON sc.singleton"
                " JOIN agent.tenants tn ON tn.id=s.tenant_id"
                " JOIN agent.tenant_memberships m ON m.tenant_id=tn.id AND m.user_id=%s"
                " JOIN agent.users u ON u.id=m.user_id"
                " JOIN agent.workspaces w ON w.tenant_id=tn.id AND w.id=%s AND w.owner_user_id=%s"
                " WHERE s.tenant_id=%s AND s.id=%s AND s.owner_user_id=%s AND s.closed_at IS NULL"
                " AND s.mode='HUMAN' AND s.state IN ('PAUSED','NEEDS_LOGIN')"
                " AND s.restore_incarnation=sc.restore_incarnation AND r.restore_incarnation=sc.restore_incarnation"
                " AND t.owner_user_id=%s AND t.workspace_id=%s AND t.active_run_id=s.run_id"
                " AND NOT r.cancel_requested AND r.status_code NOT IN ('CANCELLED','CANCELLING','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')"
                " AND tn.disabled_at IS NULL AND m.disabled_at IS NULL AND u.disabled_at IS NULL AND w.disabled_at IS NULL",
                (user_id, workspace_id, user_id, tenant_id, session_id, user_id, user_id, workspace_id),
            ).fetchone()
            if row is None:
                raise RpcError("TAKEOVER_NOT_AUTHORIZED")
        nonce = secrets.token_hex(12)
        expires = int(time.time()) + PERMIT_TTL_S
        mac = _mac(self.key, "human_takeover", tenant_id, session_id, nonce, scheme, host, port, method, expires)
        return f"ht.{tenant_id}.{session_id}.{nonce}.{expires}.{mac}"

    def verify_human_takeover(self, token: str, scheme: str, host: str, port: int, method: str) -> tuple[str, None]:
        """Verify and consume single-use Human Takeover token at send time inside EgressProxy."""
        parts = token.split(".")
        if len(parts) != 6 or parts[0] != "ht":
            raise EgressDenied("PERMIT_MALFORMED")
        _, tenant, sid, nonce, expires_s, mac = parts
        try:
            expires = int(expires_s)
        except ValueError:
            raise EgressDenied("PERMIT_MALFORMED") from None
        good = _mac(self.key, "human_takeover", tenant, sid, nonce, scheme, host, port, method, expires)
        if not hmac.compare_digest(mac, good):
            raise EgressDenied("PERMIT_BINDING")
        now_ts = int(time.time())
        if expires < now_ts:
            raise EgressDenied("PERMIT_EXPIRED")
        with self._human_lock:
            stale = [k for k, exp in self._used_human_nonces.items() if exp < now_ts]
            for k in stale:
                del self._used_human_nonces[k]
            if nonce in self._used_human_nonces:
                raise EgressDenied("PERMIT_INVALID_AT_SEND")
            self._used_human_nonces[nonce] = expires
        with self.db.tx() as c:
            row = c.execute(
                "SELECT 1 FROM agent.browser_sessions s"
                " JOIN agent.tasks t ON t.tenant_id=s.tenant_id AND t.id=s.task_id"
                " JOIN agent.runs r ON r.tenant_id=s.tenant_id AND r.id=s.run_id"
                " JOIN agent.schema_control sc ON sc.singleton"
                " JOIN agent.tenants tn ON tn.id=t.tenant_id"
                " JOIN agent.tenant_memberships m ON m.tenant_id=tn.id AND m.user_id=t.owner_user_id"
                " JOIN agent.users u ON u.id=m.user_id"
                " JOIN agent.workspaces w ON w.tenant_id=tn.id AND w.id=t.workspace_id AND w.owner_user_id=t.owner_user_id"
                " WHERE s.tenant_id=%s AND s.id=%s AND s.closed_at IS NULL"
                " AND s.mode='HUMAN' AND s.state IN ('PAUSED','NEEDS_LOGIN')"
                " AND s.restore_incarnation=sc.restore_incarnation AND r.restore_incarnation=sc.restore_incarnation"
                " AND t.active_run_id=s.run_id AND NOT r.cancel_requested"
                " AND r.status_code NOT IN ('CANCELLED','CANCELLING','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')"
                " AND tn.disabled_at IS NULL AND m.disabled_at IS NULL AND u.disabled_at IS NULL AND w.disabled_at IS NULL",
                (tenant, sid),
            ).fetchone()
        if row is None:
            raise EgressDenied("PERMIT_INVALID_AT_SEND")
        return tenant, None

    def verify_and_consume(self, token: str, scheme: str, host: str, port: int, method: str) -> tuple[str, str]:
        """Called by the egress proxy at send time. Returns (tenant_id, permit_id) or raises EgressDenied."""
        try:
            tenant, pid, expires_s, mac = token.split(".")
            expires = int(expires_s)
        except ValueError:
            raise EgressDenied("PERMIT_MALFORMED") from None
        good = _mac(self.key, tenant, pid, scheme, host, port, method, expires)
        if not hmac.compare_digest(mac, good):
            raise EgressDenied("PERMIT_BINDING")  # forged, or used for another host/port/method
        if expires < time.time():
            raise EgressDenied("PERMIT_EXPIRED")
        with self.db.tx() as c:
            row = c.execute(
                "UPDATE agent.egress_permits p SET uses = uses + 1 FROM agent.tool_intents i, agent.runs r,"
                " agent.tasks t, agent.schema_control s WHERE p.tenant_id=%s AND p.id=%s"
                " AND p.revoked_at IS NULL AND p.uses < p.max_uses"
                " AND p.expires_at > now() AND i.tenant_id=p.tenant_id AND i.id=p.tool_intent_id"
                " AND r.tenant_id=p.tenant_id AND r.id=p.run_id AND r.restore_incarnation = s.restore_incarnation"
                " AND t.tenant_id=r.tenant_id AND t.id=r.task_id AND i.task_id=t.id"
                " AND EXISTS (SELECT 1 FROM agent.tenants tn"
                " JOIN agent.tenant_memberships m ON m.tenant_id=tn.id AND m.user_id=t.owner_user_id"
                " JOIN agent.users u ON u.id=m.user_id"
                " JOIN agent.workspaces w ON w.tenant_id=tn.id AND w.id=t.workspace_id"
                " AND w.owner_user_id=t.owner_user_id WHERE tn.id=t.tenant_id AND tn.disabled_at IS NULL"
                " AND m.disabled_at IS NULL AND u.disabled_at IS NULL AND w.disabled_at IS NULL)"
                " AND p.method = %s AND p.host = %s AND p.port = %s AND p.scheme = %s"
                " AND NOT r.pause_requested AND r.status_code <> 'PAUSED'"
                " AND (p.method = 'GET' AND i.status_code = 'UNKNOWN' OR (NOT r.cancel_requested"
                " AND r.status_code NOT IN ('CANCELLED','CANCELLING')"
                " AND t.goal_revision=i.goal_revision AND r.lease_epoch = p.run_lease_epoch))"
                " AND (i.status_code = 'SENDING' OR (p.method = 'GET' AND i.status_code = 'UNKNOWN'))"
                " AND NOT EXISTS (SELECT 1 FROM agent.approval_records a WHERE a.tenant_id=i.tenant_id"
                "   AND a.tool_intent_id=i.id AND a.decision_code <> 'APPROVED' AND p.method <> 'GET')"
                " RETURNING p.id",
                (tenant, pid, method, host, port, scheme),
            ).fetchone()
        if row is None:
            raise EgressDenied("PERMIT_INVALID_AT_SEND")
        return tenant, pid

    def receipt(
        self,
        tenant: str | None,
        pid: str | None,
        decision: str,
        reason: str,
        host: str | None,
        port: int | None,
        ip: str | None,
    ) -> None:
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO agent.egress_receipts(tenant_id, permit_id, decision, reason_code, host, port,"
                " resolved_ip) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (tenant, pid, decision, reason, host, port, ip),
            )


def load_config() -> EgressConfig:
    """Destinations from AIAGENT_EGRESS_CONFIG_FILE (non-secret); standing-destination proxy tokens from the secret
    AIAGENT_EGRESS_STATIC_TOKENS (`identity=token,...`), never from the config file."""
    from aiagent_core.config import profile, secret

    path = os.environ.get("AIAGENT_EGRESS_CONFIG_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    else:
        text = os.environ.get("AIAGENT_EGRESS_CONFIG", "{}")
    cfg = EgressConfig.from_json(text, production=profile() == "production")
    if cfg.static_tokens and profile() == "production":
        raise ValueError("static proxy tokens must come from AIAGENT_EGRESS_STATIC_TOKENS_FILE, not the config file")
    tokens = secret("AIAGENT_EGRESS_STATIC_TOKENS", required=False)
    if tokens is not None:
        for part in tokens.reveal().replace("\n", ",").split(","):
            ident, _, tok = part.strip().partition("=")
            if ident and tok:
                if len(tok) < 32:
                    raise ValueError("static proxy tokens must be >= 32 characters")
                cfg.static_tokens[ident] = tok
    return cfg
