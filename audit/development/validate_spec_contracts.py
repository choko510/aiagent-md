#!/usr/bin/env python3
"""Static development/Campaign documentation audit (NOT runtime or Owner verification)."""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULES = {
    "DEV-01": "docs/16-app-development/project-workspace-and-git.md",
    "DEV-02": "docs/16-app-development/multi-agent-development.md",
    "DEV-03": "docs/16-app-development/visual-design-and-demo.md",
    "DEV-04": "docs/16-app-development/expert-advisor.md",
    "DEV-05": "docs/16-app-development/build-preview-release.md",
    "DEV-06": "docs/16-app-development/autonomous-browser-debugger.md",
    "DEV-07": "docs/16-app-development/review-and-repair-gates.md",
    "DEV-08": "docs/16-app-development/feature-factory.md",
    "DEV-09": "docs/16-app-development/development-api-and-ui.md",
    "DEV-10": "docs/16-app-development/acceptance-and-rollout.md",
    "CAM-01": "docs/07-automation/endless-run-campaign.md",
}
TEST_OWNERS = {
    "DEV-WS": ("DEV-01", 7),
    "DEV-TEAM": ("DEV-02", 6),
    "DEV-DES": ("DEV-03", 11),
    "DEV-EXP": ("DEV-04", 5),
    "DEV-BLD": ("DEV-05", 14),
    "DEV-DBG": ("DEV-06", 10),
    "DEV-REV": ("DEV-07", 8),
    "DEV-FF": ("DEV-08", 11),
    "DEV-UI": ("DEV-09", 10),
    "CAM": ("CAM-01", 18),
}
SEMANTIC_MARKERS = {
    MODULES["DEV-03"]: ("demo:interact", "REQUIRE_USER_APPROVAL", "AUTO_RECOMMEND", "PREAUTHORIZED_BOUNDED_AUTO"),
    MODULES["DEV-05"]: ("PreviewCapabilityV2", "originGeneration", "runLeaseEpoch", "DEV-BLD-T14"),
    MODULES["DEV-07"]: ("integration review", "DEV-REV-T08"),
    MODULES["DEV-08"]: ("logicalWorkItemId", "INTEGRATED", "DEV-FF-T11"),
    MODULES["DEV-09"]: ("campaign:control", "designPolicy", "demoAcceptancePolicy"),
    MODULES["CAM-01"]: ("QuotaWaitReceiptV1", "CampaignChildAdmissionV1", "controlVersion", "CAM-T18"),
    "docs/10-infrastructure/database-schema-and-migrations.md": (
        "campaign_count_receipts", "campaign_quota_wait_receipts", "development_integration_receipts",
        "development_demo_decisions", "preview_sessions", "DEV-DB-T08",
    ),
    "docs/09-security/authentication-and-user-isolation.md": ("DevelopmentActionV1", "demo:interact", "AUTH-DEV-T06"),
    "docs/01-contracts/event-envelope-and-stream.md": ("campaign.control_changed", "EVT-DEV-T05"),
    "docs/11-qa/benchmark-and-release-gates.md": ("GATE-DEV-T01", "development disabled", "Feature Factory enabled"),
    "docs/11-qa/end-to-end-acceptance.md": ("DevelopmentQaFixtureV1", "SC-24", "SC-31", "QA-E2E-T31F"),
}
errors: list[str] = []

def read(path: str) -> str:
    f = ROOT / path
    if not f.is_file():
        errors.append(f"missing file: {path}")
        return ""
    return f.read_text(encoding="utf-8")

index = read("docs/00-foundation/spec-index.md")
read("docs/16-app-development/README.md")
for module_id, path in MODULES.items():
    if module_id not in index or path not in index:
        # Index uses relative paths; allow documented basename if exact link target resolves.
        relative = path.rsplit("/", 1)[-1]
        if module_id not in index or relative not in index:
            errors.append(f"module inventory mismatch: {module_id} {path}")
    body = read(path)
    if len(body) < 400:
        errors.append(f"thin or empty module: {path}")
    # Only in newly authored modules, not all legacy reference docs.
    for match in re.finditer(r"(?m)^\s*(?:[-*]\s*)?T\d{2}(?=[:\s])", body):
        errors.append(f"abbreviated test ID: {path}:{body.count(chr(10), 0, match.start()) + 1}")
