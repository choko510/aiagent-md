"""CLI, local quota, deterministic scoring and minimal record keeping for the dev lab.

The command surface is deliberately tiny and fixed: ``list``, ``dry-run`` and ``probe``.
There is no flag for a URL, model, prompt, file, repetition or credential. ``probe``
requires two acknowledgements, is limited to one request per invocation, and records a
13-field metadata line -- never a prompt, a response body or a reasoning field.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import statistics
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NoReturn, cast, get_args

from . import ProbeStatus, ReasonCode
from .adapters import (
    NormalizedResponse,
    ResponseFormatError,
    decode_object,
    normalize_chat,
    normalize_system_one,
)
from .catalog import (
    ALL_PROVIDER_IDS,
    CATALOG_CHECKED_ON,
    LEGACY_ANONYMOUS_ALLOWED,
    PROVIDERS,
    ProviderSpec,
    catalog_specs,
    get_spec,
    require_spec,
    validate_spec,
)
from .fixtures import (
    FIXTURES,
    Fixture,
    allowed_fixtures,
    build_payload,
    require_fixture,
)
from .transport import (
    LocalInputError,
    TransportResult,
    encode_body,
    post_json,
)

PACKAGE_DIR: Final = Path(__file__).resolve().parent
RUNS_DIR: Final = PACKAGE_DIR / ".runs"
QUOTA_FILE: Final = RUNS_DIR / "quota.json"
LOCK_FILE: Final = RUNS_DIR / "lock"
LOG_FILE: Final = RUNS_DIR / "probes.jsonl"
REPORT_LOG_PATH: Final = "devtools/free_ai/.runs/probes.jsonl"
REPORT_MAX_BYTES: Final = 1_048_576
REPORT_MAX_LINES: Final = 5_000

SCHEMA_VERSION: Final = 1
LAB_VERSION: Final = "0.1"

#: Bounds that make an "impossible" persisted number a hard error instead of a bypass.
MAX_QUOTA_EPOCH: Final = 4102444800.0  # 2100-01-01T00:00:00Z
MAX_LOCAL_DAY_COUNT: Final = 10_000

EXIT_OK: Final = 0
EXIT_SKIPPED: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_RESPONSE_ISSUE: Final = 4
EXIT_UNSAFE: Final = 5

LocalDecision = Literal["RESERVED", "LIMIT"]

#: Exit code per terminal status. Local quota skips use an explicit code at their site.
STATUS_EXIT: Mapping[ProbeStatus, int] = {
    "PASS": EXIT_OK,
    "CONTENT_MISMATCH": EXIT_RESPONSE_ISSUE,
    "FORMAT_MISMATCH": EXIT_RESPONSE_ISSUE,
    "MODEL_DRIFT": EXIT_RESPONSE_ISSUE,
    "UNEXPECTED_BILLING": EXIT_RESPONSE_ISSUE,
    "UNEXPECTED_TOOL_OUTPUT": EXIT_RESPONSE_ISSUE,
    "ACCESS_RESTRICTED": EXIT_SKIPPED,
    "UNVERIFIED_TERMS": EXIT_SKIPPED,
    "LOCAL_INPUT_INVALID": EXIT_SKIPPED,
    "RATE_LIMITED": EXIT_UNAVAILABLE,
    "REMOTE_ERROR": EXIT_UNAVAILABLE,
    "NETWORK_ERROR": EXIT_UNAVAILABLE,
    "TIMEOUT": EXIT_UNAVAILABLE,
    "SAFETY_BLOCKED": EXIT_UNSAFE,
}


class LockBusyError(RuntimeError):
    """Another process holds the lab run lock; nothing may be sent."""


class QuotaStateError(RuntimeError):
    """The local quota state is unreadable, malformed, or the clock moved backwards."""


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """The only record shape this lab writes. Exactly thirteen fields, no free text."""

    schema_version: int
    provider_id: str
    model_requested: str
    model_observed: str | None
    fixture_id: str
    observed_at_utc: str
    http_status: int | None
    status: ProbeStatus
    reason_code: ReasonCode
    elapsed_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_reported_usd: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "model_requested": self.model_requested,
            "model_observed": self.model_observed,
            "fixture_id": self.fixture_id,
            "observed_at_utc": self.observed_at_utc,
            "http_status": self.http_status,
            "status": self.status,
            "reason_code": self.reason_code,
            "elapsed_ms": self.elapsed_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_reported_usd": self.cost_reported_usd,
        }


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """A record plus the process exit code it implies."""

    record: ProbeRecord
    exit_code: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Result of scoring a transport response against one fixed fixture."""

    status: ProbeStatus
    reason_code: ReasonCode
    exit_code: int
    normalized: NormalizedResponse | None


