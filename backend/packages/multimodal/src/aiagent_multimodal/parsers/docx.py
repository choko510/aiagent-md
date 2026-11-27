"""DOCX extraction from the allowlisted WordprocessingML parts (15-01 §11.1).

Paragraph, table, row and cell ordinals are the citation anchors. A physical
page number is not invented: `physicalPage` stays `unknown` because only a
layout engine could resolve it. Headers, footers, footnotes, comments and
embedded image references are reported rather than silently dropped.
"""

from __future__ import annotations

import re
from typing import Any

from ..archive_gate import inspect_archive, read_entry
from ..errors import MmiError
from ..limits import MmiLimits
from ..xml_gate import XmlNode, parse_xml
from .base import BaseParser, Document, ParseInput, Section, Table, TableCell, TextBlock, merge_warnings
from .ooxml import (
    assert_no_external_relationships,
    attr,
    drawing_alt_texts,
    int_attr,
    local_name,
    relationships,
    word_paragraph_text,
)

_HEADING = re.compile(r"^(?:heading|見出し)\s*([1-9])$", re.IGNORECASE)
_ACTIVE_PARTS = ("word/vbaProject.bin", "word/embeddings/", "word/activeX/", "word/oleObject")


class DocxParser(BaseParser):
    format = "docx"
    revision = "1.0.0"

    def parse(self, inp: ParseInput) -> Document:
        limits = inp.limits
        report = inspect_archive(inp.data, limits)
        names = set(report.names)
        for name in names:
            if name.startswith(_ACTIVE_PARTS):
                raise MmiError("UNSUPPORTED_ACTIVE_CONTENT", "DOCX contains an executable/embedded object")
        assert_no_external_relationships(inp.data, names, limits)
        if "word/document.xml" not in names:
            raise MmiError("BAD_ARCHIVE", "DOCX document part is missing")
        root = parse_xml(read_entry(inp.data, "word/document.xml", limits), limits, root_expected="document")
        body = root.child("body")
        if body is None:
            raise MmiError("BAD_XML", "DOCX body is missing")
        doc = self._document(inp, metadata=self._properties(inp, names, limits))
        section = Section(
            section_id="docx:body",
            kind="document",
            ordinal1=1,
            label="Document",
            locator=self._locator(
                inp, kind="office", format="docx", part_name="word/document.xml", physical_page="unknown"
            ),
        )
        paragraph_no = 0
        table_no = 0
        images: list[dict[str, Any]] = []
        rels = relationships(inp.data, "word/document.xml", names, limits)
        for child in body.children:
            local = local_name(child)
            if local == "p":
                paragraph_no += 1
                if paragraph_no > limits.max_docx_paragraphs:
                    raise MmiError("EXPANSION_LIMIT", "DOCX paragraph count exceeds limit")
                inp.check_deadline()
                text = word_paragraph_text(child)
                style = _paragraph_style(child)
                level = _heading_level(child, style)
                alts = drawing_alt_texts(child)
                if alts:
                    doc.warnings = merge_warnings(doc.warnings, "EMBEDDED_IMAGE_ALT_TEXT_ONLY")
                for alt in alts:
                    if alt and alt not in text:
                        text = f"{text}\n{alt}".strip()
                if text.strip():
                    section.blocks.append(
                        TextBlock(
                            block_id=f"docx:p:{paragraph_no}",
                            kind="heading" if level else _block_kind(child),
                            text=text,
                            style=style,
                            level=level,
                            ordinal1=paragraph_no,
                            locator=self._locator(
                                inp,
                                kind="office",
                                format="docx",
                                part_name="word/document.xml",
                                paragraph1=paragraph_no,
                                physical_page="unknown",
                            ),
                        )
                    )
                images.extend(_paragraph_images(child, names))
            elif local == "tbl":
                table_no += 1
                inp.check_deadline()
                table, cell_images = _table(inp, child, table_no)
                section.tables.append(table)
                images.extend(cell_images)
            elif local == "sdt":
                content = child.child("sdtContent")
                if content is not None:
                    for inner in content.children:
                        if local_name(inner) == "tbl":
                            table_no += 1
                            table, cell_images = _table(inp, inner, table_no)
                            section.tables.append(table)
                            images.extend(cell_images)
        _append_part_text(inp, names, limits, section, "word/header", "header")
        _append_part_text(inp, names, limits, section, "word/footer", "footer")
        _append_notes(inp, names, limits, section, "word/footnotes.xml", "footnote", "FOOTNOTES_PRESENT")
        _append_notes(inp, names, limits, section, "word/endnotes.xml", "endnote", "ENDNOTES_PRESENT")
        if "word/comments.xml" in names:
            doc.warnings = merge_warnings(doc.warnings, "COMMENTS_METADATA_ONLY")
        if images:
            doc.metadata["embeddedImages"] = images
            doc.warnings = merge_warnings(doc.warnings, "EMBEDDED_IMAGES_NOT_PARSED")
        if rels:
            doc.metadata["relationshipCount"] = len(rels)
        doc.sections.append(section)
        return doc.finalize()

    def _properties(self, inp: ParseInput, names: set[str], limits: MmiLimits) -> dict[str, Any]:
        meta: dict[str, Any] = {"format": "docx", "physicalPage": "unknown"}
        if "docProps/core.xml" not in names:
            return meta
        root = parse_xml(read_entry(inp.data, "docProps/core.xml", limits), limits)
        fields = {
            "title": "title",
            "subject": "subject",
            "created": "created",
            "modified": "modified",
            "revision": "revision",
            "language": "language",
        }
        for local, key in fields.items():
            node = next((n for n in root.children if local_name(n) == local), None)
            if node is not None and node.text:
                meta[key] = node.text[:4096]
        return meta


