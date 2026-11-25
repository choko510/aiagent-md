from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aiagent_development.code_intelligence.indexer_recipe import python_scip_argv, python_scip_recipe_digest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor recipe")


def test_fixed_recipe_is_deterministic_and_does_not_accept_commands(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        first = python_scip_argv(fd, 90)
        second = python_scip_argv(fd, 91)
    finally:
        os.close(fd)
    assert first[0] == "/usr/local/bin/node"
    assert first[1] == "--openssl-config=/dev/null"
    assert first[2] == "/usr/local/lib/scip-python/index.js"
    assert first[5] == str(source) and first[7] == "/proc/self/fd/90/index.scip"
    assert second[7] == "/proc/self/fd/91/index.scip"
    assert python_scip_recipe_digest() == "b14c408748716eed9e3d712af1a1c33e243e50623824d0305e350c35df1a0538"


@pytest.mark.parametrize("invalid", [0, 1, 2, -1, 1_000_001, True, "3"])
def test_fixed_recipe_rejects_invalid_descriptor(invalid: object) -> None:
    with pytest.raises(ValueError):
        python_scip_argv(invalid, 9)  # type: ignore[arg-type]


def test_recipe_rejects_overlapping_and_deleted_descriptors(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="descriptor"):
            python_scip_argv(fd, fd)
        source.rmdir()
        with pytest.raises(ValueError, match="descriptor"):
            python_scip_argv(fd, 90)
    finally:
        os.close(fd)
