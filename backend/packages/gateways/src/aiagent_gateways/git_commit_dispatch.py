"""Opt-in Development Git admission; execution remains fail closed.

The authenticated isolated Git worker and durable Git receipt are not yet
connected. A reserved operation therefore always remains UNKNOWN.
"""

from __future__ import annotations

import time
from typing import Any

from aiagent_core import config, control
from aiagent_core.control import Claim
from aiagent_core.db import Database
from aiagent_development.workspace_authority import resolve_git_commit_binding
from aiagent_domain import is_uuid7

from .context_keys import KeyRing
from .executors import ExecutorFailed, ExecutorUnknown
from .git_patch_artifacts import fetch_git_patch


class GitCommitReservationExecutor:
    """Bind the owner's approved inputs; do not execute without an isolated worker."""

    def __init__(self, db: Database, claim: Claim, keys: KeyRing) -> None:
        if config.profile() == "production":
            raise RuntimeError("Git commit isolation not verified")
        self._db, self._claim, self._keys = db, claim, keys
        self.context: dict[str, Any] = {}

    def destination(self) -> None:
        return None

    def bind_request(self, tool: str, args: dict[str, Any], *, claim: Claim) -> dict[str, Any]:
        if tool != "git.commit" or claim != self._claim or config.profile() == "production":
            raise ExecutorFailed("Git authority unavailable")
        try:
            if not is_uuid7(args["bindingId"]) or not is_uuid7(args["patchId"]):
                raise ValueError("invalid identity")
            with self._db.tx(tenant=claim.tenant_id) as c:
                run = control.check_current(c, claim)
                workspace = str(run["workspace_id"])
                row = c.execute(
                    "SELECT repository_ref FROM agent.repository_bindings WHERE tenant_id=%s AND id=%s",
                    (claim.tenant_id, args["bindingId"]),
                ).fetchone()
            if row is None:
                raise ValueError("binding unavailable")
            deadline = time.monotonic() + 10
            binding = resolve_git_commit_binding(
                self._db,
                claim,
                args["bindingId"],
                workspace_id=workspace,
                repository=str(row["repository_ref"]),
                generation=args["bindingGeneration"],
                parent=args["parent"],
                deadline=deadline,
            )
            patch = fetch_git_patch(
                self._db,
                claim,
                self._keys,
                args["patchId"],
                workspace_id=workspace,
                binding_id=binding.id,
                binding_generation=binding.generation,
                cycle_id=args["cycleId"],
                plan_ref=args["planRef"],
                deadline=deadline,
            )
            if (
                args["branch"] != binding.branch
                or args["parent"] != patch.parent
                or args["patchSha256"] != patch.digest
                or args["paths"] != list(patch.paths)
            ):
                raise ValueError("Git request does not match authority")
        except Exception as exc:
            raise ExecutorFailed("Git authority unavailable") from exc
        return {
            "method": "COMMIT",
            "action": binding.branch,
            "payloadDigest": patch.digest,
            "binding": binding.id,
            "generation": binding.generation,
            "artifact": patch.artifact_id,
            "parent": patch.parent,
            "paths": list(patch.paths),
            "workspace": workspace,
        }

    async def execute(self, effect_key: str, args: dict[str, Any], permit: str | None) -> dict[str, Any]:
        del args, permit
        context = self.context.get(effect_key)
        if not isinstance(context, dict) or not is_uuid7(context.get("operation_id")):
            raise ExecutorFailed("Git operation unavailable")
        raise ExecutorUnknown("isolated Git worker unavailable; reconcile")

    async def query(self, effect_key: str, permit: str | None = None) -> dict[str, Any] | None:
        del effect_key, permit
        return None
