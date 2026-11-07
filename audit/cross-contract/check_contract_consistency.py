#!/usr/bin/env python3
"""Static cross-contract assertions. Does not prove runtime safety, DB migration or VM behavior."""
from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")

def required(name: str, tokens: tuple[str, ...]) -> None:
    value = read(name)
    for token in tokens:
        assert token in value, f"{name}: missing marker {token!r}"

adm = json.loads(read("contracts/muse/sandbox-exec-admission.schema.json"))
goal = adm["properties"]["goalRevision"]
assert goal["format"] == "positive-int64-decimal"
assert re.fullmatch(goal["pattern"], "1")
assert not re.fullmatch(goal["pattern"], "0")
assert not re.fullmatch(goal["pattern"], "-1")
ext = json.loads(read("contracts/muse/external-action-proposal.schema.json"))
assert ext["properties"]["args"]["minProperties"] == 1
assert ext["allOf"][0]["if"]["properties"]["operation"]["const"] == "http.read"
required("docs/02-runtime/sandbox-execution.md", (
    "positive-int64-decimal", "Registry", "Broker", "NOT RUN"))
required("docs/03-tools/tool-approval-and-policy.md", (
    "approval_binding_snapshots", "REAPPROVAL_REQUIRED", "restoreIncarnation"))
required("docs/10-infrastructure/database-schema-and-migrations.md", (
    "CREATE TABLE agent.approval_binding_snapshots",
    "UNIQUE (tenant_id,tool_intent_id,id)", "restore_incarnation", "NOT EXECUTED"))
required("docs/10-infrastructure/backup-upgrade-and-recovery.md", (
    "RESTORED_QUARANTINED", "REC-T23", "REC-T27"))
required("docs/09-security/sentinel-authority.md", (
    "restoreIncarnation", "Enforced Egress Transport"))
q = read("docs/10-infrastructure/job-queue-and-outbox.md")
assert "acknowledge(record:OutboxPointer, fenceEpoch:string)" in q
assert "keep same workspace=tenant" not in q
assert "2^53+1" in q
required("docs/04-web-browser/login-challenges-human-handoff.md", (
    "発行→一回限りredeem 60秒", "lease無操作10分", "絶対期限30分", "DISABLED"))
required("docs/11-qa/fault-injection-and-concurrency.md", (
    "FIC-AUD-T01", "FIC-AUD-T08", "NOT RUN"))
required("docs/03-tools/sandboxed-tool-pipelines.md", (
    "DAG Transform Worker", "ADR-017"))
print("PASS: static cross-contract regression markers; NOT runtime/DB/network verified.")
