#!/usr/bin/env python3
"""Static cross-spec contract gate for PR #69. NOT runtime/DB/LLM safety verification."""
from __future__ import annotations
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA = ROOT / "audit/context/p0_cases.json"
CONTEXT = ROOT / "docs/02-runtime/context-manager-and-compaction.md"
REQUIRED = {
 "docs/02-runtime/context-manager-and-compaction.md": (
   "SourceBackedFact", "SemanticVerificationReceipt", "TypedGoalAnchor",
   "ModelRequestSnapshot", "dispatch_admission", "Tinput+R+S<=W",
   "UNVERIFIED", "COVERAGE_INCOMPLETE", "MAY_HAVE_SENT",
   "Sealed Original", "Redacted Projection"),
 "docs/02-runtime/goal-and-planner.md": ("interface GoalCondition", "ConditionValue", "sourceUserMessageRef"),
 "docs/01-contracts/core-domain-models.md": ("ModelRequestSnapshotRef", "DispatchAdmissionId"),
 "docs/01-contracts/evidence-artifact-contracts.md": ("SealedOriginalArtifact", "SemanticVerificationReceipt"),
 "docs/06-memory-skills/memory-data-model.md": ("dispatch_admission", "scope"),
 "docs/06-memory-skills/memory-retrieval-and-update.md": ("dispatch_admission", "Memory"),
 "docs/09-security/privacy-audit-and-retention.md": ("Model Recipient Policy", "SealedOriginal"),
 "docs/02-runtime/model-router.md": ("recipient", "ModelRequestSnapshot"),
 "docs/02-runtime/checkpoint-and-recovery.md": ("ModelRequestSnapshot", "MAY_HAVE_SENT"),
 "docs/10-infrastructure/database-schema-and-migrations.md": (
   "memory_scope_epochs", "model_request_snapshots", "model_dispatch_admissions"),
 "docs/02-runtime/subagent-delegation.md": ("Child Result", "CHILD_STALE"),
 "docs/11-qa/test-harness-and-fixtures.md": ("ACM-T21", "ACM-T36"),
 "docs/11-qa/benchmark-and-release-gates.md": ("ACM-T21", "Coverage"),
}
errors = []
for rel, markers in REQUIRED.items():
    p = ROOT / rel
    if not p.is_file():
        errors.append(f"missing spec {rel}")
        continue
    body = p.read_text(encoding="utf-8")
    for mark in markers:
        if mark not in body:
            errors.append(f"{rel}: missing contract marker {mark}")
ctx = CONTEXT.read_text(encoding="utf-8")
ids = re.findall(r"^\| ACM-T(\d\d) \|", ctx, re.M)
expected_ids = [f"{n:02d}" for n in range(1, 37)]
if sorted(set(ids)) != expected_ids or len(ids) != len(expected_ids):
    errors.append(f"expected ACM-T01..T36 once each; found {ids}")
data = json.loads(DATA.read_text(encoding="utf-8"))
if data.get("kind") != "design-oracles-not-runtime-results":
    errors.append("fixture must be marked design-only")
cases = data.get("cases", [])
expected_case_ids = {f"ACM-T{i}" for i in range(21, 37)}
if len(cases) != 16 or {c.get("id") for c in cases} != expected_case_ids:
    errors.append("fixture catalog not exactly T21..T36")
if any(c.get("runStatus") != "NOT_RUN" or not c.get("expectedOracle")
       for c in cases):
    errors.append("test fixture lacks oracle or misstates executed status")
ex = data["examples"]
parking = ex["parkingSource"]
if int(parking["lodgingTotalYen"]) + int(parking["parkingFeeYen"]) != int(parking["derivedKnownComponentTotalYen"]):
    errors.append("parking fee reference arithmetic mismatch")
if parking["parkingIncluded"] is not False or parking["expectedReceipt"] != "REFUTED_BY_SPAN":
    errors.append("parking fee negative entailment example missing")
if parking["derivedAllInTotalVerified"] is not False:
    errors.append("unknown tax/terms may not be described as verified all-in")
b = ex["tokenBuckets"]
if len(set(b["paths"])) != len(b["paths"]):
    errors.append("token accounting buckets overlap")
tokens = sum(b["tokens"])
if tokens != b["expectedUniqueTotal"]:
    errors.append("token example arithmetic mismatch")
if (tokens + b["R"] + b["S"] <= b["W"]) != b["expectedWindowPass"]:
    errors.append("token example window assertion mismatch")
m = ex["memoryOrdering"]
if not (m["deleteCommitSeq"] < m["admissionAttemptSeq"] and m["expectedAdmitted"] is False):
    errors.append("DELETE-before-Admit fixture must reject stale memory")
snap = set(ex["snapshotRequiredFields"])
if not {"messages", "toolSchemas", "promptTemplateDigest",
        "providerPayloadDigest", "dispatchAdmissionRef"}.issubset(snap):
    errors.append("snapshot example lacks final payload replay fields")

# OSS integration: contract markers and design fixture traceability ONLY, not runtime proof.
oss = data.get("ossCompaction", {})
oss_cases = oss.get("cases", [])
oss_ids = {f"ACM-OSS-T{i:02d}" for i in range(1, 15)}
if oss.get("kind") != "design-oracles-not-runtime-results" or oss.get("testCount") != 14:
    errors.append("OSS compaction cases must be explicitly design-only with 14 tests")
if len(oss_cases) != 14 or {case.get("id") for case in oss_cases} != oss_ids:
    errors.append("OSS compaction fixture missing or duplicate IDs")
allowed = {
    "RETAIN_PREVIOUS_HEAD", "REJECT", "RETRY_FROM_RAW",
    "REBUILD_AND_READMIT", "PARTIAL_OR_BLOCKED",
    "REJECT_ON_HARD_ANCHOR_LOSS", "EXPIRED"
}
for case in oss_cases:
    if (case.get("runStatus") != "NOT_RUN"
        or not case.get("failureMode") or not case.get("expectedOracle")
        or case.get("expectedDecision") not in allowed):
        errors.append(f"Invalid OSS compaction fixture: {case.get('id')}")
qa_oss = (ROOT / "docs/11-qa/benchmark-and-release-gates.md").read_text(encoding="utf-8")
for tid in oss_ids:
    if ctx.count("| " + tid + " |") != 1:
        errors.append(f"OSS context test missing/duplicate: {tid}")
    if tid not in qa_oss:
        errors.append(f"OSS QA test missing: {tid}")
for marker in (
    "DeepAgents JS", "Mastra Observational Memory", "OpenClaw",
    "qualityDegraded", "observedMessageIds", "sourcePartRangeRefs",
    "RAW_DURABLE", "SourceBackedFact", "dispatch_admission"
):
    if marker not in ctx:
        errors.append(f"OSS integration contract missing: {marker}")

print(f"adaptive_context_static: specs={len(REQUIRED)} cases={len(cases)} "
      f"fixture_arithmetic={tokens}; designed_only=True")
if errors:
    for error in errors:
        print("ERROR:", error, file=sys.stderr)
    sys.exit(1)
print("PASS: documentation markers and synthetic fixture consistency ONLY; NOT runtime verification.")