def _table(inp: ParseInput, node: XmlNode, table_no: int) -> tuple[Table, list[dict[str, Any]]]:
    cells: list[TableCell] = []
    images: list[dict[str, Any]] = []
    rows = node.children_named("tr")
    row_count = len(rows)
    column_count = 0
    for row_index, row in enumerate(rows, start=1):
        inp.check_deadline()
        col_index = 0
        for col_index, cell in enumerate(row.children_named("tc"), start=1):
            grid_before = cell.child("tcPr")
            span = int_attr(grid_before, "gridSpan", default=1) if grid_before is not None else 1
            for offset in range(max(1, min(span, 64))):
                column = col_index + offset
                column_count = max(column_count, column)
                paragraphs = cell.children_named("p")
                text = "\n".join(word_paragraph_text(p) for p in paragraphs).strip()
                locator = inp_locator(
                    inp,
                    table1=table_no,
                    row1=row_index,
                    column1=column,
                )
                cells.append(
                    TableCell(
                        row=row_index,
                        column=column,
                        a1=None,
                        text=text if text else None,
                        raw_type="docx:cell",
                        locator=locator,
                        merged=span > 1,
                        merged_anchor_a1=None,
                        value_state="observed" if text else "unknown",
                        flags=("HORIZONTAL_MERGE",) if span > 1 else (),
                    )
                )
                for paragraph in paragraphs:
                    for alt in drawing_alt_texts(paragraph):
                        if alt:
                            images.append({"part": "word/document.xml", "altText": alt, "table1": table_no})
    table = Table(
        table_id=f"docx:table:{table_no}",
        locator=inp_locator(inp, table1=table_no),
        cells=cells,
        row_count=row_count,
        column_count=column_count,
        headers=[c.text for c in cells if c.row == 1],
        warnings=("DOCX_TABLE_WITHOUT_PHYSICAL_PAGE",),
    )
    return table, images


def inp_locator(inp: ParseInput, **kwargs: Any) -> Any:
    from ..contracts import MmiSourceLocator

    return MmiSourceLocator(
        kind="office",
        artifact_id=inp.source_artifact_id,
        source_sha256=inp.source_sha256,
        format="docx",
        part_name="word/document.xml",
        physical_page="unknown",
        **kwargs,
    )


def _paragraph_style(paragraph: XmlNode) -> str | None:
    ppr = paragraph.child("pPr")
    if ppr is None:
        return None
    style = ppr.child("pStyle")
    return attr(style, "val") if style is not None else None


def _heading_level(paragraph: XmlNode, style: str | None) -> int | None:
    if style:
        match = _HEADING.match(style.strip()) or _HEADING.match(style.strip().replace("Heading", "Heading "))
        if match:
            return int(match.group(1))
        if style.strip().lower() in {"title", "タイトル"}:
            return 1
    ppr = paragraph.child("pPr")
    if ppr is not None:
        outline = ppr.child("outlineLvl")
        if outline is not None:
            value = int_attr(outline, "val", default=-1)
            if 0 <= value <= 8:
                return value + 1
    return None


def _block_kind(paragraph: XmlNode) -> str:
    ppr = paragraph.child("pPr")
    if ppr is not None and ppr.child("numPr") is not None:
        return "list_item"
    return "paragraph"


def _append_part_text(
    inp: ParseInput, names: set[str], limits: MmiLimits, section: Section, prefix: str, kind: str
) -> None:
    selected = sorted(n for n in names if n.startswith(prefix) and n.endswith(".xml") and "/_rels/" not in n)
    count = 0
    for name in selected:
        root = parse_xml(read_entry(inp.data, name, limits), limits)
        for paragraph in (n for n in root.iter() if local_name(n) == "p"):
            text = word_paragraph_text(paragraph)
            if not text.strip():
                continue
            count += 1
            section.blocks.append(
                TextBlock(
                    block_id=f"docx:{kind}:{count}",
                    kind=kind,  # type: ignore[arg-type]
                    text=text,
                    ordinal1=count,
                    locator=inp_part_locator(inp, name, count),
                )
            )


def _append_notes(
    inp: ParseInput, names: set[str], limits: MmiLimits, section: Section, part: str, kind: str, warning: str
) -> None:
    if part not in names:
        return
    root = parse_xml(read_entry(inp.data, part, limits), limits)
    count = 0
    for note in root.children:
        if local_name(note) not in {"footnote", "endnote"}:
            continue
        note_id = attr(note, "id")
        if note_id in {"-1", "0"}:
            continue
        text = "\n".join(word_paragraph_text(p) for p in note.children_named("p")).strip()
        if not text:
            continue
        count += 1
        section.blocks.append(
            TextBlock(
                block_id=f"docx:{kind}:{count}",
                kind=kind,  # type: ignore[arg-type]
                text=text,
                ordinal1=count,
                locator=inp_part_locator(inp, part, count),
            )
        )
    if count:
        section.warnings = merge_warnings(section.warnings, warning)


def inp_part_locator(inp: ParseInput, part: str, paragraph: int) -> Any:
    from ..contracts import MmiSourceLocator

    return MmiSourceLocator(
        kind="office",
        artifact_id=inp.source_artifact_id,
        source_sha256=inp.source_sha256,
        format="docx",
        part_name=part,
        paragraph1=paragraph,
        physical_page="unknown",
    )


def _paragraph_images(paragraph: XmlNode, names: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in paragraph.iter():
        if local_name(node) != "blip":
            continue
        rel_id = attr(node, "embed") or attr(node, "link")
        if rel_id:
            out.append({"relationshipId": rel_id})
    return out
