from pathlib import Path

from test_code_intelligence_git import reader
from test_code_intelligence_git import repo as _repo  # noqa: F401

from aiagent_development.code_intelligence.inventory import inventory
from aiagent_development.code_intelligence.models import Coverage
from aiagent_development.code_intelligence.search import search


def test_search_is_fixed_and_does_not_expose_text(repo: tuple[Path, str, Path]) -> None:
    snap = reader(repo)
    inv = inventory(snap)
    result = search(snap, inv, "old")
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.line == 1 and hit.path == "a.py" and hit.blob_id == inv.files[0].entry.oid
    assert hit.digest == inv.files[0].digest and hit.identity.commit == repo[1]
    assert hit.excerpt is None
    assert result.coverage == Coverage.TEXT_ONLY
    assert not search(snap, inv, "missing").hits
