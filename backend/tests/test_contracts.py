"""STEP 3-A contract tests: JSON Schema is the single source; Pydantic models are generated and agree with it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiagent_domain import INTERNAL, format_int64_wire, is_uuid7, parse_int64_wire, uuid7
from aiagent_domain.generated import MODELS, SCHEMA_FILES, RunClaimV1, RunJobPointerV1, TaskV1
from aiagent_domain.schema_validation import validator_for
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "domain" / "v1"

ACCEPT = ["0", "1", "-1", "9007199254740993", "9223372036854775807", "-9223372036854775808"]
REJECT = [
    9007199254740993,
    1,
    True,
    1.0,
    None,
    "9223372036854775808",
    "-9223372036854775809",
    "01",
    "+1",
    "-0",
    " 1",
    "1 ",
    "1e3",
    "１",
    "",
    "0x10",
    "1_000",
]


def _validator(fname: str):
    return validator_for(SCHEMAS, fname)


def test_generated_code_is_up_to_date() -> None:
    r = subprocess.run(
        [sys.executable, str(ROOT / "backend" / "tools" / "gen_contracts.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("s", ACCEPT)
def test_int64_roundtrip_accept(s: str) -> None:
    assert format_int64_wire(parse_int64_wire(s)) == s


@pytest.mark.parametrize("v", REJECT)
def test_int64_reject(v: object) -> None:
    with pytest.raises(ValueError):
        parse_int64_wire(v)


def _claim(**over: object) -> dict[str, object]:
    base = {
        "schemaVersion": "1.0",
        "tenantId": uuid7(),
        "ownerUserId": uuid7(),
        "taskId": uuid7(),
        "runId": uuid7(),
        "leaseEpoch": "9007199254740993",
        "goalRevision": "1",
        "workerRef": "w1",
        "restoreIncarnation": "1",
    }
    base.update(over)
    return base


def test_models_match_schema_structure() -> None:
    for title, fname in SCHEMA_FILES.items():
        s = json.loads((SCHEMAS / fname).read_text(encoding="utf-8"))
        fields = {f.alias or n for n, f in MODELS[title].model_fields.items()}
        assert fields == set(s["properties"]), title
        req = {f.alias or n for n, f in MODELS[title].model_fields.items() if f.is_required()}
        assert req == set(s.get("required", [])), title


@pytest.mark.parametrize(
    "case,ok",
    [
        ({}, True),
        ({"leaseEpoch": 9007199254740993}, False),  # JSON number
        ({"leaseEpoch": "0"}, False),  # positive int64
        ({"leaseEpoch": "9223372036854775808"}, False),  # out of int64
        ({"extra": 1}, False),  # unknown field
        ({"tenantId": "019a2fc0-4b21-4af0-9c22-34f78b6a1d04"}, False),  # UUIDv4 not v7
        ({"tenantId": "019A2FC0-4B21-7AF0-9C22-34F78B6A1D04"}, False),  # not canonical lowercase
        ({"schemaVersion": "2.0"}, False),  # unknown major
        ({"goalRevision": "01"}, False),
    ],
)
def test_pydantic_and_jsonschema_agree(case: dict[str, object], ok: bool) -> None:
    data = _claim(**case)
    schema_ok = _validator("run-claim.schema.json").is_valid(data)
    try:
        m = RunClaimV1.model_validate(data)
        model_ok = True
    except ValidationError:
        model_ok = False
    assert schema_ok == model_ok == ok
    if ok:
        dumped = m.model_dump(mode="json")
        assert dumped == data  # lossless wire round trip (2^53+1 stays exact)
        assert m.lease_epoch == 9007199254740993


def test_json_mode_rejects_numbers() -> None:
    with pytest.raises(ValidationError):
        RunClaimV1.model_validate_json(json.dumps(_claim(leaseEpoch=5)))


def test_internal_context_accepts_python_ints_only() -> None:
    m = RunClaimV1.model_validate(_claim(leaseEpoch=2**53 + 1), context=INTERNAL)
    assert m.model_dump(mode="json")["leaseEpoch"] == "9007199254740993"
    with pytest.raises(ValidationError):
        RunClaimV1.model_validate(_claim(leaseEpoch=True), context=INTERNAL)


def test_models_are_frozen_and_strict() -> None:
    m = RunClaimV1.model_validate(_claim())
    with pytest.raises(ValidationError):
        m.worker_ref = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):  # strict: no str->bool coercion
        TaskV1.model_validate({"cancelRequested": "true"})


def test_queue_pointer_rejects_injected_fields() -> None:
    bad = {
        "schemaVersion": "1.0",
        "kind": "admin",
        "tenantId": uuid7(),
        "taskId": uuid7(),
        "runId": uuid7(),
        "expectedGoalRevision": "1",
        "restoreIncarnation": "1",
        "scheduleOccurrenceId": None,
        "expiresAt": "2026-11-10T00:00:00.000Z",
    }
    with pytest.raises(ValidationError):
        RunJobPointerV1.model_validate(bad)
    assert not _validator("queue-job.schema.json").is_valid(bad)


def test_uuid7_monotonic_and_valid() -> None:
    ids = [uuid7() for _ in range(5000)]
    assert all(is_uuid7(i) for i in ids)
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_schema_version_compat_unknown_major_rejected_for_all_models() -> None:
    for title, model in MODELS.items():
        if "schemaVersion" in json.loads((SCHEMAS / SCHEMA_FILES[title]).read_text())["properties"]:
            with pytest.raises(ValidationError):
                model.model_validate({"schemaVersion": "2.0"})
