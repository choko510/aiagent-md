"""Construct Development commit evidence only from settled canonical Git effects."""

from __future__ import annotations

import time

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.contracts import CommitEvidence, EffectUnknown, PatchArtifact, ProjectScope
from aiagent_domain import is_uuid7

from .git_receipts import GitReceiptStore


class GitCommitEvidenceReader:
    """Bridge verified Git receipts to Development without inventing a receipt."""

    def __init__(self, db: Database, claim: Claim, receipts: GitReceiptStore) -> None:
        if config.profile() == "production":
            raise ValueError("Git receipt integration is not production verified")
        self._db, self._claim, self._receipts = db, claim, receipts

    def _settled(self, scope: ProjectScope, operation_id: str, receipt_id: str, deadline: float) -> tuple[str, str]:
        claim = self._claim
        with self._db.tx(tenant=claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
            run = control.check_current(c, claim)
            row = c.execute(
                "SELECT o.*,i.task_id,i.run_id,i.goal_revision,i.status_code AS intent_status,"
                "a.status_code AS attempt_status,a.tool_intent_id,a.run_lease_epoch,a.worker_ref,"
                "a.fence_token,e.outcome_code,e.attempt_fence,e.request_sha256 AS effect_sha,"
                "e.effect_key AS ledger_effect_key,e.external_receipt_ref,"
                "i.request_sha256 AS intent_sha,b.owner_user_id,b.workspace_id,b.repository_ref,"
                "b.generation AS current_generation,b.workspace_revision,b.approved_branch,b.active AS binding_active "
                "FROM agent.git_operations o "
                "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                "JOIN agent.side_effect_records e ON e.tenant_id=o.tenant_id AND e.tool_intent_id=o.intent_id "
                "JOIN agent.repository_bindings b ON b.tenant_id=o.tenant_id AND b.id=o.binding_id "
                "WHERE o.tenant_id=%s AND o.id=%s",
                (claim.tenant_id, operation_id),
            ).fetchone()
        if (
            row is None
            or str(run["workspace_id"]) != scope.workspace_id
            or scope.tenant_id != claim.tenant_id
            or scope.owner_user_id != claim.owner_user_id
            or str(row["owner_user_id"]) != claim.owner_user_id
            or str(row["workspace_id"]) != scope.workspace_id
            or row["repository_ref"] != scope.repository
            or row["approved_branch"] != scope.branch
            or not row["binding_active"]
            or str(row["workspace_revision"]) != scope.workspace_revision
            or int(row["current_generation"]) != int(row["binding_generation"])
            or str(row["task_id"]) != claim.task_id
            or str(row["run_id"]) != claim.run_id
            or int(row["goal_revision"]) != claim.goal_revision
            or int(row["run_lease_epoch"]) != claim.lease_epoch
            or row["worker_ref"] != claim.worker_ref
            or str(row["tool_intent_id"]) != str(row["intent_id"])
            or row["state"] != "RECEIPT_PERSISTED"
            or row["intent_status"] != "CONFIRMED"
            or row["attempt_status"] != "CONFIRMED"
            or row["outcome_code"] != "CONFIRMED"
            or row["attempt_fence"] != row["fence_token"]
            or row["ledger_effect_key"] != row["effect_key"]
            or row["external_receipt_ref"] != "git:" + receipt_id
            or row["intent_sha"] != row["request_sha256"]
            or row["effect_sha"] != row["request_sha256"]
        ):
            raise EffectUnknown("canonical Git effect is not confirmed")
        return str(row["attempt_id"]), str(row["patch_id"])

    def read(
        self, scope: ProjectScope, patch: PatchArtifact, operation_id: str, receipt_id: str, *, deadline: float
    ) -> CommitEvidence:
        if (
            config.profile() == "production"
            or not is_uuid7(operation_id)
            or not is_uuid7(receipt_id)
            or (
                patch.run.task_id,
                patch.run.run_id,
                patch.run.goal_revision,
                patch.run.lease_epoch,
                patch.run.restore_incarnation,
                patch.run.worker_ref,
            )
            != (
                self._claim.task_id,
                self._claim.run_id,
                self._claim.goal_revision,
                self._claim.lease_epoch,
                self._claim.restore_incarnation,
                self._claim.worker_ref,
            )
            or patch.workspace_revision != scope.workspace_revision
            or not time.monotonic() < deadline <= time.monotonic() + 60
        ):
            raise EffectUnknown("Git receipt unavailable")
        try:
            attempt, artifact = self._settled(scope, operation_id, receipt_id, deadline)
            payload, body_sha, proof_sha = self._receipts.fetch_with_proof(operation_id, receipt_id, deadline=deadline)
            if (
                artifact != patch.artifact_ref
                or payload.get("attempt") != attempt
                or payload.get("patch_id") != artifact
                or payload.get("parent") != patch.parent_sha
                or payload.get("patch_sha256") != patch.diff_sha256
                or payload.get("paths") != list(patch.paths)
                or payload.get("branch") != scope.branch
                or not isinstance(payload.get("commit"), str)
            ):
                raise ValueError("receipt differs from approved patch")
            if self._settled(scope, operation_id, receipt_id, deadline) != (attempt, artifact):
                raise ValueError("Git effect changed")
            return CommitEvidence(
                patch.parent_sha,
                payload["commit"],
                patch.diff_sha256,
                patch.artifact_ref,
                "git:" + receipt_id,
                body_sha,
                proof_sha,
            )
        except Exception as exc:
            raise EffectUnknown("Git receipt requires reconciliation") from exc
