"""Strict streaming CSV/TSV parser with logical records and byte offsets (15-01 §10.1).

Physical newlines inside quoted fields are not record boundaries. Every field
keeps its original bytes range so a citation can be replayed against the
immutable source. Values remain text: a leading `=`, `+`, `-`, `@` or tab is
flagged as CSV-injection-like data and is never executed or rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..errors import MmiError
from ..limits import MmiLimits
from .base import BaseParser, Document, ParseInput, Section, Table, TableCell, merge_warnings

_INJECTION_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class _Field:
    raw: bytes
    start: int
    end: int
    quoted: bool


@dataclass(frozen=True, slots=True)
class _Record:
    fields: tuple[_Field, ...]
    start: int
    end: int
    physical_lines: int


class CsvParser(BaseParser):
    format = "csv"
    revision = "1.0.0"

    def parse(self, inp: ParseInput) -> Document:
        encoding = inp.encoding or _option_str(inp, "encoding") or "utf-8"
        delimiter = inp.delimiter or _option_str(inp, "delimiter") or ","
        if delimiter not in {",", "\t", ";", "|"} or len(delimiter) != 1:
            raise MmiError("UNSUPPORTED_FORMAT", "unsupported delimiter")
        header = bool(inp.options.get("header", True))
        records, unclosed_quote = _parse_records(inp.data, delimiter, inp.limits, encoding, inp)
        if not records:
            doc = self._document(inp, metadata={"encoding": encoding, "delimiter": delimiter, "records": 0})
            doc.warnings = ("EMPTY_DOCUMENT",)
            return doc.finalize()

        decoded: list[list[str]] = []
        for record in records:
            row: list[str] = []
            for field in record.fields:
                try:
                    row.append(field.raw.decode(encoding, "strict"))
                except UnicodeDecodeError as exc:
                    raise MmiError("INVALID_ENCODING", "field is not valid in the selected encoding") from exc
            decoded.append(row)

        width = max(len(row) for row in decoded)
        if width > inp.limits.max_csv_columns:
            raise MmiError("CELL_LIMIT", "CSV column count exceeds limit")
        if len(decoded) - (1 if header else 0) > inp.limits.max_csv_rows:
            raise MmiError("ROW_LIMIT", "CSV row count exceeds limit")
        if len(decoded) * width > inp.limits.max_cells:
            raise MmiError("CELL_LIMIT", "CSV cell count exceeds limit")

        headers: list[str | None] = list(decoded[0]) + [None] * (width - len(decoded[0])) if header else [None] * width
        table_id = "csv:1"
        locator = self._locator(
            inp,
            kind="csv",
            record1=1,
            field1=1,
            byte_start="0",
            byte_end=str(len(inp.data)),
            header_text=headers[0] if headers and headers[0] is not None else None,
        )
        cells: list[TableCell] = []
        for row_index, record in enumerate(records, start=1):
            inp.check_deadline()
            for col_index, field in enumerate(record.fields, start=1):
                text = decoded[row_index - 1][col_index - 1]
                flags = _flags(text)
                cell_locator = self._locator(
                    inp,
                    kind="csv",
                    record1=row_index,
                    field1=col_index,
                    byte_start=str(field.start),
                    byte_end=str(field.end),
                    header_text=headers[col_index - 1] if col_index - 1 < len(headers) else None,
                )
                cells.append(
                    TableCell(
                        row=row_index,
                        column=col_index,
                        a1=None,
                        text=text,
                        raw_type="quoted" if field.quoted else "text",
                        locator=cell_locator,
                        flags=flags,
                    )
                )
            for col_index in range(len(record.fields) + 1, width + 1):
                cells.append(
                    TableCell(
                        row=row_index,
                        column=col_index,
                        a1=None,
                        text=None,
                        raw_type="missing",
                        locator=self._locator(
                            inp,
                            kind="csv",
                            record1=row_index,
                            field1=col_index,
                            byte_start=str(record.end),
                            byte_end=str(record.end),
                            header_text=headers[col_index - 1] if col_index - 1 < len(headers) else None,
                        ),
                        flags=("MISSING_VALUE",),
                    )
                )
        table = Table(
            table_id=table_id,
            locator=locator,
            cells=cells,
            row_count=len(records),
            column_count=width,
            headers=headers,
            warnings=("MISSING_VALUE",) if any(c.text is None for c in cells) else (),
        )
        section = Section(
            section_id="csv:records",
            kind="records",
            ordinal1=1,
            label="CSV",
            locator=locator,
            tables=[table],
        )
        doc = self._document(
            inp,
            metadata={
                "encoding": encoding,
                "delimiter": delimiter,
                "records": len(records),
                "header": headers if header else [],
                "physicalLines": inp.data.count(b"\n") + inp.data.count(b"\r") - inp.data.count(b"\r\n"),
            },
        )
        doc.sections.append(section)
        if unclosed_quote:
            doc.warnings = merge_warnings(doc.warnings, "UNCLOSED_QUOTE")
        for row in decoded:
            for value in row:
                if value.startswith(_INJECTION_PREFIXES):
                    doc.warnings = merge_warnings(doc.warnings, "CSV_INJECTION_AS_DATA")
                    break
        return doc.finalize()


def _parse_records(
    data: bytes, delimiter: str, limits: MmiLimits, encoding: str, inp: ParseInput
) -> tuple[list[_Record], bool]:
    records: list[_Record] = []
    fields: list[_Field] = []
    field = bytearray()
    field_start = 0
    field_end: int | None = None
    quoted = False
    in_quotes = False
    record_start = 0
    physical_lines = 1
    i = 0
    n = len(data)
    delim = delimiter.encode("ascii")
    while i < n:
        inp.check_deadline()
        b = data[i : i + 1]
        if in_quotes:
            if b == b'"':
                if i + 1 < n and data[i + 1 : i + 2] == b'"':
                    field.extend(b'"')
                    i += 2
                    continue
                in_quotes = False
                field_end = i
                i += 1
                continue
            if b in (b"\n", b"\r"):
                physical_lines += 1
            field.extend(b)
            i += 1
            continue
        if b == b'"' and not field:
            in_quotes = True
            quoted = True
            field_start = i + 1
            i += 1
            continue
        if b == delim:
            fields.append(_Field(bytes(field), field_start, field_end if field_end is not None else i, quoted))
            field = bytearray()
            field_start = i + 1
            field_end = None
            quoted = False
            i += 1
            continue
        if b in (b"\r", b"\n"):
            fields.append(_Field(bytes(field), field_start, field_end if field_end is not None else i, quoted))
            record_end = i
            if b == b"\r" and i + 1 < n and data[i + 1 : i + 2] == b"\n":
                i += 1
            i += 1
            _append_record(records, fields, record_start, record_end, physical_lines, limits)
            fields = []
            field = bytearray()
            field_start = i
            field_end = None
            record_start = i
            quoted = False
            physical_lines += 1
            continue
        field.extend(b)
        if len(field) > limits.max_field_bytes:
            raise MmiError("MAX_INPUT", "CSV field exceeds limit")
        i += 1
    if field or fields:
        fields.append(_Field(bytes(field), field_start, field_end if field_end is not None else n, quoted))
        _append_record(records, fields, record_start, n, physical_lines, limits)
    # A truncated quote must not silently change the record boundary.
    return records, in_quotes


def _append_record(
    records: list[_Record],
    fields: list[_Field],
    start: int,
    end: int,
    physical_lines: int,
    limits: MmiLimits,
) -> None:
    if end - start > limits.max_csv_record_bytes:
        raise MmiError("MAX_INPUT", "CSV record exceeds limit")
    if len(fields) > limits.max_csv_columns:
        raise MmiError("CELL_LIMIT", "CSV column count exceeds limit")
    if len(records) >= limits.max_csv_rows + 1:
        raise MmiError("ROW_LIMIT", "CSV row count exceeds limit")
    for field in fields:
        if len(field.raw) > limits.max_field_bytes:
            raise MmiError("MAX_INPUT", "CSV field exceeds limit")
    records.append(_Record(tuple(fields), start, end, physical_lines))


def _flags(text: str) -> tuple[str, ...]:
    flags: list[str] = []
    if text.startswith(_INJECTION_PREFIXES):
        flags.append("CSV_INJECTION_AS_DATA")
    if text == "":
        flags.append("EMPTY_VALUE")
    return tuple(flags)


def _option_str(inp: ParseInput, key: str) -> str | None:
    value = inp.options.get(key)
    return value if isinstance(value, str) and value else None
