"""Context path eligibility, decided in the database from authenticated user grants.

An environment variable can lower the ceiling (feature kill switch) but can never
raise it: the task must carry an owner-written policy for this run's task, a live
Memory consent for the same recipient, and the run must still be valid
(lease/restore/scope). The Model Gateway reads all of it through claim-bound
functions so a client-set session value is not authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_domain.rpcwire import RpcError

from .context_memory import PostgresMemoryRepository


@dataclass(frozen=True)
class ContextPolicy:
    recipient: str
    model: str


class ContextPolicyError(RpcError):
    pass


def resolve_policy(
    db: Database, claim: control.Claim, memory: PostgresMemoryRepository, scope: object
) -> ContextPolicy:
    """Return the owner-granted policy or raise CONTEXT_NOT_ELIGIBLE (never a tool permission)."""
    try:
        row = memory._call("context_policy", scope)  # type: ignore[arg-type]
    except Exception:
        raise ContextPolicyError("CONTEXT_NOT_ELIGIBLE") from None
    if row is None or not row["enabled"] or not row["recipient"] or not row["model"]:
        raise ContextPolicyError("CONTEXT_NOT_ELIGIBLE")
    try:
        consent = memory._call("memory_consent_ok", scope, str(row["recipient"]))  # type: ignore[arg-type]
    except Exception:
        raise ContextPolicyError("CONTEXT_NOT_ELIGIBLE") from None
    if consent is None or not consent["memory_consent_ok"]:
        raise ContextPolicyError("MEMORY_CONSENT_REQUIRED")
    return ContextPolicy(str(row["recipient"]), str(row["model"]))
