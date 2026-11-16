"""Phase 6 end-to-end & security boundary tests:
1. HttpOnly Web sessions + CSRF + Origin checks + cross-owner isolation
2. Campaign & Watch Write APIs (CAS control_version, bounded triggers, steer/pause/resume/stop)
3. Notification Bindings (AES-GCM secret protection), Consent/Opt-Out enforcement, Quiet Hours,
   Sentinel Egress Proxy delivery (Webhook & Telegram), SSRF denial, and READ ack gating
4. Notification ACK loss & crash recovery fail-closed DELIVERY_UNKNOWN (zero duplicate resend)
5. Real Playwright Chromium UI E2E against built React Web UI served same-origin with PostgreSQL 16.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from aiagent_api.app import create_app
from aiagent_browser import Authority
from aiagent_browser.pg_fence import PostgresFencePort
from aiagent_context.models import Scope as ContextScope
from aiagent_context.models import SourceInput
from aiagent_core import admin, commands, control, events, memory_tombstones, memory_writer, notifications
from aiagent_core.commands import Scope
from aiagent_core.db import Database
from aiagent_domain import uuid7
from aiagent_gateways.context_artifacts import PostgresArtifactStore
from aiagent_gateways.context_keys import KeyRing
from aiagent_gateways.sentinel_port import LocalSentinel
from aiagent_sentinel.authority import EgressConfig, Sentinel
from aiagent_sentinel.egress import EgressProxy
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege

pytestmark = pytest.mark.pg


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _start_proxy_thread(
    sentinel: Sentinel, ecfg: EgressConfig, resolver: Any
) -> tuple[int, asyncio.AbstractEventLoop, threading.Thread, EgressProxy]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def runner() -> None:
        asyncio.set_event_loop(loop)

        async def boot() -> None:
            proxy = EgressProxy(sentinel, ecfg, resolver=resolver)
            port = await proxy.start("127.0.0.1", 0)
            holder["proxy"] = proxy
            holder["port"] = port
            ready.set()

        loop.run_until_complete(boot())
        loop.run_forever()
        loop.run_until_complete(holder["proxy"].close())
        loop.close()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    assert ready.wait(timeout=10), "egress proxy failed to start"
    return int(holder["port"]), loop, th, holder["proxy"]


def _start_uvicorn_thread(app: Any) -> tuple[int, uvicorn.Server, threading.Thread, socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    th.start()
    deadline = time.time() + 10.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn test server failed to start"
    return port, server, th, sock


def test_web_session_cookie_csrf_origin_and_owner_isolation(
    pg: Any, dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """Cookie session exchange stores only SHA-256, rejects CSRF/cross-origin mutations with 403, and enforces owner isolation."""
    ring = KeyRing.single(b"p" * 32, key_id="p6-auth")
    other = admin.provision_tenant(pg.owner)
    app = create_app(dbs["api"], storage_key_ring=ring, egress_proxy="")

    with TestClient(app) as client:
        # Invalid bearer fails closed 401; cross-origin login request fails 403
        r_bad_auth = client.post("/v1/auth/session", json={"token": "aiag_invalid_token_xyz"})
        assert r_bad_auth.status_code == 401
        r_login_csrf = client.post(
            "/v1/auth/session",
            headers={"Origin": "https://attacker.example"},
            json={"token": tenant["token"]},
        )
        assert r_login_csrf.status_code == 403

        # Exchange bearer token -> HttpOnly Cookie + CSRF token (raw token never stored in web_sessions)
        r_login = client.post("/v1/auth/session", json={"token": tenant["token"]})
        assert r_login.status_code == 201, r_login.text
        sess_data = r_login.json()
        assert sess_data["authenticated"] is True
        assert sess_data["tenantId"] == tenant["tenantId"]
        csrf = sess_data["csrfToken"]
        assert isinstance(csrf, str) and len(csrf) >= 32
        cookie_val = r_login.cookies.get("aiagent_session")
        assert cookie_val and cookie_val != tenant["token"]

        db_row = dbs["owner"].one(
            "SELECT session_sha256, csrf_token FROM agent.web_sessions WHERE tenant_id=%s AND user_id=%s",
            (tenant["tenantId"], tenant["userId"]),
        )
        assert db_row is not None
        assert db_row["session_sha256"] != cookie_val
        assert len(db_row["session_sha256"]) == 64

        # Reload check works with cookie only
        r_get = client.get("/v1/auth/session")
        assert r_get.status_code == 200
        assert r_get.json()["csrfToken"] == csrf

        # Mutation without X-CSRF-Token -> 403 CSRF_INVALID
        r_no_csrf = client.post(
            "/v1/tasks",
            json={"schemaVersion": "1.0", "goal": "blocked without csrf", "profile": "default"},
        )
        assert r_no_csrf.status_code == 403
        assert r_no_csrf.json()["detail"]["error"] == "CSRF_INVALID"

        # Mutation with wrong X-CSRF-Token -> 403 CSRF_INVALID
        r_bad_csrf = client.post(
            "/v1/tasks",
            headers={"X-CSRF-Token": "wrong-" + csrf},
            json={"schemaVersion": "1.0", "goal": "blocked bad csrf", "profile": "default"},
        )
        assert r_bad_csrf.status_code == 403

        # Mutation with cross-origin Origin header -> 403 CSRF_ORIGIN_MISMATCH
        r_cross_origin = client.post(
            "/v1/tasks",
            headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.example"},
            json={"schemaVersion": "1.0", "goal": "blocked cross origin", "profile": "default"},
        )
        assert r_cross_origin.status_code == 403
        assert r_cross_origin.json()["detail"]["error"] == "CSRF_ORIGIN_MISMATCH"

        # Valid same-origin mutation with matching CSRF succeeds
        r_ok = client.post(
            "/v1/tasks",
            headers={"X-CSRF-Token": csrf},
            json={"schemaVersion": "1.0", "goal": "valid session task", "profile": "default"},
        )
        assert r_ok.status_code == 201, r_ok.text
        task_id = r_ok.json()["taskId"]

        # Another owner cannot see this task (404, never 403)
        r_other = client.get(
            f"/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {other['token']}"},
        )
        assert r_other.status_code == 404

        # Logout revokes session server-side; reusing old cookie fails 401
        r_logout = client.delete("/v1/auth/session", headers={"X-CSRF-Token": csrf})
        assert r_logout.status_code == 200
        with TestClient(app, cookies={"aiagent_session": cookie_val}) as stale_client:
            r_after = stale_client.get("/v1/tasks")
            assert r_after.status_code == 401


def test_campaign_and_watch_write_api_cas_and_trigger(dbs: dict[str, Database], tenant: dict[str, str]) -> None:
    """Campaign CRUD + CAS + Steer and Watch registration + bounded trigger -> Notification Outbox."""
    ring = KeyRing.single(b"a" * 32, key_id="p6-auto")
    notify_ring = KeyRing.single(b"b" * 32, key_id="p6-notify-auto")
    app = create_app(dbs["api"], storage_key_ring=ring, notification_key_ring=notify_ring, egress_proxy="")
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    with TestClient(app) as client:
        # 1. Create Campaign -> creates child task with toolsPerCycle limit
        r_create = client.post(
            "/v1/campaigns",
            headers=headers,
            json={
                "goalText": "Collect competitive pricing daily",
                "profile": "default",
                "maxCycles": 4,
                "toolsPerCycle": 7,
                "tokensPerCycle": 2048,
                "firstWorkItemId": "work-alpha",
            },
        )
        assert r_create.status_code == 201, r_create.text
        camp = r_create.json()
        cid = camp["campaignId"]
        assert camp["state"] == "ACTIVE"
        assert camp["controlVersion"] == 1
        assert len(camp["createdRuns"]) == 1

        run_row = dbs["owner"].one(
            "SELECT max_tool_calls FROM agent.runs WHERE id=%s",
            (camp["createdRuns"][0]["runId"],),
        )
        assert run_row is not None and int(run_row["max_tool_calls"]) == 7

        # 2. Pause + Stale CAS rejection + Resume + Steer + Stop
        r_pause = client.post(f"/v1/campaigns/{cid}/pause", headers=headers, json={"expectedControlVersion": 1})
        assert r_pause.status_code == 200, r_pause.text
        assert r_pause.json()["state"] == "PAUSED"
        assert r_pause.json()["controlVersion"] == 2

        r_stale = client.post(f"/v1/campaigns/{cid}/resume", headers=headers, json={"expectedControlVersion": 1})
        assert r_stale.status_code == 409
        assert r_stale.json()["detail"]["error"] == "STALE_CONTROL_VERSION"

        r_resume = client.post(f"/v1/campaigns/{cid}/resume", headers=headers, json={"expectedControlVersion": 2})
        assert r_resume.status_code == 200, r_resume.text
        assert r_resume.json()["state"] == "ACTIVE"

        r_steer = client.post(
            f"/v1/campaigns/{cid}/steer",
            headers=headers,
            json={"expectedControlVersion": 3, "newGoalRevision": "2", "newGoalText": "Focus on JPY pricing"},
        )
        assert r_steer.status_code == 200, r_steer.text
        assert r_steer.json()["goalRevision"] == "2"

        r_stop = client.post(f"/v1/campaigns/{cid}/stop", headers=headers, json={"expectedControlVersion": 4})
        assert r_stop.status_code == 200, r_stop.text
        assert r_stop.json()["state"] == "STOPPED"

        # 3. Create consented Notification Binding + Watch -> trigger claims slot & queues outbox notification
        r_bind = client.post(
            "/v1/notifications/bindings",
            headers=headers,
            json={
                "channel": "webhook",
                "label": "Watch Webhook",
                "destination": {"url": "https://hooks.watch.test:8443/hook/secret99"},
                "consentGranted": True,
                "timezoneId": "Asia/Tokyo",
            },
        )
        assert r_bind.status_code == 201, r_bind.text
        bid = r_bind.json()["binding"]["id"]

        r_watch = client.post(
            "/v1/watches",
            headers=headers,
            json={"timezoneId": "Asia/Tokyo", "localHour": 9, "localMinute": 30, "maxOccurrences": 1},
        )
        assert r_watch.status_code == 201, r_watch.text
        wid = r_watch.json()["watchId"]
        assert r_watch.json()["localHour"] == 9 and r_watch.json()["localMinute"] == 30

        slot_ts = "2026-11-17T00:30:00Z"
        r_trig1 = client.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": slot_ts, "bindingId": bid, "title": "Price Alert"},
        )
        assert r_trig1.status_code == 200, r_trig1.text
        assert r_trig1.json()["claimed"] is True
        assert len(r_trig1.json()["queuedNotifications"]) == 1

        # Second trigger beyond maxOccurrences=1 is bounded -> claimed=False
        r_trig2 = client.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": "2026-11-18T00:30:00Z", "bindingId": bid},
        )
        assert r_trig2.status_code == 200
        assert r_trig2.json()["claimed"] is False

        # Trigger without explicit bindingId/allConsentedBindings fails closed 422 (UI-P1-3)
        r_no_bind = client.post(f"/v1/watches/{wid}/trigger", headers=headers, json={"slotUtc": slot_ts})
        assert r_no_bind.status_code == 422
        assert r_no_bind.json()["detail"]["error"] == "BINDING_SELECTION_REQUIRED"

        # Deactivate watch -> further triggers fail 409 WATCH_INACTIVE
        assert client.delete(f"/v1/watches/{wid}", headers=headers).status_code == 200
        r_trig3 = client.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": "2026-11-19T00:30:00Z", "bindingId": bid},
        )
        assert r_trig3.status_code == 409


def test_notification_bindings_encryption_consent_quiet_hours_and_sentinel_proxy(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify AES-GCM secret isolation, Quiet Hours deferral, Sentinel proxy delivery (Webhook+Telegram), consent revocation suppression, SSRF rejection, and READ ack gating."""
    webhook_hits: list[dict[str, Any]] = []
    telegram_hits: list[dict[str, Any]] = []

    class _ReceiverHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n).decode("utf-8", "replace")
            if self.path == "/hook/secret-path-xyz":
                webhook_hits.append(
                    {
                        "body": json.loads(raw),
                        "idem": self.headers.get("Idempotency-Key"),
                        "nid": self.headers.get("X-Aiagent-Notification-Id"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"receiptId":"wh-rcpt-001"}')
                return
            if self.path == "/bot123456:ABCDEF_SECRET_BOT_TOKEN/sendMessage":
                telegram_hits.append(json.loads(raw))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"result":{"message_id":9001}}')
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ReceiverHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv_port = srv.server_address[1]

    async def _resolve(host: str, port_num: int) -> list[str]:
        del port_num
        if host in ("hooks.notify.test", "tg.notify.test", "blocked.notify.test"):
            return ["127.0.0.1"]
        return []

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "notify-wh": {
                        "scheme": "http",
                        "host": "hooks.notify.test",
                        "port": srv_port,
                        "methods": ["POST"],
                        "allow_private": ["127.0.0.1"],
                    },
                    "notify-tg": {
                        "scheme": "http",
                        "host": "tg.notify.test",
                        "port": srv_port,
                        "methods": ["POST"],
                        "allow_private": ["127.0.0.1"],
                    },
                }
            }
        ),
        production=False,
    )
    sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))
    proxy_port, proxy_loop, proxy_thread, _ = _start_proxy_thread(sentinel, ecfg, _resolve)
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", proxy_url)

    ring = KeyRing.single(b"n" * 32, key_id="p6-n1")
    notify_ring = KeyRing.single(b"N" * 32, key_id="notify-1")
    app = create_app(
        dbs["api"],
        storage_key_ring=ring,
        notification_key_ring=notify_ring,
        sentinel=LocalSentinel(sentinel),
        egress_proxy=proxy_url,
    )
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            # 1. Create Webhook binding WITHOUT consent first
            r_wh = client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Private Hook",
                    "destination": {"url": f"http://hooks.notify.test:{srv_port}/hook/secret-path-xyz"},
                    "consentGranted": False,
                    "timezoneId": "UTC",
                },
            )
            assert r_wh.status_code == 201, r_wh.text
            wh_binding = r_wh.json()["binding"]
            wh_id = wh_binding["id"]
            assert "secret-path-xyz" not in json.dumps(wh_binding)
            assert wh_binding["redactedTarget"].endswith("/***")

            # Raw secret path never exists in DB columns
            raw_db = dbs["owner"].one(
                "SELECT redacted_target, secret_ciphertext FROM agent.notification_bindings WHERE id=%s",
                (wh_id,),
            )
            assert raw_db is not None
            assert b"secret-path-xyz" not in bytes(raw_db["secret_ciphertext"])
            assert "secret-path-xyz" not in str(raw_db["redacted_target"])

            # Enqueue without consent fails 403
            r_no_consent = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": wh_id,
                    "semanticEventKey": "ev-no-consent",
                    "title": "Blocked",
                    "summaryText": "Should fail",
                },
            )
            assert r_no_consent.status_code == 403

            # Grant consent -> enqueue + dispatch via real Sentinel Forward Proxy -> Webhook hit once!
            assert (
                client.post(
                    f"/v1/notifications/bindings/{wh_id}/consent", headers=headers, json={"granted": True}
                ).status_code
                == 200
            )
            r_send = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": wh_id,
                    "semanticEventKey": "ev-dedup-1",
                    "sourceKind": "watch",
                    "sourceRef": "w-99",
                    "title": "Item Found",
                    "summaryText": "Price dropped",
                    "evidenceUrls": ["https://portal.test/item/1"],
                    "dispatch": True,
                },
            )
            assert r_send.status_code == 201, r_send.text
            nid_wh = r_send.json()["notification"]["id"]
            assert r_send.json()["dispatch"]["PROVIDER_ACCEPTED"] == 1
            assert len(webhook_hits) == 1
            assert webhook_hits[0]["body"]["evidenceUrls"] == ["https://portal.test/item/1"]

            # Duplicate semanticEventKey does not enqueue or re-send!
            r_dup = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": wh_id,
                    "semanticEventKey": "ev-dedup-1",
                    "title": "Item Found Again",
                    "summaryText": "Dup",
                    "dispatch": True,
                },
            )
            assert r_dup.status_code == 201
            assert r_dup.json()["notification"]["deduplicated"] is True
            assert len(webhook_hits) == 1

            # READ ack transitions PROVIDER_ACCEPTED -> READ
            r_read = client.post(f"/v1/notifications/{nid_wh}/read", headers=headers)
            assert r_read.status_code == 200
            assert r_read.json()["notification"]["status"] == "READ"

            # 2. Telegram binding via Sentinel Forward Proxy
            r_tg = client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "telegram",
                    "label": "My Telegram",
                    "destination": {
                        "botToken": "123456:ABCDEF_SECRET_BOT_TOKEN",
                        "chatId": "987654321",
                        "apiBaseUrl": f"http://tg.notify.test:{srv_port}",
                    },
                    "consentGranted": True,
                },
            )
            assert r_tg.status_code == 201, r_tg.text
            tg_b = r_tg.json()["binding"]
            assert "SECRET_BOT_TOKEN" not in json.dumps(tg_b)
            assert tg_b["redactedTarget"] == "telegram:***4321"

            r_tg_n = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": tg_b["id"],
                    "semanticEventKey": "tg-1",
                    "title": "TG Hello",
                    "summaryText": "Watch alert",
                    "dispatch": True,
                },
            )
            assert r_tg_n.status_code == 201, r_tg_n.text
            assert r_tg_n.json()["dispatch"]["PROVIDER_ACCEPTED"] == 1
            assert len(telegram_hits) == 1
            assert telegram_hits[0]["chat_id"] == "987654321"

            # 3. Consent revocation suppresses queued notifications (zero sends after revoke)
            r_q_before = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": wh_id,
                    "semanticEventKey": "ev-suppressed-after-revoke",
                    "title": "Pending",
                    "summaryText": "Should be suppressed",
                    "dispatch": False,
                },
            )
            assert r_q_before.status_code == 201
            sup_id = r_q_before.json()["notification"]["id"]
            # Cannot mark QUEUED notification as READ (409)
            assert client.post(f"/v1/notifications/{sup_id}/read", headers=headers).status_code == 409

            assert (
                client.post(
                    f"/v1/notifications/bindings/{wh_id}/consent", headers=headers, json={"granted": False}
                ).status_code
                == 200
            )
            assert client.post("/v1/notifications/dispatch", headers=headers).status_code == 200
            assert len(webhook_hits) == 1  # Zero extra sends!

            sup_row = dbs["owner"].one("SELECT status FROM agent.notification_intents WHERE id=%s", (sup_id,))
            assert sup_row is not None and sup_row["status"] == "SUPPRESSED"

            # 4. Quiet hours deferral for standard priority vs immediate for critical
            cur_utc = dt.datetime.now(dt.UTC)
            q_start = cur_utc.hour
            q_end = (cur_utc.hour + 2) % 24
            r_quiet_b = client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Quiet Hook",
                    "destination": {"url": f"http://hooks.notify.test:{srv_port}/hook/secret-path-xyz"},
                    "consentGranted": True,
                    "quietStartHour": q_start,
                    "quietEndHour": q_end,
                    "timezoneId": "UTC",
                },
            )
            assert r_quiet_b.status_code == 201, r_quiet_b.text
            qb_id = r_quiet_b.json()["binding"]["id"]

            r_std = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": qb_id,
                    "semanticEventKey": "quiet-std",
                    "title": "Standard Quiet",
                    "summaryText": "Deferred",
                    "priority": "standard",
                    "dispatch": True,
                },
            )
            assert r_std.status_code == 201
            assert r_std.json()["notification"]["status"] == "DEFERRED_QUIET_HOURS"
            assert len(webhook_hits) == 1

            r_crit = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": qb_id,
                    "semanticEventKey": "quiet-crit",
                    "title": "Critical Override",
                    "summaryText": "Immediate",
                    "priority": "critical",
                    "dispatch": True,
                },
            )
            assert r_crit.status_code == 201
            assert r_crit.json()["dispatch"]["PROVIDER_ACCEPTED"] == 1
            assert len(webhook_hits) == 2

            # 5. Disallowed host / SSRF rejected at Sentinel before sending
            r_ssrf_b = client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Blocked Host",
                    "destination": {"url": f"http://blocked.notify.test:{srv_port}/hook/secret-path-xyz"},
                    "consentGranted": True,
                },
            )
            assert r_ssrf_b.status_code == 201
            r_ssrf_n = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": r_ssrf_b.json()["binding"]["id"],
                    "semanticEventKey": "ssrf-1",
                    "title": "SSRF Test",
                    "summaryText": "Denied",
                    "dispatch": True,
                },
            )
            assert r_ssrf_n.status_code == 201
            assert r_ssrf_n.json()["dispatch"]["FAILED"] == 1
    finally:
        proxy_loop.call_soon_threadsafe(proxy_loop.stop)
        proxy_thread.join(timeout=5)
        srv.shutdown()
        srv.server_close()


