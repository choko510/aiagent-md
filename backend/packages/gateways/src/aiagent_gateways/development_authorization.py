"""Canonical preflight for a Development command's approved recipe and snapshot.

Final Tool Intent, attempt, receipt and execution authorization remain owned by
the existing Gateway and Execution lifecycle.
"""

from __future__ import annotations

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.contracts import ProjectScope, RunFence, ScopeDenied
from aiagent_domain import is_uuid7


class DevelopmentCommandAuthorization:
    """Bind Workflow preflight to an operator-approved command recipe."""

    def __init__(self, db: Database, claim: Claim, *, recipe_id: str, snapshot_id: str, cycle_id: str) -> None:
        if config.profile() == "production" or not is_uuid7(recipe_id) or not is_uuid7(snapshot_id) or not cycle_id:
            raise ValueError("Development command authority unavailable")
        self._db, self._claim, self._recipe, self._snapshot, self._cycle = db, claim, recipe_id, snapshot_id, cycle_id

    def require_operation(
        self, scope: ProjectScope, run: RunFence, operation: str, target_sha: str, intent_ref: str
    ) -> None:
        claim = self._claim
        if (
            config.profile() == "production"
            or scope.tenant_id != claim.tenant_id
            or scope.owner_user_id != claim.owner_user_id
            or (run.task_id, run.run_id, run.goal_revision, run.lease_epoch, run.restore_incarnation, run.worker_ref)
            != (
                claim.task_id,
                claim.run_id,
                claim.goal_revision,
                claim.lease_epoch,
                claim.restore_incarnation,
                claim.worker_ref,
            )
            or operation not in ("sandbox.build", "sandbox.static-check", "sandbox.test")
            or not intent_ref
        ):
            raise ScopeDenied("Development command authority unavailable")
        with self._db.tx(tenant=claim.tenant_id) as c:
            current = control.check_current(c, claim)
            row = c.execute(
                "SELECT r.kind,r.test_id,r.active,r.goal_revision,r.plan_json,c.cycle_json,c.owner_user_id,c.workspace_id,"
                "c.task_id,c.run_id,s.commit_sha,s.binding_generation,s.workspace_revision,b.generation,b.active AS binding_active,"
                "s.owner_user_id AS snapshot_owner,b.owner_user_id AS binding_owner,b.workspace_id AS binding_workspace,"
                "b.repository_ref,b.approved_commit,b.workspace_revision AS binding_revision FROM agent.execution_recipe_approvals r "
                "JOIN agent.dev_cycles c ON c.tenant_id=r.tenant_id AND c.id=r.cycle_id "
                "JOIN agent.workspace_snapshot_heads h ON h.tenant_id=c.tenant_id AND h.workspace_id=c.workspace_id "
                "JOIN agent.workspace_snapshot_attestations s ON s.tenant_id=h.tenant_id AND s.id=h.snapshot_id "
                "JOIN agent.repository_bindings b ON b.tenant_id=s.tenant_id AND b.id=s.binding_id "
                "WHERE r.tenant_id=%s AND r.id=%s AND r.cycle_id=%s AND s.id=%s",
                (claim.tenant_id, self._recipe, self._cycle, self._snapshot),
            ).fetchone()
        if (
            row is None
            or str(current["workspace_id"]) != scope.workspace_id
            or str(row["workspace_id"]) != scope.workspace_id
            or str(row["owner_user_id"]) != claim.owner_user_id
            or str(row["snapshot_owner"]) != claim.owner_user_id
            or str(row["binding_owner"]) != claim.owner_user_id
            or str(row["binding_workspace"]) != scope.workspace_id
            or row["repository_ref"] != scope.repository
            or str(row["task_id"]) != claim.task_id
            or str(row["run_id"]) != claim.run_id
            or not row["active"]
            or not row["binding_active"]
            or row["kind"] != operation.removeprefix("sandbox.")
            or row["goal_revision"] != claim.goal_revision
            or row["cycle_json"].get("plan") != row["plan_json"]
            or row["commit_sha"] != target_sha
            or row["approved_commit"] != target_sha
            or row["binding_generation"] != row["generation"]
            or row["workspace_revision"] != row["binding_revision"]
            or (
                operation == "sandbox.test"
                and (
                    not isinstance(row["plan_json"], dict)
                    or not isinstance(row["plan_json"].get("required_tests"), list)
                    or row["test_id"] not in row["plan_json"]["required_tests"]
                )
            )
        ):
            raise ScopeDenied("Development command authority unavailable")
