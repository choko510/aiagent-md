"""Multi-process test stack: Model Gateway, Tool Gateway, Sentinel (+Trusted Egress) and a mock receiver run as
separate OS processes with separate DB logins and per-caller RPC keys; runtime workers are separate processes too
(one-shot mode). Faults are real process deaths (os._exit). This is the formal RPC path - no in-process fakes."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiagent_core.db import Database
from aiagent_core.relay import OutboxRelay

HERE = Path(__file__).parent
PY = sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"port {port} not listening")


@dataclass
class Stack:
    dsn: dict[str, str]
    tmp: Path
    receiver_token: str = field(default_factory=lambda: "rcv-" + secrets.token_hex(12))
    procs: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    receiver_hang: bool = False
    owner_dsn: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("model", "tool", "sentinel", "egress", "receiver"):
            self.ports[name] = free_port()
        for k in ("rt_model", "rt_tool", "tgw_sentinel", "ops_tool"):
            self.keys[k] = secrets.token_hex(32)
        self.permit_key = secrets.token_hex(32)
        self.provider_secret = "sk-fake-provider-SECRET-" + secrets.token_hex(6)
        self.log = self.tmp / "receiver.jsonl"

    # ---------------------------------------------------------------- env
    def base_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AIAGENT_")}
        env.update({"AIAGENT_PROFILE": "test", "PYTHONUNBUFFERED": "1", **self.extra_env})
        return env

    def env_for(self, name: str) -> dict[str, str]:
        env = self.base_env()
        p = self.ports
        if name == "model":
            env.update(
                {
                    "AIAGENT_MODEL_GATEWAY_DSN": self.dsn["model_gateway"],
                    "AIAGENT_MODEL_GATEWAY_ENDPOINT": f"tcp:127.0.0.1:{p['model']}",
                    "AIAGENT_MODEL_GATEWAY_RPC_KEYS": f"runtime={self.keys['rt_model']}",
                    "AIAGENT_CONTEXT_ARTIFACT_KEY": (b"k" * 32).hex(),  # test-only key; model process only
                    "AIAGENT_ANTHROPIC_API_KEY": self.provider_secret,
                }
            )  # never used (no consent), only held
        elif name == "tool":
            env.update(
                {
                    "AIAGENT_TOOL_GATEWAY_DSN": self.dsn["tool_gateway"],
                    "AIAGENT_TOOL_GATEWAY_ENDPOINT": f"tcp:127.0.0.1:{p['tool']}",
                    "AIAGENT_TOOL_GATEWAY_RPC_KEYS": f"runtime={self.keys['rt_tool']},ops={self.keys['ops_tool']}",
                    "AIAGENT_SENTINEL_ENDPOINT": f"tcp:127.0.0.1:{p['sentinel']}",
                    "AIAGENT_SENTINEL_CLIENT_KEY": self.keys["tgw_sentinel"],
                    "AIAGENT_EGRESS_PROXY": f"http://127.0.0.1:{p['egress']}",
                    "AIAGENT_MESSAGE_RECEIVER_URL": f"http://127.0.0.1:{p['receiver']}/messages",
                    "AIAGENT_MESSAGE_RECEIVER_TOKEN": self.receiver_token,
                    "AIAGENT_MESSAGE_RECEIVER_CREDENTIAL_HOST": "127.0.0.1",
                }
            )
        elif name == "sentinel":
            env.update(
                {
                    "AIAGENT_SENTINEL_DSN": self.dsn["sentinel"],
                    "AIAGENT_SENTINEL_ENDPOINT": f"tcp:127.0.0.1:{p['sentinel']}",
                    "AIAGENT_SENTINEL_RPC_KEYS": f"tool_gateway={self.keys['tgw_sentinel']}",
                    "AIAGENT_SENTINEL_PERMIT_KEY": self.permit_key,
                    "AIAGENT_EGRESS_LISTEN": f"127.0.0.1:{p['egress']}",
                    "AIAGENT_EGRESS_CONFIG": json.dumps(self.egress_config()),
                }
            )
        return env

    def egress_config(self) -> dict[str, Any]:
        return {
            "destinations": {
                "message-receiver": {
                    "scheme": "http",
                    "host": "127.0.0.1",
                    "port": self.ports["receiver"],
                    "methods": ["POST", "GET"],
                    "allow_private": ["127.0.0.1"],
                }
            }
        }

    def worker_env(self, **extra: str) -> dict[str, str]:
        env = self.base_env()
        env.update(
            {
                "AIAGENT_RUNTIME_DSN": self.dsn["runtime"],
                "AIAGENT_CHECKPOINTER_DSN": self.dsn["checkpointer"],
                "AIAGENT_MODEL_GATEWAY_ENDPOINT": f"tcp:127.0.0.1:{self.ports['model']}",
                "AIAGENT_TOOL_GATEWAY_ENDPOINT": f"tcp:127.0.0.1:{self.ports['tool']}",
                "AIAGENT_RUNTIME_MODEL_RPC_KEY": self.keys["rt_model"],
                "AIAGENT_RUNTIME_TOOL_RPC_KEY": self.keys["rt_tool"],
                "AIAGENT_WORKER_ONCE": "1",
                "AIAGENT_LEASE_S": "6",
                **extra,
            }
        )
        return env

    # ---------------------------------------------------------------- processes
    def start(self, name: str, **extra_env: str) -> None:
        if name == "receiver":
            args = [
                PY,
                str(HERE / "mock_receiver.py"),
                str(self.ports["receiver"]),
                str(self.log),
                "hang" if self.receiver_hang else "ok",
                self.receiver_token,
            ]
            env = self.base_env()
        else:
            mod = {
                "model": ["-m", "aiagent_gateways.servers", "model"],
                "tool": ["-m", "aiagent_gateways.servers", "tool"],
                "sentinel": ["-m", "aiagent_sentinel.main"],
            }[name]
            args, env = [PY, *mod], {**self.env_for(name), "AIAGENT_ATTEMPT_ORPHAN_S": "0", **extra_env}
        out = open(self.tmp / f"{name}.log", "ab")  # noqa: SIM115
        self.procs[name] = subprocess.Popen(args, env=env, stdout=out, stderr=subprocess.STDOUT)
        wait_port(self.ports[name])
        if name == "sentinel":
            wait_port(self.ports["egress"])

    def tune_queue(self) -> None:
        """Fast retries / redelivery for tests (production values are in the migration defaults)."""
        db = Database(self.owner_dsn, max_size=1)
        try:
            db.one(
                "UPDATE queue.queues SET base_backoff_ms=50, max_backoff_ms=500, visibility_ms=3000,"
                " per_tenant_active_limit=8, global_active_limit=16, max_attempts=6, paused=false"
                " WHERE name='run' RETURNING 1"
            )
        finally:
            db.close()

    def start_all(self) -> Stack:
        if self.owner_dsn:
            self.tune_queue()
        for n in ("receiver", "sentinel", "tool", "model"):
            self.start(n)
        return self

    def stop(self, name: str) -> None:
        p = self.procs.pop(name, None)
        if p is not None:
            p.kill()
            p.wait(10)

    def restart(self, name: str, **extra_env: str) -> None:
        self.stop(name)
        self.start(name, **extra_env)

    def ensure(self, name: str) -> None:
        p = self.procs.get(name)
        if p is None or p.poll() is not None:
            self.procs.pop(name, None)
            self.start(name)

    def close(self) -> None:
        for n in list(self.procs):
            self.stop(n)

    # ---------------------------------------------------------------- driving
    def relay(self) -> int:
        db = Database(self.dsn["relay"], max_size=2)
        try:
            return OutboxRelay(db, "test-relay").run_once()
        finally:
            db.close()

    def worker(self, fault: str | None = None, timeout: float = 180, **extra: str) -> subprocess.CompletedProcess[str]:
        """Relay pending outbox rows, then run one worker process until the queue is idle (or it is killed)."""
        self.relay()
        env = self.worker_env(**extra)
        if fault:
            env["AIAGENT_FAULT"] = fault
        r = subprocess.run(
            [PY, "-m", "aiagent_runtime.worker"], env=env, capture_output=True, text=True, timeout=timeout
        )
        for n in ("model", "tool", "sentinel"):
            self.ensure(n)  # a gateway fault killed the gateway process: restart it like a supervisor would
        return r

    def drive(self, rounds: int = 6, **extra: str) -> list[subprocess.CompletedProcess[str]]:
        """Run workers until no job is due within a few seconds (retries/backoff/redelivery included)."""
        out = []
        owner = Database(self.owner_dsn, max_size=1)
        try:
            for _ in range(rounds):
                out.append(self.worker(**extra))
                row = owner.one(
                    "SELECT min(CASE WHEN status='READY' THEN run_after ELSE locked_until END) - now() AS wait"
                    " FROM queue.jobs WHERE status IN ('READY','CLAIMED')"
                )
                wait = row["wait"] if row else None
                if wait is None or wait.total_seconds() > 8:
                    break
                time.sleep(max(0.0, wait.total_seconds()) + 0.2)
        finally:
            owner.close()
        return out

    def sends(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def logs(self) -> str:
        return "\n".join(
            (self.tmp / f"{n}.log").read_text(encoding="utf-8", errors="replace")
            for n in ("model", "tool", "sentinel", "receiver")
            if (self.tmp / f"{n}.log").exists()
        )
