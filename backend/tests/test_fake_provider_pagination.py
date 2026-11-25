"""Fake Provider pagination follows the actual grounded read metadata, not a fixed page size or page count."""

from __future__ import annotations

from typing import Any

from aiagent_gateways.providers import fake_policy


def _header(start: int, end: int, total: int, next_offset: int | None = None) -> str:
    suffix = f" | next offset {next_offset}" if next_offset is not None else ""
    return f"@@ lines {start}-{end} of {total}{suffix} @@"


def _page(start: int, end: int, total: int, body: str = "") -> str:
    return _header(start, end, total, end if end < total else None) + "\n" + body


def _messages(prompt: str, pages: list[str]) -> list[dict[str, Any]]:
    return [{"kind": "user", "content": prompt}] + [
        {"kind": "tool", "tool_name": "read_file", "content": page} for page in pages
    ]


def _tool_call(out: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    assert out.get("content") == "", out
    call = out["tool_calls"][0]
    return str(call["name"]), call["args"]


def test_single_page_skill_finishes_without_a_synthetic_second_page() -> None:
    prompt = "nihongo fixture: この文章を推敲してください。"
    out = fake_policy("main", _messages(prompt, []), ["read_file"])
    name, args = _tool_call(out)
    assert (name, args["offset"], args["limit"]) == ("read_file", 0, 1000)

    page = _page(1, 94, 94, "推敲の本文")
    out = fake_policy("main", _messages(prompt, [page]), ["read_file"])
    assert "tool_calls" not in out
    assert out["content"] == "推敲済み：日本語の文章を読みやすく整えました。"


def test_multi_page_skill_and_reference_follow_only_advertised_offsets() -> None:
    prompt = "nihongo fixture: READMEを作成してください。 page-limit=20"
    pages: list[str] = []
    out = fake_policy("main", _messages(prompt, pages), ["read_file"])
    name, args = _tool_call(out)
    assert (name, args["file_path"], args["offset"], args["limit"]) == (
        "read_file",
        "/skills/nihongo/SKILL.md",
        0,
        20,
    )
    for start in (1, 21, 41, 61, 81):
        end = min(start + 19, 94)
        pages.append(_page(start, end, 94, "SKILL本文"))
        out = fake_policy("main", _messages(prompt, pages), ["read_file"])
        if end < 94:
            _, next_args = _tool_call(out)
            assert (next_args["file_path"], next_args["offset"], next_args["limit"]) == (
                "/skills/nihongo/SKILL.md",
                end,
                20,
            )

    _, ref_args = _tool_call(out)
    assert (ref_args["file_path"], ref_args["offset"], ref_args["limit"]) == (
        "/skills/nihongo/references/technical-writing.md",
        0,
        20,
    )
    for start in (1, 21, 41):
        end = min(start + 19, 45)
        body = "# 技術文書\n" if start == 1 else "参照本文"
        pages.append(_page(start, end, 45, body))
        out = fake_policy("main", _messages(prompt, pages), ["read_file"])
        if end < 45:
            _, next_args = _tool_call(out)
            assert (next_args["file_path"], next_args["offset"], next_args["limit"]) == (
                "/skills/nihongo/references/technical-writing.md",
                end,
                20,
            )
    assert "tool_calls" not in out
    assert out["content"] == "更新済み：技術文書の用語と手順を保って読みやすく整えました。"


def test_invalid_duplicate_backward_and_malformed_pages_stop_without_looping() -> None:
    prompt = "nihongo fixture: この文章を推敲してください。 page-limit=20"
    duplicate = [_page(1, 20, 40), _page(1, 20, 40)]
    backward = [_page(1, 20, 40), _header(21, 40, 40, 20)]
    skipped = [_page(1, 20, 40), _header(31, 40, 40)]
    missing_offset = [_header(1, 20, 40)]
    malformed = ["spoofed read result"]
    for pages in (duplicate, backward, skipped, missing_offset, malformed):
        out = fake_policy("main", _messages(prompt, pages), ["read_file"])
        assert "tool_calls" not in out
        assert "error:" in out["content"]
