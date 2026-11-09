"""Stream Privacy Gate (Model Router 02-09 / Realtime Events): what may leave the Model Gateway while streaming.

* public answer text deltas -> provisional stream (UI), marked provisional, never the committed answer;
* private reasoning -> dropped (never stored, never emitted);
* tool-call arguments -> buffered until the call is complete, then parsed and validated against the FIXED registry
  schema; partial arguments are never emitted; invalid JSON / schema -> the proposal is kept but marked invalid
  (`{"__invalid__": true}`) so the Tool Gateway refuses it and the model sees a refusal;
* registered secrets are masked in public text as a last line of defence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from aiagent_domain.tools import TOOLS, ToolSpec

from .providers import ProviderEvent


@dataclass
class GateResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stopped: bool = False
    reasoning_dropped_chars: int = 0


class StreamPrivacyGate:
    def __init__(self, allowed_tools: dict[str, ToolSpec], secrets: list[str] | None = None) -> None:
        self.allowed = allowed_tools
        self.secrets = [s for s in (secrets or []) if s]
        self.result = GateResult()
        self._args: dict[str, list[str]] = {}
        self._names: dict[str, str] = {}
        self._order: list[str] = []

    def _mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def feed(self, ev: ProviderEvent) -> str | None:
        """Consume one provider event; return a public text delta to publish, or None."""
        if ev.type == "text":
            public = self._mask(ev.text)
            self.result.text += public
            return public or None
        if ev.type == "reasoning":
            self.result.reasoning_dropped_chars += len(ev.text)
            return None
        if ev.type == "tool_start":
            self._names[ev.tool_id] = ev.tool_name
            self._args[ev.tool_id] = []
            self._order.append(ev.tool_id)
        elif ev.type == "tool_args":
            self._args.setdefault(ev.tool_id, []).append(ev.text)
        elif ev.type == "tool_end":
            pass
        elif ev.type == "usage":
            for k, v in ev.usage.items():
                self.result.usage[k] = max(self.result.usage.get(k, 0), int(v))
        elif ev.type == "stop":
            self.result.stopped = True
        return None

    def finish(self) -> GateResult:
        for tid in self._order:
            name = self._names[tid]
            raw = "".join(self._args.get(tid, [])) or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError
                spec = self.allowed.get(name)
                if spec is not None and name in TOOLS:
                    if not Draft202012Validator(spec.parameters).is_valid(args):
                        args = {"__invalid__": True}
                elif spec is None and name != "task":
                    args = {"__invalid__": True}  # tool not on this run's surface
            except ValueError:
                args = {"__invalid__": True}
            self.result.tool_calls.append({"id": tid, "name": name, "args": args})
        return self.result
