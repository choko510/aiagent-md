"""Fixed Git operations for a separately isolated, operator-bound provider.

This worker does not grant authority or issue receipts. It must not be exposed
to a model or used from the control process. A Gateway integration must first
provide durable admission, fencing and authenticated receipts.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess  # nosec B404 - fixed argv, in an isolated provider only
import tempfile
from collections.abc import Callable
from pathlib import Path

from .contracts import EffectUnknown, InvalidEvidence, StaleReference, require_sha
from .paths import safe_relative_path

_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_MAX_PATCH = 4 * 1024 * 1024


class IsolatedGitCommitWorker:
    """Apply a canonical patch and atomically update one operator-selected ref.

    The repository must be exclusive to this provider and its filesystem and
    network isolation must be enforced externally. This object deliberately is
    not a GitProviderPort: a Git object or a successful CAS is not a receipt.
    """

    def __init__(self, repository: Path, git: Path, branch: str) -> None:
        if (
            not repository.is_absolute()
            or repository.is_symlink()
            or repository.resolve(strict=True) != repository
            or not repository.is_dir()
            or not git.is_absolute()
            or not git.is_file()
            or not _BRANCH.fullmatch(branch)
            or branch in {"main", "master", "HEAD"}
            or any(p in {"", ".", ".."} or p.startswith(".") or p.endswith(".lock") for p in branch.split("/"))
            or ".." in branch
        ):
            raise ValueError("invalid isolated provider binding")
        self._repository, self._git, self._ref = repository, git, "refs/heads/" + branch

    def _git_call(self, home: str, index: str, *args: str, data: bytes = b"") -> bytes:
        env = {
            "PATH": os.defpath,
            "HOME": home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_INDEX_FILE": index,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_AUTHOR_NAME": "Agent Provider",
            "GIT_AUTHOR_EMAIL": "agent@example.invalid",
            "GIT_COMMITTER_NAME": "Agent Provider",
            "GIT_COMMITTER_EMAIL": "agent@example.invalid",
            "LC_ALL": "C",
        }
        try:
            result = subprocess.run(  # noqa: S603  # fixed git operations; isolated provider only
                [
                    str(self._git),
                    "-C",
                    str(self._repository),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.attributesFile=/dev/null",
                    "-c",
                    "color.ui=false",
                    "-c",
                    "protocol.allow=never",
                    *args,
                ],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidEvidence("isolated Git operation unavailable") from exc
        if result.returncode:
            raise InvalidEvidence("isolated Git operation rejected")
        return result.stdout

    def commit(
        self,
        parent: str,
        patch: bytes,
        digest: str,
        paths: tuple[str, ...],
        message: str,
        *,
        before_cas: Callable[[str, str], None] | None = None,
    ) -> str:
        require_sha(parent)
        if (
            len(parent) != 40
            or not isinstance(patch, bytes)
            or not 0 < len(patch) <= _MAX_PATCH
            or hashlib.sha256(patch).hexdigest() != digest
            or not paths
            or len(paths) > 512
            or len(set(paths)) != len(paths)
            or any(safe_relative_path(path) != path for path in paths)
            or not message.strip()
            or len(message.encode()) > 256
            or "\x00" in message
        ):
            raise InvalidEvidence("invalid approved commit request")
        with tempfile.TemporaryDirectory(prefix="git-provider-") as home:
            index = str(Path(home) / "index")

            def run(*args: str, data: bytes = b"") -> bytes:
                return self._git_call(home, index, *args, data=data)

            if run("rev-parse", "--verify", self._ref).strip().decode("ascii") != parent:
                raise StaleReference("branch moved before commit")
            run("read-tree", parent)
            run("apply", "--cached", "--binary", "--whitespace=nowarn", data=patch)
            changed = run(
                "diff", "--cached", "--name-only", "-z", "--no-renames", "--no-textconv", "--no-ext-diff", parent, "--"
            )
            try:
                actual_paths = tuple(p.decode("utf-8") for p in changed.rstrip(b"\0").split(b"\0"))
            except UnicodeDecodeError as exc:
                raise InvalidEvidence("non UTF-8 path") from exc
            if set(actual_paths) != set(paths):
                raise InvalidEvidence("patch paths do not match approval")
            entries = run("ls-files", "--stage", "-z")
            for entry in entries.split(b"\0"):
                if entry and entry.split(b" ", 1)[0] not in {b"100644", b"100755"}:
                    raise InvalidEvidence("unsupported source mode")
            generated = run(
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                parent,
                "--",
            )
            if generated != patch:
                raise InvalidEvidence("patch does not match canonical Git diff")
            tree = run("write-tree").strip().decode("ascii")
            commit = (
                run("-c", "commit.gpgsign=false", "commit-tree", tree, "-p", parent, data=(message + "\n").encode())
                .strip()
                .decode("ascii")
            )
            require_sha(commit)
            if run("show", "-s", "--format=%P %T", commit).strip().decode("ascii") != f"{parent} {tree}":
                raise InvalidEvidence("created commit mismatch")
            if before_cas is not None:
                before_cas(commit, tree)
            try:
                run("update-ref", self._ref, commit, parent)
            except InvalidEvidence as exc:
                # The child may have performed the update before a timeout.
                raise EffectUnknown("Git ref update requires reconciliation") from exc
            return commit
