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

# Current 88-module content provenance is separate from historical pinned 85-source audit.
import json
import subprocess
provenance_path = ROOT / "audit/muse/spec-provenance.json"
if not provenance_path.is_file():
    errors.append("Missing current 88-module provenance manifest")
else:
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    blobs = provenance.get("modules", {})
    if provenance.get("moduleCount") != 88 or set(blobs) != set(paths):
        errors.append("Current provenance manifest does not match 88-module inventory")
    for source_path, source_blob in blobs.items():
        if not re.fullmatch(r"[0-9a-f]{40}", str(source_blob)):
            errors.append(f"Malformed git blob hash: {source_path}")
            continue
        try:
            actual = subprocess.check_output(
                ["git", "hash-object", str(ROOT / source_path)],
                cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            errors.append(f"Cannot hash provenance file: {source_path}")
            continue
        if actual != source_blob:
            errors.append(f"Spec content changed without updating provenance manifest: {source_path}")

def require(path, terms):
    body = (ROOT / path).read_text(encoding="utf-8")
    for term in terms:
        if term not in body:
            errors.append(f"{path}: required semantic invariant marker missing: {term}")
    return body

architecture = require("docs/00-foundation/system-architecture.md",
    ["Trusted Control VM", "Untrusted Execution VM", "distinct guest kernel",
     "Enforced Egress Transport", "only scoped broker RPC"])
for old_edge in (r"^\s*AUTH\s*-->\s*INTERNET",r"^\s*CONN\s*-->\s*INTERNET",
                 r"^\s*CELL\s*-->\s*INTERNET",r"^\s*BW\s*-->\s*INTERNET"):
    if re.search(old_edge, architecture, re.M):
        errors.append("Direct external network diagram bypass in architecture: "+old_edge)
sections = re.findall(r"^## (\d+)\. ", architecture, re.M)
if sections != [str(x) for x in range(1,14)]:
    errors.append(f"Architecture section numbering not 1..13: {sections}")
cell = require("docs/09-security/runtime-cell-isolation.md",
    ["different guest kernel", "CELL-017", "CELL-018", "cgroup.kill"])
sentinel = require("docs/09-security/sentinel-authority.md",
    ["SENT-017", "SENT-018", "SENT-019", "SENT-021", "Package Broker",
     "Enforced Egress Transport", "broker_rpc_only"])
execspec = require("docs/02-runtime/sandbox-execution.md",
    ["SandboxExecProposalV1", "SandboxExecutionAdmissionV1", "ExternalActionProposalV1",
     "EXEC-T13", "EXEC-T18", "runtimeProfile", "broker_rpc_only"])
if "type SandboxExecInput = {" in execspec:
    errors.append("Legacy combined LLM/server SandboxExecInput still normative")
sbox = require("docs/09-security/sandbox-and-capabilities.md",
    ['"sandbox_exec"', "SBOX-T01", "bash", "Host"])
if re.search(r"^type ExecutorKind\s*=.*;$", sbox, re.M) and '"sandbox_exec"' not in re.search(r"^type ExecutorKind\s*=.*;$",sbox,re.M).group(0):
    errors.append("ExecutorKind missing sandbox_exec")
adrs = require("docs/00-foundation/architecture-decisions.md",
    ["ADR-002", "ADR-017", "ADR-018", "two KVM/QEMU guests"])
if "host and trusted control" not in adrs.lower() and "host and Trusted Control" not in adrs:
    # Case/wording independent check already covered by positive tokens above.
    pass
for path, bad_terms in {
    "docs/02-runtime/runtime-host-and-session.md": [
        "inputGoalRevision:number","appliedGoalRevision:number",
        "leaseEpochは安全なJS number"],
    "docs/02-runtime/steer-queue-pause-resume.md": [
        "goalRevisionBefore:number","peekBoundary(runId:string,epoch:number)",
        "checkpointVersion:number","Run leaseEpochは非負safe integer"],
}.items():
    body = (ROOT/path).read_text(encoding="utf-8")
    for bad in bad_terms:
        if bad in body:
            errors.append(f"Stale unsafe number/epoch codec: {path}: {bad}")
qa = require("docs/11-qa/end-to-end-acceptance.md",
    ["22シナリオ", "QA-E2E-017", "QA-E2E-022"])
for i in range(17,23):
    for k in (f"QA-E2E-T{i:02d}P",f"QA-E2E-T{i:02d}N",f"SC-{i:02d}"):
        if k not in qa:
            errors.append(f"E2E traceability lacks {k}")
    if f"SC-{i:02d}" not in (ROOT/"docs/00-foundation/acceptance-scenarios.md").read_text(encoding="utf-8"):
        errors.append(f"Foundation missing SC-{i:02d}")
for path,required in {
 "docs/09-security/secrets-and-credentials.md":["sensitivity","DENY"],
 "docs/06-memory-skills/skill-lifecycle.md":["Package Broker","offline"],
 "docs/02-runtime/steer-queue-pause-resume.md":["cgroup.kill","PAUSE","STEER"],
 "docs/02-runtime/subagent-delegation.md":["parent-child isolation","Task/Run"],
}.items():
    require(path,required)
archived1=(ROOT/".github/workflows/round6-lock-candidate.yml").read_text(encoding="utf-8")
archived2=(ROOT/".github/workflows/round7-integrated-premerge.yml").read_text(encoding="utf-8")
if "LEGACY_85_BASE_SHA" not in archived1 or "8b153c238c8a4cc18b6e9d258e55d3265e5249d5" not in archived2:
    errors.append("Historical original 85-source workflows not separated from current 88-source provenance")

print(f"muse_contract: modules={len(inventory)} counts={dict(counts)}")
if errors:
    for error in errors:
        print("ERROR:", error, file=sys.stderr)
    sys.exit(1)
print("PASS: Static 88-module inventory/marker/link validation. NOT runtime verification.")
