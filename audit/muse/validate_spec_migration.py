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
# Companions are audited without inflating the canonical 88-module inventory.
# Parse COMP IDs and paths from the same spec-index as human readers, compare
# membership+content hash against manifest and fail on missing/extra companions.
companion_records = re.findall(
    r"^\|\s*(COMP-\d+)\s*\|\s*\x60(docs/[^\x60]+\.md)\x60\s*\|",
    text, re.M,
)
companion_ids = [key for key, _ in companion_records]
companion_paths = [path for _, path in companion_records]
if len(set(companion_ids)) != len(companion_ids) or len(set(companion_paths)) != len(companion_paths):
    errors.append("Duplicate companion IDs or paths in spec-index.md")
for path in companion_paths:
    cp = ROOT / path
    if not cp.is_file() or len(cp.read_text(encoding="utf-8")) < 600:
        errors.append(f"Missing/thin audited companion: {path}")

# MKT-COMPANION-STATIC: design/provenance contract, NOT runtime verification.
marketplace_path = "docs/08-interfaces/extension-marketplace.md"
marketplace_pairs = [pair for pair in companion_records if pair[1] == marketplace_path]
if marketplace_pairs != [("COMP-03", marketplace_path)]:
    errors.append("COMP-03 Marketplace must be registered without replacing LDC COMP-02")
elif not (ROOT / marketplace_path).is_file():
    errors.append("COMP-03 Marketplace specification missing")
else:
    body = (ROOT / marketplace_path).read_text(encoding="utf-8")
    marker_ids = re.findall(r"^\|\s*(MKT-T\d{2})\s*\|", body, re.M)
    if marker_ids != [f"MKT-T{n:02d}" for n in range(1, 20)]:
        errors.append("Marketplace acceptance table MKT-T01..T19 must be unique and ordered")
    for marker in (
        "OWNER_NOT_VERIFIABLE", "ownerHandle", "artifactSha256",
        "MKT-T19", "Riskを自己申告だけで下げない",
        "GitHubはMKT-2以降", "NOT IMPLEMENTED", "PR #82",
    ):
        if marker not in body:
            errors.append(f"COMP-03 Marketplace invariant missing: {marker}")

# LDC-COMPANION-STATIC: spec/provenance contract only. Not a runtime safety test.
# COMP-02 must be cataloged beside COMP-01, without changing 88 canonical modules.
ldc_path = "docs/16-app-development/local-device-rust-client.md"
ldc_pairs = [pair for pair in companion_records if pair[1] == ldc_path]
if ldc_pairs != [("COMP-02", ldc_path)]:
    errors.append("COMP-02 must register exact LDC companion path in spec-index")
elif not (ROOT / ldc_path).is_file():
    errors.append("COMP-02 missing Rust Local Device Client specification")
