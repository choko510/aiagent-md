"""STEP 3-E deployment gate on a running Compose stack (dev override), checked from INSIDE the containers:
only Sentinel has an outbound route; runtime/gateways/cell cannot open sockets to the Internet, cannot resolve public
names, and cannot reach the fake external world except through Sentinel; no container sees the Docker socket; and the
whole API -> worker -> approval -> egress -> receiver flow works over the real network layout.

  sudo sh deploy/scripts/gen-secrets.sh && sh deploy/docker/build.sh   # or build aiagent/all + aiagent/cell
  docker compose -p aiagentdev -f deploy/compose/compose.yaml -f deploy/compose/compose.dev.yaml up -d
  AIAGENT_COMPOSE_PROJECT=aiagentdev pytest -m compose tests/test_compose_isolation.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.compose
PROJECT = os.environ.get("AIAGENT_COMPOSE_PROJECT")
COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose"
if not PROJECT:
    pytest.skip("AIAGENT_COMPOSE_PROJECT not set (compose stack not running)", allow_module_level=True)

PROBE = (
    "import socket,sys\n"
    "def t(h,p):\n"
    "  try:\n"
    "    socket.create_connection((h,p),timeout=4).close(); return 'OPEN'\n"
    "  except OSError as e: return 'BLOCKED'\n"
    "def d(h):\n"
    "  try: socket.getaddrinfo(h,443); return 'RESOLVED'\n"
    "  except OSError: return 'NODNS'\n"
    "print(t(sys.argv[1], int(sys.argv[2])), d('example.com'))"
)


def dc(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(COMPOSE / "compose.yaml"),
            "-f",  # type: ignore[list-item]
            str(COMPOSE / "compose.dev.yaml"),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def probe(service: str, host: str, port: int) -> str:
    r = dc("exec", "-T", service, "python", "-c", PROBE, host, str(port))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("service", ["runtime", "model-gateway", "tool-gateway", "api", "relay", "cell", "receiver"])
def test_no_direct_internet_route(service: str) -> None:
    out = probe(service, "1.1.1.1", 443)
    assert out.startswith("BLOCKED") and out.endswith("NODNS"), (service, out)


def test_sentinel_is_the_only_egress() -> None:
    assert probe("sentinel", "1.1.1.1", 443).startswith("OPEN")


@pytest.mark.parametrize("service", ["runtime", "model-gateway", "tool-gateway"])
def test_fake_world_unreachable_except_via_sentinel(service: str) -> None:
    assert probe(service, "10.250.250.10", 8099).startswith("BLOCKED")
    assert probe("sentinel", "10.250.250.10", 8099).startswith("OPEN")


@pytest.mark.parametrize("service", ["runtime", "model-gateway", "tool-gateway", "sentinel", "api", "relay"])
def test_hardening(service: str) -> None:
    r = dc(
        "exec",
        "-T",
        service,
        "python",
        "-c",
        "import os,json; print(json.dumps({'uid': os.getuid(), 'sock': os.path.exists('/var/run/docker.sock'),"
        " 'rofs': not os.access('/opt/venv', os.W_OK)}))",
    )
    info = json.loads(r.stdout.strip())
    assert info == {"uid": 10001, "sock": False, "rofs": True}, (service, info)


def test_e2e_over_compose_network() -> None:
    r = dc("run", "--rm", "--no-deps", "--entrypoint", "python", "migrate", "-m", "aiagent_core.admin", "tenant")
    assert r.returncode == 0, r.stderr
    token = next(x.split("=", 1)[1].split()[0] for x in r.stdout.splitlines() if x.startswith("token="))
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url="http://127.0.0.1:8080", timeout=10) as c:
        task = c.post(
            "/v1/tasks", headers=h, json={"schemaVersion": "1.0", "goal": "Find the widget price and notify alice."}
        ).json()["taskId"]

        def wait(pred: object, timeout: float = 90) -> dict:
            end = time.time() + timeout
            while time.time() < end:
                st = c.get(f"/v1/tasks/{task}", headers=h).json()
                if pred(st):  # type: ignore[operator]
                    return st
                time.sleep(1)
            raise AssertionError(st)

        st = wait(lambda s: s["run"]["state"] == "WAITING_APPROVAL")
        a = st["approvals"][0]
        assert (
            c.post(
                f"/v1/approvals/{a['id']}/decision",
                headers=h,
                json={
                    "schemaVersion": "1.0",
                    "decision": "APPROVED",
                    "requestSha256": a["requestSha256"],
                    "expectedRecordVersion": a["recordVersion"],
                },
            ).status_code
            == 200
        )
        st = wait(lambda s: s["state"] in ("SUCCEEDED", "FAILED", "WAITING_RECONCILIATION"))
        assert st["state"] == "SUCCEEDED" and st["run"]["result"] == "done:CONFIRMED", st
        sb = c.post(
            "/v1/tasks",
            headers=h,
            json={"schemaVersion": "1.0", "goal": "Use the sandbox to compute 6*7.", "profile": "sandbox"},
        ).json()["taskId"]
        task = sb
        st = wait(lambda s: s["state"] in ("SUCCEEDED", "FAILED"))
        assert st["run"]["result"] == "done:sandbox:CONFIRMED", st
    log = dc("exec", "-T", "receiver", "cat", "/tmp/receiver.jsonl").stdout.strip().splitlines()
    assert any(json.loads(x)["body"] == {"to": "alice", "body": "price=42"} for x in log)


def test_browser_takeover_revisit_over_compose_network() -> None:
    """Real unmodified Compose stack E2E: Browser navigate -> Human Takeover via Sentinel -> AES-GCM storageState -> Agent revisit."""
    r = dc("run", "--rm", "--no-deps", "--entrypoint", "python", "migrate", "-m", "aiagent_core.admin", "tenant")
    assert r.returncode == 0, r.stderr
    token = next(x.split("=", 1)[1].split()[0] for x in r.stdout.splitlines() if x.startswith("token="))
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url="http://127.0.0.1:8080", timeout=20) as c:
        created = c.post(
            "/v1/tasks",
            headers=h,
            json={"schemaVersion": "1.0", "goal": "Run compose browser portal revisit", "profile": "browser"},
        )
        assert created.status_code in (200, 201), created.text
        task = created.json()["taskId"]

        def wait(pred: object, timeout: float = 90) -> dict:
            st: dict = {}
            end = time.time() + timeout
            while time.time() < end:
                st = c.get(f"/v1/tasks/{task}", headers=h).json()
                if pred(st):  # type: ignore[operator]
                    return st
                time.sleep(1)
            raise AssertionError(st)

        st = wait(lambda s: s["run"]["state"] == "WAITING_APPROVAL")
        a = st["approvals"][0]

        sessions = c.get("/v1/browser/sessions", headers=h)
        assert sessions.status_code == 200, sessions.text
        bs = next(item for item in sessions.json()["items"] if item["taskId"] == task)
        sid = bs["id"]
        assert bs["state"] == "ACTIVE" and bs["mode"] == "AGENT", bs

        # Human Takeover start -> authenticate via API + Sentinel EgressProxy -> AES-GCM storageState -> complete
        assert (
            c.post(f"/v1/browser/sessions/{sid}/takeover/start", headers=h, json={"reason": "login"}).status_code == 200
        )
        auth_r = c.post(
            f"/v1/browser/sessions/{sid}/takeover/authenticate",
            headers=h,
            json={
                "url": "http://portal:8099/portal/login",
                "method": "POST",
                "formData": {"pw": "COMPOSE_PASS_99"},
                "verifyUrl": "http://portal:8099/portal/dashboard",
            },
        )
        assert auth_r.status_code == 200, auth_r.text
        auth_body = auth_r.json()
        assert auth_body["authenticated"] is True and auth_body["authStatus"] == "VERIFIED", auth_body
        assert auth_body["cookieCount"] >= 1, auth_body

        sessions_after = c.get("/v1/browser/sessions", headers=h).json()["items"]
        bs_after = next(item for item in sessions_after if item["id"] == sid)
        assert bs_after["hasStorageState"] is True, bs_after

        assert c.post(f"/v1/browser/sessions/{sid}/takeover/complete", headers=h).status_code == 200

        # Reject pending form submit so worker resumes under AGENT mode and navigates to the protected dashboard
        assert (
            c.post(
                f"/v1/approvals/{a['id']}/decision",
                headers=h,
                json={
                    "schemaVersion": "1.0",
                    "decision": "REJECTED",
                    "requestSha256": a["requestSha256"],
                    "expectedRecordVersion": a["recordVersion"],
                },
            ).status_code
            == 200
        )
        st = wait(lambda s: s["state"] in ("SUCCEEDED", "FAILED", "WAITING_RECONCILIATION"))
        assert st["state"] == "SUCCEEDED", st
        assert "done:compose-browser:ACTIVE:" in st["run"]["result"], st
        assert "SECRET_COMPOSE_PORTAL_OK" in st["run"]["result"], st


def test_phase7_production_compose_rpc_and_secret_separation() -> None:
    """R3-02/R3-03: Verify real Compose service startup wiring, secret separation (no sandbox keys in API), and live RPC."""
    # 1. API MUST NOT hold sandbox_grant_key, sandbox_rpc_key, or preview_capability_key; DOES hold api_tool_rpc_key & preview_attestation_key
    r_api = dc(
        "exec",
        "-T",
        "api",
        "python",
        "-c",
        (
            "import asyncio, json, os\n"
            "from pathlib import Path\n"
            "from aiagent_core.rpc import RpcClient\n"
            "from aiagent_domain.rpcwire import RpcError\n"
            "secrets_present = sorted(p.name for p in Path('/run/secrets').iterdir())\n"
            "ep = os.environ['AIAGENT_TOOL_GATEWAY_ENDPOINT']\n"
            "key = bytes.fromhex(Path(os.environ['AIAGENT_API_TOOL_RPC_KEY_FILE']).read_text().strip())\n"
            "async def probe():\n"
            "  cli = RpcClient(ep, 'api', key, timeout_s=10.0)\n"
            "  try:\n"
            "    await cli.call('tool.reconcile', {'tenantId': '01950000-0000-7000-8000-000000000000', "
            "'ownerUserId': '01950000-0000-7000-8000-000000000001', 'intentId': '01950000-0000-7000-8000-000000000002'})\n"
            "    return 'OK'\n"
            "  except RpcError as e:\n"
            "    return e.code\n"
            "print(json.dumps({'secrets': secrets_present, 'tg_ep': ep, 'rpc_code': asyncio.run(probe())}))\n"
        ),
    )
    assert r_api.returncode == 0, r_api.stderr
    api_info = json.loads(r_api.stdout.strip())
    assert "api_tool_rpc_key" in api_info["secrets"]
    assert "preview_attestation_key" in api_info["secrets"]
    assert "sandbox_grant_key" not in api_info["secrets"]
    assert "sandbox_rpc_key" not in api_info["secrets"]
    assert "preview_capability_key" not in api_info["secrets"]
    assert api_info["tg_ep"] == "tcp:tool-gateway:7102"
    # Authenticated RPC reached real ToolGateway service (NOT_FOUND for nonexistent intent, NOT auth error!)
    assert api_info["rpc_code"] == "NOT_FOUND", api_info

    # 2. ToolGateway holds distinct sandbox_grant_key & preview_capability_key and reaches live Cell `preview.debug` RPC
    r_tg = dc(
        "exec",
        "-T",
        "tool-gateway",
        "python",
        "-c",
        (
            "import asyncio, json, os\n"
            "from pathlib import Path\n"
            "from aiagent_core.rpc import RpcClient\n"
            "from aiagent_domain.rpcwire import RpcError\n"
            "gk = Path(os.environ['AIAGENT_SANDBOX_GRANT_KEY_FILE']).read_text().strip()\n"
            "pk = Path(os.environ['AIAGENT_PREVIEW_CAPABILITY_KEY_FILE']).read_text().strip()\n"
            "rk = bytes.fromhex(Path(os.environ['AIAGENT_SANDBOX_RPC_KEY_FILE']).read_text().strip())\n"
            "ep = os.environ['AIAGENT_SANDBOX_ENDPOINT']\n"
            "async def probe():\n"
            "  cli = RpcClient(ep, 'tool_gateway', rk, timeout_s=10.0)\n"
            "  try:\n"
            "    await cli.call('preview.debug', {'capability': {'bad': True}})\n"
            "    return 'OK'\n"
            "  except RpcError as e:\n"
            "    return e.code\n"
            "print(json.dumps({'distinct_keys': gk != pk and len(pk) == 64, "
            "'has_gh_tokens': Path(os.environ['AIAGENT_GITHUB_BINDING_TOKENS_JSON_FILE']).is_file(), "
            "'cell_rpc_code': asyncio.run(probe())}))\n"
        ),
    )
    assert r_tg.returncode == 0, r_tg.stderr
    tg_info = json.loads(r_tg.stdout.strip())
    assert tg_info["distinct_keys"] is True
    assert tg_info["has_gh_tokens"] is True
    assert tg_info["cell_rpc_code"] == "GRANT_PREVIEW_CAP_MALFORMED", tg_info

    # 3. R4-01 + R4-03: Verify live production Cell container carries ZERO DB/gateway packages AND executes
    # REAL headless Chromium Browser Debugger via a valid signed PreviewCapability bound to an instance Unix socket!
    r_cell_pkg = dc(
        "exec",
        "-T",
        "cell",
        "python",
        "-c",
        (
            "import importlib.util, json\n"
            "print(json.dumps({"
            "'has_worker': importlib.util.find_spec('aiagent_sandbox.browser_debug_worker') is not None, "
            "'has_playwright': importlib.util.find_spec('playwright.async_api') is not None, "
            "'has_psycopg': importlib.util.find_spec('psycopg') is not None, "
            "'has_gateways': importlib.util.find_spec('aiagent_gateways') is not None, "
            "'has_core': importlib.util.find_spec('aiagent_core') is not None}))\n"
        ),
    )
    assert r_cell_pkg.returncode == 0, r_cell_pkg.stderr
    cell_pkg = json.loads(r_cell_pkg.stdout.strip())
    assert cell_pkg == {
        "has_worker": True,
        "has_playwright": True,
        "has_psycopg": False,
        "has_gateways": False,
        "has_core": False,
    }, cell_pkg

    # Boot a short-lived instance-owned AF_UNIX Preview HTTP server inside the live `cell` container
    r_start_prv = dc(
        "exec",
        "-T",
        "cell",
        "python",
        "-c",
        (
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "sock = Path('/var/lib/aiagent/cells/prv-compose-e2e.sock')\n"
            "sock.unlink(missing_ok=True)\n"
            "code = '''\n"
            "import asyncio, os\n"
            "from pathlib import Path\n"
            "sock = Path('/var/lib/aiagent/cells/prv-compose-e2e.sock')\n"
            "async def handle(r, w):\n"
            "  try:\n"
            "    head = await asyncio.wait_for(r.readuntil(b'\\\\r\\\\n\\\\r\\\\n'), timeout=5.0)\n"
            "    path = head.decode('latin-1').split('\\\\r\\\\n', 1)[0].split(' ')[1]\n"
            "    if path == '/api/broken':\n"
            "      b = b'{\"err\":1}'\n"
            "      w.write(b'HTTP/1.1 500 Internal Server Error\\\\r\\\\nContent-Length: %d\\\\r\\\\nConnection: close\\\\r\\\\n\\\\r\\\\n%s' % (len(b), b))\n"
            "    else:\n"
            "      html = b\"<!doctype html><html><body style='margin:0'><div style='width:900px;height:40px'>overflow</div><script>console.error('Leak Bearer compose_secret_123');fetch('/api/broken');</script></body></html>\"\n"
            "      w.write(b'HTTP/1.1 200 OK\\\\r\\\\nContent-Type: text/html\\\\r\\\\nContent-Length: %d\\\\r\\\\nConnection: close\\\\r\\\\n\\\\r\\\\n%s' % (len(html), html))\n"
            "    await w.drain()\n"
            "  finally:\n"
            "    w.close()\n"
            "async def main():\n"
            "  srv = await asyncio.start_unix_server(handle, path=str(sock))\n"
            "  os.chmod(sock, 0o600)\n"
            "  await asyncio.sleep(60)\n"
            "  srv.close()\n"
            "asyncio.run(main())\n"
            "'''\n"
            "p = subprocess.Popen([sys.executable, '-c', code], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "for _ in range(50):\n"
            "  if sock.exists():\n"
            "    break\n"
            "  time.sleep(0.1)\n"
            "st = os.lstat(sock)\n"
            "print(json.dumps({'pid': p.pid, 'ino': st.st_ino, 'sock': str(sock)}))\n"
        ),
    )
    assert r_start_prv.returncode == 0, r_start_prv.stderr
    prv_meta = json.loads(r_start_prv.stdout.strip())
    endpoint_ref = f"unix://{prv_meta['sock']}?ino={prv_meta['ino']}&pid={prv_meta['pid']}"

    try:
        r_e2e_dbg = dc(
            "exec",
            "-T",
            "tool-gateway",
            "python",
            "-c",
            (
                "import asyncio, json, os, secrets, time\n"
                "from pathlib import Path\n"
                "from aiagent_core.rpc import RpcClient\n"
                "from aiagent_domain import uuid7\n"
                "from aiagent_sandbox.grant import PreviewCapability, sign_preview_capability\n"
                "pk = bytes.fromhex(Path(os.environ['AIAGENT_PREVIEW_CAPABILITY_KEY_FILE']).read_text().strip())\n"
                "rk = bytes.fromhex(Path(os.environ['AIAGENT_SANDBOX_RPC_KEY_FILE']).read_text().strip())\n"
                "ep = os.environ['AIAGENT_SANDBOX_ENDPOINT']\n"
                f"endpoint_ref = {endpoint_ref!r}\n"
                "cap = PreviewCapability(\n"
                "  capability_id=f'prvcap-{secrets.token_hex(12)}',\n"
                "  tenant_id=uuid7(), owner_user_id=uuid7(), project_id='compose-p1', cycle_id='compose-c1',\n"
                "  workspace_id=uuid7(), task_id=uuid7(), run_id=uuid7(), lease_id=uuid7(),\n"
                "  preview_instance_id=uuid7(), instance_generation=1, target_port=4173,\n"
                "  target_url='http://127.0.0.1:4173/', endpoint_ref=endpoint_ref, commit_sha='a'*40,\n"
                "  wall_ms=30000, memory_bytes=1536*1024*1024, pids_max=256, cpu_quota_us=100000,\n"
                "  expires=int(time.time())+45, attempt_id='compose-dbg-1',\n"
                ")\n"
                "signed = sign_preview_capability(pk, cap)\n"
                "async def run():\n"
                "  cli = RpcClient(ep, 'tool_gateway', rk, timeout_s=60.0)\n"
                "  return await cli.call('preview.debug', {'capability': signed})\n"
                "print(json.dumps(asyncio.run(run())))\n"
            ),
        )
        assert r_e2e_dbg.returncode == 0, r_e2e_dbg.stderr
        dbg_res = json.loads(r_e2e_dbg.stdout.strip())
        assert dbg_res["status"] == "EXITED", dbg_res
        assert dbg_res["exitCode"] == 0, dbg_res
        report = json.loads(dbg_res["stdout"])
        assert report["status"] == "OPEN", report
        assert any("[REDACTED]" in m for m in report["consoleErrors"]), report
        assert all("compose_secret_123" not in m for m in report["consoleErrors"]), report
        assert any("HTTP 500 /api/broken" in m for m in report["networkErrors"]), report
        assert any("[mobile] horizontal overflow" in m for m in report["layoutIssues"]), report
    finally:
        dc(
            "exec",
            "-T",
            "cell",
            "python",
            "-c",
            f"import os, signal; from pathlib import Path\ntry:\n os.kill({int(prv_meta['pid'])}, signal.SIGKILL)\nexcept OSError:\n pass\nPath({prv_meta['sock']!r}).unlink(missing_ok=True)\n",
        )
