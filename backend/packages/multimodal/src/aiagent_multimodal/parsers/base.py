"""Parser port and the unified, locator-preserving document projection (15-01 §4.2, §12.1).

Markdown is never the canonical extraction result: blocks, tables, cells,
images, warnings and unknown regions keep their typed shape and source locator.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts import MmiSourceLocator, utc_now
from ..errors import MmiError
from ..limits import MmiLimits

BLOCK_KINDS = frozenset(
    {
        "heading",
        "paragraph",
        "list_item",
        "caption",
        "note",
        "header",
        "footer",
        "footnote",
        "endnote",
        "slide_title",
        "shape_text",
    }
)


@dataclass(frozen=True, slots=True)
class ParseInput:
    source_artifact_id: str
    source_sha256: str
    format: str
    data: bytes
    parser_revision: str
    build_digest: str
    policy_revision: str
    limits: MmiLimits = field(default_factory=MmiLimits)
    options: Mapping[str, Any] = field(default_factory=dict)
    encoding: str | None = None
    delimiter: str | None = None
    deadline_monotonic: float | None = None

    def check_deadline(self) -> None:
        if self.deadline_monotonic is not None and time.monotonic() > self.deadline_monotonic:
            raise MmiError("PARSE_TIMEOUT", "parser deadline exceeded")


@dataclass(slots=True)
class TextBlock:
    block_id: str
    kind: str
    text: str
    locator: MmiSourceLocator
    style: str | None = None
    level: int | None = None
    ordinal1: int | None = None
    recognition_confidence_raw: float | None = None
    extraction_method: str = "native"
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in BLOCK_KINDS:
            raise ValueError(f"unsupported block kind: {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "blockId": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "locator": self.locator.to_dict(),
            "style": self.style,
            "level": self.level,
            "ordinal1": self.ordinal1,
            "recognitionConfidenceRaw": self.recognition_confidence_raw,
            "extractionMethod": self.extraction_method,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TextBlock:
        return cls(
            block_id=str(data["blockId"]),
            kind=str(data["kind"]),
            text=str(data["text"]),
            locator=MmiSourceLocator.from_dict(data["locator"]),
            style=data.get("style"),
            level=data.get("level"),
            ordinal1=data.get("ordinal1"),
            recognition_confidence_raw=data.get("recognitionConfidenceRaw"),
            extraction_method=str(data.get("extractionMethod", "native")),
            flags=tuple(data.get("flags") or ()),
        )


@dataclass(slots=True)
class TableCell:
    row: int
    column: int
    a1: str | None
    text: str | None
    raw_type: str
    locator: MmiSourceLocator
    formula: str | None = None
    cached_value: str | None = None
    cached_error: str | None = None
    merged: bool = False
    merged_anchor_a1: str | None = None
    hidden: bool = False
    value_state: str = "observed"
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "column": self.column,
            "a1": self.a1,
            "text": self.text,
            "rawType": self.raw_type,
            "locator": self.locator.to_dict(),
            "formula": self.formula,
            "cachedValue": self.cached_value,
            "cachedError": self.cached_error,
            "merged": self.merged,
            "mergedAnchorA1": self.merged_anchor_a1,
            "hidden": self.hidden,
            "valueState": self.value_state,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TableCell:
        return cls(
            row=int(data["row"]),
            column=int(data["column"]),
            a1=data.get("a1"),
            text=data.get("text"),
            raw_type=str(data["rawType"]),
            locator=MmiSourceLocator.from_dict(data["locator"]),
            formula=data.get("formula"),
            cached_value=data.get("cachedValue"),
            cached_error=data.get("cachedError"),
            merged=bool(data.get("merged", False)),
            merged_anchor_a1=data.get("mergedAnchorA1"),
            hidden=bool(data.get("hidden", False)),
            value_state=str(data.get("valueState", "observed")),
            flags=tuple(data.get("flags") or ()),
        )


@dataclass(slots=True)
class Table:
    table_id: str
    locator: MmiSourceLocator
    cells: list[TableCell]
    row_count: int
    column_count: int
    headers: list[str | None] = field(default_factory=list)
    warnings: tuple[str, ...] = ()
    bbox: tuple[float, float, float, float] | None = None

    def cell(self, *, row: int | None = None, column: int | None = None, a1: str | None = None) -> TableCell | None:
        for cell in self.cells:
            if a1 is not None and cell.a1 == a1:
                return cell
            if a1 is None and cell.row == row and cell.column == column:
                return cell
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tableId": self.table_id,
            "locator": self.locator.to_dict(),
            "cells": [c.to_dict() for c in self.cells],
            "rowCount": self.row_count,
            "columnCount": self.column_count,
            "headers": list(self.headers),
            "warnings": list(self.warnings),
            "bbox": list(self.bbox) if self.bbox is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Table:
        bbox = data.get("bbox")
        return cls(
            table_id=str(data["tableId"]),
            locator=MmiSourceLocator.from_dict(data["locator"]),
            cells=[TableCell.from_dict(c) for c in data.get("cells", [])],
            row_count=int(data["rowCount"]),
            column_count=int(data["columnCount"]),
            headers=list(data.get("headers") or []),
            warnings=tuple(data.get("warnings") or ()),
            bbox=tuple(bbox) if bbox else None,
        )


@dataclass(slots=True)
class ImageRef:
    image_id: str
    locator: MmiSourceLocator | None
    width_px: int
    height_px: int
    digest: str
    alt_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "imageId": self.image_id,
            "locator": self.locator.to_dict() if self.locator is not None else None,
            "widthPx": self.width_px,
            "heightPx": self.height_px,
            "digest": self.digest,
            "altText": self.alt_text,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ImageRef:
        return cls(
            image_id=str(data["imageId"]),
            locator=MmiSourceLocator.from_dict(data["locator"]) if data.get("locator") else None,
            width_px=int(data["widthPx"]),
            height_px=int(data["heightPx"]),
            digest=str(data["digest"]),
            alt_text=data.get("altText"),
        )


@dataclass(slots=True)
class Section:
    section_id: str
    kind: str
    ordinal1: int
    label: str
    locator: MmiSourceLocator | None
    blocks: list[TextBlock] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sectionId": self.section_id,
            "kind": self.kind,
            "ordinal1": self.ordinal1,
            "label": self.label,
            "locator": self.locator.to_dict() if self.locator is not None else None,
            "blocks": [b.to_dict() for b in self.blocks],
            "tables": [t.to_dict() for t in self.tables],
            "images": [i.to_dict() for i in self.images],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Section:
        return cls(
            section_id=str(data["sectionId"]),
            kind=str(data["kind"]),
            ordinal1=int(data["ordinal1"]),
            label=str(data["label"]),
            locator=MmiSourceLocator.from_dict(data["locator"]) if data.get("locator") else None,
            blocks=[TextBlock.from_dict(b) for b in data.get("blocks", [])],
            tables=[Table.from_dict(t) for t in data.get("tables", [])],
            images=[ImageRef.from_dict(i) for i in data.get("images", [])],
            warnings=tuple(data.get("warnings") or ()),
        )


@dataclass(slots=True)
class Document:
    source_artifact_id: str
    source_sha256: str
    format: str
    sections: list[Section]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    unknown_fields: list[str] = field(default_factory=list)
    parser_revision: str = ""
    build_digest: str = ""
    processed_at: str = field(default_factory=utc_now)
    partial: bool = False
    coverage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.parser_revision or not self.build_digest:
            raise ValueError("parserRevision and buildDigest are required")

    def blocks(self) -> list[TextBlock]:
        return [b for section in self.sections for b in section.blocks]

    def tables(self) -> list[Table]:
        return [t for section in self.sections for t in section.tables]

    def cells(self) -> list[TableCell]:
        return [c for t in self.tables() for c in t.cells]

    def section_by_label(self, label: str) -> Section | None:
        for section in self.sections:
            if section.label == label:
                return section
        return None

    def finalize(self) -> Document:
        blocks = self.blocks()
        tables = self.tables()
        cells = self.cells()
        self.coverage = {
            "sections": len(self.sections),
            "blocks": len(blocks),
            "tables": len(tables),
            "cells": len(cells),
            "characters": sum(len(b.text) for b in blocks),
            "emptyCells": sum(1 for c in cells if c.text is None or c.text == ""),
            "unknownCells": sum(1 for c in cells if c.value_state == "unknown"),
        }
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceArtifactId": self.source_artifact_id,
            "sourceSha256": self.source_sha256,
            "format": self.format,
            "sections": [s.to_dict() for s in self.sections],
            "metadata": dict(self.metadata),
            "warnings": list(self.warnings),
            "unknownFields": list(self.unknown_fields),
            "parserRevision": self.parser_revision,
            "buildDigest": self.build_digest,
            "processedAt": self.processed_at,
            "partial": self.partial,
            "coverage": dict(self.coverage),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Document:
        return cls(
            source_artifact_id=str(data["sourceArtifactId"]),
            source_sha256=str(data["sourceSha256"]),
            format=str(data["format"]),
            sections=[Section.from_dict(s) for s in data.get("sections", [])],
            metadata=dict(data.get("metadata") or {}),
            warnings=tuple(data.get("warnings") or ()),
            unknown_fields=list(data.get("unknownFields") or []),
            parser_revision=str(data["parserRevision"]),
            build_digest=str(data["buildDigest"]),
            processed_at=str(data["processedAt"]),
            partial=bool(data.get("partial", False)),
            coverage=dict(data.get("coverage") or {}),
        )


class Parser(Protocol):
    format: str

    def parse(self, inp: ParseInput) -> Document: ...


class BaseParser:
    format = ""
    revision = "1.0.0"

    def _document(self, inp: ParseInput, *, metadata: Mapping[str, Any] | None = None) -> Document:
        if self.format not in {"", "*"} and inp.format != self.format:
            raise MmiError("UNSUPPORTED_FORMAT", "parser/format mismatch")
        return Document(
            source_artifact_id=inp.source_artifact_id,
            source_sha256=inp.source_sha256,
            format=inp.format,
            sections=[],
            metadata=dict(metadata or {}),
            parser_revision=inp.parser_revision,
            build_digest=inp.build_digest,
        )

    def _locator(self, inp: ParseInput, **kwargs: Any) -> MmiSourceLocator:
        return MmiSourceLocator(
            artifact_id=inp.source_artifact_id,
            source_sha256=inp.source_sha256,
            **kwargs,
        )


def section_warnings(document: Document, *codes: str) -> None:
    for code in codes:
        if code not in document.warnings:
            document.warnings = (*document.warnings, code)


def merge_warnings(existing: Sequence[str], *codes: str) -> tuple[str, ...]:
    out = list(existing)
    for code in codes:
        if code not in out:
            out.append(code)
    return tuple(out)
