"""AgentRuntimePort as validated in STEP 2 (segment granularity).

STEP 2 finding: both frameworks own the inner model->tool loop, so the port boundary is a *run segment*
(run until FINAL / approval interrupt / UNKNOWN effect / fence, goal or cancel loss), not a single next_action.
aiagent-md keeps Run claim, Goal revision, Checkpoint HEAD, Approval, Tool Intent and Effect Ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .control import Claim, sha


class DerivedStateMismatch(Exception):
    """Framework state disagrees with the aiagent-md Checkpoint HEAD."""


@dataclass
class SegmentOutcome:
    status: str  # FINAL | AWAITING_APPROVAL | UNKNOWN_EFFECT | REBUILD | FENCEDOUT | STALEGOAL | CANCELLED
    text: str | None = None
    approval_ids: list[str | None] = field(default_factory=list)
    detail: str | None = None


class AgentRuntimePort(Protocol):
    name: str
    framework_schemas: list[str]

    async def launch(self) -> None:
        """Must not resume or execute runs in the background."""

    async def run_segment(self, claim: Claim, policy: str) -> SegmentOutcome:
        """Start or continue the run. Framework state is a derived cache; on mismatch return REBUILD.
        Cancellation is enforced by the control plane (run status / fence) at every Model and Tool
        admission, so it does not depend on a framework cancel API."""

    async def close(self) -> None: ...


def _norm(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": m.get("kind"),
        "content": m.get("content") or "",
        "tool_call_id": m.get("tool_call_id") if m.get("kind") == "tool" else None,
        "tool_calls": [[tc.get("id"), tc.get("name"), tc.get("args")] for tc in m.get("tool_calls") or []],
    }


def verify_against_head(framework: list[dict[str, Any]], head: list[dict[str, Any]], *, complete: bool = False) -> str:
    """Framework transcript must equal HEAD in order and content (user text, assistant text, tool calls,
    tool results). It may lag behind HEAD (killed after the HEAD commit, before the checkpoint write) but
    never differ or run ahead. With complete=True it must equal HEAD exactly. Returns the HEAD digest."""
    f = [_norm(m) for m in framework if m.get("kind") in ("user", "assistant", "tool")]
    h = [_norm(m) for m in head]
    if len(f) > len(h):
        raise DerivedStateMismatch(f"framework ahead of HEAD ({len(f)} > {len(h)} messages)")
    for i, (a, b) in enumerate(zip(f, h)):
        if a != b:
            # Diagnostics carry position, kind and digests only - never message text or tool payloads.
            raise DerivedStateMismatch(
                f"message {i} differs from HEAD: kind={a['kind']}/{b['kind']} framework_digest={sha(a)} head_digest={sha(b)}")
    if complete and len(f) != len(h):
        raise DerivedStateMismatch(f"framework final transcript shorter than HEAD ({len(f)} < {len(h)})")
    return sha(h)


def safe_detail(e: BaseException) -> str:
    """REBUILD reason for logs/events: our own mismatch messages are content-free; any other exception (DBOS,
    LangGraph, decoder errors may quote payload bytes) is reduced to its type and a digest of its text."""
    if isinstance(e, DerivedStateMismatch):
        return f"DerivedStateMismatch: {e}"
    return f"{type(e).__name__}: digest={sha(str(e))}"


def final_in_head(head: list[dict[str, Any]]) -> bool:
    return bool(head) and head[-1]["kind"] == "assistant" and not head[-1].get("tool_calls")
