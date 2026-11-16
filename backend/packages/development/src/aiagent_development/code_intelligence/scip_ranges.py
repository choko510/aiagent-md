"""Convert SCIP code-unit positions to byte offsets in verified UTF-8 source."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int


def convert_range(source: bytes, encoding: int, positions: tuple[int, int, int, int]) -> ByteRange:
    """Interpret zero-based positions in UTF-8/16/32 units, returning [start,end).

    Lines are separated by LF; a CR before LF belongs to the newline and cannot
    be addressed. UTF-8 BOMs are rejected because indexer conventions vary.
    """
    if encoding not in (1, 2, 3) or source.startswith(b"\xef\xbb\xbf"):
        raise ValueError("unsupported or ambiguous source encoding")
    source.decode("utf-8", errors="strict")
    lines = source.split(b"\n")
    offsets: list[int] = []
    byte_offset = 0
    for line in lines:
        offsets.append(byte_offset)
        byte_offset += len(line) + 1

    def point(line_number: int, column: int) -> int:
        if line_number < 0 or line_number >= len(lines) or column < 0:
            raise ValueError("position outside source")
        raw = lines[line_number]
        if line_number < len(lines) - 1 and raw.endswith(b"\r"):
            raw = raw[:-1]
        text = raw.decode("utf-8")
        units = 0
        size = 0
        for char in text:
            if units == column:
                return offsets[line_number] + size
            units += (
                len(char.encode("utf-8"))
                if encoding == 1
                else len(char.encode("utf-16-le")) // 2
                if encoding == 2
                else 1
            )
            size += len(char.encode("utf-8"))
            if units > column:
                raise ValueError("position inside encoded character")
        if units == column:
            return offsets[line_number] + size
        raise ValueError("position outside line")

    start_line, start_column, end_line, end_column = positions
    start = point(start_line, start_column)
    end = point(end_line, end_column)
    if end < start:
        raise ValueError("reversed range")
    return ByteRange(start, end)
