#!/usr/bin/env python3
"""Static Muse migration contract validation (documentation only, not runtime safety verification)."""
from __future__ import annotations

import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX = ROOT / "docs/00-foundation/spec-index.md"
EXPECTED = {
    "00": 5, "01": 6, "02": 13, "03": 6, "04": 9,
    "05": 5, "06": 5, "07": 5, "08": 6, "09": 8,
    "10": 5, "11": 4, "12": 5, "13": 3, "14": 2, "15": 1,
}
REQUIRED_NEW = (
    "docs/02-runtime/sandbox-execution.md",
    "docs/09-security/runtime-cell-isolation.md",
    "docs/09-security/sentinel-authority.md",
)
CHECKS = {
    "docs/02-runtime/sandbox-execution.md": ("Sandbox Exec", "EXEC-T12", "Runtime Cell"),
    "docs/09-security/runtime-cell-isolation.md": ("CELL-016", "CELL-T10", "production"),
    "docs/09-security/sentinel-authority.md": ("SENT-016", "SENT-T10", "opaque"),
    "docs/00-foundation/product-requirements.md": ("PRD-025", "PRD-020"),
    "docs/00-foundation/architecture-decisions.md": ("ADR-022", "DEC-P0-07"),
    "docs/00-foundation/acceptance-scenarios.md": ("SC-17", "SC-22"),
    "docs/11-qa/end-to-end-acceptance.md": ("SC-17", "SC-22"),
    "docs/03-tools/tool-execution-gateway.md": ("GW-021", "Sentinel"),
    "README.md": ("88", "Sandbox Execution"),
    "AGENTS.md": ("Runtime Cell", "Sentinel"),
}
errors: list[str] = []
text = INDEX.read_text(encoding="utf-8")
inventory = re.findall(
    r"^\|\s*(\d{2}-\d{2})\s*\|\s*\x60(docs/[^\x60]+\.md)\x60\s*\|\s*CHAT_\d{2}\s*\|",
    text, re.M,
)
if len(inventory) != 88:
    errors.append(f"Expected 88 inventory records; got {len(inventory)}")
ids = [key for key, _ in inventory]
paths = [path for _, path in inventory]
if len(set(ids)) != len(ids):
    errors.append("Duplicate module IDs in spec-index.md")
if len(set(paths)) != len(paths):
    errors.append("Duplicate module paths in spec-index.md")
counts = collections.Counter(path.split("/")[1][:2] for path in paths)
if dict(counts) != EXPECTED:
    errors.append(f"Wrong per-owner counts: {dict(counts)} vs {EXPECTED}")
for path in paths:
    p = ROOT / path
    if not p.is_file():
        errors.append(f"Missing module: {path}")
    elif len(p.read_text(encoding="utf-8")) < 600:
        errors.append(f"Empty/thin module: {path}")
for path in REQUIRED_NEW:
    if path not in paths:
        errors.append(f"Missing migration contract in index: {path}")
for path, words in CHECKS.items():
    p = ROOT / path
    if not p.exists():
        errors.append(f"Missing required evidence source: {path}")
        continue
    body = p.read_text(encoding="utf-8")
    for word in words:
        if word not in body:
            errors.append(f"{path}: expected marker {word!r}")
# Verify relative links in the migration canonical docs; legacy study/audit
# links are intentionally outside this gate and still need separate review.
for path in ("README.md", "AGENTS.md", "docs/00-foundation/spec-index.md", *REQUIRED_NEW):
    body = (ROOT / path).read_text(encoding="utf-8")
    for dest in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", body):
        dest = dest.split("#", 1)[0].split("?", 1)[0]
        if not dest or "://" in dest or dest.startswith(("mailto:", "data:")):
            continue
        candidate = (ROOT / path).parent / dest
        if not candidate.exists():
            errors.append(f"Broken relative link in {path}: {dest}")
# Do not mistake historical Round6/Round7 pinned-85 provenance workflows
# for the updated 88-module design validation.
print(f"muse_contract: modules={len(inventory)} counts={dict(counts)}")
if errors:
    for error in errors:
        print("ERROR:", error, file=sys.stderr)
    sys.exit(1)
print("PASS: Static 88-module inventory/marker/link validation. NOT runtime verification.")
