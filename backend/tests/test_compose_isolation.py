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
    assert probe(service, "172.31.250.10", 8099).startswith("BLOCKED")
    assert probe("sentinel", "172.31.250.10", 8099).startswith("OPEN")


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