@dataclass(slots=True)
class _QuotaEntry:
    last_utc_epoch: float
    local_day: str
    day_count: int


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _make_record(
    *,
    spec: ProviderSpec,
    fixture: Fixture,
    observed_at_utc: str,
    status: ProbeStatus,
    reason_code: ReasonCode,
    model_observed: str | None = None,
    http_status: int | None = None,
    elapsed_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_reported_usd: float | None = None,
) -> ProbeRecord:
    return ProbeRecord(
        schema_version=SCHEMA_VERSION,
        provider_id=spec.provider_id,
        model_requested=spec.model_id,
        model_observed=model_observed,
        fixture_id=fixture.fixture_id,
        observed_at_utc=observed_at_utc,
        http_status=http_status,
        status=status,
        reason_code=reason_code,
        elapsed_ms=elapsed_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_reported_usd=cost_reported_usd,
    )


def _evaluate(spec: ProviderSpec, fixture: Fixture, result: TransportResult) -> Evaluation:
    """Score one response with pure Python; the model never grades itself."""
    if result.status != "OK":
        return Evaluation(
            status=cast("ProbeStatus", result.status),
            reason_code=result.reason_code,
            exit_code=STATUS_EXIT[cast("ProbeStatus", result.status)],
            normalized=None,
        )
    if result.body is None:
        return Evaluation(
            status="FORMAT_MISMATCH",
            reason_code="INVALID_SCHEMA",
            exit_code=EXIT_RESPONSE_ISSUE,
            normalized=None,
        )
    try:
        payload = decode_object(result.body)
        normalized = (
            normalize_chat(payload, spec)
            if spec.wire_kind == "chat_completions"
            else normalize_system_one(payload, spec)
        )
    except ResponseFormatError as exc:
        return Evaluation(
            status="FORMAT_MISMATCH",
            reason_code=exc.reason_code,
            exit_code=EXIT_RESPONSE_ISSUE,
            normalized=None,
        )
    if normalized.cost_reported_usd is not None and normalized.cost_reported_usd > 0:
        # The fixed reason set has no billing code; the status carries the meaning.
        return Evaluation(
            status="UNEXPECTED_BILLING",
            reason_code="REMOTE_FAILURE",
            exit_code=EXIT_RESPONSE_ISSUE,
            normalized=normalized,
        )
    if normalized.tool_output_present:
        return Evaluation(
            status="UNEXPECTED_TOOL_OUTPUT",
            reason_code="PROHIBITED_TOOL",
            exit_code=EXIT_RESPONSE_ISSUE,
            normalized=normalized,
        )
    if spec.wire_kind == "chat_completions" and spec.evidence_class != "user_observed_legacy":
        # Official services must report the requested model; a missing, unparsable or
        # different model id is never scored as PASS.
        if normalized.model_observed is None:
            return Evaluation(
                status="FORMAT_MISMATCH",
                reason_code="INVALID_SCHEMA",
                exit_code=EXIT_RESPONSE_ISSUE,
                normalized=normalized,
            )
        if normalized.model_observed != spec.model_id:
            return Evaluation(
                status="MODEL_DRIFT",
                reason_code="MODEL_CHANGED",
                exit_code=EXIT_RESPONSE_ISSUE,
                normalized=normalized,
            )
    if fixture.fixture_id == "refund_noul":
        threshold = fixture.noul_threshold
        matched = normalized.noul is not None and threshold is not None and normalized.noul >= threshold
    elif fixture.allowed_answers:
        # The model must reply with exactly one allowed identifier; prose is never mined
        # for an answer and partial matches never count.
        answer = normalized.text.strip() if normalized.text is not None else ""
        if answer not in fixture.allowed_answers:
            return Evaluation(
                status="FORMAT_MISMATCH",
                reason_code="INVALID_SCHEMA",
                exit_code=EXIT_RESPONSE_ISSUE,
                normalized=normalized,
            )
        matched = answer == fixture.expected_answer
    else:
        expected = fixture.expected_text
        matched = expected is not None and normalized.text is not None and normalized.text.strip() == expected
    if not matched:
        return Evaluation(
            status="CONTENT_MISMATCH",
            reason_code="OUTPUT_MISMATCH",
            exit_code=EXIT_RESPONSE_ISSUE,
            normalized=normalized,
        )
    reason: ReasonCode = "OK" if normalized.cost_reported_usd is not None else "COST_UNKNOWN"
    return Evaluation(status="PASS", reason_code=reason, exit_code=EXIT_OK, normalized=normalized)


