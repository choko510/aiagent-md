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
from pathlib import Path

from aiagent_domain import sha256_hex

_NODE = "/usr/local/bin/node"
_INDEXER = "/usr/local/lib/scip-python/index.js"
_ENVIRONMENT = "/usr/local/share/aiagent-scip-environment.json"
_SOURCE = "<verified-source-descriptor-path>"
_OUTPUT = "/proc/self/fd/{output_fd}/index.scip"
PYTHON_SCIP_GRANT_ARGV = ("aiagent:dev13:scip-python:v1",)
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


def _check_readonly_directory(path: Path) -> None:
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts[1:]):
        raise ValueError("invalid toolchain directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if not os.fstatvfs(fd).f_flag & os.ST_RDONLY:
                raise ValueError("toolchain directory is writable")
    finally:
        os.close(fd)


def _measure_readonly_file(path: Path, max_size: int, *, executable: bool = False) -> tuple[str, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 <= before.st_size <= max_size
            or not os.fstatvfs(fd).f_flag & os.ST_RDONLY
            or (executable and (before.st_uid != 0 or not before.st_mode & 0o111))
        ):
            raise ValueError("invalid toolchain file")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            block = os.read(fd, min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("incomplete toolchain file")
            digest.update(block)
            remaining -= len(block)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("toolchain changed during measurement")
        return digest.hexdigest(), before.st_size
    finally:
        os.close(fd)


def measure_command_toolchain(executable: str) -> str:
    """Measure one approved executable; does not attest dependencies or the image."""
    path = Path(executable)
    _check_readonly_directory(path.parent)
    digest, size = _measure_readonly_file(path, 256 * 1024 * 1024, executable=True)
    return sha256_hex({"version": "dev-command-binary-v1", "path": executable, "sha256": digest, "size": size})


def measure_python_scip_toolchain() -> str:
    """Measure the Node binary, SCIP package files and fixed environment.

    This identifies locally visible bytes; it is not image attestation and does
    not measure the kernel or dynamic libraries. The caller must supply a
    read-only trusted image and compare the result with the approved recipe.
    """

    root = Path(_INDEXER).parent
    for parent in (Path(_NODE).parent, Path(_ENVIRONMENT).parent, root):
        _check_readonly_directory(parent)
    node_sha, _ = _measure_readonly_file(Path(_NODE), 256 * 1024 * 1024)
    env_sha, env_size = _measure_readonly_file(Path(_ENVIRONMENT), 1024)
    if env_size != 3 or env_sha != hashlib.sha256(b"[]\n").hexdigest():
        raise ValueError("indexer environment mismatch")
    files: list[dict[str, object]] = []
    total = 0

    def walk_error(error: OSError) -> None:
        raise error

    for directory, names, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        names.sort()
        filenames.sort()
        base = Path(directory)
        if not os.statvfs(base).f_flag & os.ST_RDONLY:
            raise ValueError("toolchain directory is writable")
        if any(not stat.S_ISDIR(os.lstat(base / name).st_mode) for name in names):
            raise ValueError("invalid toolchain directory")
        for name in filenames:
            path = base / name
            sha, size = _measure_readonly_file(path, 64 * 1024 * 1024)
            total += size
            if len(files) >= 10000 or total > 128 * 1024 * 1024:
                raise ValueError("toolchain measurement budget exceeded")
            files.append({"path": path.relative_to(root).as_posix(), "sha256": sha, "size": size})
    if not files or not any(item["path"] == "index.js" for item in files):
        raise ValueError("indexer package missing")
    return sha256_hex(
        {"version": "dev13-scip-toolchain-v1", "node_sha256": node_sha, "environment_sha256": env_sha, "files": files}
    )


def approved_recipe_digest(
    *,
    kind: str,
    test_id: str | None,
    plan_sha256: str,
    request_sha256: str,
    template_sha256: str,
    toolchain_sha256: str,
    image_digest: str,
) -> str:
    """Commit to the approved Tool request and immutable execution inputs."""
    return sha256_hex(
        {
            "version": "dev13-approved-recipe-v1",
            "kind": kind,
            "test_id": test_id,
            "plan_sha256": plan_sha256,
            "request_sha256": request_sha256,
            "template_sha256": template_sha256,
            "toolchain_sha256": toolchain_sha256,
            "image_digest": image_digest,
            "environment_policy": "sandbox-scrubbed-environment-v1",
        }
    )


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
