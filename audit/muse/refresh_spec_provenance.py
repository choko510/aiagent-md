#!/usr/bin/env python3
"""Regenerate the current canonical + companion Git blob provenance (not historical Round6/7)."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX = ROOT / "docs/00-foundation/spec-index.md"
PROVENANCE = ROOT / "audit/muse/spec-provenance.json"

text = INDEX.read_text(encoding="utf-8")
modules = re.findall(
    r"^\|\s*(\d{2}-\d{2})\s*\|\s*\x60(docs/[^\x60]+\.md)\x60\s*\|\s*CHAT_\d{2}\s*\|",
    text, re.M,
)
companions = re.findall(
    r"^\|\s*(COMP-\d+)\s*\|\s*\x60(docs/[^\x60]+\.md)\x60\s*\|",
    text, re.M,
)
if len(modules) != 88 or not companions:
    raise SystemExit("Unexpected canonical/companion inventory; review spec-index first")
for records in (modules, companions):
    if len({x for x, _ in records}) != len(records):
        raise SystemExit("Duplicate inventory IDs")
paths = [path for _, path in modules + companions]
if len(set(paths)) != len(paths):
    raise SystemExit("Overlapping canonical/companion path")
for path in paths:
    if not (ROOT / path).is_file():
        raise SystemExit(f"Missing cataloged source: {path}")

def blob_hash(path: str) -> str:
    return subprocess.check_output(
        ["git", "hash-object", str(ROOT / path)],
        cwd=ROOT, text=True,
    ).strip()

provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
provenance["moduleCount"] = len(modules)
provenance["companionCount"] = len(companions)
provenance["modules"] = {p: blob_hash(p) for _, p in modules}
provenance["companionSpecs"] = {p: blob_hash(p) for _, p in companions}
PROVENANCE.write_text(
    json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"Refreshed {len(modules)} canonical and {len(companions)} companion Git blob hashes")