RECORD_FIELDS: Final = (
    "schema_version",
    "provider_id",
    "model_requested",
    "model_observed",
    "fixture_id",
    "observed_at_utc",
    "http_status",
    "status",
    "reason_code",
    "elapsed_ms",
    "input_tokens",
    "output_tokens",
    "cost_reported_usd",
)

EVALUATED_STATUSES: Final = frozenset(
    {
        "PASS",
        "CONTENT_MISMATCH",
        "FORMAT_MISMATCH",
        "MODEL_DRIFT",
        "UNEXPECTED_BILLING",
        "UNEXPECTED_TOOL_OUTPUT",
    }
)
PROBE_STATUSES: Final = frozenset(get_args(ProbeStatus))
REASON_CODES: Final = frozenset(get_args(ReasonCode))

#: Providers whose chat responses must report the exact requested model.
OFFICIAL_CHAT_PROVIDERS: Final = frozenset({"kilo", "ovh"})
ACCESS_RESTRICTED_HTTP: Final = frozenset({401, 402, 403, 404, 410})

#: The only status/reason pairs this lab can produce. Anything else is a corrupt log.
STATUS_REASONS: Final[dict[str, frozenset[str]]] = {
    "PASS": frozenset({"OK", "COST_UNKNOWN"}),
    "CONTENT_MISMATCH": frozenset({"OUTPUT_MISMATCH"}),
    "FORMAT_MISMATCH": frozenset({"INVALID_JSON", "INVALID_SCHEMA"}),
    "MODEL_DRIFT": frozenset({"MODEL_CHANGED"}),
    "UNEXPECTED_BILLING": frozenset({"REMOTE_FAILURE"}),
    "UNEXPECTED_TOOL_OUTPUT": frozenset({"PROHIBITED_TOOL"}),
    "ACCESS_RESTRICTED": frozenset({"AUTH_REQUIRED"}),
    "RATE_LIMITED": frozenset({"RATE_LIMIT"}),
    "REMOTE_ERROR": frozenset({"REMOTE_FAILURE"}),
    "NETWORK_ERROR": frozenset({"UNKNOWN_OUTCOME"}),
    "TIMEOUT": frozenset({"UNKNOWN_OUTCOME"}),
    "UNVERIFIED_TERMS": frozenset({"LEGACY_DISABLED"}),
    "LOCAL_INPUT_INVALID": frozenset({"INVALID_SCHEMA", "BODY_TOO_LARGE"}),
    "SAFETY_BLOCKED": frozenset(
        {"UNEXPECTED_REDIRECT", "BODY_TOO_LARGE", "RATE_LIMIT", "LEGACY_DISABLED", "INVALID_SCHEMA"}
    ),
}

#: Statuses that can only be produced after a request was actually attempted.
COMMUNICATION_ATTEMPT_STATUSES: Final = frozenset({"NETWORK_ERROR", "TIMEOUT", "REMOTE_ERROR", "ACCESS_RESTRICTED"})


class ReportError(RuntimeError):
    """The local probe log is missing required structure or exceeds a fixed limit."""


@dataclass(slots=True)
class _ProviderTotals:
    attempts: int = 0
    evaluated: int = 0
    pass_count: int = 0
    content_mismatch: int = 0
    format_mismatch: int = 0
    model_drift: int = 0
    other_failures: int = 0
    communication_failures: int = 0
    local_preflight_skips: int = 0
    elapsed_ms: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    token_records: int = 0
    cost_unreported: int = 0


