"""Read an operator-provisioned, encrypted Git patch under its canonical scope.

This is not an approval or a dispatch mechanism. A caller must perform final
Gateway admission and revalidate the binding and artifact when sending.
The key ring must be dedicated to Git patches; nonce uniqueness across other
artifact stores cannot be enforced by this table.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_development.contracts import InvalidEvidence, ScopeDenied
from aiagent_development.paths import safe_relative_path
from cryptography.exceptions import InvalidTag

from .context_keys import KeyRing


@dataclass(frozen=True)
class VerifiedGitPatch:
    artifact_id: str
    parent: str
    digest: str
    paths: tuple[str, ...]
    data: bytes = field(repr=False)


def git_patch_aad(row: dict[str, Any]) -> bytes:
    """Canonical AEAD context; trusted provisioning must use exactly this format."""
    values = [
        "git-patch-v1",
        str(row["tenant_id"]),
        str(row["id"]),
        str(row["owner_user_id"]),
        str(row["workspace_id"]),
        str(row["binding_id"]),
        int(row["binding_generation"]),
        str(row["cycle_id"]),
        str(row["task_id"]),
        str(row["run_id"]),
        str(row["parent_sha"]),
        str(row["diff_sha256"]),
        list(row["paths"]),
        int(row["byte_length"]),
        str(row["key_id"]),
    ]
    return json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def fetch_git_patch(
    db: Database,
    claim: control.Claim,
    keys: KeyRing,
    artifact_id: str,
    *,
    workspace_id: str,
    binding_id: str,
    binding_generation: int,
    cycle_id: str,
    plan_ref: str,
    deadline: float,
) -> VerifiedGitPatch:
    if not time.monotonic() < deadline <= time.monotonic() + 3600:
        raise InvalidEvidence("patch deadline invalid")
    with db.tx(tenant=claim.tenant_id) as c:
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{remaining}ms",))
        c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{remaining}ms",))
        run = control.check_current(c, claim)
        row = c.execute(
            "SELECT p.*,c.cycle_json,c.phase,c.owner_user_id AS cycle_owner,c.workspace_id AS cycle_workspace,"
            "c.task_id AS cycle_task,c.run_id AS cycle_run,b.active AS binding_active,"
            "b.generation AS current_generation,b.approved_commit,b.approved_branch FROM agent.git_patch_artifacts p "
            "JOIN agent.dev_cycles c ON c.tenant_id=p.tenant_id AND c.id=p.cycle_id "
            "JOIN agent.repository_bindings b ON b.tenant_id=p.tenant_id AND b.id=p.binding_id "
            "WHERE p.tenant_id=%s AND p.id=%s",
            (claim.tenant_id, artifact_id),
        ).fetchone()
        if row is None:
            raise ScopeDenied("patch unavailable")
        patch = row["cycle_json"].get("patch")
        plan = row["cycle_json"].get("plan")
        fence = row["cycle_json"].get("run")
        if (
            not row["active"]
            or not row["binding_active"]
            or int(row["current_generation"]) != binding_generation
            or row["approved_commit"] != row["parent_sha"]
            or row["approved_branch"] is None
            or str(run["workspace_id"]) != workspace_id
            or str(row["owner_user_id"]) != claim.owner_user_id
            or str(row["workspace_id"]) != workspace_id
            or str(row["binding_id"]) != binding_id
            or int(row["binding_generation"]) != binding_generation
            or str(row["cycle_id"]) != cycle_id
            or str(row["task_id"]) != claim.task_id
            or str(row["run_id"]) != claim.run_id
            or str(row["cycle_owner"]) != claim.owner_user_id
            or str(row["cycle_workspace"]) != workspace_id
            or str(row["cycle_task"]) != claim.task_id
            or str(row["cycle_run"]) != claim.run_id
            or row["phase"] not in ("PATCH_READY", "COMMIT_PENDING")
            or not isinstance(plan, dict)
            or not plan_ref
            or plan.get("plan_ref") != plan_ref
            or plan.get("goal_revision") != claim.goal_revision
            or not isinstance(fence, dict)
            or fence.get("task_id") != claim.task_id
            or fence.get("run_id") != claim.run_id
            or fence.get("goal_revision") != claim.goal_revision
            or fence.get("lease_epoch") != claim.lease_epoch
            or fence.get("restore_incarnation") != claim.restore_incarnation
            or fence.get("worker_ref") != claim.worker_ref
            or not isinstance(patch, dict)
            or patch.get("artifact_ref") != artifact_id
            or patch.get("parent_sha") != row["parent_sha"]
            or patch.get("diff_sha256") != row["diff_sha256"]
            or patch.get("paths") != list(row["paths"])
        ):
            raise ScopeDenied("patch scope unavailable")
        paths = tuple(row["paths"])
        if len(set(paths)) != len(paths) or any(safe_relative_path(p) != p for p in paths):
            raise InvalidEvidence("patch paths invalid")
        try:
            data = keys.cipher(str(row["key_id"])).decrypt(
                bytes(row["nonce"]), bytes(row["ciphertext"]), git_patch_aad(row)
            )
        except (InvalidTag, KeyError, ValueError, TypeError) as exc:
            raise InvalidEvidence("patch integrity invalid") from exc
        if (
            len(data) != int(row["byte_length"])
            or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(row["diff_sha256"]))
            or time.monotonic() >= deadline
        ):
            raise InvalidEvidence("patch integrity invalid")
        return VerifiedGitPatch(artifact_id, str(row["parent_sha"]), str(row["diff_sha256"]), paths, data)
