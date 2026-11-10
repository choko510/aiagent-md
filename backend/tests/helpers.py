"""API-level helpers shared by the E2E / fault suites."""

from __future__ import annotations

import time
from typing import Any

from aiagent_core.db import Database


def auth(t: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {t['token']}"}


def submit(api: Any, t: dict[str, str], goal: str, **kw: str) -> str:
    r = api.post("/v1/tasks", json={"schemaVersion": "1.0", "goal": goal, **kw}, headers=auth(t))
    assert r.status_code == 201, r.text
    return str(r.json()["taskId"])


def state(api: Any, t: dict[str, str], task_id: str) -> dict[str, Any]:
    r = api.get(f"/v1/tasks/{task_id}", headers=auth(t))
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def approve(api: Any, t: dict[str, str], task_id: str, decision: str = "APPROVED") -> int:
    n = 0
    for a in state(api, t, task_id)["approvals"]:
        if a["decision"] == "PENDING":
            r = api.post(
                f"/v1/approvals/{a['id']}/decision",
                headers=auth(t),
                json={
                    "schemaVersion": "1.0",
                    "decision": decision,
                    "requestSha256": a["requestSha256"],
                    "expectedRecordVersion": a["recordVersion"],
                },
            )
            assert r.status_code == 200, r.text
            n += 1
    return n


def q(owner: str, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    db = Database(owner, max_size=1)
    try:
        return db.all(sql, args)
    finally:
        db.close()


def run_of(owner: str, task_id: str) -> dict[str, Any]:
    return q(
        owner, "SELECT r.* FROM agent.runs r JOIN agent.tasks t ON t.active_run_id=r.id WHERE t.id=%s", (task_id,)
    )[0]


def wait_lease_expiry(owner: str, task_id: str) -> None:
    r = run_of(owner, task_id)
    if r["lease_expires_at"] is not None:
        rows = q(
            owner,
            "SELECT greatest(0, extract(epoch FROM lease_expires_at - now())) AS s FROM agent.runs WHERE id=%s",
            (r["id"],),
        )
        time.sleep(float(rows[0]["s"]) + 0.3)
