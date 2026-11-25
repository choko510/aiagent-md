"""PDF preflight and native-text extraction (15-01 §8, §6).

The preflight rejects encrypted PDFs, embedded JavaScript/launch actions,
attachments, XFA and rich media before any parser touches the file. Native
text/tables come from pdfplumber when its optional extra is installed; scanned
pages go through the local OCR path only when policy permits. No PDF action,
attachment or external URL is ever executed or followed.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, Final

from ..errors import MmiError
from ..limits import MmiLimits
from ..provenance.locators import normalized_bbox
from .base import BaseParser, Document, ParseInput, Section, Table, TableCell, TextBlock, merge_warnings

_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_VERSION = re.compile(rb"%PDF-([0-9]\.[0-9])")
_ACTIVE: Final[dict[bytes, str]] = {
    b"/JavaScript": "embedded JavaScript",
    b"/JS": "JavaScript action",
    b"/Launch": "launch action",
    b"/EmbeddedFile": "embedded file",
    b"/EmbeddedFiles": "embedded files",
    b"/RichMedia": "rich media",
    b"/XFA": "XFA form",
    b"/OpenAction": "open action",
    b"/AA": "additional action",
}
_URI = re.compile(rb"/URI\s*[\(\<]")


@dataclass(frozen=True, slots=True)
class PdfPreflight:
    version: str
    page_count: int
    encrypted: bool
    active_content: tuple[str, ...]
    external_uri_count: int
    linearized: bool


def preflight_pdf(data: bytes, limits: MmiLimits) -> PdfPreflight:
    if not data.startswith(b"%PDF-"):
        raise MmiError("UNSUPPORTED_FORMAT", "not a PDF file")
    version = _VERSION.match(data)
    encrypted = b"/Encrypt" in data
    active = tuple(reason for marker, reason in _ACTIVE.items() if marker in data)
    page_count = len(_PAGE.findall(data))
    counts = [int(m.group(1)) for m in re.finditer(rb"/Count\s+(\d+)", data) if int(m.group(1)) < 1_000_000]
    if counts:
        page_count = max(page_count, *counts)
    if encrypted:
        raise MmiError("PASSWORD_REQUIRED", "encrypted PDF requires a password")
    if active:
        raise MmiError("UNSUPPORTED_ACTIVE_CONTENT", f"PDF contains {active[0]}")
    if page_count < 1:
        raise MmiError("UNSUPPORTED_FORMAT", "PDF page tree is unavailable")
    if page_count > limits.max_pdf_pages:
        raise MmiError("PAGE_LIMIT", "PDF page count exceeds limit")
    return PdfPreflight(
        version=(version.group(1).decode() if version else "unknown"),
        page_count=page_count,
        encrypted=encrypted,
        active_content=active,
        external_uri_count=len(_URI.findall(data)),
        linearized=b"/Linearized" in data[:2048],
    )


class PdfParser(BaseParser):
    format = "pdf"
    revision = "1.0.0"

    def parse(self, inp: ParseInput) -> Document:
        info = preflight_pdf(inp.data, inp.limits)
        doc = self._document(
            inp,
            metadata={
                "pdfVersion": info.version,
                "pageCount": info.page_count,
                "externalUriCount": info.external_uri_count,
                "linearized": info.linearized,
            },
        )
        if info.external_uri_count:
            doc.warnings = merge_warnings(doc.warnings, "EXTERNAL_REFERENCES_NOT_FOLLOWED")
        try:
            import pdfplumber  # type: ignore[import-not-found]
        except ImportError:
            doc.partial = True
            doc.warnings = merge_warnings(doc.warnings, "PDF_TEXT_EXTRACTION_UNAVAILABLE")
            for page_no in range(1, info.page_count + 1):
                doc.sections.append(
                    Section(
                        section_id=f"pdf:page:{page_no}",
                        kind="page",
                        ordinal1=page_no,
                        label=f"Page {page_no}",
                        locator=self._locator(
                            inp,
                            kind="pdf",
                            page1=page_no,
                            page_size_pt=(0.1, 0.1),
                            rotation_deg=0,
                            layer="native",
                        ),
                        warnings=("PAGE_TEXT_UNKNOWN",),
                    )
                )
            return doc.finalize()
        pdf_factory = getattr(pdfplumber, "open", None)
        if pdf_factory is None:  # pragma: no cover - defensive for a wrong package
            raise MmiError("PARSER_CRASH", "pdfplumber contract changed")
        try:
            pdf = pdf_factory(io.BytesIO(inp.data))
        except Exception as exc:  # pdfminer raises many types on adversarial PDFs
            raise MmiError("PARSER_CRASH", f"PDF open failed ({type(exc).__name__})") from exc
        try:
            pages = list(pdf.pages)
            if len(pages) > inp.limits.max_pdf_pages:
                raise MmiError("PAGE_LIMIT", "PDF page count exceeds limit")
            rendered_pixels = 0
            for page_no, page in enumerate(pages, start=1):
                inp.check_deadline()
                width = float(page.width or 0)
                height = float(page.height or 0)
                if width <= 0 or height <= 0:
                    raise MmiError("BAD_XML", "PDF page has invalid dimensions")
                page_pixels = int(width * height)
                rendered_pixels += page_pixels
                if page_pixels > inp.limits.max_render_pixels_per_page:
                    raise MmiError("PIXEL_LIMIT", "PDF page render budget exceeded")
                if rendered_pixels > inp.limits.max_render_pixels_total:
                    raise MmiError("PIXEL_LIMIT", "PDF render budget exceeded")
                section = Section(
                    section_id=f"pdf:page:{page_no}",
                    kind="page",
                    ordinal1=page_no,
                    label=f"Page {page_no}",
                    locator=self._locator(
                        inp,
                        kind="pdf",
                        page1=page_no,
                        page_size_pt=(width, height),
                        rotation_deg=int(getattr(page, "rotation", 0) or 0),
                        layer="native",
                    ),
                )
                text = page.extract_text() or ""
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
                for index, line in enumerate(_lines(words, inp), start=1):
                    block_text = " ".join(w["text"] for w in line).strip()
                    if not block_text:
                        continue
                    x0 = min(float(w["x0"]) for w in line)
                    top = min(float(w["top"]) for w in line)
                    x1 = max(float(w["x1"]) for w in line)
                    bottom = max(float(w["bottom"]) for w in line)
                    # Some PDFs omit font metrics; pdfminer then reports a zero-width glyph box. A citation
                    # must not invent a region, so keep the page/line but leave the bbox unknown.
                    bbox = normalized_bbox(x0, top, x1, bottom, width, height) if x1 > x0 and bottom > top else None
                    section.blocks.append(
                        TextBlock(
                            block_id=f"pdf:p{page_no}:l{index}",
                            kind="paragraph",
                            text=block_text,
                            ordinal1=index,
                            locator=self._locator(
                                inp,
                                kind="pdf",
                                page1=page_no,
                                page_size_pt=(width, height),
                                rotation_deg=int(getattr(page, "rotation", 0) or 0),
                                normalized_bbox=bbox,
                                text_span=_find_span(text, block_text),
                                layer="native",
                            ),
                        )
                    )
                tables = page.find_tables() if hasattr(page, "find_tables") else []
                for table_no, found in enumerate(tables or [], start=1):
                    self._table(inp, section, page_no, table_no, found, width, height, doc)
                if not text.strip():
                    doc.warnings = merge_warnings(doc.warnings, "PAGE_TEXT_EMPTY")
                    if bool(inp.options.get("run_ocr", False)):
                        self._ocr_page(inp, section, page, page_no, width, height, doc)
                doc.sections.append(section)
            if info.external_uri_count:
                doc.warnings = merge_warnings(doc.warnings, "PDF_ANNOTATIONS_NOT_FOLLOWED")
        finally:
            close = getattr(pdf, "close", None)
            if callable(close):
                close()
        return doc.finalize()

    def _table(
        self,
        inp: ParseInput,
        section: Section,
        page_no: int,
        table_no: int,
        found: Any,
        width: float,
        height: float,
        doc: Document,
    ) -> None:
        try:
            rows = found.extract()
        except Exception:
            doc.warnings = merge_warnings(doc.warnings, "TABLE_EXTRACTION_FAILED")
            return
        cells: list[TableCell] = []
        for row_index, row in enumerate(rows or [], start=1):
            for col_index, value in enumerate(row or [], start=1):
                text = "" if value is None else str(value)
                bbox = None
                try:
                    cell_boxes = found.rows[row_index - 1].cells
                    if cell_boxes and col_index - 1 < len(cell_boxes) and cell_boxes[col_index - 1]:
                        bbox = cell_boxes[col_index - 1]
                except (IndexError, AttributeError):
                    bbox = None
                locator = self._locator(
                    inp,
                    kind="pdf",
                    page1=page_no,
                    page_size_pt=(width, height),
                    rotation_deg=0,
                    normalized_bbox=(
                        normalized_bbox(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
                        if bbox and len(bbox) >= 4
                        else None
                    ),
                    layer="native",
                )
                cells.append(
                    TableCell(
                        row=row_index,
                        column=col_index,
                        a1=None,
                        text=text if text else None,
                        raw_type="pdf:cell",
                        locator=locator,
                        value_state="observed" if text else "unknown",
                    )
                )
        table = Table(
            table_id=f"pdf:p{page_no}:t{table_no}",
            locator=self._locator(
                inp, kind="pdf", page1=page_no, page_size_pt=(width, height), rotation_deg=0, layer="native"
            ),
            cells=cells,
            row_count=len(rows or []),
            column_count=max((len(r) for r in rows or []), default=0),
            headers=[str(v) if v is not None else None for v in (rows[0] if rows else [])],
            warnings=("PDF_TABLE_CANDIDATE",),
        )
        section.tables.append(table)

    def _ocr_page(
        self, inp: ParseInput, section: Section, page: Any, page_no: int, width: float, height: float, doc: Document
    ) -> None:
        from ..ocr.tesseract import TesseractOcr, TesseractUnavailable

        engine = TesseractOcr()
        if not engine.available():
            doc.warnings = merge_warnings(doc.warnings, "LOCAL_OCR_UNAVAILABLE")
            return
        try:
            rendered = page.to_image(resolution=150).original
            buffer = io.BytesIO()
            rendered.save(buffer, format="PNG")
            result = engine.run(buffer.getvalue(), languages=str(inp.options.get("ocr_languages", "jpn+eng")))
        except TesseractUnavailable:
            doc.warnings = merge_warnings(doc.warnings, "LOCAL_OCR_UNAVAILABLE")
            return
        except TimeoutError:
            raise MmiError("PARSE_TIMEOUT", "local OCR exceeded its deadline") from None
        for index, region in enumerate(result.regions[: inp.limits.max_ocr_regions], start=1):
            section.blocks.append(
                TextBlock(
                    block_id=f"pdf:p{page_no}:ocr:{index}",
                    kind="paragraph",
                    text=region.text,
                    ordinal1=index,
                    recognition_confidence_raw=region.confidence,
                    extraction_method="ocr",
                    locator=self._locator(
                        inp,
                        kind="pdf",
                        page1=page_no,
                        page_size_pt=(width, height),
                        rotation_deg=0,
                        normalized_bbox=normalized_bbox(
                            region.left,
                            region.top,
                            region.left + region.width,
                            region.top + region.height,
                            width,
                            height,
                        )
                        if region.width and region.height
                        else None,
                        layer="ocr",
                    ),
                )
            )
        doc.warnings = merge_warnings(doc.warnings, "LOCAL_OCR_USED")


def _lines(words: list[dict[str, Any]], inp: ParseInput) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_top: float | None = None
    for word in sorted(words, key=lambda w: (round(float(w["top"]), 1), float(w["x0"]))):
        top = float(word["top"])
        height = float(word.get("bottom", top + 10)) - top
        tolerance = max(2.0, height * 0.4)
        if last_top is None or abs(top - last_top) <= tolerance:
            current.append(word)
        else:
            if current:
                lines.append(current)
            current = [word]
        last_top = top if last_top is None else (last_top + top) / 2
    if current:
        lines.append(current)
    return lines[: inp.limits.max_ocr_regions * 100]


def _find_span(text: str, block: str) -> tuple[int, int] | None:
    start = text.find(block)
    return (start, start + len(block)) if start >= 0 else None
