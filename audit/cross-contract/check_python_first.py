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
)
HISTORY_MARK = re.compile(r"旧|superseded|historical|履歴|formerly|ADR-036|2026-11-09", re.I)

REQUIRED = {
    "docs/00-foundation/architecture-decisions.md": (
        "ADR-036", "ADR-037", "ADR-038", "ADR-039", "ADR-040", "PRODUCT_FIXED",
        "OPEN（STEP 2で選定）", "PydanticAI＋DBOS", "DeepAgents Python＋LangGraph",
        "Python-first 移行監査台帳",
    ),
    "docs/00-foundation/system-architecture.md": (
        "### 13.2 Python-first", "Untrusted Execution VM のRuntime Cellのみ",
        "独立プロセス＋認証付きRPC",
    ),
    "docs/00-foundation/architecture-rules.md": ("## E. Python-first", "論理型記法", "Retryの区別"),
    "docs/02-runtime/agent-loop.md": ("AgentRuntimePort", "PYRT-T01"),
    "docs/03-tools/mcp-adapter.md": ("公式MCP Python SDK", "MCP-PY-T01"),
    "docs/10-infrastructure/job-queue-and-outbox.md": ("ADR-039", "単一系統"),
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

# ADR-038 must stay undecided in STEP 1.
row038 = next((ln for ln in adr.splitlines() if ln.startswith("| **ADR-038**")), "")
if "OPEN" not in row038 or re.search(r"PRODUCT_FIXED|LOCKED|ACCEPTED", row038):
    errors.append("ADR-038 must remain OPEN until STEP 2")

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
    for no, line in enumerate(text.splitlines(), 1):
        for phrase in FORBIDDEN:
            if phrase in line and not HISTORY_MARK.search(line):
                errors.append(f"{rel}:{no}: TS adoption phrase without history mark: {phrase!r}")
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
