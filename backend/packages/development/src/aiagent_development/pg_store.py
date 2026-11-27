"""PostgreSQL + PreviewBroker + MultiReviewer adapters for AI-03 DevelopmentWorkflow."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from aiagent_core import config, control
from aiagent_core.db import Database
from aiagent_core.events import append_event
from aiagent_domain import canonical_json, uuid7
from psycopg.errors import UniqueViolation
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
            receipt_body_sha256=commit_raw.get("receipt_body_sha256"),
            receipt_proof_sha256=commit_raw.get("receipt_proof_sha256"),
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
            if old.phase in (Phase.COMMIT_PENDING, Phase.UNKNOWN_EXTERNAL_EFFECT) and updated.phase == Phase.COMMITTED:
                if (
                    (old.phase == Phase.UNKNOWN_EXTERNAL_EFFECT and old.commit is not None)
                    or old.patch is None
                    or updated.commit is None
                    or updated.commit.receipt_body_sha256 is None
                    or updated.commit.receipt_proof_sha256 is None
                    or not old.commit_intent_ref
                    or updated != replace(old, version=old.version + 1, phase=Phase.COMMITTED, commit=updated.commit)
                ):
                    raise StateConflict("Git recovery authority unavailable")
                c.execute("SELECT set_config('statement_timeout', '5000ms', true)")
                c.execute("SELECT set_config('lock_timeout', '5000ms', true)")
                effect = c.execute(
                    "SELECT o.id,e.external_receipt_ref FROM agent.git_operations o "
                    "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                    "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                    "JOIN agent.side_effect_records e ON e.tenant_id=i.tenant_id AND e.tool_intent_id=i.id "
                    "WHERE o.tenant_id=%s AND o.cycle_id=%s AND o.patch_id=%s "
                    "AND i.task_id=%s AND i.run_id=%s AND i.invocation_key=%s "
                    "AND i.tool_key='git.commit' AND i.status_code='CONFIRMED' AND a.status_code='CONFIRMED' "
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
                body_sha = hashlib.sha256(bytes(row["body"])).hexdigest()
                proof_sha = hashlib.sha256(bytes(row["proof"])).hexdigest()
                if (
                    not hmac.compare_digest(body_sha, str(row["body_sha256"]))
                    or not hmac.compare_digest(proof_sha, str(row["proof_sha256"]))
                    or not hmac.compare_digest(body_sha, updated.commit.receipt_body_sha256)
                    or not hmac.compare_digest(proof_sha, updated.commit.receipt_proof_sha256)
                ):
                    raise StateConflict("Git recovery receipt digest mismatch")
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


def _resolve_preview_attestation_key(explicit_key: bytes | None = None) -> bytes:
    """Resolve dedicated HMAC key for preview launch receipt attestation (R3-02/R3-05: NEVER falls back to sandbox_grant_key)."""
    if explicit_key is not None:
        if len(explicit_key) < 32:
            raise ScopeDenied("preview attestation key must be >= 32 bytes")
        return explicit_key

    sec = config.secret("AIAGENT_PREVIEW_ATTESTATION_KEY", required=False)
    if sec is not None:
        try:
            raw = bytes.fromhex(sec.reveal().strip())
        except ValueError as exc:
            raise ScopeDenied("invalid AIAGENT_PREVIEW_ATTESTATION_KEY") from exc
        if len(raw) >= 32:
            return raw
    raise ScopeDenied("PREVIEW_ATTESTATION_KEY_REQUIRED")


_MAX_RECEIPT_TTL_S = 60
_LAUNCH_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# R4-03: Instance-owned Unix domain socket + kernel inode + listening PID (NEVER raw loopback TCP port).
_PREVIEW_UNIX_ENDPOINT_RE = re.compile(
    r"^unix://(/[A-Za-z0-9._/-]{1,180})\?ino=([1-9][0-9]{0,20})&pid=([1-9][0-9]{0,9})$"
)
_INACTIVE_RUN_STATUSES = frozenset({"CANCELLED", "CANCELLING", "PAUSED", "FAILED", "EXPIRED", "BUDGET_EXHAUSTED"})


def format_preview_unix_endpoint_ref(socket_path: str, *, inode: int, pid: int) -> str:
    """Build a canonical instance-bound Unix socket endpoint_ref validated by PreviewBroker and Sandbox Supervisor."""
    ref = f"unix://{socket_path}?ino={inode}&pid={pid}"
    if len(ref) > 256 or _PREVIEW_UNIX_ENDPOINT_RE.fullmatch(ref) is None or ".." in socket_path.split("/"):
        raise ScopeDenied("INVALID_PREVIEW_ENDPOINT_REF")
    return ref


def _validate_preview_endpoint_ref(endpoint_ref: str | None) -> str:
    if (
        not isinstance(endpoint_ref, str)
        or len(endpoint_ref) > 256
        or _PREVIEW_UNIX_ENDPOINT_RE.fullmatch(endpoint_ref) is None
    ):
        raise ScopeDenied("INVALID_PREVIEW_ENDPOINT_REF")
    sock_path = _PREVIEW_UNIX_ENDPOINT_RE.fullmatch(endpoint_ref)
    assert sock_path is not None
    if ".." in sock_path.group(1).split("/") or "//" in sock_path.group(1):
        raise ScopeDenied("INVALID_PREVIEW_ENDPOINT_REF")
    return endpoint_ref


@dataclass(frozen=True)
class PreviewLeaseDetails:
    lease_id: str
    instance_id: str
    generation: int
    project_id: str
    cycle_id: str
    workspace_id: str
    task_id: str
    run_id: str
    goal_revision: int
    commit_sha: str
    target_port: int
    endpoint_ref: str
    lease_expires_at: datetime
    heartbeat_expires_at: datetime


def sign_preview_launch_receipt(
    key: bytes,
    *,
    tenant_id: str,
    owner_user_id: str,
    project_id: str,
    cycle_id: str,
    workspace_id: str,
    commit_sha: str,
    target_port: int,
    runner_kind: str,
    process_attestation_sha256: str,
    generation: int,
    launch_nonce: str,
    issued_at_epoch: int,
    expires_at_epoch: int,
    endpoint_ref: str,
) -> str:
    """Sign a short-lived, single-use Preview Worker launch receipt binding scope, commit, instance Unix socket, nonce & generation (R3-05/R4-03)."""
    if len(key) < 32:
        raise ValueError("preview attestation key must be >= 32 bytes")
    ep_ref = _validate_preview_endpoint_ref(endpoint_ref)
    msg = canonical_json(
        {
            "v": 2,
            "tenantId": tenant_id,
            "ownerUserId": owner_user_id,
            "projectId": project_id,
            "cycleId": cycle_id,
            "workspaceId": workspace_id,
            "commitSha": commit_sha,
            "targetPort": target_port,
            "endpointRef": ep_ref,
            "runnerKind": runner_kind,
            "processAttestationSha256": process_attestation_sha256,
            "generation": generation,
            "launchNonce": launch_nonce,
            "issuedAt": issued_at_epoch,
            "expiresAt": expires_at_epoch,
        }
    ).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class PreviewBroker:
    """Short-lived, tenant/owner/cycle-scoped Preview lease manager bound to single-use HMAC-attested instances (R3-05)."""

    def __init__(self, db: Database, *, attestation_key: bytes | None = None) -> None:
        self._db = db
        self._attestation_key = attestation_key

    def _verify_registration_receipt(
        self,
        cycle: Cycle,
        *,
        target_port: int,
        process_attestation_sha256: str,
        launch_receipt_hmac: str,
        launch_nonce: str,
        issued_at_epoch: int,
        expires_at_epoch: int,
        endpoint_ref: str | None,
        runner_kind: str,
        generation: int,
        heartbeat_ttl_s: int,
    ) -> str:
        if cycle.phase != Phase.CODE_READY or cycle.commit is None:
            raise StateConflict("preview instance requires CODE_READY cycle with immutable commit")
        if not 1024 <= target_port <= 65535:
            raise InvalidEvidence("invalid preview port")
        if runner_kind not in ("isolated_worker", "sandbox_cell"):
            raise InvalidEvidence("invalid preview runner_kind")
        if isinstance(generation, bool) or generation < 1:
            raise InvalidEvidence("invalid preview instance generation")
        if isinstance(heartbeat_ttl_s, bool) or not 10 <= heartbeat_ttl_s <= 300:
            raise InvalidEvidence("invalid preview heartbeat ttl")
        if not isinstance(launch_nonce, str) or _LAUNCH_NONCE_RE.fullmatch(launch_nonce) is None:
            raise ScopeDenied("INVALID_PREVIEW_LAUNCH_NONCE")
        if (
            type(issued_at_epoch) is not int
            or type(expires_at_epoch) is not int
            or expires_at_epoch <= issued_at_epoch
            or (expires_at_epoch - issued_at_epoch) > _MAX_RECEIPT_TTL_S
        ):
            raise ScopeDenied("INVALID_PREVIEW_RECEIPT_WINDOW")
        now_s = int(datetime.now(UTC).timestamp())
        if now_s > expires_at_epoch or issued_at_epoch > now_s + 10:
            raise ScopeDenied("EXPIRED_PREVIEW_LAUNCH_RECEIPT")
        ep_ref = _validate_preview_endpoint_ref(endpoint_ref)
        if len(process_attestation_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in process_attestation_sha256
        ):
            raise InvalidEvidence("invalid process_attestation_sha256")
        if len(launch_receipt_hmac) != 64 or any(ch not in "0123456789abcdef" for ch in launch_receipt_hmac):
            raise ScopeDenied("INVALID_PREVIEW_LAUNCH_RECEIPT")
        key = _resolve_preview_attestation_key(self._attestation_key)
        expected_hmac = sign_preview_launch_receipt(
            key,
            tenant_id=cycle.scope.tenant_id,
            owner_user_id=cycle.scope.owner_user_id,
            project_id=cycle.scope.project_id,
            cycle_id=cycle.cycle_id,
            workspace_id=cycle.scope.workspace_id,
            commit_sha=cycle.commit.commit_sha,
            target_port=target_port,
            endpoint_ref=ep_ref,
            runner_kind=runner_kind,
            process_attestation_sha256=process_attestation_sha256,
            generation=generation,
            launch_nonce=launch_nonce,
            issued_at_epoch=issued_at_epoch,
            expires_at_epoch=expires_at_epoch,
        )
        if not hmac.compare_digest(launch_receipt_hmac, expected_hmac):
            raise ScopeDenied("INVALID_PREVIEW_LAUNCH_RECEIPT")
        return ep_ref

    def register_instance(
        self,
        cycle: Cycle,
        *,
        target_port: int,
        process_attestation_sha256: str,
        launch_receipt_hmac: str,
        launch_nonce: str,
        issued_at_epoch: int,
        expires_at_epoch: int,
        endpoint_ref: str,
        runner_kind: str = "sandbox_cell",
        generation: int = 1,
        heartbeat_ttl_s: int = 120,
    ) -> str:
        """Register an active isolated preview instance verified by a short-lived single-use HMAC launch receipt (R3-05)."""
        ep_ref = self._verify_registration_receipt(
            cycle,
            target_port=target_port,
            process_attestation_sha256=process_attestation_sha256,
            launch_receipt_hmac=launch_receipt_hmac,
            launch_nonce=launch_nonce,
            issued_at_epoch=issued_at_epoch,
            expires_at_epoch=expires_at_epoch,
            endpoint_ref=endpoint_ref,
            runner_kind=runner_kind,
            generation=generation,
            heartbeat_ttl_s=heartbeat_ttl_s,
        )
        assert cycle.commit is not None

        inst_id = uuid7()
        hb_expires = datetime.now(UTC) + timedelta(seconds=heartbeat_ttl_s)
        issued_dt = datetime.fromtimestamp(issued_at_epoch, tz=UTC)
        receipt_exp_dt = datetime.fromtimestamp(expires_at_epoch, tz=UTC)
        try:
            with self._db.tx(tenant=cycle.scope.tenant_id) as c:
                if not control.scope_active(
                    c, cycle.scope.tenant_id, cycle.scope.owner_user_id, cycle.scope.workspace_id
                ):
                    raise ScopeDenied("scope inactive")
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"preview-port:{target_port}",))
                c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"preview-cycle:{cycle.scope.tenant_id}:{cycle.cycle_id}",),
                )
                prev_cycle = c.execute(
                    "SELECT COALESCE(MAX(generation), 0) AS max_g FROM agent.dev_preview_instances"
                    " WHERE tenant_id=%s AND project_id=%s AND cycle_id=%s",
                    (cycle.scope.tenant_id, cycle.scope.project_id, cycle.cycle_id),
                ).fetchone()
                prev_port = c.execute(
                    "SELECT COALESCE(MAX(generation), 0) AS max_g FROM agent.dev_preview_instances"
                    " WHERE tenant_id=%s AND target_port=%s",
                    (cycle.scope.tenant_id, target_port),
                ).fetchone()
                min_prev = max(
                    int(prev_cycle["max_g"]) if prev_cycle is not None else 0,
                    int(prev_port["max_g"]) if prev_port is not None else 0,
                )
                if generation <= min_prev:
                    raise StateConflict(
                        f"NON_MONOTONIC_PREVIEW_GENERATION: generation {generation} must exceed {min_prev}"
                    )
                prior = c.execute(
                    "UPDATE agent.dev_preview_instances SET status='STOPPED', stopped_at=now()"
                    " WHERE tenant_id=%s AND target_port=%s AND status='RUNNING' RETURNING id",
                    (cycle.scope.tenant_id, target_port),
                ).fetchall()
                for row in prior:
                    c.execute(
                        "UPDATE agent.preview_leases SET status='REVOKED', revoked_at=now()"
                        " WHERE tenant_id=%s AND preview_instance_id=%s AND status='ACTIVE'",
                        (cycle.scope.tenant_id, row["id"]),
                    )
                c.execute(
                    "INSERT INTO agent.dev_preview_instances(tenant_id, id, owner_user_id, project_id, cycle_id,"
                    " workspace_id, commit_sha, target_port, endpoint_ref, runner_kind, process_attestation_sha256,"
                    " generation, launch_nonce, issued_at, receipt_expires_at, launch_receipt_hmac,"
                    " heartbeat_expires_at, status)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING')",
                    (
                        cycle.scope.tenant_id,
                        inst_id,
                        cycle.scope.owner_user_id,
                        cycle.scope.project_id,
                        cycle.cycle_id,
                        cycle.scope.workspace_id,
                        cycle.commit.commit_sha,
                        target_port,
                        ep_ref,
                        runner_kind,
                        process_attestation_sha256,
                        generation,
                        launch_nonce,
                        issued_dt,
                        receipt_exp_dt,
                        launch_receipt_hmac,
                        hb_expires,
                    ),
                )
        except UniqueViolation as exc:
            msg = str(exc).lower()
            if "launch_nonce" in msg:
                raise ScopeDenied("PREVIEW_LAUNCH_RECEIPT_REPLAYED") from None
            raise ScopeDenied("PREVIEW_PORT_OCCUPIED") from None
        return inst_id

    def heartbeat_instance(
        self,
        tenant_id: str,
        owner_user_id: str,
        instance_id: str,
        *,
        generation: int,
        ttl_s: int = 120,
    ) -> bool:
        if isinstance(ttl_s, bool) or not 10 <= ttl_s <= 300:
            raise InvalidEvidence("invalid preview heartbeat ttl")
        hb_expires = datetime.now(UTC) + timedelta(seconds=ttl_s)
        with self._db.tx(tenant=tenant_id) as c:
            cur = c.execute(
                "UPDATE agent.dev_preview_instances SET heartbeat_expires_at=%s"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND generation=%s"
                " AND status='RUNNING' AND heartbeat_expires_at > now()",
                (hb_expires, tenant_id, instance_id, owner_user_id, generation),
            )
            return cur.rowcount == 1

    def stop_instance(self, tenant_id: str, owner_user_id: str, instance_id: str) -> bool:
        with self._db.tx(tenant=tenant_id) as c:
            cur = c.execute(
                "UPDATE agent.dev_preview_instances SET status='STOPPED', stopped_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND status='RUNNING'",
                (tenant_id, instance_id, owner_user_id),
            )
            c.execute(
                "UPDATE agent.preview_leases SET status='REVOKED', revoked_at=now()"
                " WHERE tenant_id=%s AND preview_instance_id=%s AND owner_user_id=%s AND status='ACTIVE'",
                (tenant_id, instance_id, owner_user_id),
            )
            return cur.rowcount == 1

    def issue_lease(
        self,
        cycle: Cycle,
        *,
        target_port: int,
        ttl_s: int = 120,
        preview_instance_id: str | None = None,
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
            if preview_instance_id is not None:
                inst = c.execute(
                    "SELECT id, generation FROM agent.dev_preview_instances"
                    " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND project_id=%s AND cycle_id=%s"
                    " AND workspace_id=%s AND commit_sha=%s AND target_port=%s"
                    " AND status='RUNNING' AND heartbeat_expires_at > now()",
                    (
                        cycle.scope.tenant_id,
                        preview_instance_id,
                        cycle.scope.owner_user_id,
                        cycle.scope.project_id,
                        cycle.cycle_id,
                        cycle.scope.workspace_id,
                        cycle.commit.commit_sha,
                        target_port,
                    ),
                ).fetchone()
            else:
                inst = c.execute(
                    "SELECT id, generation FROM agent.dev_preview_instances"
                    " WHERE tenant_id=%s AND owner_user_id=%s AND project_id=%s AND cycle_id=%s"
                    " AND workspace_id=%s AND commit_sha=%s AND target_port=%s"
                    " AND status='RUNNING' AND heartbeat_expires_at > now()"
                    " ORDER BY created_at DESC LIMIT 1",
                    (
                        cycle.scope.tenant_id,
                        cycle.scope.owner_user_id,
                        cycle.scope.project_id,
                        cycle.cycle_id,
                        cycle.scope.workspace_id,
                        cycle.commit.commit_sha,
                        target_port,
                    ),
                ).fetchone()
            if inst is None:
                raise ScopeDenied("target_port is not bound to a live attested preview instance")
            c.execute(
                "INSERT INTO agent.preview_leases(tenant_id, id, owner_user_id, project_id, workspace_id,"
                " cycle_id, preview_instance_id, instance_generation, commit_sha, target_port, token_sha256, status, expires_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s)",
                (
                    cycle.scope.tenant_id,
                    lease_id,
                    cycle.scope.owner_user_id,
                    cycle.scope.project_id,
                    cycle.scope.workspace_id,
                    cycle.cycle_id,
                    inst["id"],
                    int(inst["generation"]),
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

    def verify_lease_details(
        self,
        tenant_id: str,
        owner_user_id: str,
        lease_id: str,
        token: str,
        port: int,
        *,
        expected_cycle_id: str | None = None,
    ) -> PreviewLeaseDetails:
        """Authoritatively verify lease, instance, workspace scope, cycle, task, run & goal revision from DB (R4-04)."""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._db.tx(tenant=tenant_id) as c:
            row = c.execute(
                "SELECT l.id, l.project_id, l.workspace_id, l.commit_sha, l.target_port, l.token_sha256,"
                " l.status, l.expires_at, l.cycle_id, l.preview_instance_id, l.instance_generation,"
                " i.status AS inst_status, i.target_port AS inst_port, i.endpoint_ref AS inst_endpoint_ref,"
                " i.commit_sha AS inst_sha, i.generation AS inst_generation, i.heartbeat_expires_at,"
                " (i.heartbeat_expires_at > now()) AS inst_alive,"
                " cy.owner_user_id AS cy_owner, cy.workspace_id AS cy_workspace, cy.project_id AS cy_project,"
                " cy.phase AS cy_phase, cy.task_id AS cy_task_id, cy.run_id AS cy_run_id, cy.cycle_json,"
                " t.owner_user_id AS task_owner, t.workspace_id AS task_workspace,"
                " t.goal_revision AS live_goal_revision, t.active_run_id,"
                " r.status_code AS run_status, r.cancel_requested, r.pause_requested"
                " FROM agent.preview_leases l"
                " JOIN agent.dev_preview_instances i ON i.tenant_id=l.tenant_id AND i.id=l.preview_instance_id"
                " JOIN agent.dev_cycles cy ON cy.tenant_id=l.tenant_id AND cy.id=l.cycle_id"
                " JOIN agent.tasks t ON t.tenant_id=cy.tenant_id AND t.id=cy.task_id"
                " JOIN agent.runs r ON r.tenant_id=cy.tenant_id AND r.task_id=cy.task_id AND r.id=cy.run_id"
                " WHERE l.tenant_id=%s AND l.id=%s AND l.owner_user_id=%s",
                (tenant_id, lease_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise ScopeDenied("invalid, expired, or superseded preview lease")
            if not control.scope_active(c, tenant_id, owner_user_id, str(row["workspace_id"])):
                raise ScopeDenied("SCOPE_REVOKED: workspace or tenant membership inactive")
        if (
            row["status"] != "ACTIVE"
            or row["inst_status"] != "RUNNING"
            or not row["inst_alive"]
            or int(row["inst_generation"]) != int(row["instance_generation"])
            or int(row["target_port"]) != port
            or int(row["inst_port"]) != port
            or str(row["inst_sha"]) != str(row["commit_sha"])
            or row["expires_at"] <= datetime.now(UTC)
            or not secrets.compare_digest(str(row["token_sha256"]), token_hash)
        ):
            raise ScopeDenied("invalid, expired, or superseded preview lease")
        if expected_cycle_id is not None and str(row["cycle_id"] or "") != expected_cycle_id:
            raise ScopeDenied("preview lease bound to a different cycle")
        if (
            str(row["cy_owner"]) != owner_user_id
            or str(row["cy_workspace"]) != str(row["workspace_id"])
            or str(row["cy_project"]) != str(row["project_id"])
            or str(row["cy_phase"]) != "CODE_READY"
            or str(row["task_owner"]) != owner_user_id
            or str(row["task_workspace"]) != str(row["workspace_id"])
            or str(row["active_run_id"]) != str(row["cy_run_id"])
            or bool(row["cancel_requested"])
            or bool(row["pause_requested"])
            or str(row["run_status"]) in _INACTIVE_RUN_STATUSES
        ):
            raise ScopeDenied("PREVIEW_RUN_OR_CYCLE_INACTIVE")
        cj = dict(row["cycle_json"]) if isinstance(row.get("cycle_json"), dict) else {}
        raw_run = cj.get("run")
        cy_goal_rev = int(raw_run.get("goal_revision", 0)) if isinstance(raw_run, dict) else 0
        if cy_goal_rev < 1 or int(row["live_goal_revision"]) != cy_goal_rev:
            raise ScopeDenied("STALE_GOAL_REVISION: task goal changed since preview cycle")
        ep_ref = _validate_preview_endpoint_ref(str(row["inst_endpoint_ref"]))
        return PreviewLeaseDetails(
            lease_id=str(row["id"]),
            instance_id=str(row["preview_instance_id"]),
            generation=int(row["inst_generation"]),
            project_id=str(row["project_id"]),
            cycle_id=str(row["cycle_id"] or ""),
            workspace_id=str(row["workspace_id"]),
            task_id=str(row["cy_task_id"]),
            run_id=str(row["cy_run_id"]),
            goal_revision=int(row["live_goal_revision"]),
            commit_sha=str(row["commit_sha"]),
            target_port=int(row["inst_port"]),
            endpoint_ref=ep_ref,
            lease_expires_at=row["expires_at"],
            heartbeat_expires_at=row["heartbeat_expires_at"],
        )

    def verify_lease(
        self,
        tenant_id: str,
        owner_user_id: str,
        lease_id: str,
        token: str,
        port: int,
        *,
        expected_cycle_id: str | None = None,
    ) -> str:
        return self.verify_lease_details(
            tenant_id,
            owner_user_id,
            lease_id,
            token,
            port,
            expected_cycle_id=expected_cycle_id,
        ).commit_sha

    def revoke_lease(self, tenant_id: str, owner_user_id: str, lease_id: str) -> bool:
        with self._db.tx(tenant=tenant_id) as c:
            cur = c.execute(
                "UPDATE agent.preview_leases SET status='REVOKED', revoked_at=now()"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND status='ACTIVE'",
                (tenant_id, lease_id, owner_user_id),
            )
            return cur.rowcount == 1
