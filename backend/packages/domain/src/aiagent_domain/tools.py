"""Tool Registry (03 tool-registry-and-discovery): the ONLY source of tool names, schemas and risk classes.

The Model Gateway sends these exact schemas to providers (tool schema fixation: a runtime cannot widen or reword a
tool), the Tool Gateway validates proposals against them, and the runtime builds its framework stubs from the
per-profile allowlist. Tools absent from a profile's allowlist are never shown to the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from aiagent_domain import sha256_hex


@dataclass(frozen=True)
class ToolSpec:
    key: str
    description: str
    parameters: dict[str, Any]
    risk_class: str  # read | write_reversible | external_irreversible
    approval_required: bool
    executor: str
    egress: tuple[str, ...] = field(default_factory=tuple)  # destinations the executor may need (Sentinel binds one)

    def provider_schema(self) -> dict[str, Any]:
        return {"name": self.key, "description": self.description, "parameters": self.parameters}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: dict[str, ToolSpec] = {
    "search": ToolSpec(
        "search", "Read-only web search. Returns short result snippets.",
        _obj({"query": {"type": "string", "minLength": 1, "maxLength": 500}}, ["query"]),
        "read", False, "mock_search",
    ),
    "send_message": ToolSpec(
        "send_message", "Send an external message. Irreversible; requires the owner's explicit approval.",
        _obj({"to": {"type": "string", "pattern": "^[a-z0-9_.@-]{1,64}$"},
              "body": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["to", "body"]),
        "external_irreversible", True, "http_message", ("message-receiver",),
    ),
    "sandbox_exec": ToolSpec(
        "sandbox_exec", "Run a command inside the isolated, offline execution cell (never on the control host).",
        _obj({"argv": {"type": "array", "minItems": 1, "maxItems": 64,
                       "items": {"type": "string", "minLength": 1, "maxLength": 4096}},
              "cwdRelative": {"type": "string", "minLength": 1, "maxLength": 512,
                              "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[^\\\\\\u0000]+$"}}, ["argv", "cwdRelative"]),
        "write_reversible", False, "sandbox_cell",
    ),
}

# Profile -> tools the model may see. Framework built-ins are NOT here and are removed by the runtime.
PROFILE_TOOLS: dict[str, tuple[str, ...]] = {
    "default": ("search", "send_message"),
    "anthropic": ("search", "send_message"),
    "sandbox": ("search", "sandbox_exec"),
}
# Framework-internal tools the runtime may keep. Their results reach HEAD only through the Tool Gateway's grounded
# admission (tool.record_internal): `task` = sub-agent delegation, `read_file` = read of in-state offloaded content or
# packaged read-only skills. Snapshot-tested (tests/test_runtime_surface.py).
INTERNAL_TOOLS = frozenset({"task", "read_file"})


def tools_for(profile: str) -> list[ToolSpec]:
    return [TOOLS[k] for k in PROFILE_TOOLS.get(profile, ())]


def surface_digest(keys: list[str]) -> str:
    return sha256_hex(sorted(TOOLS[k].provider_schema()["name"] for k in keys if k in TOOLS))


SKILLS_DIR = Path(__file__).with_name("skills")


@lru_cache(maxsize=1)
def packaged_skill_texts() -> tuple[str, ...]:
    """Read-only skills shipped with the code (reviewed like code; never written by agents)."""
    if not SKILLS_DIR.is_dir():
        return ()
    return tuple(p.read_text(encoding="utf-8") for p in sorted(SKILLS_DIR.rglob("*.md")))
