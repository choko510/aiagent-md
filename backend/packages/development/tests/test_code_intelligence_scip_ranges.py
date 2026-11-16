"""SCIP code-unit offsets are checked against actual UTF-8 byte boundaries."""

import time
import tracemalloc

import pytest

from aiagent_development.code_intelligence import scip_ranges
from aiagent_development.code_intelligence.scip_ranges import PositionBudgetExceeded, PositionIndex, convert_range


@pytest.mark.parametrize("encoding,end", [(1, 8), (2, 4), (3, 3)])
def test_unicode_offsets(encoding: int, end: int) -> None:
    source = "a日😀x\r\nlast\n".encode()
    span = convert_range(source, encoding, (0, 1, 0, end))
    assert source[span.start : span.end].decode() == "日😀"
    whole = convert_range(source, encoding, (0, 0, 1, 4))
    assert source[whole.start : whole.end].decode() == "a日😀x\r\nlast"


@pytest.mark.parametrize("encoding,column", [(1, 2), (2, 3)])
def test_reject_mid_character(encoding: int, column: int) -> None:
    with pytest.raises(ValueError):
        convert_range("a日😀x".encode(), encoding, (0, column, 0, column))


def test_reject_ambiguity_and_invalid_positions() -> None:
    for source, encoding, pos in [
        (b"\xef\xbb\xbfx", 1, (0, 0, 0, 1)),
        (b"a\n", 0, (0, 0, 0, 1)),
        (b"a\r\nb", 1, (0, 2, 0, 2)),
        (b"a", 1, (0, 1, 0, 0)),
    ]:
        with pytest.raises(ValueError):
            convert_range(source, encoding, pos)


def test_combining_characters_are_not_silently_normalized() -> None:
    source = "e\u0301\n".encode("utf-8")
    span = convert_range(source, 3, (0, 0, 0, 2))
    assert source[span.start : span.end] == source[:-1]
    assert convert_range(source, 3, (0, 1, 0, 2)).start == 1


def test_long_line_many_ranges_reuse_index_and_bound_memory() -> None:
    source = ("日😀" * 100000 + "\r\nend\n").encode()
    tracemalloc.start()
    started = time.monotonic()
    index = PositionIndex(source, deadline=started + 15)
    after_build = tracemalloc.get_traced_memory()[0]
    for _ in range(10000):
        span = index.convert(2, (0, 1, 0, 3))
        assert source[span.start : span.end] == "😀".encode()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert time.monotonic() - started < 15
    assert current - after_build < 128 * 1024
    assert peak < 16 * 1024 * 1024 + 512 * 1024


def test_position_index_memory_and_mid_conversion_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PositionBudgetExceeded, match="memory"):
        PositionIndex(b"x" * 100000, deadline=float("inf"), max_index_bytes=32768)
    calls = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(scip_ranges.time, "monotonic", lambda: next(calls, 2.0))
    with pytest.raises(PositionBudgetExceeded, match="time"):
        PositionIndex(b"x" * 100000, deadline=1.0)


def test_huge_source_respects_explicit_position_memory_limit() -> None:
    source = b"x" * (4 * 1024 * 1024)
    tracemalloc.start()
    with pytest.raises(PositionBudgetExceeded, match="memory"):
        PositionIndex(source, deadline=time.monotonic() + 20, max_index_bytes=1024 * 1024)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
