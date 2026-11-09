#!/usr/bin/env python3
"""Static Python-first migration audit (ADR-036..040).

Documentation consistency only. A PASS here is NOT evidence that any Python
Agent Runtime, API, Browser Worker, queue or isolation boundary works.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADR = ROOT / "docs/00-foundation/architecture-decisions.md"

# Words that indicate a TypeScript/Node-centred backend choice or a TS-only OSS.
# Every docs/**/*.md file containing one must be classified (A/B/C) in the
# migration ledger so that no TS dependency stays unreviewed.
TRIGGER = re.compile(
    r"typescript|fastify|pg-boss|pgboss|node\.js|nodejs|\bzod\b|next\.js|"
    r"python sidecar|(?-i:\bTS\b)|TS側|deepagents ?js|deepagentsjs|langgraph ?js|"
    r"langgraphjs|mastra|vercel ai sdk|better auth|\bmsw\b|graphile",
    re.I,
)

# Adoption phrases that must not appear as current direction. A line may keep
# them only if it is explicitly marked as history/superseded.
FORBIDDEN = (
    "Fastify API",
    "Fastify REST API",
    "TypeScriptモノレポ",
    "TS runtime",
    "TS Host",
    "Proposed TS packages",
    "第一候補pg-boss",
    "pg-boss第一候補",
    "TypeScript host",
    "Zod strict schema",
    "`pgboss`",
    "pgboss.*",
    "pgboss schema",
)
# Only explicit history markers exempt a line. Dates and ADR numbers alone do
# not: "2026-11-09: Fastify APIを正式採用" is a current adoption statement.
HISTORY_MARK = re.compile(
    r"旧案|旧記述|旧方向|旧候補|旧TS|旧pg-boss|旧:|旧：|（旧|\(旧|"
    r"superseded|formerly|historical|調査履歴|履歴として|採用方針ではない",
    re.I,
)
# A heading whose title carries one of these words opens a history section;
# every line until the next heading of the same or higher level is exempt.
HISTORY_HEADING = re.compile(r"旧案|履歴|superseded|historical", re.I)


def adoption_violations(text: str) -> list[tuple[int, str]]:
    """Return (line number, phrase) for forbidden phrases outside history."""
    found: list[tuple[int, str]] = []
    history_level = 0
    for no, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            if history_level and level <= history_level:
                history_level = 0
            if HISTORY_HEADING.search(m.group(2)):
                history_level = level
        if history_level:
            continue
        for phrase in FORBIDDEN:
            if phrase in line and not HISTORY_MARK.search(line):
                found.append((no, phrase))
    return found


# Self-test with negative/positive fixtures so the matcher cannot silently widen.
_SELF_TEST = {
    "2026-11-09: Fastify APIを正式採用する": 1,
    "ADR-036に従いFastify REST APIを採用": 1,
    "Queueは`pgboss` schemaを使う": 1,
    "旧案: Fastify APIを使う構成": 0,
    "Next.js UI（旧: Fastify REST API）": 0,
    "## 旧案の記録\nFastify APIを使う\n## 現行\nFastify APIを使う": 1,
}
for sample, expected in _SELF_TEST.items():
    got = len(adoption_violations(sample))
    if got != expected:
        print(f"FAIL self-test: {sample!r} expected {expected} violation(s), got {got}")
        sys.exit(1)

REQUIRED = {
    "docs/00-foundation/architecture-decisions.md": (
        "ADR-036", "ADR-037", "ADR-038", "ADR-039", "ADR-040", "PRODUCT_FIXED",
        "STEP 2判定結果", "PydanticAI＋DBOS", "DeepAgents Python＋LangGraph", "PYRT-T12", "NOT LOCKED",
        "Python-first 移行監査台帳",
    ),
    "docs/00-foundation/system-architecture.md": (
        "### 13.2 Python-first", "Untrusted Execution VM のRuntime Cellのみ",
        "独立プロセス＋認証付きRPC",
    ),
    "docs/00-foundation/architecture-rules.md": ("## E. Python-first", "論理型記法", "Retryの区別", "round-trip fixture", "strict `int`へwire値を直接流さず"),
    "docs/02-runtime/agent-loop.md": ("AgentRuntimePort", "PYRT-T01", "責務の分担"),
    "docs/02-runtime/model-router.md": ("Tool proposalの確定", "予約をUNKNOWNのまま保持", "Stream Privacy Gate", "未知の種別はfail closed"),
    "docs/08-interfaces/realtime-events.md": ("Stream Privacy Gate",),
    "docs/10-infrastructure/database-schema-and-migrations.md": ("PostgreSQLを使う基盤（DBOS、自作SKIP LOCKED等）の場合のみ",),
    "docs/09-security/authentication-and-user-isolation.md": ("自作の認証基盤を第一候補に固定しない", "AUTH-T"),
    "docs/03-tools/mcp-adapter.md": ("公式MCP Python SDK", "MCP-PY-T01"),
    "docs/10-infrastructure/job-queue-and-outbox.md": ("ADR-039", "単一系統", "現行の採用構成は`QueuePort`"),
    "docs/02-runtime/context-manager-and-compaction.md": ("DeepAgents Python",),
    "README.md": ("Python-first", "STEP 2"),
    "AGENTS.md": ("Python-first", "STEP 2"),
}

errors: list[str] = []
adr = ADR.read_text(encoding="utf-8")
if "## ADR-036〜040" not in adr:
    errors.append("ADR-036..040 section missing")
ledger_part = adr.split("Python-first 移行監査台帳", 1)[-1]
ledger = dict(re.findall(r"^\|\s*`(docs/[^`]+\.md)`\s*\|\s*([ABC](?:/[ABC])*)\s*\|", ledger_part, re.M))

for path, words in REQUIRED.items():
    body = (ROOT / path).read_text(encoding="utf-8")
    for w in words:
        if w not in body:
            errors.append(f"{path}: missing marker {w!r}")

# STEP 2 selected ADR-038/039 from measured conformance results; they must not be presented as
# LOCKED/VERIFIED before Owner review, and the evidence suite must exist.
for adr_id in ("ADR-038", "ADR-039"):
    row = next((ln for ln in adr.splitlines() if ln.startswith(f"| **{adr_id}**")), "")
    if "SELECTED" not in row or "NOT LOCKED" not in row or re.search(r"PRODUCT_FIXED|VERIFIED|ACCEPTED", row):
        errors.append(f"{adr_id} must be SELECTED (STEP 2) and NOT LOCKED")
for rel in ("conformance/agent_runtime/tests/test_conformance.py", ".github/workflows/agent-runtime-conformance.yml"):
    if not (ROOT / rel).is_file():
        errors.append(f"STEP 2 evidence missing: {rel}")

triggered: list[str] = []
for md in sorted((ROOT / "docs").rglob("*.md")):
    rel = md.relative_to(ROOT).as_posix()
    text = md.read_text(encoding="utf-8")
    if TRIGGER.search(text):
        triggered.append(rel)
        if rel not in ledger:
            errors.append(f"unclassified TS/Node reference: {rel} (add to ADR migration ledger)")
        elif "A" in ledger[rel] and "ADR-036" not in text and rel != ADR.relative_to(ROOT).as_posix():
            errors.append(f"class A file lacks ADR-036 disposition: {rel}")
    for no, phrase in adoption_violations(text):
        errors.append(f"{rel}:{no}: TS adoption phrase without history mark: {phrase!r}")
    for no, line in enumerate(text.splitlines(), 1):
        # Rust must never become a mandatory dependency.
        if re.search(r"\bRust\b", line) and re.search(r"MUST|必須(?!にしない|依存にしない|要素にしない)", line):
            if not re.search(r"必須にしない|必須依存にしない|必須要素にしない|任意", line):
                errors.append(f"{rel}:{no}: Rust stated as mandatory")

for rel in ledger:
    if not (ROOT / rel).is_file():
        errors.append(f"ledger lists missing file: {rel}")

# Same-language must not merge trust boundaries.
rules = (ROOT / "docs/00-foundation/architecture-rules.md").read_text(encoding="utf-8")
for w in ("venv", "network namespace", "exec"):
    if w not in rules:
        errors.append(f"architecture-rules §E missing isolation term {w!r}")

if errors:
    print("FAIL python-first static audit")
    for e in errors:
        print(" -", e)
    sys.exit(1)
counts = {k: sum(1 for v in ledger.values() if k in v) for k in "ABC"}
print(f"PASS python-first static audit: triggered={len(triggered)} ledger={len(ledger)} A={counts['A']} B={counts['B']} C={counts['C']}")
print("STATIC ONLY - not Python runtime/API/Browser/Queue/VM verification.")
