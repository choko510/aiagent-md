"""Fixed, internal Python SCIP command shape for a pre-provisioned Cell image.

This module does not verify the image, authorize an execution, install tools,
run a command, or authenticate any resulting Artifact. The trusted integration
must attest the complete image and grant before using this recipe.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

_NODE = "/usr/local/bin/node"
_INDEXER = "/usr/local/lib/scip-python/index.js"
_ENVIRONMENT = "/usr/local/share/aiagent-scip-environment.json"
_SOURCE = "<verified-source-descriptor-path>"
_OUTPUT = "/proc/self/fd/{output_fd}/index.scip"
_TEMPLATE = (
    _NODE,
    "--openssl-config=/dev/null",
    _INDEXER,
    "index",
    "--cwd",
    _SOURCE,
    "--output",
    _OUTPUT,
    "--environment",
    _ENVIRONMENT,
    "--project-name",
    "dev13-workspace",
    "--project-version",
    "1.0",
    "--quiet",
)


def python_scip_recipe_digest() -> str:
    """Digest the complete fixed argv template and environment commitment."""
    payload = {
        "schema": "dev13-python-scip-recipe-v1",
        "argv_template": _TEMPLATE,
        "indexer_version": "0.6.8",
        "environment_sha256": hashlib.sha256(b"[]\n").hexdigest(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


def python_scip_argv(source_fd: int, output_fd: int) -> tuple[str, ...]:
    """Construct fixed argv using only a trusted, inherited source descriptor."""
    if (
        sys.platform != "linux"
        or type(source_fd) is not int
        or type(output_fd) is not int
        or not 3 <= source_fd <= 1_000_000
        or not 3 <= output_fd <= 1_000_000
        or source_fd == output_fd
    ):
        raise ValueError("invalid source descriptor")
    try:
        source = os.fstat(source_fd)
        source_path = os.readlink(f"/proc/self/fd/{source_fd}")
        named = os.stat(source_path, follow_symlinks=False)
        if (
            not source_path.startswith("/")
            or source_path.endswith(" (deleted)")
            or not stat.S_ISDIR(source.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (source.st_dev, source.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError("source descriptor path mismatch")
    except OSError as exc:
        raise ValueError("source descriptor unavailable") from exc
    return tuple(
        source_path if item == _SOURCE else _OUTPUT.format(output_fd=output_fd) if item == _OUTPUT else item
        for item in _TEMPLATE
    )