def _optional_count(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError("log record has an invalid count")
    return value


def _record_cost(record: Mapping[str, object]) -> float | None:
    cost = record.get("cost_reported_usd")
    if cost is None:
        return None
    return float(cast("int | float", cost))


def _validate_record_semantics(
    record: Mapping[str, object],
    *,
    spec: ProviderSpec,
    status: str,
    reason: str,
) -> None:
    """Reject records whose fields contradict the status this lab can actually produce."""

    def fail() -> NoReturn:
        raise ReportError("log record is semantically inconsistent")

    provider_id = cast("str", record["provider_id"])
    http_status = record.get("http_status")
    cost = _record_cost(record)
    model_observed = record.get("model_observed")
    official = provider_id in OFFICIAL_CHAT_PROVIDERS

    # Legacy refusal codes only exist for the two disabled legacy services. A record
    # claiming otherwise cannot have been produced by this lab.
    if reason == "LEGACY_DISABLED" and not spec.is_legacy:
        fail()
    if status == "UNVERIFIED_TERMS" and not spec.is_legacy:
        fail()

    if status == "PASS":
        if http_status != 200:
            fail()
        if cost is not None and cost > 0:
            fail()
        if (reason == "OK") != (cost is not None):
            fail()
        if official and model_observed != spec.model_id:
            fail()
    elif status == "CONTENT_MISMATCH":
        if http_status != 200:
            fail()
        if cost is not None and cost > 0:
            fail()
        if official and model_observed != spec.model_id:
            fail()
    elif status == "MODEL_DRIFT":
        if not official or http_status != 200:
            fail()
        if cost is not None and cost > 0:
            fail()
        if model_observed is None or model_observed == spec.model_id:
            fail()
    elif status == "UNEXPECTED_BILLING":
        if http_status != 200:
            fail()
        if cost is None or cost <= 0:
            fail()
    elif status == "UNEXPECTED_TOOL_OUTPUT":
        if http_status != 200:
            fail()
        if cost is not None and cost > 0:
            fail()
    elif status == "FORMAT_MISMATCH":
        # A decoding failure can be recorded without an HTTP status.
        if http_status not in (None, 200):
            fail()
        if cost is not None and cost > 0:
            fail()
    elif status == "ACCESS_RESTRICTED":
        if http_status not in ACCESS_RESTRICTED_HTTP:
            fail()
        if cost is not None or model_observed is not None:
            fail()
    elif status == "RATE_LIMITED":
        # 429 means the provider was reached; no HTTP status means a local quota skip.
        if http_status not in (None, 429):
            fail()
        if cost is not None or model_observed is not None:
            fail()
    elif status == "REMOTE_ERROR":
        if not isinstance(http_status, int) or isinstance(http_status, bool):
            fail()
        if http_status in {200, 429} or http_status in ACCESS_RESTRICTED_HTTP or 300 <= http_status < 400:
            fail()
        if cost is not None or model_observed is not None:
            fail()
    elif status in {"NETWORK_ERROR", "TIMEOUT", "UNVERIFIED_TERMS", "LOCAL_INPUT_INVALID"}:
        if http_status is not None or cost is not None or model_observed is not None:
            fail()
    elif status == "SAFETY_BLOCKED":
        if cost is not None or model_observed is not None:
            fail()
        if reason == "UNEXPECTED_REDIRECT":
            if not isinstance(http_status, int) or isinstance(http_status, bool) or not 300 <= http_status < 400:
                fail()
        elif reason == "BODY_TOO_LARGE":
            if http_status != 200:
                fail()
        elif http_status is not None:
            fail()


def _validated_record(line: str) -> dict[str, object]:
    try:
        parsed = json.loads(
            line,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise ReportError("log record is not valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != set(RECORD_FIELDS):
        raise ReportError("log record has an unexpected shape")
    if parsed.get("schema_version") != 1 or isinstance(parsed.get("schema_version"), bool):
        raise ReportError("log record has an unsupported schema version")
    provider_id = parsed.get("provider_id")
    if not isinstance(provider_id, str) or provider_id not in PROVIDERS:
        raise ReportError("log record names an unknown provider")
    for key in ("model_requested", "fixture_id", "observed_at_utc", "status", "reason_code"):
        if not isinstance(parsed.get(key), str):
            raise ReportError("log record has a non-string field")
    model_observed = parsed.get("model_observed")
    if model_observed is not None and not isinstance(model_observed, str):
        raise ReportError("log record has an invalid observed model")
    http_status = parsed.get("http_status")
    if http_status is not None and (
        isinstance(http_status, bool) or not isinstance(http_status, int) or not 100 <= http_status <= 599
    ):
        raise ReportError("log record has an invalid HTTP status")
    _optional_count(parsed, "elapsed_ms")
    _optional_count(parsed, "input_tokens")
    _optional_count(parsed, "output_tokens")
    cost = parsed.get("cost_reported_usd")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ReportError("log record has an invalid cost")
        try:
            amount = float(cost)
        except (OverflowError, ValueError):
            raise ReportError("log record has an invalid cost") from None
        if not math.isfinite(amount) or amount < 0:
            raise ReportError("log record has an invalid cost")
    status = parsed.get("status")
    reason = parsed.get("reason_code")
    if status not in PROBE_STATUSES:
        raise ReportError("log record has an unknown status")
    if reason not in REASON_CODES:
        raise ReportError("log record has an unknown reason code")
    spec = require_spec(provider_id)
    if parsed.get("model_requested") != spec.model_id:
        raise ReportError("log record does not match the frozen catalog model")
    if parsed.get("fixture_id") not in allowed_fixtures(spec):
        raise ReportError("log record pairs the provider with a disallowed fixture")
    if reason not in STATUS_REASONS[cast("str", status)]:
        raise ReportError("log record pairs an impossible status and reason code")
    _validate_record_semantics(
        parsed,
        spec=spec,
        status=cast("str", status),
        reason=cast("str", reason),
    )
    return parsed


def _read_report_records() -> list[dict[str, object]]:
    """Read only the fixed local log, with a hard byte and line limit."""
    try:
        size = LOG_FILE.stat().st_size
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ReportError("probe log is unreadable") from exc
    if size > REPORT_MAX_BYTES:
        # Refuse before allocating a buffer for an oversized log.
        raise ReportError("probe log is too large")
    try:
        raw = LOG_FILE.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ReportError("probe log is unreadable") from exc
    if len(raw) > REPORT_MAX_BYTES:
        raise ReportError("probe log is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportError("probe log is not UTF-8") from exc
    lines = text.splitlines()
    if len(lines) > REPORT_MAX_LINES:
        raise ReportError("probe log has too many lines")
    records: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            raise ReportError("probe log has a blank line")
        records.append(_validated_record(line))
    return records


def _provider_report(totals: _ProviderTotals) -> dict[str, object]:
    pass_rate: float | None = None if totals.evaluated == 0 else round(totals.pass_count / totals.evaluated, 4)
    median_elapsed: float | None = (
        None if not totals.elapsed_ms else round(float(statistics.median(totals.elapsed_ms)), 3)
    )
    return {
        "attempts": totals.attempts,
        "evaluated": totals.evaluated,
        "pass": totals.pass_count,
        "content_mismatch": totals.content_mismatch,
        "format_mismatch": totals.format_mismatch,
        "model_drift": totals.model_drift,
        "other_failures": totals.other_failures,
        "pass_rate": pass_rate,
        "communication_failures": totals.communication_failures,
        "local_preflight_skips": totals.local_preflight_skips,
        "median_elapsed_ms": median_elapsed,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "token_records": totals.token_records,
        "cost_unreported": totals.cost_unreported,
    }


def _build_report() -> dict[str, object]:
    records = _read_report_records()
    totals: dict[str, _ProviderTotals] = {provider_id: _ProviderTotals() for provider_id in ALL_PROVIDER_IDS}
    for record in records:
        provider_id = cast("str", record["provider_id"])
        entry = totals[provider_id]
        entry.attempts += 1
        status = record["status"]
        if status in EVALUATED_STATUSES:
            entry.evaluated += 1
            if status == "PASS":
                entry.pass_count += 1
            elif status == "CONTENT_MISMATCH":
                entry.content_mismatch += 1
            elif status == "FORMAT_MISMATCH":
                entry.format_mismatch += 1
            elif status == "MODEL_DRIFT":
                entry.model_drift += 1
            else:
                entry.other_failures += 1
        elif status in COMMUNICATION_ATTEMPT_STATUSES or record.get("http_status") is not None:
            entry.communication_failures += 1
        else:
            entry.local_preflight_skips += 1
        elapsed = _optional_count(record, "elapsed_ms")
        if elapsed is not None:
            entry.elapsed_ms.append(elapsed)
        input_tokens = _optional_count(record, "input_tokens")
        output_tokens = _optional_count(record, "output_tokens")
        if input_tokens is not None or output_tokens is not None:
            entry.token_records += 1
        entry.input_tokens += 0 if input_tokens is None else input_tokens
        entry.output_tokens += 0 if output_tokens is None else output_tokens
        if record.get("cost_reported_usd") is None:
            entry.cost_unreported += 1
    return {
        "schema_version": 1,
        "log_path": REPORT_LOG_PATH,
        "records": len(records),
        "providers": {provider_id: _provider_report(entry) for provider_id, entry in totals.items()},
    }


def _invalid_report_output() -> str:
    return '{"schema_version":1,"status":"INVALID_LOG","reason_code":"INVALID_SCHEMA"}'


def _run_report() -> int:
    try:
        report = _build_report()
    except ReportError:
        print(_invalid_report_output())
        return EXIT_UNSAFE
    except Exception:
        print(_invalid_report_output())
        return EXIT_UNSAFE
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return EXIT_OK


def _flock_exclusive(fd: int) -> None:
    """Take a non-blocking exclusive POSIX lock; the lab runs in WSL/Linux for probes."""
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise LockBusyError("run lock is held by another process") from exc


@contextlib.contextmanager
def _exclusive_run() -> Iterator[None]:
    """Create ``.runs`` mode 0700 and hold the run lock for the whole send."""
    RUNS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _flock_exclusive(fd)
        yield
    finally:
        os.close(fd)


def _reject_json_constant(token: str) -> object:
    raise ValueError("non-finite number in quota state")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in quota state")
        result[key] = value
    return result


def _finite_epoch(value: object) -> float:
    """Return a finite, plausible epoch or fail closed (no send, exit 5)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuotaStateError("quota entry has an invalid timestamp")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        # A 309-digit timestamp is valid JSON but not a usable clock reading.
        raise QuotaStateError("quota entry has an invalid timestamp") from None
    if not math.isfinite(number) or number < 0 or number > MAX_QUOTA_EPOCH:
        raise QuotaStateError("quota entry has an invalid timestamp")
    return number


def _read_quota() -> dict[str, _QuotaEntry]:
    try:
        raw = QUOTA_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise QuotaStateError("quota state is unreadable") from exc
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise QuotaStateError("quota state is not valid JSON") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != 1:
        raise QuotaStateError("quota state has an unsupported shape")
    providers = parsed.get("providers")
    if not isinstance(providers, dict):
        raise QuotaStateError("quota state has no provider table")
    state: dict[str, _QuotaEntry] = {}
    for provider_id, entry in providers.items():
        if not isinstance(provider_id, str) or get_spec(provider_id) is None:
            raise QuotaStateError("quota state names an unknown provider")
        if not isinstance(entry, dict):
            raise QuotaStateError("quota entry is not an object")
        if set(entry) != {"last_utc_epoch", "local_day", "day_count"}:
            raise QuotaStateError("quota entry has unexpected fields")
        last_utc_epoch = _finite_epoch(entry.get("last_utc_epoch"))
        local_day = entry.get("local_day")
        day_count = entry.get("day_count")
        if not isinstance(local_day, str) or len(local_day) != 10:
            raise QuotaStateError("quota entry has an invalid day")
        if isinstance(day_count, bool) or not isinstance(day_count, int):
            raise QuotaStateError("quota entry has an invalid count")
        if day_count < 0 or day_count > MAX_LOCAL_DAY_COUNT:
            raise QuotaStateError("quota entry has an invalid count")
        state[provider_id] = _QuotaEntry(
            last_utc_epoch=last_utc_epoch,
            local_day=local_day,
            day_count=day_count,
        )
    return state


def _write_quota(state: Mapping[str, _QuotaEntry]) -> None:
    payload = {
        "version": 1,
        "providers": {
            provider_id: {
                "last_utc_epoch": entry.last_utc_epoch,
                "local_day": entry.local_day,
                "day_count": entry.day_count,
            }
            for provider_id, entry in sorted(state.items())
        },
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    temp_path = QUOTA_FILE.with_name(f"{QUOTA_FILE.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, QUOTA_FILE)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise QuotaStateError("quota state cannot be written") from exc


def _reserve_slot(spec: ProviderSpec) -> LocalDecision:
    """Reserve one send before the socket is opened.

    A reserved send is consumed even if the request later fails, so traffic that may
    have reached the provider is never attempted twice. The caller holds the run lock.
    """
    now_epoch = time.time()
    today = _utc_today()
    state = _read_quota()
    entry = state.get(spec.provider_id)
    if entry is not None:
        if now_epoch < entry.last_utc_epoch:
            raise QuotaStateError("system clock moved backwards")
        day_count = entry.day_count if entry.local_day == today else 0
        if day_count >= spec.max_calls_per_local_day:
            return "LIMIT"
        if now_epoch - entry.last_utc_epoch < spec.min_interval_seconds:
            return "LIMIT"
    else:
        day_count = 0
    state[spec.provider_id] = _QuotaEntry(
        last_utc_epoch=now_epoch,
        local_day=today,
        day_count=day_count + 1,
    )
    _write_quota(state)
    return "RESERVED"


def _append_probe(record: ProbeRecord) -> None:
    line = json.dumps(record.as_dict(), ensure_ascii=False, separators=(",", ":"))
    try:
        # A rejected preflight may run before the run lock ever created ``.runs``.
        # Creating the directory here keeps local refusals observable without sending.
        RUNS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(LOG_FILE), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        raise QuotaStateError("probe log cannot be appended") from exc


def _probe(args: argparse.Namespace) -> ProbeOutcome:
    observed_at = _utc_now_iso()
    spec: ProviderSpec = require_spec(cast("str", args.provider))
    fixture: Fixture = require_fixture(cast("str", args.fixture))

    def outcome(
        status: ProbeStatus,
        reason_code: ReasonCode,
        exit_code: int,
        *,
        model_observed: str | None = None,
        http_status: int | None = None,
        elapsed_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_reported_usd: float | None = None,
    ) -> ProbeOutcome:
        record = _make_record(
            spec=spec,
            fixture=fixture,
            observed_at_utc=observed_at,
            status=status,
            reason_code=reason_code,
            model_observed=model_observed,
            http_status=http_status,
            elapsed_ms=elapsed_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_reported_usd=cost_reported_usd,
        )
        return ProbeOutcome(record=record, exit_code=exit_code)

    def logged(outcome_value: ProbeOutcome) -> ProbeOutcome:
        """Persist a preflight decision; a write failure stays fail-closed."""
        try:
            _append_probe(outcome_value.record)
        except QuotaStateError:
            return outcome("SAFETY_BLOCKED", "INVALID_SCHEMA", EXIT_UNSAFE)
        return outcome_value

    if fixture.fixture_id not in allowed_fixtures(spec):
        return logged(outcome("LOCAL_INPUT_INVALID", "INVALID_SCHEMA", EXIT_SKIPPED))
    if spec.is_legacy and (
        not cast("bool", args.allow_legacy) or not LEGACY_ANONYMOUS_ALLOWED.get(spec.provider_id, False)
    ):
        # The anonymous terms of both legacy services could not be confirmed on
        # CATALOG_CHECKED_ON, so nothing is sent even with --allow-legacy.
        return logged(outcome("UNVERIFIED_TERMS", "LEGACY_DISABLED", EXIT_SKIPPED))
    try:
        validate_spec(spec)
        payload = build_payload(spec, fixture.fixture_id)
        encode_body(payload)
    except LocalInputError as exc:
        return logged(outcome("LOCAL_INPUT_INVALID", exc.reason_code, EXIT_UNSAFE))

    try:
        with _exclusive_run():
            if _reserve_slot(spec) != "RESERVED":
                return logged(outcome("RATE_LIMITED", "RATE_LIMIT", EXIT_SKIPPED))
            result = asyncio.run(post_json(spec, fixture.fixture_id))
    except LockBusyError:
        return logged(outcome("SAFETY_BLOCKED", "RATE_LIMIT", EXIT_UNSAFE))
    except QuotaStateError:
        return logged(outcome("SAFETY_BLOCKED", "RATE_LIMIT", EXIT_UNSAFE))

    evaluation = _evaluate(spec, fixture, result)
    normalized = evaluation.normalized
    outcome_value = outcome(
        evaluation.status,
        evaluation.reason_code,
        evaluation.exit_code,
        model_observed=None if normalized is None else normalized.model_observed,
        http_status=result.http_status,
        elapsed_ms=result.elapsed_ms,
        input_tokens=None if normalized is None else normalized.input_tokens,
        output_tokens=None if normalized is None else normalized.output_tokens,
        cost_reported_usd=None if normalized is None else normalized.cost_reported_usd,
    )
    try:
        _append_probe(outcome_value.record)
    except QuotaStateError:
        return outcome("SAFETY_BLOCKED", "INVALID_SCHEMA", EXIT_UNSAFE)
    return outcome_value


def _print_list() -> None:
    print(
        "provider_id\tmodel_id\tprotocol\tendpoint_host\teligibility\tevidence_class\tavailable_for_live\treason_code"
    )
    for spec in catalog_specs():
        available = "no" if spec.is_legacy else "unverified"
        reason = "LEGACY_DISABLED" if spec.is_legacy else "NOT_PROBED"
        print(
            f"{spec.provider_id}\t{spec.model_id}\t{spec.wire_kind}\t{spec.endpoint_host}\t"
            f"{spec.eligibility}\t{spec.evidence_class}\t{available}\t{reason}"
        )
    print(f"# catalog checked on {CATALOG_CHECKED_ON}; no live request was made and no price is guaranteed")


def _print_dry_run() -> None:
    print("# synthetic fixtures only; network_sent=0; byte counts are not a budget estimate")
    for spec in catalog_specs():
        for fixture_id in allowed_fixtures(spec):
            payload = build_payload(spec, fixture_id)
            size = len(encode_body(payload))
            print(f"{spec.provider_id}\t{fixture_id}\tbytes={size}\tdestination={spec.endpoint}\tnetwork_sent=0")


def _fallback_outcome(args: argparse.Namespace, reason: ReasonCode) -> ProbeOutcome:
    provider_id = getattr(args, "provider", None)
    fixture_id = getattr(args, "fixture", None)
    spec = get_spec(provider_id) if isinstance(provider_id, str) else None
    record = ProbeRecord(
        schema_version=SCHEMA_VERSION,
        provider_id=spec.provider_id if spec is not None else "",
        model_requested=spec.model_id if spec is not None else "",
        model_observed=None,
        fixture_id=fixture_id if isinstance(fixture_id, str) else "",
        observed_at_utc=_utc_now_iso(),
        http_status=None,
        status="SAFETY_BLOCKED",
        reason_code=reason,
        elapsed_ms=None,
        input_tokens=None,
        output_tokens=None,
        cost_reported_usd=None,
    )
    return ProbeOutcome(record=record, exit_code=EXIT_UNSAFE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m devtools.free_ai",
        description="Development-only lab for fixed synthetic probes against free external AI services.",
    )
    parser.add_argument("--version", action="version", version=f"dev-free-ai-lab {LAB_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=False)
    subparsers.add_parser("list", help="show the frozen catalog without any network access")
    subparsers.add_parser("dry-run", help="show fixture ids, payload sizes and destinations; send nothing")
    subparsers.add_parser("report", help="aggregate the local probe log; no network access")
    probe = subparsers.add_parser("probe", help="send one fixed synthetic fixture to one provider")
    probe.add_argument("--provider", required=True, choices=list(ALL_PROVIDER_IDS))
    probe.add_argument("--fixture", required=True, choices=list(FIXTURES))
    probe.add_argument("--ack-synthetic-only", action="store_true", required=True)
    probe.add_argument("--ack-external-request", action="store_true", required=True)
    probe.add_argument("--allow-legacy", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Exit codes: 0 pass, 2 skip, 3 unavailable, 4 bad response, 5 unsafe."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = cast("str | None", getattr(args, "command", None))
    if command is None:
        parser.print_help()
        return EXIT_SKIPPED
    if command == "list":
        _print_list()
        return EXIT_OK
    if command == "dry-run":
        _print_dry_run()
        return EXIT_OK
    if command == "report":
        return _run_report()
    try:
        probe_outcome = _probe(args)
    except Exception:
        # Never print an exception message, a URL or a body: only a safe constant.
        probe_outcome = _fallback_outcome(args, "INVALID_SCHEMA")
    print(json.dumps(probe_outcome.record.as_dict(), ensure_ascii=False, separators=(",", ":")))
    return probe_outcome.exit_code


__all__ = [
    "EXIT_OK",
    "EXIT_RESPONSE_ISSUE",
    "EXIT_SKIPPED",
    "EXIT_UNAVAILABLE",
    "EXIT_UNSAFE",
    "ProbeOutcome",
    "ProbeRecord",
    "main",
]
