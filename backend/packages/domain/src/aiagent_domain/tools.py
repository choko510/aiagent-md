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


def _browser_target_props() -> dict[str, Any]:
    return {
        "kind": {"type": "string", "enum": ["role", "label", "test_id", "css"]},
        "selector": {"type": "string", "minLength": 1, "maxLength": 512},
        "roleName": {"type": "string", "minLength": 1, "maxLength": 256},
    }


TOOLS: dict[str, ToolSpec] = {
    "search": ToolSpec(
        "search",
        "Read-only web search. Returns short result snippets.",
        _obj({"query": {"type": "string", "minLength": 1, "maxLength": 500}}, ["query"]),
        "read",
        False,
        "mock_search",
        ("search-api",),
    ),
    "http_fetch": ToolSpec(
        "http_fetch",
        "Fetch a read-only HTTP/HTTPS webpage or JSON endpoint safely with SSRF checks and secret redaction.",
        _obj(
            {
                "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "maxBytes": {"type": "integer", "minimum": 256, "maximum": 1048576},
            },
            ["url"],
        ),
        "read",
        False,
        "http_fetch",
        ("web-fetch",),
    ),
    "send_message": ToolSpec(
        "send_message",
        "Send an external message. Irreversible; requires the owner's explicit approval.",
        _obj(
            {
                "to": {"type": "string", "pattern": "^[a-z0-9_.@-]{1,64}$"},
                "body": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            ["to", "body"],
        ),
        "external_irreversible",
        True,
        "http_message",
        ("message-receiver",),
    ),
    "sandbox_exec": ToolSpec(
        "sandbox_exec",
        "Run a command inside the isolated, offline execution cell (never on the control host).",
        _obj(
            {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                },
                "cwdRelative": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[^\\\\\\u0000]+$",
                },
            },
            ["argv", "cwdRelative"],
        ),
        "write_reversible",
        False,
        "sandbox_cell",
    ),
    "browser_navigate": ToolSpec(
        "browser_navigate",
        "Navigate an isolated sandboxed browser session (`fixture://` or policy-permitted `https://`) and return redacted visible text.",
        _obj(
            {
                "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            ["url"],
        ),
        "read",
        False,
        "browser_fixture",
        ("web-fetch",),
    ),
    "browser_click": ToolSpec(
        "browser_click",
        "Click a single visible control inside an active fixture browser session.",
        _obj(
            {
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 64},
                **_browser_target_props(),
            },
            ["sessionId", "kind", "selector"],
        ),
        "write_reversible",
        False,
        "browser_fixture",
        ("web-fetch",),
    ),
    "browser_submit": ToolSpec(
        "browser_submit",
        "Fill inputs and click a form submit control in an active browser session. Irreversible; requires owner approval.",
        _obj(
            {
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 64},
                **_browser_target_props(),
                "inputs": {
                    "type": "array",
                    "maxItems": 16,
                    "items": _obj(
                        {**_browser_target_props(), "value": {"type": "string", "maxLength": 4096}},
                        ["kind", "selector", "value"],
                    ),
                },
            },
            ["sessionId", "kind", "selector"],
        ),
        "external_irreversible",
        True,
        "browser_fixture",
        ("web-fetch",),
    ),
}

# Profile -> tools the model may see. Framework built-ins are NOT here and are removed by the runtime.
PROFILE_TOOLS: dict[str, tuple[str, ...]] = {
    "default": ("search", "send_message"),
    "anthropic": ("search", "send_message"),
    "sandbox": ("search", "sandbox_exec"),
    "research": ("search", "http_fetch", "send_message"),
    "browser": ("search", "http_fetch", "browser_navigate", "browser_click", "browser_submit"),
}
# Framework-internal read-only operations: provider schema is fixed here instead of trusting a runtime-supplied
# schema. These are NOT external Tool Gateway executors. All results enter HEAD only via grounded admission.
INTERNAL_TOOL_SPECS: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        "read_file",
        "Read an existing skill or offloaded runtime file; never execute its contents.",
        _obj(
            {
                "file_path": {"type": "string", "minLength": 1, "maxLength": 1024, "pattern": "^/"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["file_path"],
        ),
        "read",
        False,
        "framework_internal",
    ),
    "task": ToolSpec(
        "task",
        "Delegate a sub-task to an approved sub-agent.",
        _obj(
            {"description": {"type": "string"}, "subagent_type": {"type": "string"}}, ["description", "subagent_type"]
        ),
        "read",
        False,
        "framework_internal",
    ),
}
INTERNAL_TOOLS = frozenset(INTERNAL_TOOL_SPECS)


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
