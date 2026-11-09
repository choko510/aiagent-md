"""AgentRuntimePort as validated in STEP 2 (segment granularity).

STEP 2 finding: both frameworks own the inner model->tool loop, so the port boundary is a *run segment*
(run until FINAL / approval interrupt / UNKNOWN effect / fence or goal loss), not a single next_action.
aiagent-md keeps Run claim, Goal revision, Checkpoint HEAD, Approval, Tool Intent and Effect Ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .control import Claim


class DerivedStateMismatch(Exception):
    """Framework state proposed a tool call that is not in the aiagent-md Checkpoint HEAD."""


@dataclass
class SegmentOutcome:
    status: str  # FINAL | AWAITING_APPROVAL | UNKNOWN_EFFECT | REBUILD | FENCEDOUT | STALEGOAL
    text: str | None = None
    approval_ids: list[str | None] = field(default_factory=list)
    detail: str | None = None


class AgentRuntimePort(Protocol):
    name: str
    framework_schemas: list[str]

    async def launch(self) -> None: ...

    async def run_segment(self, claim: Claim, policy: str) -> SegmentOutcome:
        """Start or continue the run. Framework state is a derived cache; on mismatch return REBUILD."""

    async def close(self) -> None: ...
