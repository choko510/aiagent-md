"""Bounded, reusable mapping from SCIP code units to UTF-8 byte offsets."""

from __future__ import annotations

import codecs
import sys
import time
from array import array
from bisect import bisect_left
from dataclasses import dataclass

from .models import ReadError


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int


class PositionBudgetExceeded(ReadError):
    """Position index exceeded its time or memory budget."""


class PositionIndex:
    """One UTF-8 pass per document; lookups use bounded arrays and binary search.

    The index payload has a 16 MiB default ceiling. It does not include the
    caller-owned source bytes or the decoder's bounded 16 KiB temporary chunk.
    """

    def __init__(self, source: bytes, *, deadline: float, max_index_bytes: int = 16 * 1024 * 1024):
        if max_index_bytes <= 0:
            raise ValueError("position memory limit must be positive")
        if source.startswith(b"\xef\xbb\xbf"):
            raise ValueError("ambiguous UTF-8 BOM")
        self.deadline = deadline
        self.byte_offsets = array("I", [0])
        self.utf16_offsets = array("I", [0])
        self.line_starts = array("I", [0])
        self.line_ends = array("I")
        if self.byte_offsets.itemsize != 4 or len(source) >= 2**32:
            raise PositionBudgetExceeded("position index unsupported")
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        last = ""
        count = 0
        for begin in range(0, len(source), 16384):
            self._check_deadline()
            text = decoder.decode(source[begin : begin + 16384], final=False)
            for char in text:
                if count % 1024 == 0:
                    self._check_deadline()
                    self._check_memory(max_index_bytes)
                if char == "\n":
                    self.line_ends.append(count - 1 if last == "\r" else count)
                    self.line_starts.append(count + 1)
                point = ord(char)
                width = 1 if point < 0x80 else 2 if point < 0x800 else 3 if point < 0x10000 else 4
                self.byte_offsets.append(self.byte_offsets[-1] + width)
                self.utf16_offsets.append(self.utf16_offsets[-1] + (2 if point >= 0x10000 else 1))
                count += 1
                last = char
        decoder.decode(b"", final=True)
        self.line_ends.append(count)
        self._check_memory(max_index_bytes)
        self._check_deadline()

    def _check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise PositionBudgetExceeded("position time budget exceeded")

    def _check_memory(self, limit: int) -> None:
        arrays = (self.byte_offsets, self.utf16_offsets, self.line_starts, self.line_ends)
        # Five bytes per 4-byte element reserves space for CPython's array
        # over-allocation and for the next 1024 elements between checks.
        reserved = sum(len(item) * 5 + 128 for item in arrays) + 1024 * 20
        if reserved > limit or sum(sys.getsizeof(item) for item in arrays) > limit:
            raise PositionBudgetExceeded("position memory budget exceeded")

    def point(self, line: int, column: int, encoding: int) -> int:
        self._check_deadline()
        if encoding not in (1, 2, 3) or line < 0 or line >= len(self.line_starts) or column < 0:
            raise ValueError("invalid position or encoding")
        start, end = self.line_starts[line], self.line_ends[line]
        if encoding == 3:
            position = start + column
        else:
            offsets = self.byte_offsets if encoding == 1 else self.utf16_offsets
            target = offsets[start] + column
            position = bisect_left(offsets, target, start, end + 1)
            if position > end or offsets[position] != target:
                raise ValueError("position outside line or inside encoded character")
        if position > end:
            raise ValueError("position outside line")
        return int(self.byte_offsets[position])

    def convert(self, encoding: int, positions: tuple[int, int, int, int]) -> ByteRange:
        start_line, start_column, end_line, end_column = positions
        start = self.point(start_line, start_column, encoding)
        end = self.point(end_line, end_column, encoding)
        if end < start:
            raise ValueError("reversed range")
        return ByteRange(start, end)


def convert_range(source: bytes, encoding: int, positions: tuple[int, int, int, int]) -> ByteRange:
    """Compatibility helper; repeated conversions should reuse PositionIndex."""
    return PositionIndex(source, deadline=time.monotonic() + 30).convert(encoding, positions)
