"""PPTX extraction in presentation order with slide/shape/table anchors (15-01 §11.2).

Slide order comes from `p:sldIdLst` and its relationships, never from file
names. Charts and embedded objects are reported as unparsed (never calculated
or launched). Tables keep cell coordinates so a value can be cited.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from ..archive_gate import inspect_archive, read_entry
from ..contracts import MmiSourceLocator
from ..errors import MmiError
from ..limits import MmiLimits
from ..xml_gate import XmlNode, parse_xml
from .base import BaseParser, Document, ParseInput, Section, Table, TableCell, TextBlock, merge_warnings
from .image import sniff_image
from .ooxml import (
    assert_no_external_relationships,
    attr,
    int_attr,
    local_name,
    pptx_paragraph_text,
    relationships,
)

_ACTIVE_PARTS = ("ppt/vbaProject.bin", "ppt/embeddings/", "ppt/activeX/", "ppt/oleObject")


class PptxParser(BaseParser):
    format = "pptx"
    revision = "1.0.0"

    def parse(self, inp: ParseInput) -> Document:
        limits = inp.limits
        report = inspect_archive(inp.data, limits)
        names = set(report.names)
        for name in names:
            if name.startswith(_ACTIVE_PARTS):
                raise MmiError("UNSUPPORTED_ACTIVE_CONTENT", "PPTX contains an executable/embedded object")
        assert_no_external_relationships(inp.data, names, limits)
        if "ppt/presentation.xml" not in names:
            raise MmiError("BAD_ARCHIVE", "PPTX presentation part is missing")
        root = parse_xml(read_entry(inp.data, "ppt/presentation.xml", limits), limits, root_expected="presentation")
        rels = relationships(inp.data, "ppt/presentation.xml", names, limits)
        slides = _slide_order(root, rels, names, limits)
        doc = self._document(inp, metadata={"format": "pptx", "physicalPage": "unknown", "slideCount": len(slides)})
        if not slides:
            doc.warnings = ("EMPTY_DOCUMENT",)
            return doc.finalize()
        size = root.child("sldSz")
        if size is not None:
            doc.metadata["slideSizeEmu"] = {
                "cx": int_attr(size, "cx", default=0),
                "cy": int_attr(size, "cy", default=0),
            }
        embedded: list[dict[str, Any]] = []
        for slide_no, slide_path in enumerate(slides, start=1):
            inp.check_deadline()
            slide = parse_xml(read_entry(inp.data, slide_path, limits), limits, root_expected="sld")
            slide_rels = relationships(inp.data, slide_path, names, limits)
            section = Section(
                section_id=f"pptx:slide:{slide_no}",
                kind="slide",
                ordinal1=slide_no,
                label=f"Slide {slide_no}",
                locator=self._locator(
                    inp, kind="office", format="pptx", part_name=slide_path, slide1=slide_no, physical_page="unknown"
                ),
            )
            sp_tree = slide.find("cSld/spTree")
            if sp_tree is not None:
                _walk_shapes(
                    inp,
                    slide_no,
                    slide_path,
                    sp_tree.children,
                    slide_rels,
                    section,
                    embedded,
                    limits,
                    doc,
                )
            _append_notes(inp, slide_no, slide_path, slide_rels, names, limits, section)
            doc.sections.append(section)
        if embedded:
            doc.metadata["embeddedImages"] = embedded
            doc.warnings = merge_warnings(doc.warnings, "EMBEDDED_IMAGES_NOT_PARSED")
        return doc.finalize()


def _rel_id(node: XmlNode) -> str:
    for key, value in node.attrs.items():
        if key.rsplit(":", 1)[-1] == "id" and key != "id":
            return value
    return ""


def _locator(inp: ParseInput, **kwargs: Any) -> MmiSourceLocator:
    return MmiSourceLocator(
        artifact_id=inp.source_artifact_id,
        source_sha256=inp.source_sha256,
        **kwargs,
    )


def _slide_order(root: XmlNode, rels: dict[str, tuple[str, bool]], names: set[str], limits: MmiLimits) -> list[str]:
    listing = root.child("sldIdLst")
    if listing is None:
        return []
    out: list[str] = []
    for slide in listing.children_named("sldId"):
        if len(out) >= limits.max_pptx_slides:
            raise MmiError("EXPANSION_LIMIT", "presentation slide count exceeds limit")
        rel_id = _rel_id(slide)
        target = rels.get(rel_id)
        if target is None or target[0] not in names:
            raise MmiError("BAD_ARCHIVE", "slide relationship target is missing")
        if not target[0].startswith("ppt/slides/"):
            raise MmiError("BAD_ARCHIVE", "slide part outside the allowlisted namespace")
        out.append(target[0])
    return out


def _walk_shapes(
    inp: ParseInput,
    slide_no: int,
    slide_path: str,
    nodes: list[XmlNode],
    slide_rels: dict[str, tuple[str, bool]],
    section: Section,
    embedded: list[dict[str, Any]],
    limits: MmiLimits,
    doc: Document,
) -> None:
    for node in nodes:
        local = local_name(node)
        if local == "grpSp":
            _walk_shapes(inp, slide_no, slide_path, node.children, slide_rels, section, embedded, limits, doc)
            continue
        if local == "sp":
            _shape(inp, slide_no, slide_path, node, section, doc)
        elif local == "graphicFrame":
            _graphic_frame(inp, slide_no, slide_path, node, section, doc, limits)
        elif local == "pic":
            _picture(inp, slide_no, slide_path, node, slide_rels, embedded, limits)
        elif local in {"cxnSp", "contentPart"}:
            doc.warnings = merge_warnings(doc.warnings, "UNSUPPORTED_SHAPE_SKIPPED")


def _shape(inp: ParseInput, slide_no: int, slide_path: str, node: XmlNode, section: Section, doc: Document) -> None:
    props = node.child("nvSpPr")
    c_nv = props.child("cNvPr") if props is not None else None
    shape_id = attr(c_nv, "id") or ""
    name = attr(c_nv, "name") or ""
    descr = attr(c_nv, "descr")
    placeholder = _placeholder_type(node)
    text = "\n".join(pptx_paragraph_text(p) for p in _shape_paragraphs(node)).strip()
    if descr and descr not in text:
        text = f"{text}\n{descr}".strip()
    if not text:
        return
    kind = "slide_title" if placeholder in {"title", "ctrTitle"} else "shape_text"
    block = TextBlock(
        block_id=f"pptx:s{slide_no}:shape:{shape_id or len(section.blocks) + 1}",
        kind=kind,
        text=text,
        ordinal1=len(section.blocks) + 1,
        style=placeholder or name or None,
        locator=_locator(
            inp,
            kind="office",
            format="pptx",
            part_name=slide_path,
            slide1=slide_no,
            shape_id=shape_id or None,
            physical_page="unknown",
        ),
    )
    section.blocks.append(block)


def _graphic_frame(
    inp: ParseInput, slide_no: int, slide_path: str, node: XmlNode, section: Section, doc: Document, limits: MmiLimits
) -> None:
    table = node.find("graphic/graphicData/tbl")
    if table is not None:
        existing = sum(len(s.tables) for s in doc.sections)
        table_no = existing + 1
        built, images = _table(inp, slide_no, slide_path, table, table_no)
        section.tables.append(built)
        for image in images:
            doc.metadata.setdefault("embeddedImages", []).append(image)
        return
    chart = node.find("graphic/graphicData")
    if chart is not None:
        doc.warnings = merge_warnings(doc.warnings, "CHART_METADATA_NOT_PARSED")


def _table(
    inp: ParseInput, slide_no: int, slide_path: str, node: XmlNode, table_no: int
) -> tuple[Table, list[dict[str, Any]]]:
    cells: list[TableCell] = []
    rows = node.children_named("tr")
    column_count = 0
    images: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=1):
        col_index = 0
        for col_index, cell in enumerate(row.children_named("tc"), start=1):
            column_count = max(column_count, col_index)
            text = "\n".join(pptx_paragraph_text(p) for p in _cell_paragraphs(cell)).strip()
            for paragraph in _cell_paragraphs(cell):
                for node2 in paragraph.iter():
                    if local_name(node2) == "blip":
                        rel_id = attr(node2, "embed") or attr(node2, "link")
                        if rel_id:
                            images.append({"slide": slide_no, "relationshipId": rel_id})
            cells.append(
                TableCell(
                    row=row_index,
                    column=col_index,
                    a1=None,
                    text=text if text else None,
                    raw_type="pptx:cell",
                    locator=_locator(
                        inp,
                        kind="office",
                        format="pptx",
                        part_name=slide_path,
                        slide1=slide_no,
                        table1=table_no,
                        row1=row_index,
                        column1=col_index,
                        physical_page="unknown",
                    ),
                    value_state="observed" if text else "unknown",
                )
            )
    return (
        Table(
            table_id=f"pptx:slide:{slide_no}:table:{table_no}",
            locator=_locator(
                inp,
                kind="office",
                format="pptx",
                part_name=slide_path,
                slide1=slide_no,
                table1=table_no,
                physical_page="unknown",
            ),
            cells=cells,
            row_count=len(rows),
            column_count=column_count,
            headers=[c.text for c in cells if c.row == 1],
            warnings=("PPTX_TABLE_WITHOUT_PHYSICAL_PAGE",),
        ),
        images,
    )


def _picture(
    inp: ParseInput,
    slide_no: int,
    slide_path: str,
    node: XmlNode,
    rels: dict[str, tuple[str, bool]],
    embedded: list[dict[str, Any]],
    limits: MmiLimits,
) -> None:
    props = node.child("nvPicPr")
    c_nv = props.child("cNvPr") if props is not None else None
    descr = attr(c_nv, "descr")
    blip = None
    for candidate in node.iter():
        if local_name(candidate) == "blip":
            blip = candidate
            break
    if blip is None:
        return
    rel_id = attr(blip, "embed") or ""
    target = rels.get(rel_id)
    if target is None:
        return
    part = target[0]
    try:
        data = read_entry(inp.data, part, limits, max_bytes=limits.max_zip_entry_bytes)
        info = sniff_image(data)
    except MmiError:
        embedded.append({"slide": slide_no, "part": part, "altText": descr, "imageState": "unreadable"})
        return
    embedded.append(
        {
            "slide": slide_no,
            "part": part,
            "digest": f"sha256:{sha256(data).hexdigest()}",
            "widthPx": info.width_px,
            "heightPx": info.height_px,
            "altText": descr,
        }
    )


def _append_notes(
    inp: ParseInput,
    slide_no: int,
    slide_path: str,
    rels: dict[str, tuple[str, bool]],
    names: set[str],
    limits: MmiLimits,
    section: Section,
) -> None:
    for target, _external in rels.values():
        if "/notesSlides/" not in target or target not in names:
            continue
        root = parse_xml(read_entry(inp.data, target, limits), limits, root_expected="notes")
        count = 0
        for paragraph in (n for n in root.iter() if local_name(n) == "p"):
            text = pptx_paragraph_text(paragraph).strip()
            if not text:
                continue
            count += 1
            section.blocks.append(
                TextBlock(
                    block_id=f"pptx:s{slide_no}:note:{count}",
                    kind="note",
                    text=text,
                    ordinal1=count,
                    locator=_locator(
                        inp,
                        kind="office",
                        format="pptx",
                        part_name=target,
                        slide1=slide_no,
                        paragraph1=count,
                        physical_page="unknown",
                    ),
                )
            )


def _shape_paragraphs(node: XmlNode) -> list[XmlNode]:
    tx = node.child("txBody")
    if tx is None:
        return []
    return tx.children_named("p")


def _cell_paragraphs(node: XmlNode) -> list[XmlNode]:
    tx = node.child("txBody")
    if tx is None:
        return []
    return tx.children_named("p")


def _placeholder_type(node: XmlNode) -> str | None:
    nv = node.child("nvSpPr")
    if nv is None:
        return None
    props = nv.child("nvPr")
    if props is None:
        return None
    placeholder = props.child("ph")
    return attr(placeholder, "type") if placeholder is not None else None