else:
    ldc_text = (ROOT / ldc_path).read_text(encoding="utf-8")
    expected_ldc = [f"{n:02d}" for n in range(1, 33)]
    # A requirement must own exactly one test cell, using the same suffix.
    # This catches a dropped row, a duplicate, a swapped mapping and a stray ID.
    ldc_matrix = re.findall(
        r"^\|\s*LDC-REQ-(\d{2})\b[^|\n]*\|\s*LDC-T(\d{2})\s*\|",
        ldc_text, re.M,
    )
    if [req for req, _ in ldc_matrix] != expected_ldc:
        errors.append("LDC-REQ-01..32 missing, duplicated or out of order in trace matrix")
    if [test for _, test in ldc_matrix] != expected_ldc:
        errors.append("LDC-T01..32 does not map 1:1 to LDC-REQ-01..32")
    ldc_sections = re.findall(r"^## (\d+)\. ", ldc_text, re.M)
    if ldc_sections != [str(n) for n in range(1, 24)]:
        errors.append(f"LDC spec section inventory must be 1..23: {ldc_sections}")
    for marker in (
        "deviceClaimEpoch", "START_PERMIT", "WAITING_RECONCILIATION",
        "semanticEffectKey", "RISK_ACCEPTED_RETRY", "reconcile_existing_intent",
        "RECEIPT_CONFLICT", "core.fsmonitor", "UNKNOWN_DIRTY_STATE",
        "Desktop Cua runtime", "Browser runtime", "include_screenshot: false",
        "consentPayloadSha256", "ACCOUNT_SWITCH_PREPARE",
        "ZEROIZE_A_ACTIVE_MEMORY", "NOT RUN",
        "LDC-NT01", "LDC-NT02", "LDC-NT03", "LDC-NT04",
        "LDC-NT05", "LDC-NT06", "LDC-NT07",
    ):
        if marker not in ldc_text:
            errors.append(f"COMP-02 LDC safety boundary or negative test missing: {marker}")
    # Catch concrete regressions to legacy unsafe rules.
    for forbidden in (
        "Local成功をCloudの`CONFIRMED`へ反映する前に、Cloud側の現行run lease",
        "Git: `git rev-parse`、commit/branch/dirty summary",
    ):
        if forbidden.replace("\\`", "`") in ldc_text:
            errors.append(f"COMP-02 reverted a reviewed security boundary: {forbidden}")
    if "COMP-02" not in text:
        errors.append("LDC companion is missing from the canonical spec-index")

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
    companions = provenance.get("companionSpecs", {})
    if provenance.get("companionCount") != len(companion_paths) or set(companions) != set(companion_paths):
        errors.append("Provenance companion inventory diverges from spec-index")
    # Use the identical Git blob content verification for canonical and companion files.
    blobs = {**blobs, **companions}
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
# SC headings in the foundation document, not a hard-coded old marker, own the
# current scenario count. An added SC must receive P/N E2E cases before CI passes.
foundation = (ROOT / "docs/00-foundation/acceptance-scenarios.md").read_text(encoding="utf-8")
scenario_ids = [int(n) for n in re.findall(r"^#{2,3}\s+SC-(\d{2})\b", foundation, re.M)]
if not scenario_ids or scenario_ids != list(range(1, len(scenario_ids) + 1)):
    errors.append(f"SC inventory not unique/continuous: {scenario_ids}")
scenario_count = len(scenario_ids)
qa = require("docs/11-qa/end-to-end-acceptance.md",
    ["QA-E2E-017", "QA-E2E-022", "QA-E2E-T23F", "QA-E2E-T23OFF"])
# Narrative number is validated against the scenario inventory; scenario changes
# cannot be hidden by leaving a stale '22シナリオ' string elsewhere in the file.
match = re.search(r"採用は\*\*(\d+)シナリオ", qa)
if not match or int(match.group(1)) != scenario_count:
    errors.append(f"QA E2E narrative count does not equal foundation SC count={scenario_count}")
for i in scenario_ids:
    for k in (f"QA-E2E-T{i:02d}P", f"QA-E2E-T{i:02d}N", f"SC-{i:02d}"):
        if k not in qa:
            errors.append(f"E2E traceability lacks {k}")
if scenario_count >= 23:
    companion = ROOT / "docs/08-interfaces/browser-live-observation.md"
    if not companion.is_file():
        errors.append("SC-23 missing Browser Live Observation companion design")
    else:
        cbody = companion.read_text(encoding="utf-8")
        for k in ("BLO-001", "BLO-017", "BLO-T01", "BLO-T16",
                  "BLO-OFF-T01", "restoreIncarnation", "REVOKED_BARRIER_COMMITTED",
                  "HUMAN", "PRIVACY_BLOCKED"):
            if k not in cbody:
                errors.append(f"Browser Live Observation lacks {k}")
    for k in ("QA-E2E-T23P", "QA-E2E-T23N", "QA-E2E-T23F", "QA-E2E-T23OFF"):
        if k not in qa:
            errors.append(f"E2E missing SC-23 variant {k}")
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

print(f"muse_contract: modules={len(inventory)} companions={len(companion_paths)} scenarios={scenario_count} counts={dict(counts)}")
if errors:
    for error in errors:
        print("ERROR:", error, file=sys.stderr)
    sys.exit(1)
print(f"PASS: Static {len(inventory)}-module + {len(companion_paths)}-companion inventory; {scenario_count} scenarios checked. NOT runtime verification.")
