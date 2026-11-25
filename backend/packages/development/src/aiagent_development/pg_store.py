"""PostgreSQL + PreviewBroker + MultiReviewer adapters for AI-03 DevelopmentWorkflow."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_core.events import append_event
from aiagent_domain import uuid7
from psycopg.types.json import Jsonb

from .contracts import (
    CommitEvidence,
    Cycle,
    ExecutionEvidence,
    InvalidEvidence,
    PatchArtifact,
    Phase,
    ProjectScope,
    ReviewFinding,
    ReviewVerdict,
    RunFence,
    ScopeDenied,
    StaleReference,
    StateConflict,
    TaskPlan,
)
from .ports import CycleStorePort


def _serialize_cycle(c: Cycle) -> dict[str, Any]:
    data = asdict(c)
    data["phase"] = str(c.phase)
    data["submitted_digests"] = sorted(c.submitted_digests)
    return data


def _deserialize_fence(raw: dict[str, Any]) -> RunFence:
    worker_ref = raw.get("worker_ref")
    return RunFence(
        task_id=str(raw["task_id"]),
        run_id=str(raw["run_id"]),
        goal_revision=int(raw["goal_revision"]),
        lease_epoch=int(raw["lease_epoch"]),
        restore_incarnation=int(raw["restore_incarnation"]),
        worker_ref=str(worker_ref) if worker_ref is not None else None,
    )


def _deserialize_cycle(raw: dict[str, Any]) -> Cycle:
    s_raw = raw["scope"]
    scope = ProjectScope(
        tenant_id=str(s_raw["tenant_id"]),
        owner_user_id=str(s_raw["owner_user_id"]),
        project_id=str(s_raw["project_id"]),
        workspace_id=str(s_raw["workspace_id"]),
        repository=str(s_raw["repository"]),
        branch=str(s_raw["branch"]),
        base_sha=str(s_raw["base_sha"]),
        workspace_revision=str(s_raw["workspace_revision"]),
    )
    run = _deserialize_fence(raw["run"])
    plan_raw = raw.get("plan")
    plan = (
        TaskPlan(
            plan_ref=str(plan_raw["plan_ref"]),
            change_request_ref=str(plan_raw["change_request_ref"]),
            required_tests=tuple(str(x) for x in plan_raw["required_tests"]),
            goal_revision=int(plan_raw["goal_revision"]),
        )
        if plan_raw is not None
        else None
    )
    patch_raw = raw.get("patch")
    patch = (
        PatchArtifact(
            artifact_ref=str(patch_raw["artifact_ref"]),
            diff_sha256=str(patch_raw["diff_sha256"]),
            paths=tuple(str(x) for x in patch_raw["paths"]),
            parent_sha=str(patch_raw["parent_sha"]),
            workspace_revision=str(patch_raw["workspace_revision"]),
            run=_deserialize_fence(patch_raw["run"]),
        )
        if patch_raw is not None
        else None
    )
    commit_raw = raw.get("commit")
    commit = (
        CommitEvidence(
            parent_sha=str(commit_raw["parent_sha"]),
            commit_sha=str(commit_raw["commit_sha"]),
            diff_sha256=str(commit_raw["diff_sha256"]),
            artifact_ref=str(commit_raw["artifact_ref"]),
            receipt_ref=str(commit_raw["receipt_ref"]),
        )
        if commit_raw is not None
        else None
    )

    def _ev(item: dict[str, Any]) -> ExecutionEvidence:
        if type(item.get("executed")) is not bool or (
            item.get("exit_code") is not None and type(item.get("exit_code")) is not int
        ):
            raise InvalidEvidence("invalid stored execution status")
        return ExecutionEvidence(
            intent_ref=str(item["intent_ref"]),
            run=_deserialize_fence(item["run"]),
            kind=str(item["kind"]),
            exit_code=item.get("exit_code"),
            executed=item["executed"],
            artifact_sha256=str(item["artifact_sha256"]),
            toolchain_digest=str(item["toolchain_digest"]),
            stdout=str(item["stdout"]),
            stderr=str(item["stderr"]),
            source_commit_sha=str(item["source_commit_sha"]) if item.get("source_commit_sha") is not None else None,
            test_id=str(item["test_id"]) if item.get("test_id") is not None else None,
            receipt_ref=str(item.get("receipt_ref", "")),
            timed_out=item.get("timed_out") if type(item.get("timed_out")) is bool else None,
            truncated=item.get("truncated") if type(item.get("truncated")) is bool else None,
            snapshot_revision=item.get("snapshot_revision"),
        )

    tests = tuple(_ev(x) for x in raw.get("tests", []))
    receipts = tuple(_ev(x) for x in raw.get("execution_receipts", []))
    reviews = tuple(
        ReviewVerdict(
            reviewer_task_id=str(r["reviewer_task_id"]),
            reviewer_role=str(r["reviewer_role"]),
            target_sha=str(r["target_sha"]),
            passed=bool(r["passed"]),
            evidence_ref=str(r["evidence_ref"]),
            findings=tuple(
                ReviewFinding(
                    finding_id=str(f["finding_id"]),
                    severity=str(f["severity"]),
                    status=str(f["status"]),
                )
                for f in r.get("findings", [])
            ),
        )
        for r in raw.get("reviews", [])
    )
    return Cycle(
        cycle_id=str(raw["cycle_id"]),
        scope=scope,
        run=run,
        phase=Phase(str(raw["phase"])),
        change_request_ref=str(raw["change_request_ref"]),
        version=int(raw["version"]),
        plan=plan,
        patch=patch,
        commit=commit,
        tests=tests,
        reviews=reviews,
        execution_receipts=receipts,
        submitted_digests=frozenset(str(x) for x in raw.get("submitted_digests", [])),
        round_number=int(raw.get("round_number", 1)),
        push_receipt=str(raw["push_receipt"]) if raw.get("push_receipt") is not None else None,
        pr_receipt=str(raw["pr_receipt"]) if raw.get("pr_receipt") is not None else None,
        commit_intent_ref=str(raw["commit_intent_ref"]) if raw.get("commit_intent_ref") is not None else None,
    )


class PostgresWorkspacePort:
    def __init__(self, db: Database) -> None:
        self._db = db

    def assert_scope(self, scope: ProjectScope) -> None:
        with self._db.tx(tenant=scope.tenant_id) as c:
            if not control.scope_active(c, scope.tenant_id, scope.owner_user_id, scope.workspace_id):
                raise ScopeDenied("workspace/tenant membership inactive")


class PostgresRunAuthority:
    def __init__(self, db: Database) -> None:
        self._db = db

    def assert_current(self, scope: ProjectScope, run: RunFence) -> None:
        with self._db.tx(tenant=scope.tenant_id) as c:
            if not control.scope_active(c, scope.tenant_id, scope.owner_user_id, scope.workspace_id):
                raise ScopeDenied("scope revoked")
            sc = c.execute("SELECT restore_incarnation FROM agent.schema_control WHERE singleton").fetchone()
            if sc is None or int(sc["restore_incarnation"]) != run.restore_incarnation:
                raise StaleReference("restore incarnation mismatch")
            row = c.execute(
                "SELECT t.owner_user_id, t.workspace_id, t.goal_revision, t.active_run_id,"
                " r.lease_epoch, r.lease_owner_ref, (r.lease_expires_at > clock_timestamp()) AS lease_alive,"
                " r.restore_incarnation, r.cancel_requested, r.pause_requested, r.status_code"
                " FROM agent.tasks t JOIN agent.runs r ON r.tenant_id = t.tenant_id AND r.id = %s"
                " WHERE t.tenant_id = %s AND t.id = %s AND r.task_id = t.id",
                (run.run_id, scope.tenant_id, run.task_id),
            ).fetchone()
            if (
                row is None
                or str(row["owner_user_id"]) != scope.owner_user_id
                or str(row["workspace_id"]) != scope.workspace_id
            ):
                raise ScopeDenied("run/task not owned by scope")
            if (
                str(row["active_run_id"]) != run.run_id
                or int(row["goal_revision"]) != run.goal_revision
                or int(row["lease_epoch"]) != run.lease_epoch
                or not row["lease_owner_ref"]
                or not row["lease_alive"]
                or (run.worker_ref is not None and str(row["lease_owner_ref"]) != run.worker_ref)
                or int(row["restore_incarnation"]) != run.restore_incarnation
                or row["cancel_requested"]
                or row["pause_requested"]
                or row["status_code"]
                in (
                    "CANCELLED",
                    "CANCELLING",
                    "PAUSED",
                    "FAILED",
                    "SUCCEEDED",
                    "PARTIAL",
                    "EXPIRED",
                    "BUDGET_EXHAUSTED",
                )
            ):
                raise StaleReference("run fence stale or inactive")


class PostgresCycleStore(CycleStorePort):
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, cycle: Cycle) -> None:
        payload = _serialize_cycle(cycle)
        with self._db.tx(tenant=cycle.scope.tenant_id) as c:
            c.execute(
                "INSERT INTO agent.dev_cycles(tenant_id, id, owner_user_id, project_id, workspace_id,"
                " task_id, run_id, version, phase, cycle_json)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    cycle.scope.tenant_id,
                    cycle.cycle_id,
                    cycle.scope.owner_user_id,
                    cycle.scope.project_id,
                    cycle.scope.workspace_id,
                    cycle.run.task_id,
                    cycle.run.run_id,
                    cycle.version,
                    str(cycle.phase),
                    Jsonb(payload),
                ),
            )

    def load(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        with self._db.tx(tenant=scope.tenant_id) as c:
            row = c.execute(
                "SELECT owner_user_id, workspace_id, project_id, cycle_json"
                " FROM agent.dev_cycles WHERE tenant_id=%s AND id=%s",
                (scope.tenant_id, cycle_id),
            ).fetchone()
        if row is None:
            raise StateConflict(f"unknown cycle {cycle_id}")
        if (
            str(row["owner_user_id"]) != scope.owner_user_id
            or str(row["workspace_id"]) != scope.workspace_id
            or str(row["project_id"]) != scope.project_id
        ):
            raise ScopeDenied("cycle belongs to another scope")
        return _deserialize_cycle(dict(row["cycle_json"]))

    def save(self, old: Cycle, updated: Cycle) -> None:
        self._save_internal(old, updated, claim_diff=None)

    def save_with_diff_claim(self, old: Cycle, updated: Cycle, diff_sha256: str) -> None:
        self._save_internal(old, updated, claim_diff=diff_sha256)

    def _save_internal(self, old: Cycle, updated: Cycle, *, claim_diff: str | None) -> None:
        if old.scope != updated.scope or old.cycle_id != updated.cycle_id:
            raise ScopeDenied("cycle scope mismatch")
        if updated.version != old.version + 1:
            raise StateConflict("non-monotonic cycle version")
        payload = _serialize_cycle(updated)
        with self._db.tx(tenant=updated.scope.tenant_id) as c:
            if old.phase == Phase.UNKNOWN_EXTERNAL_EFFECT and updated.phase == Phase.COMMITTED:
                if (
                    old.commit is not None
                    or old.patch is None
                    or updated.commit is None
                    or not old.commit_intent_ref
                    or updated != replace(old, version=old.version + 1, phase=Phase.COMMITTED, commit=updated.commit)
                ):
                    raise StateConflict("Git recovery authority unavailable")
                c.execute("SELECT set_config('statement_timeout', '5000ms', true)")
                c.execute("SELECT set_config('lock_timeout', '5000ms', true)")
                effect = c.execute(
                    "SELECT o.id,e.external_receipt_ref FROM agent.git_operations o "
                    "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                    "JOIN agent.side_effect_records e ON e.tenant_id=i.tenant_id AND e.tool_intent_id=i.id "
                    "WHERE o.tenant_id=%s AND o.cycle_id=%s AND o.patch_id=%s "
                    "AND i.task_id=%s AND i.run_id=%s AND i.invocation_key=%s "
                    "AND i.tool_key='git.commit' AND i.status_code='CONFIRMED' "
                    "AND e.outcome_code='CONFIRMED' AND o.state='RECEIPT_PERSISTED'",
                    (
                        old.scope.tenant_id,
                        old.cycle_id,
                        old.patch.artifact_ref,
                        old.run.task_id,
                        old.run.run_id,
                        old.commit_intent_ref,
                    ),
                ).fetchall()
                if len(effect) != 1 or effect[0]["external_receipt_ref"] != updated.commit.receipt_ref:
                    raise StateConflict("Git recovery effect unavailable")
                row = c.execute(
                    "SELECT * FROM agent.git_operation_fetch(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        old.scope.tenant_id,
                        old.scope.owner_user_id,
                        old.scope.workspace_id,
                        old.run.task_id,
                        old.run.run_id,
                        old.run.goal_revision,
                        old.run.lease_epoch,
                        old.run.worker_ref,
                        old.run.restore_incarnation,
                        effect[0]["id"],
                    ),
                ).fetchone()
                if row is None or "git:" + str(row["receipt_id"]) != updated.commit.receipt_ref:
                    raise StateConflict("Git recovery receipt unavailable")
                body = json.loads(bytes(row["body"]))
                if (
                    body.get("commit") != updated.commit.commit_sha
                    or body.get("parent") != old.patch.parent_sha
                    or body.get("patch_id") != old.patch.artifact_ref
                    or body.get("patch_sha256") != old.patch.diff_sha256
                    or updated.commit.parent_sha != old.patch.parent_sha
                    or updated.commit.artifact_ref != old.patch.artifact_ref
                    or updated.commit.diff_sha256 != old.patch.diff_sha256
                ):
                    raise StateConflict("Git recovery receipt mismatch")
            if claim_diff is not None:
                claimed = c.execute(
                    "INSERT INTO agent.dev_diff_claims(tenant_id, project_id, diff_sha256, cycle_id)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (updated.scope.tenant_id, updated.scope.project_id, claim_diff, updated.cycle_id),
                )
                if claimed.rowcount != 1:
                    raise StateConflict("duplicate diff claim")
            cur = c.execute(
                "UPDATE agent.dev_cycles SET version=%s, phase=%s, cycle_json=%s, updated_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND version=%s",
                (
                    updated.version,
                    str(updated.phase),
                    Jsonb(payload),
                    updated.scope.tenant_id,
                    updated.cycle_id,
                    updated.scope.owner_user_id,
                    old.version,
                ),
            )
            if cur.rowcount != 1:
                raise StateConflict("concurrent cycle modification (CAS mismatch)")
            if updated.commit is not None and (
                old.commit is None or old.commit.commit_sha != updated.commit.commit_sha
            ):
                append_event(
                    c,
                    tenant_id=updated.scope.tenant_id,
                    aggregate_type="task",
                    aggregate_id=updated.run.task_id,
                    kind="dev.cycle_committed",
                    payload={"cycleId": updated.cycle_id, "sha": updated.commit.commit_sha},
                    actor={"type": "service", "id": "development"},
                    owner_user_id=updated.scope.owner_user_id,
                    task_id=updated.run.task_id,
                    run_id=updated.run.run_id,
                )


class PreviewBroker:
    """Short-lived, tenant/owner-scoped, loopback-only Preview lease manager bound to an exact commit SHA."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def issue_lease(
        self,
        cycle: Cycle,
        *,
        target_port: int,
        ttl_s: int = 120,
    ) -> tuple[str, str]:
        if cycle.phase != Phase.CODE_READY or cycle.commit is None:
            raise StateConflict("preview requires CODE_READY cycle with immutable commit")
        if not 1024 <= target_port <= 65535 or not 10 <= ttl_s <= 300:
            raise InvalidEvidence("invalid preview port or ttl")
        lease_id = uuid7()
        raw_token = "prv-" + secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires = datetime.now(UTC) + timedelta(seconds=ttl_s)
        with self._db.tx(tenant=cycle.scope.tenant_id) as c:
            if not control.scope_active(c, cycle.scope.tenant_id, cycle.scope.owner_user_id, cycle.scope.workspace_id):
                raise ScopeDenied("scope inactive")
            c.execute(
                "INSERT INTO agent.preview_leases(tenant_id, id, owner_user_id, project_id, workspace_id,"
                " commit_sha, target_port, token_sha256, status, expires_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s)",
                (
                    cycle.scope.tenant_id,
                    lease_id,
                    cycle.scope.owner_user_id,
                    cycle.scope.project_id,
                    cycle.scope.workspace_id,
                    cycle.commit.commit_sha,
                    target_port,
                    token_hash,
                    expires,
                ),
            )
            append_event(
                c,
                tenant_id=cycle.scope.tenant_id,
                aggregate_type="task",
                aggregate_id=cycle.run.task_id,
                kind="dev.preview_leased",
                payload={"cycleId": cycle.cycle_id, "leaseId": lease_id, "port": target_port},
                actor={"type": "service", "id": "preview_broker"},
                owner_user_id=cycle.scope.owner_user_id,
                task_id=cycle.run.task_id,
                run_id=cycle.run.run_id,
            )
        return lease_id, raw_token

    def verify_lease(self, tenant_id: str, owner_user_id: str, lease_id: str, token: str, port: int) -> str:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "SELECT commit_sha, target_port, token_sha256, status, expires_at"
                " FROM agent.preview_leases WHERE tenant_id=%s AND id=%s AND owner_user_id=%s",
                (tenant_id, lease_id, owner_user_id),
            ).fetchone()
        if (
            row is None
            or row["status"] != "ACTIVE"
            or int(row["target_port"]) != port
            or row["expires_at"] <= datetime.now(UTC)
            or not secrets.compare_digest(str(row["token_sha256"]), token_hash)
        ):
            raise ScopeDenied("invalid or expired preview lease")
        return str(row["commit_sha"])

    def revoke_lease(self, tenant_id: str, owner_user_id: str, lease_id: str) -> bool:
        with self._db.tx(tenant=tenant_id) as c:
            cur = c.execute(
                "UPDATE agent.preview_leases SET status='REVOKED', revoked_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND status='ACTIVE'",
                (tenant_id, lease_id, owner_user_id),
            )
            return cur.rowcount == 1
