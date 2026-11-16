"""STEP 4 shared admission gate.

The STEP 4 packages are currently isolated mock/contract slices.  No new code may
obtain STEP 3 Run authority, write to the Queue, issue Sentinel permits, or execute
untrusted code merely because a Python package was installed.

This gate is intentionally *not* controlled by an environment variable. A future
adapter must provide audited runtime/ledger/tenant transactions and a separate
integration change before any reserved tool can be exposed.

`context` and `memory` now have a narrow, owner-gated egress path: the Runtime
Worker may send an owner-approved Context HEAD through the Model Gateway. That is
NOT external-action/tool authority and NOT production readiness. The projection
reports those facts separately so operators do not read `INTEGRATION_PENDING` as
"no bytes can leave the building".
"""

from __future__ import annotations

from dataclasses import dataclass

FEATURES = (
    "browser",
    "context",
    "memory",
    "development",
    "automation",
    "device",
)
# Features integrated into the STEP 3 Runtime/Gateway pipeline with explicit owner gating.
_OWNER_GATED_EGRESS = frozenset({"context", "memory"})
_SANDBOXED_INTEGRATED = frozenset({"context", "memory", "browser", "development", "automation"})
_RESERVED_PREFIXES = (*(f + "." for f in FEATURES), "step4.")
_RESERVED_EXACT = frozenset(FEATURES) | {"step4"}


@dataclass(frozen=True, slots=True)
class IntegrationBlocked(PermissionError):
    reason: str = "STEP4_NOT_INTEGRATED"

    def __str__(self) -> str:
        return self.reason


def is_reserved_tool(tool: str) -> bool:
    """True for STEP 4 names, including names accidentally added to a future registry."""
    if not isinstance(tool, str):
        return True
    value = tool.casefold()
    return value in _RESERVED_EXACT or value.startswith(_RESERVED_PREFIXES)


def require_step4_admission(tool: str) -> None:
    """A non-overridable deny until STEP 3 authority adapters have been verified."""
    if is_reserved_tool(tool):
        raise IntegrationBlocked()


def capability_snapshot() -> list[dict[str, str | bool]]:
    """Read-only UI projection; not proof of readiness, not an authorization token.

    `integrationState` describes code integration with the STEP 3 path.
    `memoryEgressPath` describes whether Memory/Context bytes can reach an external
    recipient when the owner enables it, separately from production readiness.
    `externalActionsEnabled` stays False: this record grants no autonomous Tool or
    external-action authority.
    """
    return [
        {
            "component": name,
            "integrationState": "INTEGRATED_CONDITIONAL" if name in _SANDBOXED_INTEGRATED else "INTEGRATION_PENDING",
            "memoryEgressPath": "OWNER_ENABLED_CONTEXT" if name in _OWNER_GATED_EGRESS else "NONE",
            "hybridPipelineWired": name == "context",
            "semanticVerifierMode": "HEURISTIC_NLI_DEFAULT_LLM_GATED" if name == "context" else "NONE",
            "externalActionsEnabled": False,
            "ownerConsentRequired": True,
            "hostVerified": False,
            "productionReady": False,
        }
        for name in FEATURES
    ]