for prefix, (module, maximum) in TEST_OWNERS.items():
    body = read(MODULES[module])
    pattern = rf"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(prefix)}-T(\d{{2}})\b"
    numbered = [int(i) for i in re.findall(pattern, body)]
    if sorted(numbered) != list(range(1, maximum + 1)):
        errors.append(f"{prefix} definitions expected 01..{maximum:02d}, got {numbered}")

for path, words in SEMANTIC_MARKERS.items():
    body = read(path)
    for marker in words:
        if marker not in body:
            errors.append(f"{path}: missing contract marker {marker}")

qa = read("docs/11-qa/end-to-end-acceptance.md")
scenarios = read("docs/00-foundation/acceptance-scenarios.md")
requirements = read(MODULES["DEV-10"])
fixture_path = "audit/development/scenarios.json"
try:
    obj = json.loads(read(fixture_path))
except (ValueError, TypeError) as exc:
    errors.append(f"malformed fixture JSON: {exc}")
    obj = {}
if obj.get("schemaVersion") != 1:
    errors.append("fixture schemaVersion must be 1")
cases = obj.get("scenarios", [])
if not isinstance(cases, list):
    errors.append("fixture scenarios must be array")
    cases = []
ids = [x.get("scenarioId") for x in cases if isinstance(x, dict)]
if sorted(ids) != [f"SC-{n}" for n in range(24, 32)]:
    errors.append(f"SC-24..31 fixture list incorrect: {ids}")
for case in cases:
    if not isinstance(case, dict):
        errors.append("non-object fixture")
        continue
    sc = case.get("scenarioId", "")
    try:
        num = int(sc.split("-")[1])
    except (IndexError, ValueError):
        errors.append(f"invalid scenario id {sc}")
        continue
    if case.get("status") != "DESIGNED_NOT_RUN":
        errors.append(f"fixture must not falsely claim executed status: {sc}")
    if case.get("featureGate") not in ("development", "campaign"):
        errors.append(f"unknown feature gate: {sc}")
    lanes = case.get("cases", {})
    if sorted(lanes) != ["fault", "negative", "positive"]:
        errors.append(f"missing positive/negative/fault lanes: {sc}")
        continue
    for lane, suffix in (("positive", "P"), ("negative", "N"), ("fault", "F")):
        item = lanes[lane]
        test_id = f"QA-E2E-T{num}{suffix}"
        if item.get("id") != test_id:
            errors.append(f"wrong fixture id {item.get('id')} expected {test_id}")
        if test_id not in qa:
            errors.append(f"QA missing test: {test_id}")
        for field in ("setup", "action", "oracle", "forbidden"):
            if not isinstance(item.get(field), str) or len(item[field].strip()) < 8:
                errors.append(f"fixture {test_id} lacks {field}")
        if item.get("evidenceRefs") != []:
            errors.append(f"static design fixture {test_id} unexpectedly claims evidence")
    if sc not in scenarios or sc not in qa or sc not in requirements:
        errors.append(f"{sc} not linked in foundation/QA/development acceptance")
for number in range(1, 19):
    req = f"DEV-REQ-{number:02d}"
    if req not in requirements:
        errors.append(f"missing requirement {req}")
for path in [*MODULES.values(), "docs/16-app-development/README.md"]:
    body = read(path)
    # The Markdown inventory and other links may include an anchor.
    for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", body):
        candidate = target.split("#", 1)[0].split("?", 1)[0]
        if not candidate or "://" in candidate or candidate.startswith(("mailto:", "data:")):
            continue
        resolved = (ROOT / path).parent / candidate
        if not resolved.exists():
            errors.append(f"broken relative link {path}: {candidate}")

if errors:
    print("FAIL development spec static audit (NOT runtime verification)", file=sys.stderr)
    for err in errors:
        print("ERROR:", err, file=sys.stderr)
    sys.exit(1)
print(f"PASS development spec static audit: {len(MODULES)} additional modules, "
      f"{len(cases)} P/N/F scenarios, {sum(n for _,n in TEST_OWNERS.values())} full test definitions.")
print("Designed only — NOT runtime / DB / Browser / security or Owner verified.")
