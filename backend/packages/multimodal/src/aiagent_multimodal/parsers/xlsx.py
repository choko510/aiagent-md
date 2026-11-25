"""Bounded XLSX reader using the standard library after the ZIP/XML gate (15-01 §10.2-10.3).

Only allowlisted OOXML parts are read. Formulas are never evaluated: the
formula text, the cached value and a cached error are separate observations,
and a cached error is never converted to blank or zero.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ..archive_gate import inspect_archive, read_entry
from ..contracts import MmiSourceLocator
from ..errors import MmiError
from ..limits import MmiLimits
from ..provenance.locators import a1_ref, parse_a1, parse_range
from ..xml_gate import XmlNode, parse_xml
from .base import BaseParser, Document, ParseInput, Section, Table, TableCell, merge_warnings

_BUILTIN_FORMATS: Final[dict[int, str]] = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    14: "m/d/yyyy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yyyy h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    49: "@",
}
_SAFE_PART = re.compile(r"^[A-Za-z0-9_./\-\[\]]+$")


@dataclass(frozen=True, slots=True)
class _SheetRef:
    name: str
    ordinal1: int
    state: str
    path: str


class XlsxParser(BaseParser):
    format = "xlsx"
    revision = "1.0.0"

    def parse(self, inp: ParseInput) -> Document:
        limits = inp.limits
        report = inspect_archive(inp.data, limits)
        names = set(report.names)
        if "xl/workbook.xml" not in names:
            raise MmiError("BAD_ARCHIVE", "XLSX workbook part is missing")
        workbook = parse_xml(
            read_entry(inp.data, "xl/workbook.xml", limits, max_bytes=8 * 1024 * 1024), limits, root_expected="workbook"
        )
        rels = _relationships(inp, "xl/_rels/workbook.xml.rels", names, limits)
        sheets = _sheet_refs(workbook, rels, names, inp, limits)
        strings = _shared_strings(inp, names, limits)
        formats = _styles(inp, names, limits)
        doc = self._document(
            inp,
            metadata={
                "sheets": [
                    {"name": s.name, "ordinal1": s.ordinal1, "hiddenState": s.state, "part": s.path} for s in sheets
                ],
                "sharedStrings": len(strings),
                "calculationMode": _attr(workbook.find("calcPr"), "calcMode") or "unknown",
            },
        )
        total_cells = 0
        for sheet in sheets:
            sheet_root = parse_xml(read_entry(inp.data, sheet.path, limits), limits, root_expected="worksheet")
            cells, rows, columns, warnings = _sheet_cells(sheet, sheet_root, strings, formats, inp, limits)
            total_cells += len(cells)
            if total_cells > limits.max_cells:
                raise MmiError("CELL_LIMIT", "workbook cell count exceeds limit")
            table = Table(
                table_id=f"sheet:{sheet.ordinal1}",
                locator=self._locator(
                    inp,
                    kind="sheet",
                    sheet_name=sheet.name,
                    sheet_ordinal1=sheet.ordinal1,
                    cell_a1="A1",
                    hidden_state=sheet.state,
                    raw_type="sheet",
                ),
                cells=cells,
                row_count=rows,
                column_count=columns,
                headers=_headers(cells),
                warnings=warnings,
            )
            section = Section(
                section_id=f"sheet:{sheet.ordinal1}",
                kind="sheet",
                ordinal1=sheet.ordinal1,
                label=sheet.name,
                locator=self._locator(
                    inp,
                    kind="sheet",
                    sheet_name=sheet.name,
                    sheet_ordinal1=sheet.ordinal1,
                    cell_a1="A1",
                    hidden_state=sheet.state,
                    raw_type="sheet",
                ),
                tables=[table],
            )
            doc.sections.append(section)
            doc.warnings = merge_warnings(doc.warnings, *warnings)
        if "xl/externalLinks/" in "".join(names):
            doc.warnings = merge_warnings(doc.warnings, "EXTERNAL_REFERENCE_NOT_READ")
        return doc.finalize()


def _sheet_cells(
    sheet: _SheetRef,
    root: XmlNode,
    strings: list[str],
    formats: Mapping[int, str],
    inp: ParseInput,
    limits: MmiLimits,
) -> tuple[list[TableCell], int, int, tuple[str, ...]]:
    warnings: list[str] = []
    hidden_cols = _hidden_columns(root)
    merged = _merged_cells(root)
    cells: dict[tuple[int, int], TableCell] = {}
    max_row = 0
    max_col = 0
    sheet_data = root.child("sheetData")
    if sheet_data is None:
        return [], 0, 0, ("EMPTY_SHEET",)
    for row_node in sheet_data.children_named("row"):
        inp.check_deadline()
        row_number = _int_attr(row_node, "r", default=len(cells) + 1)
        if row_number < 1 or row_number > limits.max_xlsx_rows_per_sheet:
            raise MmiError("ROW_LIMIT", "worksheet row exceeds limit")
        row_hidden = _bool_attr(row_node, "hidden")
        col_cursor = 0
        for cell_node in row_node.children_named("c"):
            ref = _attr(cell_node, "r")
            if ref:
                try:
                    row_i, col_i = parse_a1(ref)
                except ValueError as exc:
                    raise MmiError("BAD_XML", "invalid cell reference") from exc
                if row_i != row_number:
                    row_number = row_i
            else:
                col_cursor += 1
                col_i = col_cursor
            if col_i > 16_384:
                raise MmiError("CELL_LIMIT", "worksheet column exceeds the XLSX limit")
            max_row = max(max_row, row_number)
            max_col = max(max_col, col_i)
            if len(cells) >= limits.max_cells:
                raise MmiError("CELL_LIMIT", "workbook cell count exceeds limit")
            hidden = row_hidden or _column_hidden(col_i, hidden_cols)
            cell = _make_cell(inp, sheet, row_number, col_i, cell_node, strings, formats, hidden, merged)
            cells[(row_number, col_i)] = cell
            col_cursor = col_i
    for anchor, ref in merged.items():
        for row_i, col_i in _merge_members(ref, limits):
            key = (row_i, col_i)
            if key in cells:
                cell = cells[key]
                cell.merged = True
                cell.merged_anchor_a1 = anchor
            else:
                cells[key] = TableCell(
                    row=row_i,
                    column=col_i,
                    a1=a1_ref(row_i, col_i),
                    text=None,
                    raw_type="merged-empty",
                    locator=_sheet_locator(inp, sheet, row_i, col_i),
                    merged=True,
                    merged_anchor_a1=anchor,
                    hidden=_column_hidden(col_i, hidden_cols),
                    flags=("MERGED_EMPTY",),
                )
    ordered = [cells[key] for key in sorted(cells)]
    for cell in ordered:
        if cell.cached_error is not None:
            warnings.append("FORMULA_CACHED_ERROR")
        if cell.formula is not None and cell.value_state == "estimated":
            warnings.append("FORMULA_CACHED_VALUE_ESTIMATED")
        if cell.formula is not None and "SHARED_FORMULA" in cell.flags:
            warnings.append("SHARED_FORMULA_NOT_EXPANDED")
    return ordered, max_row, max_col, tuple(dict.fromkeys(warnings))


def _make_cell(
    inp: ParseInput,
    sheet: _SheetRef,
    row: int,
    column: int,
    node: XmlNode,
    strings: list[str],
    formats: Mapping[int, str],
    hidden: bool,
    merged: Mapping[str, str],
) -> TableCell:
    ref = a1_ref(row, column)
    cell_type = (_attr(node, "t") or "n").strip()
    style_index = _int_attr(node, "s", default=0)
    display_format = formats.get(style_index)
    formula = None
    formula_node = node.child("f")
    if formula_node is not None:
        formula = formula_node.text or ""
    value_node = node.child("v")
    raw_value = value_node.text if value_node is not None else None
    inline = node.child("is")
    flags: list[str] = []
    if formula_node is not None and _attr(formula_node, "t") == "shared":
        flags.append("SHARED_FORMULA")
    text: str | None
    cached_value: str | None = None
    cached_error: str | None = None
    value_state = "observed"
    if cell_type == "s":
        if raw_value is None or not raw_value.strip().isdigit():
            text = None
            value_state = "unknown"
            flags.append("INVALID_SHARED_STRING_INDEX")
        else:
            index = int(raw_value)
            if index >= len(strings):
                text = None
                value_state = "unknown"
                flags.append("SHARED_STRING_OUT_OF_RANGE")
            else:
                text = strings[index]
    elif cell_type == "inlineStr":
        text = inline.all_text() if inline is not None else (raw_value or "")
    elif cell_type == "e":
        cached_error = raw_value or "#UNKNOWN!"
        text = None
        value_state = "unknown"
        flags.append("FORMULA_CACHED_ERROR")
    elif cell_type in {"str", "d"}:
        text = raw_value if raw_value is not None else (inline.all_text() if inline is not None else None)
    elif cell_type == "b":
        text = raw_value
        if text not in {"0", "1"}:
            value_state = "unknown"
            flags.append("INVALID_BOOLEAN")
    else:
        text = raw_value
    if formula is not None:
        cached_value = raw_value if cell_type != "e" else None
        if cached_error is not None:
            value_state = "unknown"
        elif cached_value is not None and cached_value != "":
            # The cached value is the previous calculation, not a current result.
            value_state = "estimated"
            flags.append("CACHED_FORMULA_VALUE")
        else:
            value_state = "unknown"
            flags.append("FORMULA_VALUE_UNKNOWN")
    if text == "":
        flags.append("EMPTY_VALUE")
    hidden_sheet = sheet.state != "visible"
    if hidden_sheet:
        flags.append("HIDDEN_SHEET")
    if hidden:
        flags.append("HIDDEN_SOURCE")
    merged_anchor = merged.get(ref)
    return TableCell(
        row=row,
        column=column,
        a1=ref,
        text=text,
        raw_type=f"xlsx:{cell_type}",
        locator=_sheet_cell_locator(inp, sheet, row, column, display_format, cell_type, merged_anchor, hidden),
        formula=formula,
        cached_value=cached_value,
        cached_error=cached_error,
        merged=merged_anchor is not None,
        merged_anchor_a1=merged_anchor,
        hidden=hidden,
        value_state=value_state,
        flags=tuple(flags),
    )


def _sheet_cell_locator(
    inp: ParseInput,
    sheet: _SheetRef,
    row: int,
    column: int,
    display_format: str | None,
    raw_type: str,
    merged_anchor: str | None,
    hidden: bool,
) -> MmiSourceLocator:
    return MmiSourceLocator(
        kind="sheet",
        artifact_id=inp.source_artifact_id,
        source_sha256=inp.source_sha256,
        sheet_name=sheet.name,
        sheet_ordinal1=sheet.ordinal1,
        cell_a1=a1_ref(row, column),
        hidden_state=sheet.state if sheet.state != "visible" else ("hidden" if hidden else "visible"),
        merged_anchor_a1=merged_anchor,
        raw_type=raw_type,
        display_format=display_format,
    )


def _sheet_locator(inp: ParseInput, sheet: _SheetRef, row: int, column: int) -> MmiSourceLocator:
    return MmiSourceLocator(
        kind="sheet",
        artifact_id=inp.source_artifact_id,
        source_sha256=inp.source_sha256,
        sheet_name=sheet.name,
        sheet_ordinal1=sheet.ordinal1,
        cell_a1=a1_ref(row, column),
        hidden_state=sheet.state,
        raw_type="merged-empty",
    )


def _relationships(inp: ParseInput, path: str, names: set[str], limits: MmiLimits) -> dict[str, str]:
    if path not in names:
        return {}
    root = parse_xml(read_entry(inp.data, path, limits), limits, root_expected="Relationships")
    out: dict[str, str] = {}
    for rel in root.children_named("Relationship"):
        target = _attr(rel, "Target") or ""
        rel_id = _attr(rel, "Id") or ""
        if _attr(rel, "TargetMode") == "External":
            raise MmiError("EXTERNAL_REFERENCE", "workbook external relationship")
        if not rel_id or not target:
            continue
        out[rel_id] = _resolve_part("xl", target)
    return out


def _resolve_part(base: str, target: str) -> str:
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved.startswith("../") or not _SAFE_PART.match(resolved):
        raise MmiError("BAD_ARCHIVE", "unsafe OOXML relationship target")
    return resolved


def _sheet_refs(
    workbook: XmlNode, rels: Mapping[str, str], names: set[str], inp: ParseInput, limits: MmiLimits
) -> list[_SheetRef]:
    sheets_node = workbook.child("sheets")
    if sheets_node is None:
        raise MmiError("BAD_XML", "workbook has no sheets")
    refs: list[_SheetRef] = []
    for ordinal, node in enumerate(sheets_node.children_named("sheet"), start=1):
        if ordinal > limits.max_xlsx_sheets:
            raise MmiError("EXPANSION_LIMIT", "workbook sheet count exceeds limit")
        name = _attr(node, "name") or f"Sheet{ordinal}"
        rel_id = _attr(node, "id") or ""
        state = _attr(node, "state") or "visible"
        if state not in {"visible", "hidden", "veryHidden"}:
            state = "hidden"
        target = rels.get(rel_id)
        if not target or target not in names:
            raise MmiError("BAD_ARCHIVE", "worksheet relationship target is missing")
        if not target.startswith("xl/worksheets/"):
            raise MmiError("BAD_ARCHIVE", "worksheet part outside the allowlisted namespace")
        refs.append(_SheetRef(name=name, ordinal1=ordinal, state=state, path=target))
    if not refs:
        raise MmiError("BAD_XML", "workbook has no sheets")
    return refs


def _shared_strings(inp: ParseInput, names: set[str], limits: MmiLimits) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    data = read_entry(inp.data, "xl/sharedStrings.xml", limits, max_bytes=limits.max_shared_strings_bytes)
    if len(data) > limits.max_shared_strings_bytes:
        raise MmiError("EXPANSION_LIMIT", "shared strings exceed limit")
    root = parse_xml(data, limits, root_expected="sst")
    out: list[str] = []
    total = 0
    for node in root.children_named("si"):
        if len(out) >= limits.max_shared_strings:
            raise MmiError("EXPANSION_LIMIT", "shared string count exceeds limit")
        text = node.all_text()
        total += len(text.encode("utf-8"))
        if total > limits.max_shared_strings_bytes:
            raise MmiError("EXPANSION_LIMIT", "shared string bytes exceed limit")
        out.append(text)
    return out


def _styles(inp: ParseInput, names: set[str], limits: MmiLimits) -> dict[int, str]:
    if "xl/styles.xml" not in names:
        return {}
    root = parse_xml(read_entry(inp.data, "xl/styles.xml", limits), limits, root_expected="styleSheet")
    custom: dict[int, str] = {}
    num_fmts = root.child("numFmts")
    if num_fmts is not None:
        for node in num_fmts.children_named("numFmt"):
            fmt_id = _int_attr(node, "numFmtId", default=-1)
            code = _attr(node, "formatCode")
            if fmt_id >= 0 and code:
                custom[fmt_id] = code
    xfs = root.child("cellXfs")
    out: dict[int, str] = {}
    if xfs is not None:
        for index, node in enumerate(xfs.children_named("xf")):
            fmt_id = _int_attr(node, "numFmtId", default=0)
            out[index] = custom.get(fmt_id) or _BUILTIN_FORMATS.get(fmt_id, f"fmt:{fmt_id}")
    return out


def _hidden_columns(root: XmlNode) -> list[tuple[int, int]]:
    cols = root.child("cols")
    out: list[tuple[int, int]] = []
    if cols is not None:
        for node in cols.children_named("col"):
            if _bool_attr(node, "hidden"):
                lo = _int_attr(node, "min", default=1)
                hi = _int_attr(node, "max", default=lo)
                if 1 <= lo <= hi <= 16_384:
                    out.append((lo, hi))
    return out


def _column_hidden(column: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= column <= hi for lo, hi in ranges)


def _merged_cells(root: XmlNode) -> dict[str, str]:
    out: dict[str, str] = {}
    merged = root.child("mergeCells")
    if merged is not None:
        for node in merged.children_named("mergeCell"):
            ref = _attr(node, "ref") or ""
            try:
                start, _end = parse_range(ref)
            except ValueError:
                continue
            anchor = a1_ref(*start)
            out[anchor] = ref
    return out


def _merge_members(ref: str, limits: MmiLimits) -> list[tuple[int, int]]:
    (r1, c1), (r2, c2) = parse_range(ref)
    if (r2 - r1 + 1) * (c2 - c1 + 1) > limits.max_cells:
        raise MmiError("CELL_LIMIT", "merged range exceeds limit")
    return [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def _headers(cells: list[TableCell]) -> list[str | None]:
    first: dict[int, str | None] = {}
    for cell in cells:
        if cell.row == 1:
            first[cell.column] = cell.text
    if not first:
        return []
    width = max(first)
    return [first.get(col) for col in range(1, width + 1)]


def _attr(node: XmlNode | None, name: str) -> str | None:
    if node is None:
        return None
    if name in node.attrs:
        return node.attrs[name]
    for key, value in node.attrs.items():
        if key.rsplit(":", 1)[-1] == name:
            return value
    return None


def _int_attr(node: XmlNode, name: str, *, default: int) -> int:
    value = _attr(node, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise MmiError("BAD_XML", f"invalid integer attribute {name}") from exc


def _bool_attr(node: XmlNode, name: str) -> bool:
    value = (_attr(node, name) or "").strip().lower()
    return value in {"1", "true"}


def _parse_a1(ref: str) -> tuple[int, int]:
    from ..provenance.locators import parse_a1

    return parse_a1(ref)
