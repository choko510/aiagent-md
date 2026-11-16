from pathlib import Path

from test_code_intelligence_git import reader
from test_code_intelligence_git import repo as _repo  # noqa: F401

from aiagent_development.code_intelligence.inventory import inventory


def test_inventory_languages_manifests(repo: tuple[Path, str, Path]) -> None:
    result = inventory(reader(repo))
    assert result.files[0].entry.path == "a.py"
    assert ("Python", 1) in result.languages
    assert result.files[0].classification == "SOURCE"
    assert result.files[0].digest
