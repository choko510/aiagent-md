"""A1, range and normalized-bbox helpers shared by parsers and verifiers."""

from __future__ import annotations

import re
from typing import Final

_A1 = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]{0,6})$")
_RANGE = re.compile(r"^([A-Za-z]{1,3}[1-9][0-9]{0,6})(?::([A-Za-z]{1,3}[1-9][0-9]{0,6}))?$")
MAX_COLUMN: Final[int] = 16_384


def col_to_letters(column: int) -> str:
    if not 1 <= column <= MAX_COLUMN:
        raise ValueError("column out of range")
    letters = ""
    n = column
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def letters_to_col(letters: str) -> int:
    value = 0
    for char in letters.upper():
        if not "A" <= char <= "Z":
            raise ValueError("invalid column letters")
        value = value * 26 + (ord(char) - 64)
    if not 1 <= value <= MAX_COLUMN:
        raise ValueError("column out of range")
    return value


def a1_ref(row: int, column: int) -> str:
    if row < 1:
        raise ValueError("row must be 1-based")
    return f"{col_to_letters(column)}{row}"


def parse_a1(ref: str) -> tuple[int, int]:
    match = _A1.match(ref.strip())
    if not match:
        raise ValueError(f"invalid A1 reference: {ref!r}")
    return int(match.group(2)), letters_to_col(match.group(1))


def normalize_a1(ref: str) -> str:
    row, column = parse_a1(ref)
    return a1_ref(row, column)


def parse_range(ref: str) -> tuple[tuple[int, int], tuple[int, int]]:
    match = _RANGE.match(ref.strip())
    if not match:
        raise ValueError(f"invalid A1 range: {ref!r}")
    start = parse_a1(match.group(1))
    end = parse_a1(match.group(2) or match.group(1))
    return start, end


def range_cells(ref: str) -> list[str]:
    (r1, c1), (r2, c2) = parse_range(ref)
    if r1 > r2:
        r1, r2 = r2, r1
    if c1 > c2:
        c1, c2 = c2, c1
    return [a1_ref(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def range_bounds(ref: str) -> tuple[int, int, int, int]:
    (r1, c1), (r2, c2) = parse_range(ref)
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def normalized_bbox(
    x0: float, y0: float, x1: float, y1: float, width: float, height: float
) -> tuple[float, float, float, float]:
    if width <= 0 or height <= 0:
        raise ValueError("page dimensions must be positive")
    xa, xb = sorted((max(0.0, x0) / width, max(0.0, x1) / width))
    ya, yb = sorted((max(0.0, y0) / height, max(0.0, y1) / height))
    return (min(1.0, xa), min(1.0, ya), min(1.0, xb), min(1.0, yb))
