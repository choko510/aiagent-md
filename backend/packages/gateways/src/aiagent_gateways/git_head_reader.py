"""Read an operator-bound Git branch through the authenticated worker."""

from __future__ import annotations

import asyncio
import time

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_core.rpc import RpcClient
from aiagent_development.contracts import ProjectScope, StaleReference, require_sha
from aiagent_development.workspace_authority import GitCommitBinding, resolve_git_commit_binding
from aiagent_domain import is_uuid7
from aiagent_sandbox.git_worker_rpc import sign_git_head_grant


class RpcGitHeadReader:
    """Test/development-only, claim-bound read; the returned HEAD is not commit evidence."""

    def __init__(
        self,
        db: Database,
        claim: Claim,
        *,
        binding_id: str,
        generation: int,
        endpoint: str,
        rpc_key: bytes,
        grant_key: bytes,
        worker_id: str,
        cycle_id: str | None = None,
    ) -> None:
        if (
            config.profile() == "production"
            or not is_uuid7(binding_id)
            or type(generation) is not int
            or generation < 1
            or not endpoint.startswith("unix:/")
            or len(rpc_key) < 32
            or len(grant_key) < 32
            or rpc_key == grant_key
            or not worker_id
        ):
            raise ValueError("Git head reader unavailable")
        self._db, self._claim, self._binding, self._generation = db, claim, binding_id, generation
        self._endpoint, self._rpc_key, self._grant_key, self._worker_id = endpoint, rpc_key, grant_key, worker_id
        self._cycle_id = cycle_id

    def _promoted_binding(self, scope: ProjectScope, deadline: float) -> GitCommitBinding:
        """Resolve only the next verified snapshot of this exact committed cycle."""
        if self._cycle_id is None or not scope.workspace_revision.isascii() or not scope.workspace_revision.isdecimal():
            raise ValueError("committed cycle unavailable")
        with self._db.tx(tenant=self._claim.tenant_id) as c:
            ms = max(1, int((deadline - time.monotonic()) * 1000))
            c.execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            c.execute("SELECT set_config('lock_timeout', %s, true)", (f"{ms}ms",))
            current = control.check_current(c, self._claim)
            row = c.execute(
                "SELECT b.id,b.repository_ref,b.approved_branch,b.approved_commit,b.generation,b.workspace_revision,"
                "b.active,c.cycle_json,c.phase,c.owner_user_id,c.project_id,c.task_id,c.run_id,"
                "o.parent_sha,o.candidate_commit,o.binding_generation,o.state,o.intent_id,o.effect_key,i.status_code AS intent_state,"
                "a.status_code AS attempt_state,a.tool_intent_id,a.fence_token,e.attempt_fence,"
                "e.effect_key AS ledger_effect_key,e.outcome_code,e.external_receipt_ref,"
                "s.binding_id AS snapshot_binding,s.commit_sha AS snapshot_commit,"
                "s.binding_generation AS snapshot_generation,s.workspace_revision AS snapshot_revision "
                "FROM agent.repository_bindings b JOIN agent.git_operations o ON o.tenant_id=b.tenant_id AND o.binding_id=b.id "
                "JOIN agent.dev_cycles c ON c.tenant_id=o.tenant_id AND c.id=o.cycle_id "
                "JOIN agent.tool_intents i ON i.tenant_id=o.tenant_id AND i.id=o.intent_id "
                "JOIN agent.tool_attempts a ON a.tenant_id=o.tenant_id AND a.id=o.attempt_id "
                "JOIN agent.side_effect_records e ON e.tenant_id=o.tenant_id AND e.tool_intent_id=o.intent_id "
                "JOIN agent.workspace_snapshot_heads h ON h.tenant_id=b.tenant_id AND h.workspace_id=b.workspace_id "
                "JOIN agent.workspace_snapshot_attestations s ON s.tenant_id=h.tenant_id AND s.id=h.snapshot_id "
                "WHERE b.tenant_id=%s AND b.id=%s AND b.workspace_id=%s AND c.id=%s",
                (self._claim.tenant_id, self._binding, scope.workspace_id, self._cycle_id),
            ).fetchall()
        if len(row) != 1:
            raise ValueError("committed binding unavailable")
        r = row[0]
        state = r["cycle_json"]
        committed = state.get("commit") if isinstance(state, dict) else None
        stored_scope = state.get("scope") if isinstance(state, dict) else None
        if (
            str(current["workspace_id"]) != scope.workspace_id
            or not r["active"]
            or str(r["owner_user_id"]) != scope.owner_user_id
            or str(r["project_id"]) != scope.project_id
            or str(r["task_id"]) != self._claim.task_id
            or str(r["run_id"]) != self._claim.run_id
            or r["repository_ref"] != scope.repository
            or r["approved_branch"] != scope.branch
            or r["phase"] not in ("COMMITTED", "REVIEWING", "CODE_READY")
            or not isinstance(stored_scope, dict)
            or stored_scope.get("base_sha") != scope.base_sha
            or stored_scope.get("workspace_revision") != scope.workspace_revision
            or r["parent_sha"] != scope.base_sha
            or int(r["binding_generation"]) != self._generation
            or int(r["generation"]) != self._generation + 1
            or int(r["workspace_revision"]) != int(scope.workspace_revision) + 1
            or r["state"] != "RECEIPT_PERSISTED"
            or r["intent_state"] != "CONFIRMED"
            or r["attempt_state"] != "CONFIRMED"
            or r["outcome_code"] != "CONFIRMED"
            or str(r["tool_intent_id"]) != str(r["intent_id"])
            or r["fence_token"] != r["attempt_fence"]
            or r["ledger_effect_key"] != r["effect_key"]
            or not isinstance(committed, dict)
            or committed.get("commit_sha") != r["candidate_commit"]
            or committed.get("parent_sha") != scope.base_sha
            or committed.get("receipt_ref") != r["external_receipt_ref"]
            or not isinstance(r["external_receipt_ref"], str)
            or not r["external_receipt_ref"].startswith("git:")
            or not is_uuid7(r["external_receipt_ref"][4:])
            or r["approved_commit"] != r["candidate_commit"]
            or str(r["snapshot_binding"]) != self._binding
            or r["snapshot_commit"] != r["candidate_commit"]
            or r["snapshot_generation"] != r["generation"]
            or r["snapshot_revision"] != r["workspace_revision"]
        ):
            raise ValueError("committed binding mismatch")
        return GitCommitBinding(
            self._binding,
            int(r["generation"]),
            scope.workspace_id,
            scope.repository,
            scope.branch,
            str(r["approved_commit"]),
            int(r["workspace_revision"]),
        )

    def __call__(self, scope: ProjectScope, *, deadline: float) -> str:
        if (
            config.profile() == "production"
            or scope.tenant_id != self._claim.tenant_id
            or scope.owner_user_id != self._claim.owner_user_id
            or not time.monotonic() < deadline <= time.monotonic() + 10
        ):
            raise StaleReference("Git head unavailable")
        try:

            def check() -> GitCommitBinding:
                try:
                    binding = resolve_git_commit_binding(
                        self._db,
                        self._claim,
                        self._binding,
                        workspace_id=scope.workspace_id,
                        repository=scope.repository,
                        generation=self._generation,
                        parent=scope.base_sha,
                        deadline=deadline,
                    )
                except Exception:
                    return self._promoted_binding(scope, deadline)
                if binding.branch != scope.branch or str(binding.workspace_revision) != scope.workspace_revision:
                    raise ValueError("Git binding mismatch")
                return binding

            binding = check()
            payload = {
                "tenant": self._claim.tenant_id,
                "owner": self._claim.owner_user_id,
                "workspace": scope.workspace_id,
                "task": self._claim.task_id,
                "run": self._claim.run_id,
                "goal": self._claim.goal_revision,
                "lease": self._claim.lease_epoch,
                "restore": self._claim.restore_incarnation,
                "worker_ref": self._claim.worker_ref,
                "binding": binding.id,
                "generation": binding.generation,
                "branch": binding.branch,
                "expires_at": int(time.time()) + min(5, max(1, int(deadline - time.monotonic()))),
            }
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ValueError("Git head reader requires a worker thread")
            response = asyncio.run(
                RpcClient(
                    self._endpoint, "tool_gateway", self._rpc_key, timeout_s=max(0.1, deadline - time.monotonic())
                ).call("git.head", {"grant": sign_git_head_grant(self._grant_key, payload)})
            )
            if (
                set(response) != {"binding", "branch", "head", "worker_id"}
                or response["binding"] != binding.id
                or response["branch"] != binding.branch
                or response["worker_id"] != self._worker_id
            ):
                raise ValueError("Git head response mismatch")
            head = require_sha(response["head"])
            if time.monotonic() >= deadline or check() != binding:
                raise ValueError("Git binding changed")
            return head
        except Exception as exc:
            raise StaleReference("Git head unavailable") from exc
