"""Trusted Workspace owner operations for execution snapshots.

A repository binding must already be provisioned by the trusted operator or
provider authority. These functions are not a model-facing API. Git proofs and
projection bytes are untrusted data and never executed in the control process.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from aiagent_core import control
from aiagent_core.db import Database, Row
from aiagent_domain import sha256_hex, uuid7
from psycopg import Connection, errors

from .code_intelligence.deadline import require_deadline
from .code_intelligence.git_proof import GitTreeProof
from .code_intelligence.indexer_recipe import approved_recipe_digest, python_scip_recipe_digest
from .code_intelligence.models import Identity, ReadError
from .code_intelligence.projection import ProjectionManifest, projection_manifest_digest
from .code_intelligence.projection_stage import stage_git_projection
from .code_intelligence.projection_stream import ProjectionStreamReceipt
from .contracts import InvalidEvidence, ScopeDenied, StaleReference


@dataclass(frozen=True)
class SnapshotRef:
    id: str
    binding_id: str
    workspace_id: str
    revision: int
    commit: str
    tree: str
    manifest_sha256: str
    source_sha256: str
    hash_strength: str


@dataclass(frozen=True)
class GitCommitBinding:
    id: str
    generation: int
    workspace_id: str
    repository: str
    branch: str
    parent: str
    workspace_revision: int


def resolve_git_commit_binding(
    db: Database,
    claim: control.Claim,
    binding_id: str,
    *,
    workspace_id: str,
    repository: str,
    generation: int,
    parent: str,
    deadline: float,
) -> GitCommitBinding:
    """Resolve an operator-approved Git branch under the current Run fence.

    This read is not permission to send: dispatch must recheck the same binding,
    Run and lease under its final admission transaction and fence the worker.
    """
    require_deadline(deadline)
    with db.tx(tenant=claim.tenant_id) as c:
        _db_deadline(c, deadline)
        _lock_scope(c, claim, workspace_id)
        run = _locked_claim(c, claim)
        binding = _binding_row(c, claim, binding_id, lock=True)
        branch = binding["approved_branch"]
        if (
            str(run["workspace_id"]) != workspace_id
            or str(binding["workspace_id"]) != workspace_id
            or str(binding["repository_ref"]) != repository
            or int(binding["generation"]) != generation
            or str(binding["approved_commit"]) != parent
            or not isinstance(branch, str)
            or not branch
        ):
            raise ScopeDenied("Git commit binding unavailable or stale")
        return GitCommitBinding(
            binding_id, generation, workspace_id, repository, branch, parent, int(binding["workspace_revision"])
        )


def _db_deadline(c: Connection[Row], deadline: float) -> None:
    require_deadline(deadline)
    milliseconds = max(1, int((deadline - time.monotonic()) * 1000))
    c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{milliseconds}ms",))
    c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{milliseconds}ms",))


def _binding_row(c: Connection[Row], claim: control.Claim, binding_id: str, *, lock: bool) -> Row:
    query = (
        "SELECT * FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s FOR UPDATE"
        if lock
        else "SELECT * FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s"
    )
    row = c.execute(
        query,
        (claim.tenant_id, binding_id),
    ).fetchone()
    if row is None or not row["active"] or str(row["owner_user_id"]) != claim.owner_user_id:
        raise ScopeDenied("repository binding unavailable")
    return row


def _identity(claim: control.Claim, binding: Row) -> Identity:
    return Identity(
        claim.tenant_id,
        claim.owner_user_id,
        str(binding["id"]),
        str(binding["workspace_revision"]),
        str(binding["approved_commit"]),
    )


def _lock_scope(c: Connection[Row], claim: control.Claim, workspace_id: str) -> None:
    try:
        c.execute("SELECT agent.execution_lock_scope(%s,%s,%s)", (claim.tenant_id, claim.owner_user_id, workspace_id))
    except errors.InsufficientPrivilege as exc:
        raise ScopeDenied("workspace scope inactive or unavailable") from exc


def _locked_claim(c: Connection[Row], claim: control.Claim) -> Row:
    task = c.execute(
        "SELECT id FROM agent.tasks WHERE tenant_id=%s AND id=%s FOR UPDATE", (claim.tenant_id, claim.task_id)
    ).fetchone()
    if task is None:
        raise ScopeDenied("task unavailable")
    return control.check_current(c, claim, for_update=True)


def register_verified_snapshot(
    db: Database,
    claim: control.Claim,
    binding_id: str,
    manifest: ProjectionManifest,
    proof: GitTreeProof,
    receipt: ProjectionStreamReceipt,
    stream: BinaryIO,
    private_parent: Path,
    *,
    deadline: float,
) -> SnapshotRef:
    """Independently verify object bytes, then CAS a scoped current snapshot in the canonical DB.

    The caller must be a trusted Workspace service using the Tool Gateway DB role,
    an authenticated claim and a pre-provisioned operator/provider binding. A
    projection digest is integrity evidence, not repository/Cell attestation.
    The stream transport must impose its own timeout while reads are blocked.
    """
    require_deadline(deadline)
    with db.tx(tenant=claim.tenant_id) as c:
        _db_deadline(c, deadline)
        run = control.check_current(c, claim)
        binding = _binding_row(c, claim, binding_id, lock=False)
        if str(binding["workspace_id"]) != str(run["workspace_id"]):
            raise ScopeDenied("binding outside Run workspace")
        expected = _identity(claim, binding)
        generation = int(binding["generation"])
    try:
        with stage_git_projection(
            private_parent, stream, manifest, receipt, proof, expected, deadline=deadline
        ) as staged:
            strength = staged.check.collision_resistance
    except (ReadError, OSError, ValueError) as exc:
        raise InvalidEvidence("snapshot proof or source unavailable") from exc
    manifest_sha = projection_manifest_digest(manifest)
    require_deadline(deadline)
    with db.tx(tenant=claim.tenant_id) as c:
        _db_deadline(c, deadline)
        _lock_scope(c, claim, str(binding["workspace_id"]))
        run = _locked_claim(c, claim)
        require_deadline(deadline)
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("execution-workspace:" + claim.tenant_id + ":" + str(run["workspace_id"]),),
        )
        require_deadline(deadline)
        current = _binding_row(c, claim, binding_id, lock=True)
        if (
            str(current["workspace_id"]) != str(run["workspace_id"])
            or int(current["generation"]) != generation
            or _identity(claim, current) != expected
        ):
            raise StaleReference("repository binding changed during verification")
        head = c.execute(
            "SELECT s.* FROM agent.workspace_snapshot_heads h JOIN agent.workspace_snapshot_attestations s"
            " ON s.tenant_id=h.tenant_id AND s.id=h.snapshot_id"
            " WHERE h.tenant_id=%s AND h.workspace_id=%s FOR UPDATE OF h",
            (claim.tenant_id, str(run["workspace_id"])),
        ).fetchone()
        revision = int(current["workspace_revision"])
        if head is not None and int(head["workspace_revision"]) >= revision:
            if (
                int(head["workspace_revision"]) == revision
                and str(head["binding_id"]) == binding_id
                and int(head["binding_generation"]) == generation
                and str(head["commit_sha"]) == expected.commit
                and str(head["tree_sha"]) == manifest.tree
                and int(head["source_bytes"]) == receipt.byte_length
                and str(head["hash_strength"]) == strength
                and str(head["manifest_sha256"]).strip() == manifest_sha
                and str(head["source_sha256"]).strip() == receipt.sha256
            ):
                return _snapshot_ref(head)
            raise StaleReference("workspace revision already used or superseded")
        require_deadline(deadline)
        snapshot_id = uuid7()
        row = c.execute(
            "INSERT INTO agent.workspace_snapshot_attestations(tenant_id,id,owner_user_id,workspace_id,binding_id,"
            "binding_generation,workspace_revision,commit_sha,tree_sha,manifest_sha256,source_sha256,source_bytes,hash_strength)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                claim.tenant_id,
                snapshot_id,
                claim.owner_user_id,
                str(run["workspace_id"]),
                binding_id,
                generation,
                revision,
                expected.commit,
                manifest.tree,
                manifest_sha,
                receipt.sha256,
                receipt.byte_length,
                strength,
            ),
        ).fetchone()
        assert row is not None
        c.execute(
            "INSERT INTO agent.workspace_snapshot_heads(tenant_id,workspace_id,snapshot_id) VALUES (%s,%s,%s)"
            " ON CONFLICT (tenant_id,workspace_id) DO UPDATE SET snapshot_id=excluded.snapshot_id",
            (claim.tenant_id, str(run["workspace_id"]), snapshot_id),
        )
        return _snapshot_ref(row)


def _snapshot_ref(row: Row) -> SnapshotRef:
    return SnapshotRef(
        str(row["id"]),
        str(row["binding_id"]),
        str(row["workspace_id"]),
        int(row["workspace_revision"]),
        str(row["commit_sha"]),
        str(row["tree_sha"]),
        str(row["manifest_sha256"]).strip(),
        str(row["source_sha256"]).strip(),
        str(row["hash_strength"]),
    )


@dataclass(frozen=True)
class PreparedExecution:
    """A DB-bound preparation, not an execution grant or verified Cell receipt."""

    id: str
    attempt_id: str
    intent_id: str
    snapshot_id: str
    recipe_id: str
    commit: str
    tree: str
    revision: int
    recipe_sha256: str
    toolchain_sha256: str
    image_digest: str
    test_id: str | None
    binding_sha256: str
    source_sha256: str
    manifest_sha256: str


def prepare_execution(
    db: Database,
    claim: control.Claim,
    attempt_id: str,
    snapshot_id: str,
    recipe_id: str,
    *,
    deadline: float,
) -> PreparedExecution:
    """Bind a new server-issued ID to canonical state; this does not permit dispatch.

    Operator/provider binding and recipe provisioning must already exist. Cell
    image attestation, broker grants and live revocation are not implemented.
    """
    require_deadline(deadline)
    with db.tx(tenant=claim.tenant_id) as c:
        _db_deadline(c, deadline)
        initial = control.check_current(c, claim)
        workspace_id = str(initial["workspace_id"])
        _lock_scope(c, claim, workspace_id)
        run = _locked_claim(c, claim)
        if str(run["workspace_id"]) != workspace_id:
            raise ScopeDenied("Run workspace changed")
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("execution-workspace:" + claim.tenant_id + ":" + workspace_id,),
        )
        require_deadline(deadline)
        return _prepare_locked(c, claim, workspace_id, attempt_id, snapshot_id, recipe_id, deadline)


def _check_plan(c: Connection[Row], claim: control.Claim, workspace_id: str, recipe: Row) -> None:
    cycle = c.execute(
        "SELECT * FROM agent.dev_cycles WHERE tenant_id=%s AND id=%s FOR UPDATE",
        (claim.tenant_id, str(recipe["cycle_id"])),
    ).fetchone()
    if (
        cycle is None
        or str(cycle["owner_user_id"]) != claim.owner_user_id
        or str(cycle["workspace_id"]) != workspace_id
        or str(cycle["task_id"]) != claim.task_id
        or str(cycle["run_id"]) != claim.run_id
    ):
        raise ScopeDenied("execution recipe is outside Run scope")
    plan = cycle["cycle_json"].get("plan")
    if (
        not isinstance(plan, dict)
        or plan != recipe["plan_json"]
        or sha256_hex(plan) != str(recipe["plan_sha256"]).strip()
    ):
        raise StaleReference("approved Plan changed")
    if type(plan.get("goal_revision")) is not int or plan["goal_revision"] != claim.goal_revision:
        raise StaleReference("approved Plan goal changed")
    required_tests = plan.get("required_tests")
    if not isinstance(required_tests, list) or not all(isinstance(item, str) and item for item in required_tests):
        raise InvalidEvidence("invalid approved Plan tests")
    if recipe["kind"] == "test" and recipe["test_id"] not in required_tests:
        raise ScopeDenied("test ID is not in approved Plan")


def _check_attempt(c: Connection[Row], claim: control.Claim, attempt_id: str, recipe: Row) -> Row:
    attempt = c.execute(
        "SELECT a.*, i.task_id, i.run_id, i.goal_revision, i.tool_key, i.request_sha256, i.safe_arguments_json, i.status_code AS intent_status"
        " FROM agent.tool_attempts a JOIN agent.tool_intents i ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id"
        " WHERE a.tenant_id=%s AND a.id=%s FOR UPDATE OF a, i",
        (claim.tenant_id, attempt_id),
    ).fetchone()
    if (
        attempt is None
        or str(attempt["task_id"]) != claim.task_id
        or str(attempt["run_id"]) != claim.run_id
        or int(attempt["goal_revision"]) != claim.goal_revision
        or int(attempt["run_lease_epoch"]) != claim.lease_epoch
        or str(attempt["worker_ref"]) != claim.worker_ref
        or attempt["status_code"] != "SENDING"
        or attempt["intent_status"] != "SENDING"
        or attempt["tool_key"] != "sandbox_exec"
        or str(attempt["request_sha256"]).strip() != str(recipe["request_sha256"]).strip()
    ):
        raise ScopeDenied("Tool Attempt or recipe request mismatch")
    template_sha = str(recipe["template_sha256"]).strip()
    if recipe["kind"] == "index" and template_sha != python_scip_recipe_digest():
        raise ScopeDenied("unapproved indexer template")
    request_sha = str(attempt["request_sha256"]).strip()
    if sha256_hex({"tool": "sandbox_exec", "args": attempt["safe_arguments_json"]}) != request_sha:
        raise InvalidEvidence("stored Tool request digest mismatch")
    expected_recipe = approved_recipe_digest(
        kind=str(recipe["kind"]),
        test_id=str(recipe["test_id"]) if recipe["test_id"] is not None else None,
        plan_sha256=str(recipe["plan_sha256"]).strip(),
        request_sha256=request_sha,
        template_sha256=template_sha,
        toolchain_sha256=str(recipe["toolchain_sha256"]).strip(),
        image_digest=str(recipe["image_digest"]),
    )
    if expected_recipe != str(recipe["recipe_sha256"]).strip():
        raise InvalidEvidence("approved recipe digest mismatch")
    return attempt


def _prepare_locked(
    c: Connection[Row],
    claim: control.Claim,
    workspace_id: str,
    attempt_id: str,
    snapshot_id: str,
    recipe_id: str,
    deadline: float,
) -> PreparedExecution:
    snapshot = c.execute(
        "SELECT s.*, b.active AS binding_active, b.approved_commit, b.generation AS current_generation,"
        " b.workspace_revision AS current_revision FROM agent.workspace_snapshot_heads h"
        " JOIN agent.workspace_snapshot_attestations s ON s.tenant_id=h.tenant_id AND s.id=h.snapshot_id"
        " JOIN agent.repository_bindings b ON b.tenant_id=s.tenant_id AND b.id=s.binding_id"
        " WHERE h.tenant_id=%s AND h.workspace_id=%s AND s.id=%s FOR UPDATE OF h, b",
        (claim.tenant_id, workspace_id, snapshot_id),
    ).fetchone()
    if (
        snapshot is None
        or str(snapshot["owner_user_id"]) != claim.owner_user_id
        or not snapshot["binding_active"]
        or str(snapshot["commit_sha"]) != str(snapshot["approved_commit"])
        or int(snapshot["binding_generation"]) != int(snapshot["current_generation"])
        or int(snapshot["workspace_revision"]) != int(snapshot["current_revision"])
    ):
        raise StaleReference("workspace snapshot is not current")
    recipe = c.execute(
        "SELECT * FROM agent.execution_recipe_approvals WHERE tenant_id=%s AND id=%s FOR UPDATE",
        (claim.tenant_id, recipe_id),
    ).fetchone()
    if recipe is None or not recipe["active"] or int(recipe["goal_revision"]) != claim.goal_revision:
        raise ScopeDenied("execution recipe is not approved")
    _check_plan(c, claim, workspace_id, recipe)
    attempt = _check_attempt(c, claim, attempt_id, recipe)
    require_deadline(deadline)
    row = c.execute(
        "SELECT * FROM agent.execution_instances WHERE tenant_id=%s AND tool_attempt_id=%s FOR UPDATE",
        (claim.tenant_id, attempt_id),
    ).fetchone()
    if row is not None:
        if str(row["snapshot_id"]) != snapshot_id or str(row["recipe_id"]) != recipe_id or row["state"] != "PREPARED":
            raise StaleReference("Tool Attempt already bound differently")
        return _prepared(row, snapshot, recipe)
    candidate: Row = {
        "id": uuid7(),
        "tenant_id": claim.tenant_id,
        "owner_user_id": claim.owner_user_id,
        "workspace_id": workspace_id,
        "task_id": claim.task_id,
        "run_id": claim.run_id,
        "goal_revision": claim.goal_revision,
        "lease_epoch": claim.lease_epoch,
        "worker_ref": claim.worker_ref,
        "restore_incarnation": claim.restore_incarnation,
        "tool_intent_id": str(attempt["tool_intent_id"]),
        "tool_attempt_id": attempt_id,
        "snapshot_id": snapshot_id,
        "recipe_id": recipe_id,
    }
    binding_digest = _binding_digest(candidate, snapshot, recipe)
    row = c.execute(
        "INSERT INTO agent.execution_instances(tenant_id,id,owner_user_id,workspace_id,task_id,run_id,goal_revision,"
        "lease_epoch,worker_ref,restore_incarnation,tool_intent_id,tool_attempt_id,snapshot_id,recipe_id,binding_sha256,state)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PREPARED') RETURNING *",
        (
            claim.tenant_id,
            candidate["id"],
            claim.owner_user_id,
            workspace_id,
            claim.task_id,
            claim.run_id,
            claim.goal_revision,
            claim.lease_epoch,
            claim.worker_ref,
            claim.restore_incarnation,
            str(attempt["tool_intent_id"]),
            attempt_id,
            snapshot_id,
            recipe_id,
            binding_digest,
        ),
    ).fetchone()
    assert row is not None
    return _prepared(row, snapshot, recipe)


def _binding_payload(row: Row, snapshot: Row, recipe: Row) -> dict[str, object]:
    binding = {
        "version": "dev13-execution-binding-v1",
        "execution": str(row["id"]),
        "tenant": str(row["tenant_id"]),
        "owner": str(row["owner_user_id"]),
        "workspace": str(row["workspace_id"]),
        "task": str(row["task_id"]),
        "run": str(row["run_id"]),
        "goal_revision": int(row["goal_revision"]),
        "lease_epoch": int(row["lease_epoch"]),
        "worker_ref": str(row["worker_ref"]),
        "restore_incarnation": int(row["restore_incarnation"]),
        "intent": str(row["tool_intent_id"]),
        "attempt": str(row["tool_attempt_id"]),
        "snapshot": str(row["snapshot_id"]),
        "repository_binding": str(snapshot["binding_id"]),
        "workspace_revision": int(snapshot["workspace_revision"]),
        "commit": str(snapshot["commit_sha"]),
        "tree": str(snapshot["tree_sha"]),
        "source_sha256": str(snapshot["source_sha256"]).strip(),
        "manifest_sha256": str(snapshot["manifest_sha256"]).strip(),
        "recipe_id": str(row["recipe_id"]),
        "plan_sha256": str(recipe["plan_sha256"]).strip(),
        "request_sha256": str(recipe["request_sha256"]).strip(),
        "recipe_sha256": str(recipe["recipe_sha256"]).strip(),
        "toolchain_sha256": str(recipe["toolchain_sha256"]).strip(),
        "image_digest": str(recipe["image_digest"]),
        "test_id": str(recipe["test_id"]) if recipe["test_id"] is not None else None,
    }
    return binding


def _binding_digest(row: Row, snapshot: Row, recipe: Row) -> str:
    return sha256_hex(_binding_payload(row, snapshot, recipe))


def _prepared(row: Row, snapshot: Row, recipe: Row) -> PreparedExecution:
    binding_digest = _binding_digest(row, snapshot, recipe)
    if not hmac.compare_digest(binding_digest, str(row["binding_sha256"]).strip()):
        raise InvalidEvidence("execution binding digest mismatch")
    return PreparedExecution(
        str(row["id"]),
        str(row["tool_attempt_id"]),
        str(row["tool_intent_id"]),
        str(row["snapshot_id"]),
        str(row["recipe_id"]),
        str(snapshot["commit_sha"]),
        str(snapshot["tree_sha"]),
        int(snapshot["workspace_revision"]),
        str(recipe["recipe_sha256"]).strip(),
        str(recipe["toolchain_sha256"]).strip(),
        str(recipe["image_digest"]),
        str(recipe["test_id"]) if recipe["test_id"] is not None else None,
        binding_digest,
        str(snapshot["source_sha256"]).strip(),
        str(snapshot["manifest_sha256"]).strip(),
    )
