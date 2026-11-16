"""Read-only Git path validation does not access the host filesystem."""

import json

import pytest

from aiagent_development.code_intelligence.paths import display_path, validate_git_path


@pytest.mark.parametrize("path", ["src/hello world.py", "src/日本語.py", "src/line\nbreak.py", "-option.py"])
def test_valid_git_paths(path: str) -> None:
    assert validate_git_path(path) == path


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../secret", "a/../b", ".git/config", "a/.GIT/x", "a\\b", "a//b", "a\x00b", "C:/secret"]
)
def test_reject_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        validate_git_path(path)


def test_display_path_escapes_controls_without_changing_original() -> None:
    original = "code\n\x1b[31m\u2028.py"
    displayed = display_path(original)
    assert json.loads(displayed) == original
    assert all(ord(char) >= 32 for char in displayed)
    assert "\\n" in displayed and "\\u001b" in displayed and "\\u2028" in displayed
