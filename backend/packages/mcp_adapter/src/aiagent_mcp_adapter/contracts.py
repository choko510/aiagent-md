"""Wire-independent contracts. No MCP SDK type crosses this module boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import digest, model_alias, stable_label
from .errors import ErrorCode, McpError

RiskClass = Literal["read", "write_reversible", "external_irreversible"]
RISK_CLASSES: frozenset[str] = frozenset({"read", "write_reversible", "external_irreversible"})
ResultStatus = Literal["OK", "TOOL_ERROR", "UNSUPPORTED", "TRUNCATED"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ToolDescriptor(BaseModel):
    """Validated, normalized view of one server tool. Descriptions and annotations are data only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=8192)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    revision: str

    @model_validator(mode="after")
    def _schema_must_be_object(self) -> ToolDescriptor:
        if not isinstance(self.input_schema, dict) or self.input_schema.get("type") != "object":
            raise ValueError("input schema must be an object schema")
        return self


class ToolEntry(BaseModel):
    """One approved, publishable tool. Immutable once inside a snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    tenant_id: str
    owner_user_id: str
    source_name: str
    alias: str
    tool_id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    input_digest: str
    output_digest: str | None = None
    descriptor_revision: str
    risk_class: RiskClass
    approval_required: bool
    policy_revision: int = Field(ge=1)
    credential_version: int = Field(ge=1)
    generation: int = Field(ge=1)

    def schema_digest(self) -> str:
        return digest({"input": self.input_schema, "output": self.output_schema})

    def verify_digests(self) -> None:
        if digest(self.input_schema) != self.input_digest:
            raise McpError(ErrorCode.SCHEMA_INVALID, "input_schema_mutated")
        if self.output_schema is not None and digest(self.output_schema) != self.output_digest:
            raise McpError(ErrorCode.SCHEMA_INVALID, "output_schema_mutated")
        if digest({"input": self.input_schema, "output": self.output_schema}) != self.schema_digest():
            raise McpError(ErrorCode.SCHEMA_INVALID, "schema_digest_mismatch")


class ToolRef(BaseModel):
    """What a Gateway admission binds. Nothing here may be supplied by the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    owner_user_id: str
    provider_id: str
    tool_id: str
    alias: str
    generation: int = Field(ge=1)
    descriptor_revision: str
    schema_digest: str
    risk_class: RiskClass
    policy_revision: int = Field(ge=1)
    credential_version: int = Field(ge=1)


class CallBinding(BaseModel):
    """Gateway-issued authorization for exactly one MCP call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_ref: ToolRef
    run_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=128)
    permit: str = Field(min_length=1, max_length=4096)
    arguments_digest: str
    risk_class: RiskClass
    deadline_epoch_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _permit_not_placeholder(self) -> CallBinding:
        if self.permit.strip().lower() in {"", "none", "null", "test", "fake"}:
            raise ValueError("permit must be a real Sentinel authorization")
        return self


class CallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding: CallBinding
    arguments: dict[str, Any]

    def check_arguments_digest(self) -> None:
        if digest(self.arguments) != self.binding.arguments_digest:
            raise McpError(ErrorCode.ARGUMENTS_INVALID, "arguments_digest_mismatch")


class RegistrySnapshot(BaseModel):
    """Immutable, generation-fenced view of one provider for one tenant/owner scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    owner_user_id: str
    provider_id: str
    generation: int = Field(ge=1)
    policy_revision: int = Field(ge=1)
    credential_version: int = Field(ge=1)
    protocol_revision: str
    endpoint_fingerprint: str
    created_at: datetime
    expires_at: datetime
    entries: tuple[ToolEntry, ...]
    surface_digest: str

    @model_validator(mode="after")
    def _validate(self) -> RegistrySnapshot:
        if self.expires_at <= self.created_at:
            raise ValueError("snapshot expires before it is created")
        ids: set[str] = set()
        aliases: set[str] = set()
        names: set[str] = set()
        for entry in self.entries:
            if entry.generation != self.generation:
                raise ValueError("entry generation differs from snapshot generation")
            if (entry.tenant_id, entry.owner_user_id, entry.provider_id) != (
                self.tenant_id,
                self.owner_user_id,
                self.provider_id,
            ):
                raise ValueError("entry scope differs from snapshot scope")
            if entry.risk_class not in RISK_CLASSES:
                raise ValueError("risk class must be assigned, never defaulted")
            entry.verify_digests()
            if entry.tool_id in ids:
                raise ValueError("duplicate tool_id")
            if entry.alias in aliases:
                raise ValueError("duplicate model alias")
            if entry.source_name in names:
                raise ValueError("duplicate source tool name")
            ids.add(entry.tool_id)
            aliases.add(entry.alias)
            names.add(entry.source_name)
        expected = digest(
            [
                [
                    e.tool_id,
                    e.alias,
                    e.descriptor_revision,
                    e.input_digest,
                    e.output_digest,
                    e.risk_class,
                    bool(e.approval_required),
                    e.policy_revision,
                ]
                for e in self.entries
            ]
        )
        if expected != self.surface_digest:
            raise ValueError("surface digest mismatch")
        return self

    def entry_by_tool_id(self, tool_id: str) -> ToolEntry:
        for entry in self.entries:
            if entry.tool_id == tool_id:
                return entry
        raise McpError(ErrorCode.STALE_TOOL_REF, "tool_not_in_snapshot")

    def entry_by_alias(self, alias: str) -> ToolEntry:
        for entry in self.entries:
            if entry.alias == alias:
                return entry
        raise McpError(ErrorCode.STALE_TOOL_REF, "alias_not_in_snapshot")

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) >= self.expires_at

    def visible_aliases(self) -> tuple[str, ...]:
        return tuple(e.alias for e in self.entries)


class ContentBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["text", "image", "audio", "resource_link", "resource"]
    text: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    uri: str | None = None  # data only; never fetched by the adapter


class NormalizedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ResultStatus
    blocks: tuple[ContentBlock, ...] = ()
    text: str = ""
    structured: dict[str, Any] | None = None
    truncated: bool = False
    notes: tuple[str, ...] = ()


def build_entry(
    *,
    descriptor: ToolDescriptor,
    provider_id: str,
    tenant_id: str,
    owner_user_id: str,
    risk_class: RiskClass,
    approval_required: bool,
    policy_revision: int,
    credential_version: int,
    generation: int,
) -> ToolEntry:
    """Build an entry from an already-validated descriptor. Risk class never defaults."""
    if risk_class not in RISK_CLASSES:
        raise McpError(ErrorCode.POLICY_DENIED, "risk_class_missing")
    return ToolEntry(
        provider_id=provider_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_name=descriptor.source_name,
        alias=model_alias(provider_id, descriptor.source_name),
        tool_id=stable_label(provider_id, descriptor.source_name),
        description=descriptor.description,
        input_schema=descriptor.input_schema,
        output_schema=descriptor.output_schema,
        input_digest=digest(descriptor.input_schema),
        output_digest=digest(descriptor.output_schema) if descriptor.output_schema is not None else None,
        descriptor_revision=descriptor.revision,
        risk_class=risk_class,
        approval_required=approval_required,
        policy_revision=policy_revision,
        credential_version=credential_version,
        generation=generation,
    )


def compute_surface_digest(entries: tuple[ToolEntry, ...] | list[ToolEntry]) -> str:
    return digest(
        [
            [
                e.tool_id,
                e.alias,
                e.descriptor_revision,
                e.input_digest,
                e.output_digest,
                e.risk_class,
                bool(e.approval_required),
                e.policy_revision,
            ]
            for e in entries
        ]
    )


def descriptor_from_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
) -> ToolDescriptor:
    from .schema_validation import sanitize_description

    safe_description = sanitize_description(description)
    revision = digest(
        {
            "name": name,
            "description": safe_description,
            "input": input_schema,
            "output": output_schema,
        }
    )
    return ToolDescriptor(
        source_name=name,
        description=safe_description,
        input_schema=input_schema,
        output_schema=output_schema,
        revision=revision,
    )


def snapshot_from_entries(
    *,
    tenant_id: str,
    owner_user_id: str,
    provider_id: str,
    generation: int,
    policy_revision: int,
    credential_version: int,
    protocol_revision: str,
    endpoint_fingerprint: str,
    entries: list[ToolEntry],
    ttl_s: float,
    now: datetime | None = None,
) -> RegistrySnapshot:
    created = now or utc_now()
    return RegistrySnapshot(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        provider_id=provider_id,
        generation=generation,
        policy_revision=policy_revision,
        credential_version=credential_version,
        protocol_revision=protocol_revision,
        endpoint_fingerprint=endpoint_fingerprint,
        created_at=created,
        expires_at=created + timedelta(seconds=ttl_s),
        entries=tuple(entries),
        surface_digest=compute_surface_digest(entries),
    )
