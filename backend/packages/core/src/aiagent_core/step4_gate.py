"""STEP 4 shared admission gate.

The STEP 4 packages are currently isolated mock/contract slices.  No new code may
obtain STEP 3 Run authority, write to the Queue, issue Sentinel permits, or execute
untrusted code merely because a Python package was installed.

This gate is intentionally *not* controlled by an environment variable. A future
adapter must provide audited runtime/ledger/tenant transactions and a separate
integration change before any reserved tool can be exposed.
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
    """Read-only UI projection; not proof of readiness, not an authorization token."""
    return [
        {
            "component": name,
            "integrationState": "INTEGRATION_PENDING",
            "externalActionsEnabled": False,
            "hostVerified": False,
            "productionReady": False,
        }
        for name in FEATURES
    ]
