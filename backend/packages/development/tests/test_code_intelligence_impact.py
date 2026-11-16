import subprocess
from pathlib import Path

from test_code_intelligence_git import (
    reader,
)
from test_code_intelligence_git import repo as _repo  # noqa: F401

from aiagent_development.code_intelligence.impact import impact
from aiagent_development.code_intelligence.inventory import inventory


def test_impact_import_and_tests(repo: tuple[Path, str, Path]) -> None:
    root, _, git = repo
    (root / "consumer.py").write_text("import a\n")
    (root / "test_a.py").write_text("def test_a(): pass\n")
    subprocess.run([str(git), "-C", str(root), "add", "."], check=True)
    subprocess.run([str(git), "-C", str(root), "commit", "-m", "second"], check=True, stdout=subprocess.DEVNULL)
    sha = subprocess.check_output([str(git), "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    snap = reader((root, sha, git))
    result = impact(snap, inventory(snap), ("a.py",))
    mapping = {c.path: c.reasons for c in result.candidates}
    assert "direct change" in mapping["a.py"]
    assert "Python static import name candidate" in mapping["consumer.py"]
    assert "test filename heuristic" in mapping["test_a.py"]
