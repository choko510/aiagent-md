"""STEP 3-E: generated code runs ONLY in the execution cell on a different kernel (Linux container / VM), reached via a
scoped, authenticated RPC with a single-use grant - never in the control-plane processes. Requires Docker."""

from __future__ import annotations

import os
import secrets
import shutil
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
            "-d",
            "--rm",
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
        time.sleep(1)
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
        assert st["state"] == "SUCCEEDED" and st["run"]["result"] == "done:sandbox:CONFIRMED", st
        res = q(
            pg.owner, "SELECT result_json FROM agent.tool_intents WHERE task_id=%s AND tool_key='sandbox_exec'", (task,)
        )[0]["result_json"]
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
