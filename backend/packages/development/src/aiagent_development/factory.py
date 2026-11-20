"""Sandbox Cell gateway adapter and 3-role independent MultiReviewer / FeatureFactory vertical slice."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aiagent_core import config, control
from aiagent_core.db import Database
from aiagent_domain import sha256_hex

from .contracts import (
    Cycle,
    EffectUnknown,
    ExecutionEvidence,
    ExecutionRequest,
    InvalidEvidence,
    PatchArtifact,
    Phase,
    ProjectScope,
    ReviewVerdict,
    TaskPlan,
)
from .ports import SandboxExecutionPort
from .workflow import DevelopmentWorkflow


def _complete_sandbox_result(result: Any) -> dict[str, Any]:
    """Require a complete command result; intent settlement alone is not success."""
    if (
        not isinstance(result, dict)
        or type(result.get("exitCode")) is not int
        or result.get("timedOut") is not False
        or result.get("truncated") is not False
        or result.get("outcome") not in {"EXITED_ZERO", "NONZERO_EXIT"}
        or (result["exitCode"] == 0) != (result["outcome"] == "EXITED_ZERO")
    ):
        raise InvalidEvidence("sandbox result incomplete or invalid")
    return result


class ToolGatewaySandboxAdapter(SandboxExecutionPort):
    """Bridges DevelopmentWorkflow SandboxExecutionPort -> ToolGateway (`sandbox_exec` inside isolated Cell)."""

    def __init__(
        self,
        db_model: Database,
        gateway: Any,
        claim: control.Claim,
        *,
        cwd_relative: str = ".",
        receipt_reader: Callable[[str, str], tuple[dict[str, object], str]] | None = None,
    ) -> None:
        self._db_model = db_model
        self._gateway = gateway
        self._claim = claim
        self._cwd_relative = cwd_relative
        self._receipt_reader = receipt_reader

    def execute(self, request: ExecutionRequest) -> ExecutionEvidence:  # noqa: PLR0912, PLR0915
        if self._receipt_reader is None or config.profile() == "production":
            raise InvalidEvidence("sandbox execution requires verified workspace and test identity contracts")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise InvalidEvidence("synchronous sandbox adapter requires a worker thread")
        claim, scope, run = self._claim, request.project, request.run
        if (
            scope.tenant_id != claim.tenant_id
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
            or self._cwd_relative != "."
            or not scope.workspace_revision.isascii()
            or not scope.workspace_revision.isdecimal()
            or str(int(scope.workspace_revision)) != scope.workspace_revision
            or int(scope.workspace_revision) < 1
            or request.target_commit_sha is None
        ):
            raise InvalidEvidence("sandbox request scope mismatch")
        preflight = getattr(self._receipt_reader, "preflight", None)
        if not callable(preflight):
            raise InvalidEvidence("sandbox snapshot authority unavailable")
        revision = request.snapshot_revision if request.snapshot_revision is not None else int(scope.workspace_revision)
        try:
            preflight(scope.workspace_id, revision, request.target_commit_sha)
        except Exception as exc:
            raise InvalidEvidence("sandbox snapshot authority rejected") from exc
        args = {"argv": list(request.argv), "cwdRelative": self._cwd_relative}
        call_id = "dev_" + sha256_hex({"run": claim.run_id, "intent": request.intent_ref})[:32]
        with self._db_model.tx(tenant=claim.tenant_id) as c:
            current = control.check_current(c, claim)
            if str(current["workspace_id"]) != scope.workspace_id:
                raise InvalidEvidence("sandbox workspace mismatch")
            existing = control.find_proposal(c, claim.tenant_id, claim.run_id, call_id)
            if existing is None:
                control.append_head(
                    c,
                    claim,
                    "assistant",
                    {
                        "kind": "assistant",
                        "content": "",
                        "tool_calls": [{"id": call_id, "name": "sandbox_exec", "args": args}],
                    },
                    writer="development_cell_adapter",
                    request_key=sha256_hex({"version": "dev-command-proposal-v1", "call": call_id, "args": args}),
                )
            elif existing[0].get("name") != "sandbox_exec" or existing[0].get("args") != args:
                raise InvalidEvidence("sandbox intent conflicts with committed proposal")
        try:
            result = asyncio.run(self._gateway.submit(claim, call_id, "sandbox_exec", args))
        except Exception as exc:
            raise EffectUnknown("sandbox command dispatch requires reconciliation") from exc
        if result.get("status") == "UNKNOWN":
            raise EffectUnknown("sandbox command outcome requires reconciliation")
        if result.get("status") != "CONFIRMED":
            if result.get("status") in ("DENIED", "FAILED", "REVOKED", "EXPIRED", "CANCELLED"):
                raise InvalidEvidence("sandbox command was not admitted")
            raise EffectUnknown("sandbox command outcome requires reconciliation")
        if not isinstance(result.get("result"), dict):
            raise EffectUnknown("sandbox command receipt unavailable")
        response = result["result"]
        execution_id, receipt_id = response.get("executionId"), response.get("receiptId")
        if not isinstance(execution_id, str) or not isinstance(receipt_id, str):
            raise EffectUnknown("sandbox command receipt unavailable")
        try:
            payload, digest = self._receipt_reader(execution_id, receipt_id)
        except Exception as exc:
            raise EffectUnknown("sandbox command receipt requires reconciliation") from exc
        if not isinstance(payload, dict) or not isinstance(digest, str):
            raise EffectUnknown("sandbox command receipt unavailable")
        expected = {
            "tenant": claim.tenant_id,
            "owner": claim.owner_user_id,
            "task": claim.task_id,
            "run": claim.run_id,
            "goal": claim.goal_revision,
            "lease": claim.lease_epoch,
            "workspace": scope.workspace_id,
            "workspace_revision": revision,
            "execution": execution_id,
            "attempt": response.get("toolAttemptId"),
            "kind": request.kind,
            "test_id": request.test_id,
            "toolchain_sha256": request.toolchain_digest,
            "source_commit": request.target_commit_sha,
        }
        if (
            any(payload.get(k) != v for k, v in expected.items())
            or response.get("bodySha256") != digest
            or type(payload.get("exit_code")) is not int
            or type(payload.get("timed_out")) is not bool
            or type(payload.get("truncated")) is not bool
            or not isinstance(payload.get("source_commit"), str)
        ):
            raise EffectUnknown("sandbox command receipt mismatch")
        try:
            return ExecutionEvidence(
                request.intent_ref,
                request.run,
                request.kind,
                cast(int, payload["exit_code"]),
                True,
                digest,
                request.toolchain_digest,
                "",
                "",
                cast(str, payload["source_commit"]),
                request.test_id,
                "command:" + receipt_id,
                cast(bool, payload["timed_out"]),
                cast(bool, payload["truncated"]),
                revision,
            )
        except InvalidEvidence as exc:
            raise EffectUnknown("sandbox command receipt mismatch") from exc


@dataclass(frozen=True, slots=True)
class ReviewerCheckSpec:
    role: str  # functional | security | ui-e2e
    reviewer_task_id: str
    evaluator: Callable[[Cycle], ReviewVerdict]


class MultiReviewerRunner:
    """Runs independent 3-role reviews against exact commit SHA and coordinates P0/P1 repair rounds."""

    def __init__(self, workflow: DevelopmentWorkflow, specs: tuple[ReviewerCheckSpec, ...]) -> None:
        if {s.role for s in specs} != {"functional", "security", "ui-e2e"} or len(specs) != 3:
            raise InvalidEvidence("exactly functional, security, ui-e2e reviewer specs required")
        self._workflow = workflow
        self._specs = specs

    def run_review_round(self, scope: ProjectScope, cycle_id: str) -> Cycle:
        c = self._workflow._load(scope, cycle_id)
        for spec in self._specs:
            c = self._workflow.review(scope, cycle_id, spec.evaluator(c))
        return c

    def run_bounded_factory(
        self,
        *,
        cycle_id: str,
        scope: ProjectScope,
        run: Any,
        change_request_ref: str,
        plan: TaskPlan,
        repo_root: Path,
        build_requests: Callable[[Cycle], tuple[ExecutionRequest, ...]],
        build_patch: Callable[[Cycle], PatchArtifact],
        test_requests: Callable[[Cycle], tuple[ExecutionRequest, ...]],
        repair_patch: Callable[[Cycle], PatchArtifact] | None = None,
        max_repairs: int = 1,
    ) -> Cycle:
        c = self._workflow.create(cycle_id, scope, run, change_request_ref)
        c = self._workflow.plan(scope, cycle_id, plan)
        for attempt in range(max_repairs + 1):
            for req in build_requests(c):
                c = self._workflow.execute(scope, cycle_id, req)
            patch = build_patch(c) if attempt == 0 or repair_patch is None else repair_patch(c)
            c = self._workflow.submit_patch(scope, cycle_id, patch, repo_root)
            c = self._workflow.commit(
                scope,
                cycle_id,
                message=f"cycle {cycle_id} round {c.round_number}",
                intent_ref=f"commit:{cycle_id}:r{c.round_number}",
            )
            for req in test_requests(c):
                c = self._workflow.execute(scope, cycle_id, req)
            c = self.run_review_round(scope, cycle_id)
            if c.phase == Phase.CODE_READY:
                return c
            if attempt >= max_repairs:
                return c
            c = self._workflow.start_repair_round(scope, cycle_id)
        return c
