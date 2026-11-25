"""Test-profile GitProvider adapter over the authenticated Development Gateway.

Approval must already exist for the exact committed proposal. This adapter does
not create or approve Tool Intents and never executes Git on the control host.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.rpc import RpcClient
from aiagent_development.contracts import (
    CommitEvidence,
    EffectUnknown,
    PatchArtifact,
    ProjectScope,
    RunFence,
    ScopeDenied,
)
from aiagent_domain import is_uuid7

from .git_commit_evidence import GitCommitEvidenceReader
from .git_head_reader import RpcGitHeadReader


class ToolGatewayGitProvider:
    """Use only a pre-approved committed proposal and reverified Git receipt."""

    def __init__(
        self,
        db: Database,
        claim: Claim,
        *,
        endpoint: str,
        rpc_key: bytes,
        head_reader: RpcGitHeadReader,
        receipt_reader: GitCommitEvidenceReader,
    ) -> None:
        if (
            config.profile() != "test"
            or not endpoint.startswith(("unix:/", "tcp:127.0.0.1:"))
            or len(rpc_key) < 32
            or not isinstance(head_reader, RpcGitHeadReader)
            or not isinstance(receipt_reader, GitCommitEvidenceReader)
        ):
            raise ValueError("Git provider is unavailable without verified isolation")
        self._db, self._claim, self._endpoint, self._rpc_key = db, claim, endpoint, rpc_key
        self._head, self._receipts = head_reader, receipt_reader

    def _scope(self, scope: ProjectScope, run: RunFence | None = None) -> None:
        c = self._claim
        if (
            config.profile() != "test"
            or scope.tenant_id != c.tenant_id
            or scope.owner_user_id != c.owner_user_id
            or (
                run is not None
                and (
                    run.task_id,
                    run.run_id,
                    run.goal_revision,
                    run.lease_epoch,
                    run.restore_incarnation,
                    run.worker_ref,
                )
                != (c.task_id, c.run_id, c.goal_revision, c.lease_epoch, c.restore_incarnation, c.worker_ref)
            )
        ):
            raise ScopeDenied("Git provider scope unavailable")

    def _approved(self, scope: ProjectScope, target: str, intent_ref: str) -> dict[str, Any]:
        self._scope(scope)
        with self._db.tx(tenant=self._claim.tenant_id) as c:
            current = control.check_current(c, self._claim)
            proposal = control.find_proposal(c, self._claim.tenant_id, self._claim.run_id, intent_ref)
            row = c.execute(
                "SELECT i.safe_arguments_json,i.status_code,i.request_sha256,i.goal_revision,"
                "a.decision_code,a.owner_user_id,a.request_sha256 AS approval_sha,"
                "a.goal_revision AS approval_goal,(a.expires_at>clock_timestamp()) AS unexpired "
                "FROM agent.tool_intents i JOIN agent.approval_records a "
                "ON a.tenant_id=i.tenant_id AND a.tool_intent_id=i.id "
                "WHERE i.tenant_id=%s AND i.task_id=%s AND i.run_id=%s AND i.invocation_key=%s "
                "AND i.tool_key='git.commit'",
                (self._claim.tenant_id, self._claim.task_id, self._claim.run_id, intent_ref),
            ).fetchone()
        if (
            str(current["workspace_id"]) != scope.workspace_id
            or proposal is None
            or row is None
            or proposal[0].get("name") != "git.commit"
            or not isinstance(proposal[0].get("args"), dict)
            or proposal[0]["args"] != row["safe_arguments_json"]
            or row["status_code"] != "PENDING_APPROVAL"
            or row["decision_code"] != "APPROVED"
            or not row["unexpired"]
            or str(row["owner_user_id"]) != self._claim.owner_user_id
            or row["goal_revision"] != self._claim.goal_revision
            or row["approval_goal"] != self._claim.goal_revision
            or row["approval_sha"] != row["request_sha256"]
            or row["safe_arguments_json"].get("parent") != target
            or row["safe_arguments_json"].get("branch") != scope.branch
        ):
            raise ScopeDenied("approved Git proposal unavailable")
        return dict(row["safe_arguments_json"])

    def require_operation(
        self, scope: ProjectScope, run: RunFence, operation: str, target_sha: str, intent_ref: str
    ) -> None:
        self._scope(scope, run)
        if operation != "git.commit":
            raise ScopeDenied("operation not owned by Git provider")
        self._approved(scope, target_sha, intent_ref)

    def head(self, scope: ProjectScope) -> str:
        self._scope(scope)
        return self._head(scope, deadline=time.monotonic() + 8)

    def commit_patch(
        self, scope: ProjectScope, patch: PatchArtifact, expected_head_sha: str, intent_ref: str, message: str
    ) -> CommitEvidence:
        self._scope(scope, patch.run)
        args = self._approved(scope, expected_head_sha, intent_ref)
        if (
            patch.parent_sha != expected_head_sha
            or patch.workspace_revision != scope.workspace_revision
            or args.get("patchId") != patch.artifact_ref
            or args.get("patchSha256") != patch.diff_sha256
            or args.get("paths") != list(patch.paths)
            or args.get("message") != message
        ):
            raise ScopeDenied("Git request differs from approved patch")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise EffectUnknown("Git provider requires a worker thread")
        try:
            result = asyncio.run(
                RpcClient(self._endpoint, "development", self._rpc_key, timeout_s=40).call(
                    "tool.development_git_commit",
                    {"claim": self._claim.to_wire(), "toolCallId": intent_ref, "args": args},
                )
            )
            if result.get("status") != "CONFIRMED" or not isinstance(result.get("result"), dict):
                raise ValueError("Git effect not confirmed")
            output = result["result"]
            operation_id, receipt_id = output.get("operationId"), output.get("receiptId")
            if not is_uuid7(operation_id) or not is_uuid7(receipt_id):
                raise ValueError("Git receipt identity unavailable")
            evidence = self._receipts.read(scope, patch, operation_id, receipt_id, deadline=time.monotonic() + 5)
            if output.get("commitSha") != evidence.commit_sha or output.get("toolAttemptId") is None:
                raise ValueError("Git receipt response mismatch")
            return evidence
        except Exception as exc:
            raise EffectUnknown("Git commit requires reconciliation") from exc

    def push(self, scope: ProjectScope, expected_head_sha: str, intent_ref: str) -> str:
        raise ScopeDenied("Git push is not supported by this adapter")

    def recover_commit(
        self, scope: ProjectScope, cycle_id: str, patch: PatchArtifact, intent_ref: str
    ) -> CommitEvidence:
        """Re-read only a settled effect for the Workflow's persisted invocation."""
        self._scope(scope, patch.run)
        if not cycle_id or not intent_ref:
            raise EffectUnknown("Git recovery scope unavailable")
        deadline = time.monotonic() + 8
        try:
            with self._db.tx(tenant=self._claim.tenant_id) as c:
                c.execute("SELECT set_config('statement_timeout', '5000ms', true)")
                current = control.check_current(c, self._claim)
                if str(current["workspace_id"]) != scope.workspace_id:
                    raise ValueError("Git recovery workspace mismatch")
                rows = c.execute(
                    "SELECT o.id,e.external_receipt_ref FROM agent.git_operations o "
                    "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                    "JOIN agent.side_effect_records e ON e.tenant_id=i.tenant_id AND e.tool_intent_id=i.id "
                    "WHERE o.tenant_id=%s AND o.cycle_id=%s AND o.patch_id=%s "
                    "AND i.task_id=%s AND i.run_id=%s AND i.invocation_key=%s "
                    "AND i.tool_key='git.commit' AND i.status_code='CONFIRMED' "
                    "AND e.outcome_code='CONFIRMED' AND o.state='RECEIPT_PERSISTED'",
                    (
                        self._claim.tenant_id,
                        cycle_id,
                        patch.artifact_ref,
                        self._claim.task_id,
                        self._claim.run_id,
                        intent_ref,
                    ),
                ).fetchall()
            if len(rows) != 1 or not isinstance(rows[0]["external_receipt_ref"], str):
                raise ValueError("confirmed Git effect unavailable")
            reference = rows[0]["external_receipt_ref"]
            if not reference.startswith("git:") or not is_uuid7(reference[4:]):
                raise ValueError("confirmed Git receipt unavailable")
            return self._receipts.read(scope, patch, str(rows[0]["id"]), reference[4:], deadline=deadline)
        except Exception as exc:
            raise EffectUnknown("Git commit requires reconciliation") from exc

    def create_pr(self, scope: ProjectScope, head_sha: str, intent_ref: str) -> str:
        raise ScopeDenied("PR creation is not supported by this adapter")