def test_notification_ack_loss_and_crash_recovery_fail_closed_unknown(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-send disconnect marks DELIVERY_UNKNOWN (zero auto-resend); post-begin crash lease expiry also recovers to DELIVERY_UNKNOWN."""
    received_bodies: list[bytes] = []

    async def _flaky_target(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=5.0)
            cl = 0
            for ln in head[:-4].decode("latin-1").split("\r\n")[1:]:
                k, _, v = ln.partition(":")
                if k.lower().strip() == "content-length":
                    cl = int(v.strip())
            body = await asyncio.wait_for(r.readexactly(cl), timeout=5.0) if cl else b""
            received_bodies.append(body)
        finally:
            # Close without sending HTTP response -> simulates ACK loss after delivery!
            w.close()

    def _run_raw_socket_test() -> tuple[int, asyncio.AbstractEventLoop, threading.Thread]:
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        holder: dict[str, Any] = {}

        def run() -> None:
            asyncio.set_event_loop(loop)

            async def boot() -> None:
                srv = await asyncio.start_server(_flaky_target, "127.0.0.1", 0)
                holder["srv"] = srv
                holder["port"] = srv.sockets[0].getsockname()[1]
                ready.set()

            loop.run_until_complete(boot())
            loop.run_forever()
            holder["srv"].close()
            loop.run_until_complete(holder["srv"].wait_closed())
            loop.close()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        assert ready.wait(timeout=10)
        return int(holder["port"]), loop, th

    target_port, target_loop, target_th = _run_raw_socket_test()

    async def _resolve(host: str, port_num: int) -> list[str]:
        del port_num
        return ["127.0.0.1"] if host == "flaky.notify.test" else []

    ecfg = EgressConfig.from_json(
        json.dumps(
            {
                "destinations": {
                    "flaky": {
                        "scheme": "http",
                        "host": "flaky.notify.test",
                        "port": target_port,
                        "methods": ["POST"],
                        "allow_private": ["127.0.0.1"],
                    }
                }
            }
        ),
        production=False,
    )
    sentinel = Sentinel(dbs["sentinel"], ecfg, secrets.token_bytes(32))
    proxy_port, proxy_loop, proxy_thread, _ = _start_proxy_thread(sentinel, ecfg, _resolve)
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", proxy_url)

    ring = KeyRing.single(b"u" * 32, key_id="p6-unk")
    notify_ring = KeyRing.single(b"U" * 32, key_id="p6-n-unk")
    app = create_app(
        dbs["api"],
        storage_key_ring=ring,
        notification_key_ring=notify_ring,
        sentinel=LocalSentinel(sentinel),
        egress_proxy=proxy_url,
    )
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            b_res = client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Flaky Webhook",
                    "destination": {"url": f"http://flaky.notify.test:{target_port}/hook"},
                    "consentGranted": True,
                },
            )
            assert b_res.status_code == 201
            bid = b_res.json()["binding"]["id"]

            # 1) ACK loss after send -> DELIVERY_UNKNOWN and ZERO auto-retry on subsequent dispatch
            n1 = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": bid,
                    "semanticEventKey": "ack-loss-1",
                    "title": "Ack Loss",
                    "summaryText": "Should go DELIVERY_UNKNOWN",
                    "dispatch": True,
                },
            )
            assert n1.status_code == 201
            assert n1.json()["dispatch"]["DELIVERY_UNKNOWN"] == 1
            nid1 = n1.json()["notification"]["id"]
            assert len(received_bodies) == 1

            # Another dispatch cycle must NOT re-send DELIVERY_UNKNOWN!
            d2 = client.post("/v1/notifications/dispatch", headers=headers)
            assert d2.status_code == 200
            assert d2.json()["counts"]["claimed"] == 0
            assert len(received_bodies) == 1
            assert client.post(f"/v1/notifications/{nid1}/read", headers=headers).status_code == 409

            # 2) Simulated crash after NOT_STARTED attempt inserted -> recover_abandoned -> DELIVERY_UNKNOWN
            n2 = client.post(
                "/v1/notifications",
                headers=headers,
                json={
                    "bindingId": bid,
                    "semanticEventKey": "crash-1",
                    "title": "Crash Recovery",
                    "summaryText": "Must become DELIVERY_UNKNOWN on recovery",
                    "dispatch": False,
                },
            )
            nid2 = n2.json()["notification"]["id"]
            monkeypatch.setenv("AIAGENT_NOTIFY_FAULT", "crash_after_begin")
            worker = notifications.NotificationDeliveryWorker(
                dbs["api"], notify_ring, sentinel=LocalSentinel(sentinel), egress_proxy=proxy_url
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                _run(worker.run_once(tenant_id=tenant["tenantId"]))
            monkeypatch.delenv("AIAGENT_NOTIFY_FAULT", raising=False)

            # Fast-forward lease expiry in DB and run recovery
            with dbs["owner"].tx() as c:
                c.execute(
                    "UPDATE agent.notification_intents SET lease_until = now() - interval '10 seconds' WHERE id=%s",
                    (nid2,),
                )
            rec = _run(worker.run_once(tenant_id=tenant["tenantId"]))
            assert rec["recoveredUnknown"] == 1
            st2 = dbs["owner"].one("SELECT status FROM agent.notification_intents WHERE id=%s", (nid2,))
            assert st2 is not None and st2["status"] == "DELIVERY_UNKNOWN"
            assert len(received_bodies) == 1
    finally:
        proxy_loop.call_soon_threadsafe(proxy_loop.stop)
        proxy_thread.join(timeout=5)
        target_loop.call_soon_threadsafe(target_loop.stop)
        target_th.join(timeout=5)


def test_web_ui_full_playwright_e2e(
    memory_dbs: dict[str, Database], memory_tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Chromium UI E2E against compiled web/dist served same-origin with PostgreSQL 16."""
    dbs = memory_dbs
    tenant = memory_tenant
    from playwright.async_api import async_playwright

    repo_root = Path(__file__).resolve().parents[2]
    dist_dir = repo_root / "web" / "dist"
    assert (dist_dir / "index.html").is_file(), f"Missing built Web UI at {dist_dir}; run npm run build in web/"

    # Mock external Webhook receiver for UI notification workflow
    ui_wh_hits: list[dict[str, Any]] = []

    class _UiReceiver(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            ui_wh_hits.append(json.loads(self.rfile.read(n).decode("utf-8")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"receiptId":"ui-wh-1"}')

    wh_srv = ThreadingHTTPServer(("127.0.0.1", 0), _UiReceiver)
    threading.Thread(target=wh_srv.serve_forever, daemon=True).start()
    wh_port = wh_srv.server_address[1]

    # Seed one Browser session and one Approval + UNKNOWN effect task for UI interaction checks
    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    seed_ids = commands.submit_task(dbs["api"], scope, "UI approval and UNKNOWN check")
    claim = control.claim_run(dbs["runtime"], tenant["tenantId"], seed_ids["runId"], "ui-e2e-worker")
    intent_unk_id = uuid7()
    intent_apr_id = uuid7()
    approval_id = uuid7()
    req_sha = secrets.token_hex(32)
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
            " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
            " VALUES (%s,%s,%s,%s,'http_message','inv-ui-unk',%s,'{}'::jsonb,'external_irreversible',%s,'UNKNOWN')",
            (tenant["tenantId"], intent_unk_id, seed_ids["taskId"], seed_ids["runId"], req_sha, claim.goal_revision),
        )
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
            " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
            " VALUES (%s,%s,%s,%s,'http_message','inv-ui-apr',%s,'{}'::jsonb,'external_irreversible',%s,'PENDING_APPROVAL')",
            (tenant["tenantId"], intent_apr_id, seed_ids["taskId"], seed_ids["runId"], req_sha, claim.goal_revision),
        )
        c.execute(
            "INSERT INTO agent.approval_records(tenant_id, id, tool_intent_id, task_id, run_id, owner_user_id,"
            " tool_key, request_sha256, goal_revision, safe_summary, expires_at, decision_code, record_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,'http_message',%s,%s,'Send external status alert',now() + interval '1 hour','PENDING',1)",
            (
                tenant["tenantId"],
                approval_id,
                intent_apr_id,
                seed_ids["taskId"],
                seed_ids["runId"],
                tenant["userId"],
                req_sha,
                claim.goal_revision,
            ),
        )
    bsid = uuid7()
    _run(
        PostgresFencePort(dbs["tool_gateway"]).register(
            bsid,
            Authority(
                claim.tenant_id,
                claim.owner_user_id,
                claim.task_id,
                claim.run_id,
                str(claim.goal_revision),
                str(claim.lease_epoch),
                claim.worker_ref,
            ),
        )
    )

    ring = KeyRing.single(b"w" * 32, key_id="p6-ui")
    notify_ring = KeyRing.single(b"W" * 32, key_id="p6-notify-ui")
    tlog_path = tmp_path / "ui_tombstone.jsonl"
    memory_tombstones.TombstoneLog(tlog_path, integrity_key=b"t" * 32).initialize()
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_LOG", str(tlog_path))
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_HMAC_KEY", (b"t" * 32).hex())
    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", "")
    app = create_app(
        dbs["api"],
        storage_key_ring=ring,
        notification_key_ring=notify_ring,
        egress_proxy="",
        notify_allow_private=frozenset({"127.0.0.1"}),
        web_dist_dir=dist_dir,
    )
    api_port, uv_srv, uv_th, uv_sock = _start_uvicorn_thread(app)
    base_url = f"http://127.0.0.1:{api_port}"

    async def _drive_browser() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(base_url, wait_until="networkidle")

                # 1. Login via token -> Cookie session exchange -> input cleared & localStorage empty
                await page.get_by_label("APIトークン").fill(tenant["token"])
                await page.get_by_role("button", name="セッション開始").click()
                await page.get_by_text("AUTHENTICATED").wait_for(timeout=10000)
                assert await page.get_by_label("APIトークン").count() == 0
                ls_len = await page.evaluate("() => window.localStorage.length")
                assert ls_len == 0
                cookies = await context.cookies([base_url])
                sess_cookies = [c for c in cookies if c["name"] == "aiagent_session"]
                assert len(sess_cookies) == 1 and sess_cookies[0]["httpOnly"] is True

                # 2. Page reload preserves session automatically without re-entering token!
                await page.reload(wait_until="networkidle")
                await page.get_by_text("AUTHENTICATED").wait_for(timeout=10000)

                # 3. Tasks tab: open seeded task -> verify goal summary & UNKNOWN warning & confirm pending approval
                await (
                    page.get_by_test_id(f"task-item-{seed_ids['taskId']}")
                    .get_by_text("UI approval and UNKNOWN check")
                    .wait_for(timeout=10000)
                )
                await page.get_by_test_id(f"task-item-{seed_ids['taskId']}").click()
                await page.get_by_test_id("unknown-effect-warning").wait_for(timeout=10000)
                await page.get_by_role("button", name="承認 (APPROVE)").click()
                await page.get_by_test_id("confirm-dialog").wait_for(timeout=10000)
                await page.get_by_test_id("confirm-dialog-confirm").click()
                await page.get_by_text("判定: APPROVED").wait_for(timeout=10000)

                # Create a new task from UI form
                await page.get_by_label("新しいTask目標").fill("UI Created Research Task")
                await page.get_by_role("button", name="Task 送信").click()
                await page.get_by_text("タスクを投入しました。").wait_for(timeout=10000)

                # 4. Memory & Policy tab: Grant then revoke consent for openai, and update Context Policy
                await page.get_by_test_id("tab-memory").click()
                await page.get_by_label("Memory同意送信先").fill("openai")
                await page.get_by_role("button", name="同意付与").click()
                await page.get_by_test_id("consent-openai").wait_for(timeout=10000)
                await page.get_by_test_id("consent-openai").get_by_role("button", name="同意撤回").click()
                await page.get_by_test_id("consent-openai").get_by_text("REVOKED").wait_for(timeout=10000)
                await page.get_by_label("Semantic Verifierモード").select_option("LLM_GATED")
                await page.get_by_role("button", name="Context Policy 保存").click()
                await page.get_by_text("Context Policy を更新しました。").wait_for(timeout=10000)
                await page.get_by_text("mode=LLM_GATED").wait_for(timeout=10000)

                # 5. Browser Takeover tab: observe -> start takeover -> complete
                await page.get_by_test_id("tab-browser").click()
                await page.get_by_test_id(f"browser-session-{bsid}").click()
                await page.get_by_test_id("browser-observation-box").wait_for(timeout=10000)
                await page.get_by_role("button", name="Takeover 開始").click()
                await page.get_by_text("mode=HUMAN").first.wait_for(timeout=10000)
                await page.get_by_role("button", name="Takeover 完了 (AGENT へ返還)").click()
                await page.get_by_text("Takeover を完了しました").wait_for(timeout=10000)

                # 6. Automation & Notify tab: Watch + Campaign + Notification Binding -> send -> delivered -> READ
                await page.get_by_test_id("tab-automation").click()
                await page.get_by_role("button", name="Watch 作成").click()
                await page.get_by_text("Watch を登録しました。").wait_for(timeout=10000)

                await page.get_by_label("Campaign目標").fill("UI Autonomous Monitoring")
                await page.get_by_role("button", name="Campaign 開始").click()
                await page.get_by_text("Campaign を開始しました。").wait_for(timeout=10000)
                await page.get_by_role("button", name="Pause").click()
                await page.get_by_text("Campaign pause を実行しました。").wait_for(timeout=10000)
                await page.get_by_role("button", name="Stop").click()
                await page.get_by_text("Campaign stop を実行しました。").wait_for(timeout=10000)

                await page.get_by_label("通知ラベル").fill("E2E Hook")
                await page.get_by_label("Webhook URL").fill(f"http://127.0.0.1:{wh_port}/ui-hook")
                await page.get_by_role("button", name="Binding 登録").click()
                await page.locator("li", has_text="E2E Hook").wait_for(timeout=10000)

                # Explicit binding required (no auto-pick fallback): send button disabled while empty
                await page.get_by_label("通知タイトル").fill("E2E Alert")
                await page.get_by_label("通知本文").fill("From Playwright UI")
                assert await page.get_by_role("button", name="実際に送信する").last.is_disabled()

                sel = page.get_by_label("送信先Binding", exact=True)
                opt_val = await sel.locator("option", has_text="E2E Hook").get_attribute("value")
                assert opt_val
                await sel.select_option(opt_val)

                # Dry-Run preview does NOT send HTTP or insert DB rows
                await page.get_by_role("button", name="プレビュー (Dry-Run)").last.click()
                await page.get_by_test_id("manual-dryrun-preview").wait_for(timeout=10000)
                assert len(ui_wh_hits) == 0

                # Live send opens confirmation dialog -> confirm sends once -> delivered -> READ
                await page.get_by_role("button", name="実際に送信する").last.click()
                await page.get_by_test_id("confirm-dialog").wait_for(timeout=10000)
                await page.get_by_test_id("confirm-dialog-confirm").click()
                await page.get_by_text("配信先サーバー受理済み（ユーザー未確認）").first.wait_for(timeout=10000)
                assert len(ui_wh_hits) == 1 and ui_wh_hits[0]["title"] == "E2E Alert"

                await page.get_by_role("button", name="既読にする (READ)").first.click()
                await page.get_by_text("ユーザー確認済み (READ)").first.wait_for(timeout=10000)

                # 7. Logout revokes cookie session cleanly
                await page.get_by_role("button", name="ログアウト").click()
                await page.get_by_label("APIトークン").wait_for(timeout=10000)
            finally:
                await context.close()
                await browser.close()

    try:
        _run(_drive_browser())
    finally:
        uv_srv.should_exit = True
        uv_th.join(timeout=5)
        uv_sock.close()
        wh_srv.shutdown()
        wh_srv.server_close()


def test_notification_post_send_non2xx_statuses_become_delivery_unknown_no_retry(
    dbs: dict[str, Database], tenant: dict[str, str]
) -> None:
    """BE-P1-1: Upstream 400/409/429/500/503 after POST transmission ALL transition to DELIVERY_UNKNOWN with 0 auto-retries."""
    status_queue = [400, 409, 429, 503, 500]
    hits: list[str] = []

    class _StatusHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(n)
            hits.append(self.path)
            code = status_queue.pop(0) if status_queue else 200
            self.send_response(code)
            self.end_headers()
            self.wfile.write(f"upstream-{code}".encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv_port = srv.server_address[1]

    notify_ring = KeyRing.single(b"k" * 32, key_id="notify-non2xx")
    app = create_app(
        dbs["api"],
        notification_key_ring=notify_ring,
        egress_proxy="",
        notify_allow_private=frozenset({"127.0.0.1"}),
    )
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    try:
        with TestClient(app) as client:
            rb = client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Status Test Hook",
                    "destination": {"url": f"http://127.0.0.1:{srv_port}/hook"},
                    "consentGranted": True,
                },
            )
            assert rb.status_code == 201, rb.text
            bid = rb.json()["binding"]["id"]

            intent_ids: list[str] = []
            for idx, expected_code in enumerate((400, 409, 429, 503, 500)):
                rn = client.post(
                    "/v1/notifications",
                    headers=headers,
                    json={
                        "bindingId": bid,
                        "semanticEventKey": f"non2xx-{expected_code}-{idx}",
                        "title": f"Status {expected_code}",
                        "summaryText": "Ambiguous post-send status",
                        "dispatch": True,
                    },
                )
                assert rn.status_code == 201, rn.text
                body = rn.json()
                assert body["dispatch"].get("DELIVERY_UNKNOWN") == 1, body
                intent_ids.append(body["notification"]["id"])

            assert len(hits) == 5
            # Subsequent manual dispatch cycle must NEVER re-send any of those 5 DELIVERY_UNKNOWN intents
            r_disp = client.post("/v1/notifications/dispatch", headers=headers)
            assert r_disp.status_code == 200
            assert r_disp.json()["counts"]["claimed"] == 0
            assert len(hits) == 5

            rows = dbs["owner"].all(
                "SELECT status, last_error_code, attempt_count FROM agent.notification_intents"
                " WHERE tenant_id=%s AND id = ANY(%s) ORDER BY created_at",
                (tenant["tenantId"], intent_ids),
            )
            assert [r["status"] for r in rows] == ["DELIVERY_UNKNOWN"] * 5
            assert [r["attempt_count"] for r in rows] == [1] * 5
            att_disps = dbs["owner"].all(
                "SELECT disposition FROM agent.notification_attempts WHERE tenant_id=%s AND intent_id = ANY(%s)",
                (tenant["tenantId"], intent_ids),
            )
            assert [a["disposition"] for a in att_disps] == ["UNKNOWN"] * 5
    finally:
        srv.shutdown()
        srv.server_close()


