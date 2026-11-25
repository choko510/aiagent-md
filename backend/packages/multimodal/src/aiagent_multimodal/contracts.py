"""Versioned internal contracts for multimodal ingestion (15-01 §5).

These are **local, versioned metadata** types. Artifact, Evidence, Task, Run,
Scope and Event are canonical contracts owned elsewhere and are deliberately
not redefined here. A locator is bound to the immutable source artifact and its
SHA-256 and is projected to a canonical CitationLocator by an adapter.

Only `version="1.0"` envelopes are accepted and unknown keys are rejected so a
newer producer cannot silently change an older consumer's meaning.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final

from .errors import MmiError

SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {"user_upload", "authorized_fetch", "authorized_connector", "authorized_artifact"}
)
FORMATS: Final[frozenset[str]] = frozenset({"pdf", "png", "jpeg", "webp", "csv", "tsv", "xlsx", "docx", "pptx"})
VALUE_STATES: Final[frozenset[str]] = frozenset({"observed", "derived", "estimated", "unknown"})
VERIFICATION_STATES: Final[frozenset[str]] = frozenset({"anchored", "unverified", "contradicted"})
OUTCOME_STATUSES: Final[frozenset[str]] = frozenset({"complete", "partial", "unknown", "rejected", "cancelled"})
EXTRACTION_METHODS: Final[frozenset[str]] = frozenset({"native", "csv", "xlsx", "ooxml", "ocr", "vision"})
LOCATOR_KINDS: Final[frozenset[str]] = frozenset({"pdf", "image", "sheet", "csv", "office"})
LAYERS: Final[frozenset[str]] = frozenset({"native", "ocr", "vision"})


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _instant(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an ISO-8601 instant")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} requires a timezone")
    return value


def _text(value: object, name: str, *, max_len: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return value


def _sha(value: object, name: str = "sourceSha256") -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


def _optional_int(value: object, name: str, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("normalizedBbox must have four numbers")
    out = tuple(float(v) for v in value)
    if any(v < 0.0 or v > 1.0 for v in out) or out[2] <= out[0] or out[3] <= out[1]:
        raise ValueError("normalizedBbox must be [0,1]^4 with positive width/height")
    return out  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class MmiSource:
    kind: str
    acquired_at: str
    source_url_sanitized: str | None = None
    upstream_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"unsupported source kind: {self.kind}")
        _instant(self.acquired_at, "acquiredAt")
        if self.source_url_sanitized is not None and not self.source_url_sanitized.startswith(("https://", "http://")):
            raise ValueError("sourceUrlSanitized must be an http(s) URL")
        if self.upstream_artifact_id is not None:
            _text(self.upstream_artifact_id, "upstreamArtifactId", max_len=256)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "acquiredAt": self.acquired_at}
        if self.source_url_sanitized is not None:
            out["sourceUrlSanitized"] = self.source_url_sanitized
        if self.upstream_artifact_id is not None:
            out["upstreamArtifactId"] = self.upstream_artifact_id
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MmiSource:
        _only(data, {"kind", "acquiredAt", "sourceUrlSanitized", "upstreamArtifactId"}, "MmiSource")
        return cls(
            kind=str(data["kind"]),
            acquired_at=str(data["acquiredAt"]),
            source_url_sanitized=data.get("sourceUrlSanitized"),
            upstream_artifact_id=data.get("upstreamArtifactId"),
        )


@dataclass(frozen=True, slots=True)
class MmiSourceLocator:
    """A format-specific anchor that can be replayed against the source bytes."""

    kind: str
    artifact_id: str
    source_sha256: str
    page1: int | None = None
    page_size_pt: tuple[float, float] | None = None
    rotation_deg: int | None = None
    normalized_bbox: tuple[float, float, float, float] | None = None
    text_span: tuple[int, int] | None = None
    layer: str | None = None
    source_width_px: int | None = None
    source_height_px: int | None = None
    exif_orientation: int | None = None
    transform_id: str | None = None
    sheet_name: str | None = None
    sheet_ordinal1: int | None = None
    cell_a1: str | None = None
    end_cell_a1: str | None = None
    hidden_state: str | None = None
    merged_anchor_a1: str | None = None
    raw_type: str | None = None
    display_format: str | None = None
    record1: int | None = None
    field1: int | None = None
    byte_start: str | None = None
    byte_end: str | None = None
    header_text: str | None = None
    format: str | None = None
    part_name: str | None = None
    paragraph1: int | None = None
    table1: int | None = None
    row1: int | None = None
    column1: int | None = None
    slide1: int | None = None
    shape_id: str | None = None
    physical_page: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in LOCATOR_KINDS:
            raise ValueError(f"unsupported locator kind: {self.kind}")
        _text(self.artifact_id, "artifactId", max_len=256)
        _sha(self.source_sha256)
        if self.kind == "pdf":
            if _optional_int(self.page1, "page1", 1) is None:
                raise ValueError("pdf locator requires page1")
            if self.layer not in LAYERS:
                raise ValueError("pdf locator requires layer")
            if self.rotation_deg not in {0, 90, 180, 270}:
                raise ValueError("pdf locator requires rotationDeg in {0,90,180,270}")
            if self.normalized_bbox is not None:
                _bbox(self.normalized_bbox)
            if self.text_span is not None:
                start, end = self.text_span
                if start < 0 or end < start:
                    raise ValueError("pdf textSpan must be [start,end] with 0 <= start <= end")
        elif self.kind == "image":
            if _optional_int(self.source_width_px, "sourceWidthPx", 1) is None:
                raise ValueError("image locator requires sourceWidthPx")
            if _optional_int(self.source_height_px, "sourceHeightPx", 1) is None:
                raise ValueError("image locator requires sourceHeightPx")
            if self.layer not in LAYERS:
                raise ValueError("image locator requires layer")
            if self.normalized_bbox is not None:
                _bbox(self.normalized_bbox)
        elif self.kind == "sheet":
            _text(self.sheet_name, "sheetName", max_len=256)
            if _optional_int(self.sheet_ordinal1, "sheetOrdinal1", 1) is None:
                raise ValueError("sheet locator requires sheetOrdinal1")
            _a1(self.cell_a1, "cellA1")
            if self.end_cell_a1 is not None:
                _a1(self.end_cell_a1, "endCellA1")
            if self.hidden_state not in {"visible", "hidden", "veryHidden"}:
                raise ValueError("sheet locator requires hiddenState")
            _text(self.raw_type, "rawType", max_len=64)
        elif self.kind == "csv":
            if _optional_int(self.record1, "record1", 1) is None:
                raise ValueError("csv locator requires record1")
            if _optional_int(self.field1, "field1", 1) is None:
                raise ValueError("csv locator requires field1")
            byte_start = _optional_int(int(self.byte_start) if self.byte_start is not None else None, "byteStart")
            byte_end = _optional_int(int(self.byte_end) if self.byte_end is not None else None, "byteEnd")
            if byte_start is None or byte_end is None or byte_end < byte_start:
                raise ValueError("csv locator requires byteStart <= byteEnd")
        elif self.kind == "office":
            if self.format not in {"docx", "pptx"}:
                raise ValueError("office locator requires format docx|pptx")
            _part(self.part_name)
            if self.physical_page != "unknown":
                raise ValueError("office locator must keep physicalPage='unknown'")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "artifactId": self.artifact_id, "sourceSha256": self.source_sha256}
        for name, value in (
            ("page1", self.page1),
            ("pageSizePt", self.page_size_pt),
            ("rotationDeg", self.rotation_deg),
            ("normalizedBbox", self.normalized_bbox),
            ("textSpan", self.text_span),
            ("layer", self.layer),
            ("sourceWidthPx", self.source_width_px),
            ("sourceHeightPx", self.source_height_px),
            ("exifOrientation", self.exif_orientation),
            ("transformId", self.transform_id),
            ("sheetName", self.sheet_name),
            ("sheetOrdinal1", self.sheet_ordinal1),
            ("cellA1", self.cell_a1),
            ("endCellA1", self.end_cell_a1),
            ("hiddenState", self.hidden_state),
            ("mergedAnchorA1", self.merged_anchor_a1),
            ("rawType", self.raw_type),
            ("displayFormat", self.display_format),
            ("record1", self.record1),
            ("field1", self.field1),
            ("byteStart", self.byte_start),
            ("byteEnd", self.byte_end),
            ("headerText", self.header_text),
            ("format", self.format),
            ("partName", self.part_name),
            ("paragraph1", self.paragraph1),
            ("table1", self.table1),
            ("row1", self.row1),
            ("column1", self.column1),
            ("slide1", self.slide1),
            ("shapeId", self.shape_id),
            ("physicalPage", self.physical_page),
        ):
            if value is not None:
                out[name] = list(value) if isinstance(value, tuple) else value
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MmiSourceLocator:
        payload = dict(data)
        for key in ("pageSizePt", "normalizedBbox", "textSpan"):
            if isinstance(payload.get(key), list):
                payload[key] = tuple(payload[key])
        return cls(
            kind=str(payload.get("kind")),
            artifact_id=str(payload.get("artifactId")),
            source_sha256=str(payload.get("sourceSha256")),
            page1=payload.get("page1"),
            page_size_pt=payload.get("pageSizePt"),
            rotation_deg=payload.get("rotationDeg"),
            normalized_bbox=payload.get("normalizedBbox"),
            text_span=payload.get("textSpan"),
            layer=payload.get("layer"),
            source_width_px=payload.get("sourceWidthPx"),
            source_height_px=payload.get("sourceHeightPx"),
            exif_orientation=payload.get("exifOrientation"),
            transform_id=payload.get("transformId"),
            sheet_name=payload.get("sheetName"),
            sheet_ordinal1=payload.get("sheetOrdinal1"),
            cell_a1=payload.get("cellA1"),
            end_cell_a1=payload.get("endCellA1"),
            hidden_state=payload.get("hiddenState"),
            merged_anchor_a1=payload.get("mergedAnchorA1"),
            raw_type=payload.get("rawType"),
            display_format=payload.get("displayFormat"),
            record1=payload.get("record1"),
            field1=payload.get("field1"),
            byte_start=payload.get("byteStart"),
            byte_end=payload.get("byteEnd"),
            header_text=payload.get("headerText"),
            format=payload.get("format"),
            part_name=payload.get("partName"),
            paragraph1=payload.get("paragraph1"),
            table1=payload.get("table1"),
            row1=payload.get("row1"),
            column1=payload.get("column1"),
            slide1=payload.get("slide1"),
            shape_id=payload.get("shapeId"),
            physical_page=payload.get("physicalPage"),
        )


@dataclass(frozen=True, slots=True)
class MmiCandidate:
    field_key: str
    raw_text: str | None
    value_state: str
    normalized: Mapping[str, Any] | None
    conditions: tuple[str, ...] = ()
    missing_reason: str | None = None
    locator: MmiSourceLocator | None = None
    extraction_method: str = "native"
    recognition_confidence_raw: float | None = None
    verification: str = "unverified"
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.field_key, "fieldKey", max_len=128)
        if self.value_state not in VALUE_STATES:
            raise ValueError(f"unsupported value state: {self.value_state}")
        if self.verification not in VERIFICATION_STATES:
            raise ValueError(f"unsupported verification state: {self.verification}")
        if self.extraction_method not in EXTRACTION_METHODS:
            raise ValueError(f"unsupported extraction method: {self.extraction_method}")
        if self.raw_text is not None and not isinstance(self.raw_text, str):
            raise ValueError("rawText must be a string or null")
        if self.value_state == "unknown" and self.normalized is not None:
            raise ValueError("unknown values must not carry a normalized value")
        if self.recognition_confidence_raw is not None and not 0.0 <= float(self.recognition_confidence_raw) <= 100.0:
            raise ValueError("recognitionConfidenceRaw must be within 0..100")
        if self.locator is not None and not isinstance(self.locator, MmiSourceLocator):
            raise ValueError("locator must be an MmiSourceLocator")
        _sha(self.provenance.get("sourceSha256"), "provenance.sourceSha256")
        _text(self.provenance.get("parserBuildDigest"), "provenance.parserBuildDigest", max_len=256)
        _text(self.provenance.get("normalizerVersion"), "provenance.normalizerVersion", max_len=64)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fieldKey": self.field_key,
            "rawText": self.raw_text,
            "valueState": self.value_state,
            "normalized": dict(self.normalized) if self.normalized is not None else None,
            "conditions": list(self.conditions),
            "missingReason": self.missing_reason,
            "locator": self.locator.to_dict() if self.locator is not None else None,
            "extractionMethod": self.extraction_method,
            "recognitionConfidenceRaw": self.recognition_confidence_raw,
            "verification": self.verification,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MmiCandidate:
        _only(
            data,
            {
                "fieldKey",
                "rawText",
                "valueState",
                "normalized",
                "conditions",
                "missingReason",
                "locator",
                "extractionMethod",
                "recognitionConfidenceRaw",
                "verification",
                "provenance",
            },
            "MmiCandidate",
        )
        locator = data.get("locator")
        return cls(
            field_key=str(data["fieldKey"]),
            raw_text=data.get("rawText"),
            value_state=str(data["valueState"]),
            normalized=data.get("normalized"),
            conditions=tuple(data.get("conditions") or ()),
            missing_reason=data.get("missingReason"),
            locator=MmiSourceLocator.from_dict(locator) if isinstance(locator, Mapping) else None,
            extraction_method=str(data.get("extractionMethod", "native")),
            recognition_confidence_raw=data.get("recognitionConfidenceRaw"),
            verification=str(data.get("verification", "unverified")),
            provenance=data.get("provenance") or {},
        )


@dataclass(frozen=True, slots=True)
class MmiRequestV1:
    source_artifact_id: str
    source: MmiSource
    requested_fields: tuple[str, ...] = ()
    preferred_locale: str | None = None
    allow_local_ocr: bool = False
    allow_external_vision: bool = False
    idempotency_key: str = ""
    deadline_at: str = ""
    version: str = "1.0"

    def __post_init__(self) -> None:
        if self.version != "1.0":
            raise ValueError("only MmiRequestV1 version 1.0 is accepted")
        _text(self.source_artifact_id, "sourceArtifactId", max_len=256)
        if not isinstance(self.source, MmiSource):
            raise ValueError("source must be an MmiSource")
        if self.preferred_locale is not None and len(self.preferred_locale) > 35:
            raise ValueError("preferredLocale is too long")
        _instant(self.deadline_at, "deadlineAt")
        if not self.idempotency_key or len(self.idempotency_key) > 256:
            raise ValueError("idempotencyKey is required")
        for field_name in self.requested_fields:
            _text(field_name, "requestedFields[]", max_len=128)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sourceArtifactId": self.source_artifact_id,
            "source": self.source.to_dict(),
            "requestedFields": list(self.requested_fields),
            "preferredLocale": self.preferred_locale,
            "allowLocalOcr": self.allow_local_ocr,
            "allowExternalVision": self.allow_external_vision,
            "idempotencyKey": self.idempotency_key,
            "deadlineAt": self.deadline_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MmiRequestV1:
        _only(
            data,
            {
                "version",
                "sourceArtifactId",
                "source",
                "requestedFields",
                "preferredLocale",
                "allowLocalOcr",
                "allowExternalVision",
                "idempotencyKey",
                "deadlineAt",
            },
            "MmiRequestV1",
        )
        if not isinstance(data.get("source"), Mapping):
            raise ValueError("source must be an object")
        fields = data.get("requestedFields", [])
        if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
            raise ValueError("requestedFields must be a list of strings")
        return cls(
            source_artifact_id=str(data["sourceArtifactId"]),
            source=MmiSource.from_dict(data["source"]),
            requested_fields=tuple(fields),
            preferred_locale=data.get("preferredLocale"),
            allow_local_ocr=bool(data.get("allowLocalOcr", False)),
            allow_external_vision=bool(data.get("allowExternalVision", False)),
            idempotency_key=str(data["idempotencyKey"]),
            deadline_at=str(data["deadlineAt"]),
            version=str(data.get("version", "1.0")),
        )


@dataclass(frozen=True, slots=True)
class MmiOutcomeV1:
    source_artifact_id: str
    status: str
    candidates: tuple[MmiCandidate, ...] = ()
    missing_fields: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    derived_artifact_id: str | None = None
    parser_build_digest: str = ""
    processed_at: str = ""
    version: str = "1.0"

    def __post_init__(self) -> None:
        if self.version != "1.0":
            raise ValueError("only MmiOutcomeV1 version 1.0 is accepted")
        _text(self.source_artifact_id, "sourceArtifactId", max_len=256)
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(f"unsupported outcome status: {self.status}")
        _text(self.parser_build_digest, "parserBuildDigest", max_len=256)
        _instant(self.processed_at, "processedAt")
        for warning in self.warning_codes:
            _text(warning, "warningCodes[]", max_len=128)
        if any(c.field_key in self.missing_fields for c in self.candidates):
            raise ValueError("a field cannot be both missing and present")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sourceArtifactId": self.source_artifact_id,
            "status": self.status,
            "candidates": [c.to_dict() for c in self.candidates],
            "missingFields": list(self.missing_fields),
            "warningCodes": list(self.warning_codes),
            "derivedArtifactId": self.derived_artifact_id,
            "parserBuildDigest": self.parser_build_digest,
            "processedAt": self.processed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MmiOutcomeV1:
        _only(
            data,
            {
                "version",
                "sourceArtifactId",
                "status",
                "candidates",
                "missingFields",
                "warningCodes",
                "derivedArtifactId",
                "parserBuildDigest",
                "processedAt",
            },
            "MmiOutcomeV1",
        )
        return cls(
            source_artifact_id=str(data["sourceArtifactId"]),
            status=str(data["status"]),
            candidates=tuple(MmiCandidate.from_dict(c) for c in data.get("candidates", [])),
            missing_fields=tuple(data.get("missingFields", [])),
            warning_codes=tuple(data.get("warningCodes", [])),
            derived_artifact_id=data.get("derivedArtifactId"),
            parser_build_digest=str(data["parserBuildDigest"]),
            processed_at=str(data["processedAt"]),
            version=str(data.get("version", "1.0")),
        )


def parse_json_envelope(raw: bytes | str, *, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    body = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(body) > max_bytes:
        raise MmiError("MAX_INPUT", "request envelope too large")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    return data


def canonical_parse_key(
    *,
    tenant_id: str,
    owner_id: str,
    source_artifact_id: str,
    source_sha256: str,
    request: MmiRequestV1,
    policy_revision: str,
    parser_build_digest: str,
    visibility: str = "private",
) -> str:
    """Domain-separated idempotency key.

    Same bytes under a different tenant, owner, policy, parser build, requested
    fields, visibility or OCR/vision permission is a different result and must
    not reuse another scope's cached outcome.
    """
    parts = [
        "MMI/1",
        tenant_id,
        owner_id,
        source_artifact_id,
        source_sha256,
        policy_revision,
        parser_build_digest,
        visibility,
        ",".join(sorted(request.requested_fields)),
        request.preferred_locale or "",
        "ocr=1" if request.allow_local_ocr else "ocr=0",
        "vision=1" if request.allow_external_vision else "vision=0",
    ]
    h = sha256()
    for part in parts:
        payload = part.encode("utf-8")
        h.update(len(payload).to_bytes(8, "big"))
        h.update(payload)
    return h.hexdigest()


def _only(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{name} has unknown key(s): {sorted(unknown)}")


def _a1(value: object, name: str) -> None:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", value):
        raise ValueError(f"{name} must be an uppercase A1 reference")


def _part(value: object) -> None:
    if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.split("/") or "\\" in value:
        raise ValueError("partName must be a relative OOXML part name")
