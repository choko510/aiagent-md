"""SCIP code-unit offsets are checked against actual UTF-8 byte boundaries."""

import pytest

from aiagent_development.code_intelligence.scip_ranges import convert_range


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
