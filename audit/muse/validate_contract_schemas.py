#!/usr/bin/env python3
"""Validate proposed Muse JSON Schemas and actual adversarial examples.

This checks contract shape only. It does not exercise VM/kernel/network isolation.
Run with: python3 -m pip install jsonschema==4.23.0
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
from datetime import datetime

from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = ROOT / "contracts/muse"

checker = FormatChecker()

@checker.checks("signed-int64-decimal")
def signed_int64(v: object) -> bool:
    if not isinstance(v, str) or not re.fullmatch(r"-?(0|[1-9][0-9]*)", v):
        return False
    return -(2**63) <= int(v) <= 2**63 - 1

@checker.checks("nonnegative-int64-decimal")
def nonnegative_int64(v: object) -> bool:
    return signed_int64(v) and int(v) >= 0

def validator(filename: str) -> Draft202012Validator:
    raw = json.loads((HERE / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=checker)

proposal = validator("sandbox-exec-proposal.schema.json")
admission = validator("sandbox-exec-admission.schema.json")
external = validator("external-action-proposal.schema.json")

def must_pass(v: Draft202012Validator, sample: dict, why: str) -> None:
    errors = list(v.iter_errors(sample))
    assert not errors, f"{why}: {errors}"

def must_fail(v: Draft202012Validator, sample: dict, why: str) -> None:
    errors = list(v.iter_errors(sample))
    assert errors, f"should deny {why}"

p = {"tool":"sandbox.exec","argv":["python3","script.py"],"cwdRelative":"workspace/scripts","requestedLimits":{"wallMs":2000}}
must_pass(proposal,p,"minimal valid proposal")
for key in ("executionId","taskId","ownerUserId","tenantId","runId","goalRevision","leaseEpoch",
            "toolIntentId","cellId","workspaceId","workspaceRevision","runtimeProfile",
            "imageDigest","policyEpoch","networkMode","attestationRef"):
    forged=copy.deepcopy(p)
    forged[key]="admin"
    must_fail(proposal,forged,"model-supplied "+key)

for bad in ("/etc", "../secrets", "dir/../secret", "C:\\Windows", ""):
    forged=copy.deepcopy(p); forged["cwdRelative"]=bad
    must_fail(proposal,forged,"unsafe workspace path "+bad)
for value in ([""],[],["x"]*65):
    forged=copy.deepcopy(p);forged["argv"]=value
    must_fail(proposal,forged,"invalid argv")

a = {
    "executionId":"e-1","toolIntentId":"i-1","proposalDigest":"a"*64,
    "ownerUserId":"u-1","tenantId":"t-1","taskId":"t-2","runId":"r-1",
    "goalRevision":"9223372036854775807","leaseEpoch":"9223372036854775807",
    "workspaceId":"w-1","workspaceRevision":"0","cellId":"c-1",
    "vmIdentity":"untrusted-vm-1","imageDigest":"sha256:"+"b"*64,
    "attestationRef":"att-1","policyEpoch":"policy-1","policyDigest":"c"*64,
    "runtimeProfile":"secure_isolated","networkMode":"broker_rpc_only",
    "limits":{"wallMs":10000,"cpuMs":4000,"memoryBytes":100000000,"pids":10,
              "stdoutBytes":10000,"stderrBytes":10000,"diskBytes":10000000,"networkBytes":100000},
    "expiresAt":"2026-11-08T12:00:00Z","oneUseNonce":"nonce-1"
}
must_pass(admission,a,"server admission including int64 max")
for key in ("runtimeProfile","leaseEpoch","policyDigest","limits","vmIdentity"):
    forged=copy.deepcopy(a);forged.pop(key)
    must_fail(admission,forged,"missing trusted key "+key)
for key in ("leaseEpoch","goalRevision","workspaceRevision"):
    for bad in (9007199254740993,"9223372036854775808","-9223372036854775809","01"):
        forged=copy.deepcopy(a);forged[key]=bad
        must_fail(admission,forged,"invalid int64 for "+key)
for field,val in [("runtimeProfile","development_only"),("networkMode","unrestricted")]:
    forged=copy.deepcopy(a);forged[field]=val
    must_fail(admission,forged,field)
forged=copy.deepcopy(a);forged["agentInput"]="evil"
must_fail(admission,forged,"unknown admission field")

e={"operation":"http.read","args":{"url":"https://example.com"},"requestId":"req-1"}
must_pass(external,e,"broker request envelope")
forged={**e,"grant":"I approve"}
must_fail(external,forged,"forged broker grant")
forged={**e,"operation":"tcp.connect"}
must_fail(external,forged,"raw internet tunnel")
forged={**e,"args":"https://example.com"}
must_fail(external,forged,"untyped args")

print("PASS Muse schema/positive/negative tests: model vs admission, int64, broker envelope.")
print("NOTE: runtime, transport, exfiltration and VM isolation remain NOT TESTED.")
