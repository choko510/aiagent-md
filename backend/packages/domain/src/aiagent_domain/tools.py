"""Tool Registry (03 tool-registry-and-discovery): the ONLY source of tool names, schemas and risk classes.

The Model Gateway sends these exact schemas to providers (tool schema fixation: a runtime cannot widen or reword a
tool), the Tool Gateway validates proposals against them, and the runtime builds its framework stubs from the
per-profile allowlist. Tools absent from a profile's allowlist are never shown to the model.
"""

from __future__ import annotations

import re
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


@lru_cache(maxsize=1)
def packaged_nihongo_lite() -> str:
    """Trusted standard Japanese instructions (nihongo Lite), pinned and hash-verified at image build.

    This is a reviewed product rule embedded in every main-inference system prompt (ordinary and Context paths),
    not user-installed Skill data. It never grants tools, approvals, egress or policy authority, and it is never
    downloaded during a Run. A missing file means the release artifact is broken; fail closed instead of silently
    dropping the standard Japanese policy.
    """
    path = SKILLS_DIR / "nihongo" / "ALWAYS.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"packaged nihongo Lite is missing: {path}") from exc


# Deterministic, trusted-goal gate for the tool-free Context inference. The input is the current Task goal revision
# (trusted user input), never Context payload or Memory. Default is Lite only; Full is added only for a writing,
# editing or translation request. This never calls another LLM and never changes tools, approval, consent or egress.
_NIHONGO_DOC_NOUN = re.compile(
    r"文章|文書|メール|お知らせ|企画書|提案書|報告書|議事録|資料|スライド|プレゼン|マニュアル|技術文書|"
    r"仕様書|設計書|リリースノート|プレスリリース|案内|挨拶|スピーチ|履歴書|職務経歴書|志望動機|"
    r"ブログ|記事|コラム|商品説明|紹介文|自己紹介|推薦状|推薦文|カバーレター|返信|返事|文面|文案|"
    r"下書き|UI文言|キャッチコピー|タイトル|手順書|契約書|稟議書|始末書|"
    r"(?<![A-Za-z0-9])(?:e-?mail|report|essay|article|blog(?:\s+post)?|readme|proposal|memo|letter|cover\s+letter|"
    r"resume|cv|slides?|presentation|press\s+release|announcement|manual|documentation|technical\s+writing|"
    r"release\s+notes|ui\s+copy|spec(?:ification)?|design\s+doc)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_NIHONGO_WRITE_VERB = re.compile(
    r"書いて|書く|作成|作って|作る|直して|直す|修正|整えて|整える|まとめて|まとめる|確認|見直し|レビュー|"
    r"返信|返事|更新|編集|追記|加筆|変更|(?<![A-Za-z0-9])(?:write|writing|compose|edit|revise|create|make|prepare|review)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_NIHONGO_STRONG_WRITING = re.compile(
    r"推敲|校正|添削|翻訳|書き直|書き換え|文面|文案|下書き|起草|構成を考え|文章を書|"
    r"(?<![A-Za-z0-9])(?:proofread|copyedit|rewrite|translate|draft)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_NIHONGO_NON_WRITING = re.compile(
    r"コード|プログラム|スクリプト|関数|実装|SQL|JSON|YAML|XML|CSV|API|バグ|エラー|デバッグ|計算|"
    r"天気|検索|調べ|要約|説明して|"
    r"(?<![A-Za-z0-9])(?:code|script|function|implement|sql|json|yaml|xml|csv|api|bug|error|debug|calculate|compute|"
    r"weather|search|lookup|summari[sz]e|explain)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_NIHONGO_OPT_OUT = re.compile(
    r"nihongo\s*(?:を|は|の)?\s*(?:使わ(?:ず|ない)|不要|無効|オフ|抜き)|"
    r"(?:日本語|nihongo)\s*(?:の)?\s*(?:skill|スキル|ルール|指示|文体)\s*(?:を|は)?\s*(?:使わ(?:ず|ない)|不要|無効|オフ)|"
    r"(?:Lite|Full)\s*(?:を|は)?\s*(?:使わ(?:ず|ない)|不要)|"
    r"(?:do\s+not|don't|without|skip|no)\s+(?:use\s+)?nihongo",
    re.IGNORECASE,
)
_NIHONGO_SEARCH_ACTION = re.compile(
    r"検索|探して|探す|調べ|一覧|抽出|"
    r"(?<![A-Za-z0-9])(?:search|lookup|find|list|extract)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Writing actions that turn a research/read request into a deliverable. A search/summarize request is not a writing
# deliverable and stays on Lite; "research and write a report" still reaches Full through 作成/書いて.
_NIHONGO_CREATE_ACTION = re.compile(
    r"書いて|書く|作成|作って|作る|直して|直す|修正|推敲|翻訳|校正|添削|更新|編集|"
    r"(?<![A-Za-z0-9])(?:write|writing|compose|edit|revise|create|make|rewrite|proofread|translate)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def nihongo_full_for_goal(goal: str) -> bool:
    """Return True only when the trusted Task goal asks for a user-facing writing/editing deliverable.

    Conservative by design: ambiguous or non-writing goals keep the Context path on Lite. A strong writing signal
    (proofread/translate/draft/文面/推敲...) always applies Full; otherwise a writing verb must be paired with a
    document noun, and code/data/read/search goals are excluded. This is product policy, not authority.
    """
    text = goal.strip()
    if not text:
        return False
    # Explicit opt-out wins for Full. Lite is still a reviewed product rule in the ordinary/Context system prompt;
    # that remaining limitation is documented instead of pretending the opt-out removes Lite.
    if _NIHONGO_OPT_OUT.search(text):
        return False
    if _NIHONGO_STRONG_WRITING.search(text):
        return True
    if _NIHONGO_SEARCH_ACTION.search(text) and not _NIHONGO_CREATE_ACTION.search(text):
        return False
    if not _NIHONGO_WRITE_VERB.search(text):
        return False
    document = bool(_NIHONGO_DOC_NOUN.search(text))
    if _NIHONGO_NON_WRITING.search(text) and not document:
        return False
    return document


@lru_cache(maxsize=1)
def packaged_nihongo_full() -> str:
    """Trusted nihongo Full instructions (SKILL.md), used only on the tool-free Context inference path.

    Ordinary inference reads Full on demand through the Tool Gateway's grounded `read_file`. The Context request
    has no tools, so the fixed, hash-verified Full text is included as trusted style policy instead. Reference
    files remain unavailable there; callers must not claim otherwise.
    """
    path = SKILLS_DIR / "nihongo" / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"packaged nihongo Full is missing: {path}") from exc
