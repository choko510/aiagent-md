"""Shared harness: one PostgreSQL server, one database per candidate, workers as real subprocesses.

Env:
  AIAGENT_PG_URL   server URL without database, e.g. postgresql://postgres:pw@127.0.0.1:5432
  AIAGENT_PY_A     python of the candidate-A environment (default: current interpreter)
  AIAGENT_PY_B     python of the candidate-B environment (default: current interpreter)
  AIAGENT_CANDIDATES  comma list, default "a,b"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiagent_conformance.control import Control  # noqa: E402
from aiagent_conformance.fixtures import USER_TEXT  # noqa: E402

PG = os.environ.get("AIAGENT_PG_URL", "postgresql://postgres@127.0.0.1:5432").rstrip("/")
CANDIDATES = os.environ.get("AIAGENT_CANDIDATES", "a,b").split(",")
LEASE = 2.0


def _db_url(c: str) -> str:
    return f"{PG}/conf_{c}"


_created: set[str] = set()


def _ensure_db(c: str) -> None:
    if c in _created:
        return
    with psycopg.connect(f"{PG}/postgres", autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS conf_{c} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE conf_{c}")
    Control(_db_url(c)).reset()
    _created.add(c)


class Harness:
    def __init__(self, cand: str):
        self.cand = cand
        self.url = _db_url(cand)
        self.ctl = Control(self.url)
        self.py = os.environ.get(f"AIAGENT_PY_{cand.upper()}", sys.executable)

    def new_run(self, policy: str = "notify", history: list[dict[str, Any]] | None = None) -> str:
        rid = f"{self.cand}-{policy}-{uuid.uuid4().hex[:8]}"
        self.ctl.create_run(rid, USER_TEXT[policy], history or [])
        return rid

    def worker(self, run_id: str, policy: str = "notify", *, fault: str | None = None, args: list[str] | None = None,
               timeout: float = 120) -> dict[str, Any]:
        env = {**os.environ, "PYDANTIC_AI_NO_BANNER": "1", "PYTHONIOENCODING": "utf-8"}
        env.pop("AIAGENT_FAULT", None)
        for k in list(env):  # no provider credential is ever given to a worker
            if k.endswith("_API_KEY"):
                env.pop(k)
        if fault:
            env["AIAGENT_FAULT"] = fault
        cmd = [self.py, "-m", "aiagent_conformance.worker", "--candidate", self.cand, "--db", self.url,
               "--run", run_id, "--policy", policy, "--lease", str(LEASE), *(args or [])]
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        if p.returncode == 137:
            time.sleep(LEASE + 0.3)  # the dead worker's lease must expire before anyone else may claim
            return {"status": "KILLED"}
        line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
        if p.returncode != 0 or line is None:
            raise AssertionError(f"worker failed rc={p.returncode}\nSTDOUT:{p.stdout[-3000:]}\nSTDERR:{p.stderr[-6000:]}")
        return json.loads(line)

    def approve_all(self, run_id: str, status: str = "APPROVED") -> list[str]:
        ids = [a["approval_id"] for a in self.ctl.approvals(run_id) if a["status"] == "PENDING"]
        for i in ids:
            self.ctl.decide(i, status)
        return ids

    def sends(self, run_id: str) -> list[dict[str, Any]]:
        return self.ctl.db.execute(
            "SELECT s.payload FROM aiagent.external_sends s JOIN aiagent.tool_ledger l USING (intent_key)"
            " WHERE l.root_run_id = %s", (run_id,)).fetchall()

    def naive_sends(self, run_id: str) -> int:
        return self.ctl.db.execute(
            "SELECT count(*) AS n FROM aiagent.external_sends s WHERE NOT EXISTS"
            " (SELECT 1 FROM aiagent.tool_ledger l WHERE l.intent_key = s.intent_key)"
            " AND s.intent_key IN (SELECT tc->>'id' FROM aiagent.head_entries, jsonb_array_elements(payload->'tool_calls') tc WHERE run_id=%s)",
            (run_id,)).fetchone()["n"]

    def model_calls(self, run_id: str) -> list[dict[str, Any]]:
        return self.ctl.db.execute("SELECT * FROM aiagent.model_calls WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()


@pytest.fixture(params=CANDIDATES)
def h(request: pytest.FixtureRequest) -> Harness:
    _ensure_db(request.param)
    return Harness(request.param)
