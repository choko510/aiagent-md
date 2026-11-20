"""Read an operator-bound Git branch through the authenticated worker."""

from __future__ import annotations

import asyncio
import time

from aiagent_core import config
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
