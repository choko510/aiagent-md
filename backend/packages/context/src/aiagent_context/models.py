"""AI-02 private immutable DTOs. Not shared Task/Run/Goal or DB contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
import re


class SafeStop(RuntimeError):
    """A controlled stop. Never treat this as a model/Tool permission."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def utc_instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise SafeStop("INVALID_TIMESTAMP") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SafeStop("INVALID_TIMESTAMP")
    return result


def stable_digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# Deliberately conservative. A broader DLP/redaction adapter is a production prerequisite.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+\S+"),
    re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-zA-Z0-9]{16,})\b"),
)


def forbid_secrets(value: str | bytes) -> None:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise SafeStop("SECRET_DETECTED")


class AnchorKind(StrEnum):
    CONSTRAINT = "CONSTRAINT"
    EXCLUSION = "EXCLUSION"
    DATE_TIME = "DATE_TIME"
    PARTY_SIZE = "PARTY_SIZE"
    MONEY = "MONEY"
    TIMEZONE = "TIMEZONE"
    GOAL_REF = "GOAL_REF"
    PENDING_EFFECT = "PENDING_EFFECT"
    APPROVAL = "APPROVAL"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class Anchor:
    """The canonical value is supplied by a trusted upstream service, never by a summarizer."""

    kind: AnchorKind
    source_ref: str
    value: str
    unit: str = ""
    currency: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AnchorKind) or not self.source_ref or not self.value.strip():
            raise SafeStop("INVALID_ANCHOR")
        forbid_secrets(self.value)
        forbid_secrets(self.source_ref)
        if self.kind == AnchorKind.PARTY_SIZE and (not self.value.isdecimal() or int(self.value) < 1):
            raise SafeStop("INVALID_PARTY_SIZE")
        if self.kind == AnchorKind.MONEY:
            if not re.fullmatch(r"[A-Z]{3}", self.currency):
                raise SafeStop("INVALID_MONEY")
            try:
                price = Decimal(self.value)
            except InvalidOperation as exc:
                raise SafeStop("INVALID_MONEY") from exc
            if not price.is_finite() or price < 0:
                raise SafeStop("INVALID_MONEY")
        if self.kind == AnchorKind.DATE_TIME:
            utc_instant(self.value)
        if self.kind == AnchorKind.TIMEZONE:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(self.value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise SafeStop("INVALID_TIMEZONE") from exc


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str
    owner_id: str
    task_id: str
    run_id: str

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.owner_id, self.task_id, self.run_id)):
            raise SafeStop("INVALID_SCOPE")


@dataclass(frozen=True, slots=True)
class ModelBudget:
    model_id: str
    model_revision: str
    tokenizer_id: str
    context_window: int
    output_reserve: int
    safety_margin: int
    system_tokens: int = 0
    tool_schema_tokens: int = 0
    framing_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.model_id or not self.model_revision or not self.tokenizer_id:
            raise SafeStop("INVALID_MODEL_PROFILE")
        if min(self.context_window, self.output_reserve, self.safety_margin) < 0:
            raise SafeStop("INVALID_MODEL_PROFILE")
        if any(x < 0 for x in (self.system_tokens, self.tool_schema_tokens, self.framing_tokens)):
            raise SafeStop("INVALID_MODEL_PROFILE")
        if self.context_window <= self.output_reserve + self.safety_margin:
            raise SafeStop("BLOCKED_CONTEXT_CAPACITY")


@dataclass(frozen=True, slots=True)
class MemoryProjection:
    ref: str
    text: str
    source_refs: tuple[str, ...]
    source_epoch: str
    trust_tier: str = "MEMORY_DATA"

    def __post_init__(self) -> None:
        if self.trust_tier != "MEMORY_DATA" or not self.ref or not self.source_refs:
            raise SafeStop("UNAUTHORIZED_MEMORY_PROJECTION")
        forbid_secrets(self.text)


@dataclass(frozen=True, slots=True)
class ContextRequest:
    scope: Scope
    expected_head: str
    lease_epoch: str
    goal_revision: str
    policy_version: str
    memory_source_epoch: str
    raw_event_cursor: str
    anchors: tuple[Anchor, ...]
    budget: ModelBudget
    memory: tuple[MemoryProjection, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.expected_head, self.lease_epoch, self.goal_revision,
                    self.policy_version, self.memory_source_epoch, self.raw_event_cursor)):
            raise SafeStop("INVALID_CONTEXT_REQUEST")
        if not self.anchors or not any(a.kind == AnchorKind.GOAL_REF for a in self.anchors):
            raise SafeStop("MISSING_GOAL_ANCHOR")
        if any(m.source_epoch != self.memory_source_epoch for m in self.memory):
            raise SafeStop("STALE_MEMORY")

    @property
    def anchor_digest(self) -> str:
        return stable_digest([(a.kind.value, a.source_ref, a.value, a.unit, a.currency) for a in self.anchors])


@dataclass(frozen=True, slots=True)
class ToolEvent:
    role: str
    call_id: str


@dataclass(frozen=True, slots=True)
class SourceInput:
    source_ref: str
    observed_at: str
    body: bytes
    media_type: str = "text/plain"
    locator_ref: str = ""

    def __post_init__(self) -> None:
        if not self.source_ref or not self.locator_ref or not self.body:
            raise SafeStop("INVALID_SOURCE")
        utc_instant(self.observed_at)
        if len(self.body) > 2_000_000:
            raise SafeStop("SOURCE_TOO_LARGE")
        # Reject at ingress, before an artifact could retain a secret.
        forbid_secrets(self.body)
        forbid_secrets(self.source_ref)
        forbid_secrets(self.locator_ref)


@dataclass(frozen=True, slots=True)
class RawArtifact:
    artifact_ref: str
    source_ref: str
    locator_ref: str
    observed_at: str
    sha256: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SummaryClaim:
    text: str
    quote: str
    artifact_ref: str
    source_hash: str
    observed_at: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class SummaryProposal:
    goal_revision: str
    anchor_digest: str
    covered_artifact_refs: tuple[str, ...]
    claims: tuple[SummaryClaim, ...]
    # Producer cannot confer permissions or approve effects.
    asserted_authority: str = "OBSERVATION"


@dataclass(frozen=True, slots=True)
class ContextPlan:
    scope: Scope
    goal_revision: str
    policy_version: str
    memory_source_epoch: str
    previous_head: str
    committed_head: str
    anchors: tuple[Anchor, ...]
    raw_artifacts: tuple[RawArtifact, ...]
    summary_artifact_ref: str
    summary_digest: str
    selected_claims: tuple[SummaryClaim, ...]
    memory: tuple[MemoryProjection, ...]
    estimated_input_tokens: int
    reserved_output_tokens: int
    context_digest: str
    model_payload: str
    model_payload_digest: str
    model_id: str
    model_revision: str
    tokenizer_id: str
    tool_pairs: tuple[str, ...]
    raw_event_cursor: str
    schema_major: int = field(default=1, init=False)