def test_notification_crash_recovery_key_isolation_rotation_suppressed_race_and_owner_scope(
    pg: Any, dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-P1-2, BE-P1-3, BE-P1-5, BE-P2-1, BE-P2-2: DB grants, separate key ring + rotation, crash_after_claim,
    delayed ACK vs SUPPRESSED CAS, and same-tenant cross-owner dispatch isolation."""
    wh_hits: list[dict[str, Any]] = []

    class _Hook(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            wh_hits.append(json.loads(self.rfile.read(n).decode("utf-8")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"receiptId":"rot-ok"}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv_port = srv.server_address[1]

    try:
        # 1. BE-P1-3: Low-privilege roles cannot access encrypted notification secrets
        for tbl in ("notification_bindings", "notification_intents", "notification_attempts"):
            with pytest.raises(InsufficientPrivilege):
                dbs["tool_gateway"].all(f"SELECT id FROM agent.{tbl} LIMIT 1")
            with pytest.raises(InsufficientPrivilege):
                dbs["relay"].all(f"SELECT id FROM agent.{tbl} LIMIT 1")
        with pytest.raises(InsufficientPrivilege):
            dbs["sentinel"].all("SELECT secret_ciphertext FROM agent.notification_bindings LIMIT 1")

        # 2. BE-P1-3: Browser storage key MUST NOT encrypt/decrypt notifications
        browser_key_hex = (b"B" * 32).hex()
        monkeypatch.setenv(
            "AIAGENT_BROWSER_STORAGE_KEYS", json.dumps({"active": "b1", "keys": {"b1": browser_key_hex}})
        )
        monkeypatch.delenv("AIAGENT_NOTIFICATION_SECRET_KEYS", raising=False)
        app_no_notify_key = create_app(
            dbs["api"],
            storage_key_ring=KeyRing.single(b"B" * 32, key_id="b1"),
            egress_proxy="",
            notify_allow_private=frozenset({"127.0.0.1"}),
        )
        headers_a = {"Authorization": f"Bearer {tenant['token']}"}
        with TestClient(app_no_notify_key) as cli_nokey:
            r_nokey = cli_nokey.post(
                "/v1/notifications/bindings",
                headers=headers_a,
                json={
                    "channel": "webhook",
                    "label": "Must Fail Without Dedicated Key",
                    "destination": {"url": f"http://127.0.0.1:{srv_port}/hook"},
                    "consentGranted": True,
                },
            )
            assert r_nokey.status_code == 503
            assert r_nokey.json()["error"] == "NOTIFICATION_KEY_UNAVAILABLE"

        # 3. BE-P2-2: Dedicated AIAGENT_NOTIFICATION_SECRET_KEYS rotation preserves decrypt of notify-1 under active notify-2
        k1_hex = (b"1" * 32).hex()
        k2_hex = (b"2" * 32).hex()
        monkeypatch.setenv(
            "AIAGENT_NOTIFICATION_SECRET_KEYS", json.dumps({"active": "notify-1", "keys": {"notify-1": k1_hex}})
        )
        app_env = create_app(
            dbs["api"],
            egress_proxy="",
            notify_allow_private=frozenset({"127.0.0.1"}),
        )
        with TestClient(app_env) as cli_rot:
            r_b1 = cli_rot.post(
                "/v1/notifications/bindings",
                headers=headers_a,
                json={
                    "channel": "webhook",
                    "label": "Rotated Key Hook",
                    "destination": {"url": f"http://127.0.0.1:{srv_port}/hook"},
                    "consentGranted": True,
                },
            )
            assert r_b1.status_code == 201, r_b1.text
            b1_id = r_b1.json()["binding"]["id"]

            # Rotate active key to notify-2 while retaining notify-1
            monkeypatch.setenv(
                "AIAGENT_NOTIFICATION_SECRET_KEYS",
                json.dumps({"active": "notify-2", "keys": {"notify-1": k1_hex, "notify-2": k2_hex}}),
            )
            r_send_old = cli_rot.post(
                "/v1/notifications",
                headers=headers_a,
                json={
                    "bindingId": b1_id,
                    "semanticEventKey": "rot-old-key",
                    "title": "Rotation Works",
                    "summaryText": "Decrypts notify-1 binding under active notify-2",
                    "dispatch": True,
                },
            )
            assert r_send_old.status_code == 201, r_send_old.text
            assert r_send_old.json()["dispatch"]["PROVIDER_ACCEPTED"] == 1
            assert len(wh_hits) == 1

            # 4. BE-P1-5: Same-tenant user B CANNOT dispatch user A's queued notifications
            user_b = admin.add_user(pg.owner, tenant["tenantId"])
            headers_b = {"Authorization": f"Bearer {user_b['token']}"}
            r_qa = cli_rot.post(
                "/v1/notifications",
                headers=headers_a,
                json={
                    "bindingId": b1_id,
                    "semanticEventKey": "owner-scope-a",
                    "title": "Only Owner A",
                    "summaryText": "User B must not dispatch",
                    "dispatch": False,
                },
            )
            assert r_qa.status_code == 201
            qa_id = r_qa.json()["notification"]["id"]

            r_disp_b = cli_rot.post("/v1/notifications/dispatch", headers=headers_b)
            assert r_disp_b.status_code == 200
            assert r_disp_b.json()["counts"]["claimed"] == 0
            st_a = dbs["owner"].one("SELECT status FROM agent.notification_intents WHERE id=%s", (qa_id,))
            assert st_a is not None and st_a["status"] == "QUEUED"

            # 5. BE-P1-2: crash_after_claim atomic TX inserts NOT_STARTED attempt and lease expiry recovers to DELIVERY_UNKNOWN
            monkeypatch.setenv("AIAGENT_NOTIFY_FAULT", "crash_after_claim")
            with pytest.raises(RuntimeError, match="simulated crash immediately after batch claim"):
                cli_rot.post("/v1/notifications/dispatch", headers=headers_a)
            monkeypatch.delenv("AIAGENT_NOTIFY_FAULT", raising=False)

            after_crash_row = dbs["owner"].one(
                "SELECT status, lease_until FROM agent.notification_intents WHERE id=%s",
                (qa_id,),
            )
            assert after_crash_row is not None and after_crash_row["status"] == "SENDING"
            att_row = dbs["owner"].one(
                "SELECT disposition FROM agent.notification_attempts WHERE intent_id=%s AND attempt_no=1",
                (qa_id,),
            )
            assert att_row is not None and att_row["disposition"] == "NOT_STARTED"

            with dbs["owner"].tx() as c:
                c.execute(
                    "UPDATE agent.notification_intents SET lease_until = now() - interval '5 seconds' WHERE id=%s",
                    (qa_id,),
                )
            r_rec = cli_rot.post("/v1/notifications/dispatch", headers=headers_a)
            assert r_rec.status_code == 200
            assert r_rec.json()["counts"]["recoveredUnknown"] >= 1
            recovered_row = dbs["owner"].one(
                "SELECT status FROM agent.notification_intents WHERE id=%s",
                (qa_id,),
            )
            assert recovered_row is not None and recovered_row["status"] == "DELIVERY_UNKNOWN"

            # 6. BE-P2-1: Concurrent SUPPRESSED during in-flight send is NEVER overwritten by late PROVIDER_ACCEPTED
            r_race = cli_rot.post(
                "/v1/notifications",
                headers=headers_a,
                json={
                    "bindingId": b1_id,
                    "semanticEventKey": "suppressed-race-1",
                    "title": "Race Test",
                    "summaryText": "Must remain SUPPRESSED",
                    "dispatch": False,
                },
            )
            assert r_race.status_code == 201
            race_id = r_race.json()["notification"]["id"]

            worker = notifications.NotificationDeliveryWorker(dbs["api"])
            claims = worker._claim_batch(tenant_filter=tenant["tenantId"], owner_user_id=tenant["userId"])
            target_claims = [cl for cl in claims if cl.intent_id == race_id]
            assert len(target_claims) == 1
            # Owner opts out while in-flight -> sets intent SUPPRESSED
            assert cli_rot.post(f"/v1/notifications/bindings/{b1_id}/opt-out", headers=headers_a).status_code == 200
            # Late ACK arrives from provider and calls _finish_attempt
            worker._finish_attempt(
                target_claims[0],
                disposition="PROVIDER_ACCEPTED",
                intent_status="PROVIDER_ACCEPTED",
                receipt_ref="late-ack-999",
            )
            race_row = dbs["owner"].one(
                "SELECT status, provider_receipt_ref FROM agent.notification_intents WHERE id=%s",
                (race_id,),
            )
            assert race_row is not None and race_row["status"] == "SUPPRESSED"
            assert race_row["provider_receipt_ref"] is None
            race_att = dbs["owner"].one(
                "SELECT disposition, provider_receipt_ref FROM agent.notification_attempts WHERE intent_id=%s AND attempt_no=1",
                (race_id,),
            )
            assert race_att is not None and race_att["disposition"] == "PROVIDER_ACCEPTED"
    finally:
        monkeypatch.delenv("AIAGENT_NOTIFY_FAULT", raising=False)
        monkeypatch.delenv("AIAGENT_NOTIFICATION_SECRET_KEYS", raising=False)
        monkeypatch.delenv("AIAGENT_BROWSER_STORAGE_KEYS", raising=False)
        srv.shutdown()
        srv.server_close()


def test_watch_atomic_rollback_and_dryrun_and_validation_422(
    dbs: dict[str, Database], tenant: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE-P1-4, BE-P2-4, UI-P1-2, UI-P1-3: Watch claim+enqueue rollback leaves 0 PENDING slots; DryRun=0 writes; invalid inputs->422."""
    notify_ring = KeyRing.single(b"w" * 32, key_id="notify-watch-tx")
    app = create_app(
        dbs["api"],
        notification_key_ring=notify_ring,
        egress_proxy="",
        notify_allow_private=frozenset({"127.0.0.1"}),
    )
    headers = {"Authorization": f"Bearer {tenant['token']}"}

    with TestClient(app) as client:
        rw = client.post(
            "/v1/watches",
            headers=headers,
            json={"timezoneId": "UTC", "localHour": 10, "localMinute": 0, "maxOccurrences": 1},
        )
        assert rw.status_code == 201, rw.text
        wid = rw.json()["watchId"]

        # Invalid timestamps / boolean quiet hours -> HTTP 422 (BE-P2-4)
        assert (
            client.post(
                f"/v1/watches/{wid}/trigger",
                headers=headers,
                json={"slotUtc": "not-a-timestamp", "allConsentedBindings": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Bad Hour",
                    "destination": {"url": "http://127.0.0.1:9999/h"},
                    "quietStartHour": True,
                    "quietEndHour": 7,
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/notifications/bindings",
                headers=headers,
                json={
                    "channel": "webhook",
                    "label": "Bad Str Hour",
                    "destination": {"url": "http://127.0.0.1:9999/h"},
                    "quietStartHour": "abc",
                    "quietEndHour": 7,
                },
            ).status_code
            == 422
        )

        # 0 active bindings with allConsentedBindings=True rolls back claim_bounded_tx -> maxOccurrences=1 preserved!
        r0 = client.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": "2026-11-17T10:00:00Z", "allConsentedBindings": True},
        )
        assert r0.status_code == 422
        slots_after_0 = dbs["owner"].all(
            "SELECT status FROM agent.automation_watch_slots WHERE tenant_id=%s AND watch_id=%s",
            (tenant["tenantId"], wid),
        )
        assert slots_after_0 == []

        # Create active consented binding
        rb = client.post(
            "/v1/notifications/bindings",
            headers=headers,
            json={
                "channel": "webhook",
                "label": "Atomic Hook",
                "destination": {"url": "http://127.0.0.1:19999/hook"},
                "consentGranted": True,
            },
        )
        assert rb.status_code == 201
        bid = rb.json()["binding"]["id"]

        # Dry-Run preview on Watch & Manual -> ZERO DB rows created!
        r_dry_w = client.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": "2026-11-17T10:00:00Z", "bindingId": bid, "dryRun": True},
        )
        assert r_dry_w.status_code == 200, r_dry_w.text
        assert r_dry_w.json()["dryRun"] is True and r_dry_w.json()["preview"]["sendableCount"] == 1
        assert dbs["owner"].all("SELECT status FROM agent.automation_watch_slots WHERE watch_id=%s", (wid,)) == []

        r_dry_m = client.post(
            "/v1/notifications",
            headers=headers,
            json={"bindingId": bid, "title": "Dry", "summaryText": "Check", "dryRun": True, "observedAt": "bad-date"},
        )
        assert r_dry_m.status_code == 422
        r_dry_m_ok = client.post(
            "/v1/notifications",
            headers=headers,
            json={"bindingId": bid, "title": "Dry", "summaryText": "Check", "dryRun": True},
        )
        assert r_dry_m_ok.status_code == 201 and r_dry_m_ok.json()["dryRun"] is True
        assert dbs["owner"].all("SELECT id FROM agent.notification_intents WHERE binding_id=%s", (bid,)) == []

        # Crash inside watch trigger after claim -> entire TX rolls back (0 PENDING slots, budget still 1 available)
        monkeypatch.setenv("AIAGENT_WATCH_FAULT", "fail_after_claim")
        cli_raise = TestClient(app, raise_server_exceptions=False)
        r_fault = cli_raise.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": "2026-11-17T10:00:00Z", "bindingId": bid},
        )
        assert r_fault.status_code == 500
        monkeypatch.delenv("AIAGENT_WATCH_FAULT", raising=False)
        assert dbs["owner"].all("SELECT status FROM agent.automation_watch_slots WHERE watch_id=%s", (wid,)) == []

        # Next live trigger succeeds using the unconsumed maxOccurrences=1 budget!
        r_live = client.post(
            f"/v1/watches/{wid}/trigger",
            headers=headers,
            json={"slotUtc": "2026-11-17T10:00:00Z", "bindingId": bid},
        )
        assert r_live.status_code == 200, r_live.text
        assert r_live.json()["claimed"] is True
        slots_final = dbs["owner"].all("SELECT status FROM agent.automation_watch_slots WHERE watch_id=%s", (wid,))
        assert [s["status"] for s in slots_final] == ["SENT"]


