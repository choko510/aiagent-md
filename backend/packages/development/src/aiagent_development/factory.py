"""Sandbox Cell gateway adapter and 3-role independent MultiReviewer / FeatureFactory vertical slice."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_domain import sha256_hex

from .contracts import (
    Cycle,
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


class ToolGatewaySandboxAdapter(SandboxExecutionPort):
    """Bridges DevelopmentWorkflow SandboxExecutionPort -> ToolGateway (`sandbox_exec` inside isolated Cell)."""

    def __init__(
        self,
        db_model: Database,
        gateway: Any,
        claim: control.Claim,
        *,
        cwd_relative: str = ".",
    ) -> None:
        self._db_model = db_model
        self._gateway = gateway
        self._claim = claim
        self._cwd_relative = cwd_relative

    def execute(self, request: ExecutionRequest) -> ExecutionEvidence:
        scope = request.project
        args = {"argv": list(request.argv), "cwdRelative": self._cwd_relative}
        call_id = f"dev_{request.intent_ref[:32]}"
        with self._db_model.tx() as c:
            control.append_head(
                c,
                self._claim,
                "assistant",
                {
                    "kind": "assistant",
                    "content": "",
                    "tool_calls": [{"id": call_id, "name": "sandbox_exec", "args": args}],
                },
                writer="development_cell_adapter",
                request_key=os.urandom(32).hex(),
            )
        out = asyncio.run(self._gateway.submit(self._claim, call_id, "sandbox_exec", args))
        if out.get("status") not in ("DONE", "CONFIRMED"):
            raise InvalidEvidence(f"sandbox_exec denied or unknown: {out.get('status')}")
        res = dict(out["result"])
        stdout = str(res.get("stdout", ""))[:4096]
        stderr = str(res.get("stderr", ""))[:4096]
        digest = hashlib.sha256((stdout + "\n" + stderr).encode("utf-8")).hexdigest()
        return ExecutionEvidence(
            intent_ref=request.intent_ref,
            run=request.run,
            kind=request.kind,
            exit_code=int(res["exitCode"]),
            executed=not bool(res.get("timedOut", False)),
            artifact_sha256=digest,
            toolchain_digest=request.toolchain_digest,
            stdout=stdout,
            stderr=stderr,
            source_commit_sha=scope.base_sha,
            test_id=request.plan_ref,
            receipt_ref=f"tgw:{sha256_hex({'call': call_id, 'digest': digest})[:24]}",
        )


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
