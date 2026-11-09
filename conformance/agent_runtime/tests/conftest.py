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

    def new_run(self, policy: str = "notify", history: list[dict[str, Any]] | None = None, tenant: str = "t1") -> str:
        rid = f"{self.cand}-{policy}-{uuid.uuid4().hex[:8]}"
        self.ctl.create_run(rid, USER_TEXT[policy], history or [], tenant_id=tenant, policy=policy)
        return rid

    def _env(self, fault: str | None, race: str | None, race_run: str | None) -> dict[str, str]:
        env = {**os.environ, "PYDANTIC_AI_NO_BANNER": "1", "PYTHONIOENCODING": "utf-8"}
        for k in ("AIAGENT_FAULT", "AIAGENT_RACE", "AIAGENT_RACE_RUN"):
            env.pop(k, None)
        for k in list(env):  # no provider credential is ever given to a worker
            if k.endswith("_API_KEY"):
                env.pop(k)
        if fault:
            env["AIAGENT_FAULT"] = fault
        if race:
            env["AIAGENT_RACE"], env["AIAGENT_RACE_RUN"] = race, race_run or ""
        return env

    def _cmd(self, args: list[str]) -> list[str]:
        return [self.py, "-m", "aiagent_conformance.worker", "--candidate", self.cand, "--db", self.url,
                "--lease", str(LEASE), *args]

    @staticmethod
    def _parse(rc: int, out: str, err: str) -> dict[str, Any]:
        if rc == 137:
            return {"status": "KILLED"}
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), None)
        if rc != 0 or line is None:
            raise AssertionError(f"worker failed rc={rc}\nSTDOUT:{out[-3000:]}\nSTDERR:{err[-6000:]}")
        return json.loads(line)

    def worker(self, run_id: str, policy: str = "notify", *, fault: str | None = None, args: list[str] | None = None,
               race: str | None = None, timeout: float = 120) -> dict[str, Any]:
        p = subprocess.run(self._cmd(["--run", run_id, "--policy", policy, *(args or [])]), cwd=ROOT,
                           env=self._env(fault, race, run_id.split(",")[0]), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8")
        out = self._parse(p.returncode, p.stdout, p.stderr)
        if out.get("status") == "KILLED":
            time.sleep(LEASE + 0.3)  # the dead worker's lease must expire before anyone else may claim
        return out

    def queue_workers(self, n: int = 1, *, fault: str | None = None, timeout: float = 180) -> list[dict[str, Any]]:
        """Start n QueuePort consumer processes concurrently; each runs until the queue stays idle."""
        procs = [subprocess.Popen(self._cmd(["--queue-worker"]), cwd=ROOT, env=self._env(fault, None, None),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                 for _ in range(n)]
        outs = []
        for p in procs:
            o, e = p.communicate(timeout=timeout)
            outs.append(self._parse(p.returncode, o, e))
        if any(o.get("status") == "KILLED" for o in outs):
            time.sleep(LEASE + 0.3)
        return outs

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

    def model_calls(self, run_id: str, *, include_cached: bool = False) -> list[dict[str, Any]]:
        """Provider calls (default) or every admission including answers replayed from HEAD."""
        q = "SELECT * FROM aiagent.model_calls WHERE run_id=%s" + ("" if include_cached else " AND NOT cached")
        return self.ctl.db.execute(q + " ORDER BY id", (run_id,)).fetchall()


@pytest.fixture(params=CANDIDATES)
def h(request: pytest.FixtureRequest) -> Harness:
    _ensure_db(request.param)
    return Harness(request.param)