def test_web_ui_regression_sse_races_confirm_mobile_and_a11y(
    memory_dbs: dict[str, Database], memory_tenant: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI-7..UI-13: SSE live updates, Task/Browser switch race protection, form error preservation,
    logout failure protection, ConfirmDialog cancel 0-network, 320px/375px/768px mobile width, ARIA tabs."""
    dbs = memory_dbs
    tenant = memory_tenant
    from playwright.async_api import async_playwright

    repo_root = Path(__file__).resolve().parents[2]
    dist_dir = repo_root / "web" / "dist"
    assert (dist_dir / "index.html").is_file()

    scope = Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"])
    t_sse = commands.submit_task(dbs["api"], scope, "SSE Live Task A")
    t_other = commands.submit_task(dbs["api"], scope, "Static Task B")
    control.claim_run(dbs["runtime"], tenant["tenantId"], t_sse["runId"], "sse-worker")
    claim_b = control.claim_run(dbs["runtime"], tenant["tenantId"], t_other["runId"], "static-worker")
    apr_id = uuid7()
    int_id = uuid7()
    req_sha = secrets.token_hex(32)
    with dbs["owner"].tx() as c:
        c.execute(
            "INSERT INTO agent.tool_intents(tenant_id, id, task_id, run_id, tool_key, invocation_key,"
            " request_sha256, safe_arguments_json, risk_class, goal_revision, status_code)"
            " VALUES (%s,%s,%s,%s,'http_message','inv-reg-apr',%s,'{}'::jsonb,'external_irreversible',%s,'PENDING_APPROVAL')",
            (tenant["tenantId"], int_id, t_other["taskId"], t_other["runId"], req_sha, claim_b.goal_revision),
        )
        c.execute(
            "INSERT INTO agent.approval_records(tenant_id, id, tool_intent_id, task_id, run_id, owner_user_id,"
            " tool_key, request_sha256, goal_revision, safe_summary, expires_at, decision_code, record_version)"
            " VALUES (%s,%s,%s,%s,%s,%s,'http_message',%s,%s,'Confirm cancel regression',now() + interval '1 hour','PENDING',1)",
            (
                tenant["tenantId"],
                apr_id,
                int_id,
                t_other["taskId"],
                t_other["runId"],
                tenant["userId"],
                req_sha,
                claim_b.goal_revision,
            ),
        )
    tlog_path = tmp_path / "ui_reg_tomb.jsonl"
    memory_tombstones.TombstoneLog(tlog_path, integrity_key=b"t" * 32).initialize()
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_LOG", str(tlog_path))
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_HMAC_KEY", (b"t" * 32).hex())
    ctx_scope_b = ContextScope(claim_b.tenant_id, claim_b.owner_user_id, claim_b.task_id, claim_b.run_id)
    art_store = PostgresArtifactStore(
        dbs["model_gateway"], claim_b, b"k" * 32, expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    )
    src_art = art_store.put_raw(ctx_scope_b, SourceInput("source", "2026-11-16T00:00:00Z", b"src", locator_ref="s1"))
    proj_art = art_store.put_raw(
        ctx_scope_b, SourceInput("projection", "2026-11-16T00:00:00Z", b"proj", locator_ref="p1")
    )
    memory_writer.grant_memory_consent(dbs["api"], scope, recipient="anthropic", consent_basis="user_explicit")
    memory_writer.create_memory_with_status(
        dbs["api"],
        scope,
        task_id=t_other["taskId"],
        source_ref=src_art.artifact_ref,
        projection_ref=proj_art.artifact_ref,
        recipient="anthropic",
        observed_at=dt.datetime.now(dt.UTC),
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=30),
        consent_basis="user_explicit",
        idempotency_key="ui-reg-mem-1",
    )

    bs_a = uuid7()
    bs_b = uuid7()
    for bsid, url in ((bs_a, "https://session-a.example/page"), (bs_b, "https://session-b.example/page")):
        claim_x = control.claim_run(
            dbs["runtime"],
            tenant["tenantId"],
            commands.submit_task(dbs["api"], scope, f"Browser {url}")["runId"],
            "br-w",
        )
        _run(
            PostgresFencePort(dbs["tool_gateway"]).register(
                bsid,
                Authority(
                    claim_x.tenant_id,
                    claim_x.owner_user_id,
                    claim_x.task_id,
                    claim_x.run_id,
                    str(claim_x.goal_revision),
                    str(claim_x.lease_epoch),
                    claim_x.worker_ref,
                ),
            )
        )
        with dbs["owner"].tx() as c:
            c.execute(
                "UPDATE agent.browser_sessions SET current_url=%s, last_observation_text=%s WHERE id=%s",
                (url, f"OBS_FOR_{url}", bsid),
            )

    monkeypatch.setenv("AIAGENT_EGRESS_PROXY", "")
    app = create_app(
        dbs["api"],
        storage_key_ring=KeyRing.single(b"s" * 32, key_id="reg-st"),
        notification_key_ring=KeyRing.single(b"n" * 32, key_id="reg-nt"),
        egress_proxy="",
        notify_allow_private=frozenset({"127.0.0.1"}),
        web_dist_dir=dist_dir,
    )
    api_port, uv_srv, uv_th, uv_sock = _start_uvicorn_thread(app)
    base_url = f"http://127.0.0.1:{api_port}"

    async def _drive() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(base_url, wait_until="networkidle")
                await page.get_by_label("APIトークン").fill(tenant["token"])
                await page.get_by_role("button", name="セッション開始").click()
                await page.get_by_text("AUTHENTICATED").wait_for(timeout=10000)

                # UI-7: SSE live state transition updates task detail without reload
                await page.get_by_test_id(f"task-item-{t_sse['taskId']}").click()
                await page.get_by_test_id("task-detail-panel").wait_for(timeout=10000)

                def _complete_task_bg() -> None:
                    time.sleep(0.35)
                    with dbs["owner"].tx() as c:
                        c.execute(
                            "UPDATE agent.tasks SET status_code='SUCCEEDED', updated_at=now() WHERE id=%s",
                            (t_sse["taskId"],),
                        )
                        events.append_event(
                            c,
                            tenant_id=tenant["tenantId"],
                            aggregate_type="task",
                            aggregate_id=t_sse["taskId"],
                            kind="task.state_changed",
                            payload={"fromState": "RUNNING", "toState": "SUCCEEDED", "reason": "sse-live"},
                            actor={"kind": "worker", "id": "sse-worker"},
                            owner_user_id=tenant["userId"],
                            task_id=t_sse["taskId"],
                            run_id=t_sse["runId"],
                        )

                bg = threading.Thread(target=_complete_task_bg, daemon=True)
                bg.start()
                await page.get_by_test_id("task-detail-panel").get_by_text("SUCCEEDED").first.wait_for(timeout=10000)
                await (
                    page.get_by_test_id(f"task-item-{t_sse['taskId']}")
                    .get_by_text("[SUCCEEDED]")
                    .wait_for(timeout=10000)
                )
                await page.get_by_test_id("task-sse-log").get_by_text("sse-live").wait_for(timeout=10000)

                # UI-11: ConfirmDialog Cancel -> 0 API requests on Approval
                mut_calls: list[str] = []

                async def _track_approval(route: Any) -> None:
                    mut_calls.append(route.request.url)
                    await route.continue_()

                await page.route("**/v1/approvals/*/decision", _track_approval)
                await page.get_by_test_id(f"task-item-{t_other['taskId']}").click()
                await page.get_by_role("button", name="承認 (APPROVE)").click()
                await page.get_by_test_id("confirm-dialog").wait_for(timeout=10000)
                # UI-13: Escape closes confirm dialog cleanly with 0 network calls
                await page.keyboard.press("Escape")
                assert await page.get_by_test_id("confirm-dialog").count() == 0
                assert mut_calls == []
                await page.unroute("**/v1/approvals/*/decision")

                # UI-9: Form preservation on 422 Task submit error
                await page.get_by_label("新しいTask目標").fill("Preserve Me On 422")

                async def _fail_task_422(route: Any) -> None:
                    if route.request.method == "POST":
                        await route.fulfill(
                            status=422, content_type="application/json", body='{"detail":{"error":"BAD_INPUT"}}'
                        )
                    else:
                        await route.continue_()

                await page.route("**/v1/tasks", _fail_task_422)
                await page.get_by_role("button", name="Task 送信").click()
                await page.get_by_test_id("console-error-banner").wait_for(timeout=10000)
                assert await page.get_by_label("新しいTask目標").input_value() == "Preserve Me On 422"
                assert await page.get_by_test_id("console-status-banner").count() == 0
                await page.unroute("**/v1/tasks")

                # UI-13: ARIA Tabs keyboard navigation (ArrowRight, End, Home)
                tab_tasks = page.get_by_test_id("tab-tasks")
                await tab_tasks.focus()
                await page.keyboard.press("ArrowRight")
                assert await page.get_by_test_id("tab-memory").get_attribute("aria-selected") == "true"
                await page.keyboard.press("End")
                assert await page.get_by_test_id("tab-automation").get_attribute("aria-selected") == "true"
                await page.keyboard.press("Home")
                assert await page.get_by_test_id("tab-tasks").get_attribute("aria-selected") == "true"

                # UI-11: Memory delete ConfirmDialog cancel -> 0 DELETE requests
                await page.get_by_test_id("tab-memory").click()
                mem_deletes: list[str] = []

                async def _track_mem_del(route: Any) -> None:
                    if route.request.method == "DELETE":
                        mem_deletes.append(route.request.url)
                    await route.continue_()

                await page.route("**/v1/tasks/*/memory/*", _track_mem_del)
                await page.get_by_role("button", name="削除（復元時再適用を防止）").first.click()
                await page.get_by_test_id("confirm-dialog-cancel").click()
                assert mem_deletes == []
                await page.unroute("**/v1/tasks/*/memory/*")

                # UI-10: Browser Session A/B fast switch where Session A observation responds late
                await page.get_by_test_id("tab-browser").click()

                async def _delay_a(route: Any) -> None:
                    await asyncio.sleep(0.35)
                    await route.continue_()

                await page.route(f"**/v1/browser/sessions/{bs_a}/observation", _delay_a)
                await page.get_by_test_id(f"browser-session-{bs_a}").click()
                await page.get_by_test_id(f"browser-session-{bs_b}").click()
                await (
                    page.get_by_test_id("browser-observation-box")
                    .get_by_text("OBS_FOR_https://session-b.example/page")
                    .wait_for(timeout=10000)
                )
                await asyncio.sleep(0.45)
                obs_text = await page.get_by_test_id("browser-observation-box").inner_text()
                assert "OBS_FOR_https://session-b.example/page" in obs_text
                assert "OBS_FOR_https://session-a.example/page" not in obs_text
                await page.unroute(f"**/v1/browser/sessions/{bs_a}/observation")

                # UI-12: Mobile responsive width check (320px / 375px / 768px) -> 0 horizontal overflow
                for vw in (320, 375, 768):
                    await page.set_viewport_size({"width": vw, "height": 800})
                    for tname in ("tab-tasks", "tab-memory", "tab-browser", "tab-automation"):
                        await page.get_by_test_id(tname).click()
                        fits = await page.evaluate(
                            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                        )
                        assert fits, f"Horizontal overflow detected at width={vw}px on {tname}"
                await page.set_viewport_size({"width": 1280, "height": 900})

                # UI-9 / BE-P2-3: DELETE /v1/auth/session 500 keeps session & shows error
                async def _fail_logout_500(route: Any) -> None:
                    if route.request.method == "DELETE":
                        await route.fulfill(
                            status=500, content_type="application/json", body='{"detail":{"error":"REV_ERR"}}'
                        )
                    else:
                        await route.continue_()

                await page.route("**/v1/auth/session", _fail_logout_500)
                await page.get_by_role("button", name="ログアウト").click()
                await page.get_by_test_id("console-error-banner").wait_for(timeout=10000)
                assert await page.get_by_text("AUTHENTICATED").count() == 1
                assert await page.get_by_test_id("console-status-banner").count() == 0
                await page.unroute("**/v1/auth/session")

                # Clean logout succeeds once backend recovers
                await page.get_by_role("button", name="ログアウト").click()
                await page.get_by_label("APIトークン").wait_for(timeout=10000)
            finally:
                await context.close()
                await browser.close()

    try:
        _run(_drive())
    finally:
        uv_srv.should_exit = True
        uv_th.join(timeout=5)
        uv_sock.close()
