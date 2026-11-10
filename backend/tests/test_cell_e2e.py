"""STEP 3-E: generated code runs in a Docker cell via authenticated RPC and single-use grants.

Docker is NOT a separate guest kernel; this test does not prove KVM/libvirt VM isolation.
Distinct guest kernel / vsock security requires a separate production topology acceptance test.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from aiagent_core import admin
from conftest import PgEnv
from helpers import q, state, submit
from stack import Stack, free_port, wait_port

pytestmark = [pytest.mark.pg, pytest.mark.compose]
BACKEND = Path(__file__).resolve().parents[1]


def _docker_ok() -> bool:
    return (
        shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


def _supervisor_answers(port: int, timeout_s: float) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                s.sendall(struct.pack(">I", 2) + b"{}")
                head = s.recv(4)
                if len(head) == 4:
                    reply = json.loads(s.recv(struct.unpack(">I", head)[0]))
                    if reply.get("code") == "UNAUTHENTICATED":
                        return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def cell(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    if not _docker_ok() or not (BACKEND / ".linuxdeps").exists():
        pytest.skip("docker or .linuxdeps (see tests/linux/test_sandbox_cell.py) not available")
    d = tmp_path_factory.mktemp("cell")
    rpc, grant = secrets.token_hex(32), secrets.token_hex(32)
    (d / "rpc").write_text(rpc)
    (d / "grant").write_text(grant)
    port = free_port()
    name = f"aiagent-cell-{secrets.token_hex(3)}"
    src = str(BACKEND).replace("\\", "/")
    keys = str(d).replace("\\", "/")
    subprocess.run(
        [
            "docker",
            "run",
            "-d",  # no --rm: keep a crashed supervisor's logs readable; the fixture removes it in `finally`
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:7000",
            "--cap-add",
            "SYS_ADMIN",
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "apparmor=unconfined",
            "--read-only",
            "--tmpfs",
            "/cells:exec,mode=1777",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "-v",
            f"{src}:/w:ro",
            "-v",
            f"{keys}:/keys:ro",
            "-e",
            "PYTHONPATH=/w/.linuxdeps:/w/packages/domain/src:/w/packages/sandbox/src",
            "-e",
            "AIAGENT_SANDBOX_GRANT_KEY_FILE=/keys/grant",
            "-e",
            "AIAGENT_SANDBOX_RPC_KEY_FILE=/keys/rpc",
            "-e",
            "AIAGENT_SANDBOX_ROOT=/cells",
            "-e",
            "AIAGENT_SANDBOX_LISTEN=tcp:0.0.0.0:7000",
            "-e",
            "AIAGENT_SANDBOX_CELL_UID=20001",
            "python:3.13-slim",
            "python",
            "-m",
            "aiagent_sandbox.supervisor",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    try:
        wait_port(port, 60)
        # An open published port proves nothing (docker-proxy accepts even if the supervisor crashed on start):
        # require the supervisor itself to answer an unauthenticated probe with UNAUTHENTICATED.
        if not _supervisor_answers(port, 30):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            raise AssertionError(f"cell supervisor not answering; container logs:\n{logs.stdout}{logs.stderr}"[-4000:])
        yield {"endpoint": f"tcp:127.0.0.1:{port}", "rpc": rpc, "grant": grant, "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_sandbox_exec_runs_in_cell_not_on_control_host(stack: Stack, api, pg: PgEnv, cell: dict[str, str]) -> None:
    stack.restart(
        "tool",
        AIAGENT_SANDBOX_ENDPOINT=cell["endpoint"],
        AIAGENT_SANDBOX_RPC_KEY=cell["rpc"],
        AIAGENT_SANDBOX_GRANT_KEY=cell["grant"],
    )
    try:
        t = admin.provision_tenant(pg.owner)
        task = submit(api, t, "Use the sandbox to compute 6*7.", profile="sandbox")
        stack.drive()
        st = state(api, t, task)
        res = q(
            pg.owner, "SELECT result_json FROM agent.tool_intents WHERE task_id=%s AND tool_key='sandbox_exec'", (task,)
        )[0]["result_json"]
        assert st["state"] == "SUCCEEDED" and st["run"]["result"] == "done:sandbox:CONFIRMED", (st["run"], res)
        assert res["exitCode"] == 0 and res["stdout"].strip() == "42"
        # the command executed in the Linux cell as the unprivileged cell user, inside the per-task workspace
        out = subprocess.run(
            ["docker", "exec", cell["name"], "sh", "-c", f"ls -n /cells/{t['tenantId']}"],
            capture_output=True,
            text=True,
        )
        assert task in out.stdout and " 20001 " in out.stdout
    finally:
        stack.restart("tool")
