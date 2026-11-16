"""Bounded read-only Git object access. Requires a stable, isolated local repository.

The trusted caller supplies the repository, executable and authorization. This library
is not an authorization boundary and must run inside the untrusted execution cell.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .models import AuthorizedSnapshot, Coverage, Entry, Limits, ReadError
from .paths import validate_git_path

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class GitSnapshot:
    def __init__(self, snapshot: AuthorizedSnapshot, git_executable: Path, limits: Limits | None = None):
        limits = limits or Limits()
        identity = snapshot.identity
        if not all((identity.tenant_id, identity.owner_id, identity.binding_id, identity.revision)):
            raise ReadError("missing trusted scope")
        if not _SHA.fullmatch(identity.commit):
            raise ReadError("invalid full commit SHA")
        self.snapshot = snapshot
        self.limits = limits
        self.deadline = time.monotonic() + limits.total_seconds
        self.bytes_read = 0
        # Verified immutable blob bytes, bounded by total_bytes and scoped to this reader.
        self._blob_cache: dict[str, bytes] = {}
        self._entries: dict[str, Entry] | None = None
        self._issues: tuple[str, ...] = ()
        self.git = git_executable
        self.root = snapshot.repository
        if not self.git.is_absolute() or not self.git.is_file():
            raise ReadError("Git executable must be a trusted absolute path")
        if not self.root.is_absolute() or self.root.is_symlink() or not self.root.is_dir():
            raise ReadError("repository must be a trusted real directory")
        self.gitdir = self.root / ".git"
        if self.gitdir.is_symlink() or not self.gitdir.is_dir():
            raise ReadError("only ordinary local repositories are supported")
        self._check_metadata()
        version = self._git("--version")
        if not version.startswith(b"git version "):
            raise ReadError("unable to determine Git version")
        config = self._git("config", "--local", "--no-includes", "--null", "--list")
        for record in config.split(b"\0"):
            key = record.partition(b"\n")[0].lower()
            if (
                key.startswith((b"include.", b"includeif."))
                or key in {b"extensions.partialclone", b"extensions.worktreeconfig"}
                or (key.startswith(b"remote.") and key.endswith(b".promisor"))
            ):
                raise ReadError("unsupported partial clone or configuration include")
        if self._git("cat-file", "-t", identity.commit).strip() != b"commit":
            raise ReadError("object is not a commit")
        commit = self._git("cat-file", "commit", identity.commit)
        self._verify_object("commit", identity.commit, commit)
        first = commit.partition(b"\n")[0]
        if not first.startswith(b"tree ") or not _SHA.fullmatch(first[5:].decode("ascii", "ignore")):
            raise ReadError("invalid commit tree")
        self.tree = first[5:].decode("ascii")
        self._verify_object("tree", self.tree, self._git("cat-file", "tree", self.tree))

    @staticmethod
    def _verify_object(kind: str, oid: str, data: bytes) -> None:
        algorithm = "sha1" if len(oid) == 40 else "sha256"
        digest = hashlib.new(algorithm, usedforsecurity=False)
        digest.update(f"{kind} {len(data)}\0".encode("ascii"))
        digest.update(data)
        if digest.hexdigest() != oid:
            raise ReadError("Git object digest mismatch")

    def _check_metadata(self) -> None:
        # Refuse indirection, object-store overlays and promisor packs before any
        # command that could resolve an object. Repository must not mutate concurrently.
        objects = self.gitdir / "objects"
        if (self.gitdir / "config.worktree").exists() or (self.gitdir / "config.worktree").is_symlink():
            raise ReadError("worktree configuration unsupported")
        for path in (self.gitdir / "config", objects, objects / "info", objects / "pack"):
            if path.is_symlink():
                raise ReadError("symlinked Git metadata")
        if not objects.is_dir():
            raise ReadError("missing object store")
        if (objects / "info" / "alternates").exists() or (objects / "info" / "http-alternates").exists():
            raise ReadError("alternate object stores unsupported")
        pack = objects / "pack"
        if pack.is_dir():
            for path in pack.iterdir():
                if path.is_symlink() or path.suffix == ".promisor":
                    raise ReadError("promisor or symlinked pack unsupported")
        count = 0

        def walk_error(_error: OSError) -> None:
            raise ReadError("object metadata unavailable")

        for parent, dirs, files in os.walk(objects, followlinks=False, onerror=walk_error):
            for name in (*dirs, *files):
                count += 1
                if time.monotonic() > self.deadline:
                    raise ReadError("metadata inspection timeout")
                if count > 200000 or (Path(parent) / name).is_symlink():
                    raise ReadError("unsafe or oversized object store")

    def _git(self, *args: str) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReadError("overall time budget exceeded")
        env = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
        }
        try:
            proc = subprocess.Popen(  # noqa: S603
                [str(self.git), "-C", str(self.root), "-c", "protocol.allow=never", *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise ReadError("Git process failed to start") from exc
        chunks: list[bytes] = []
        stream = proc.stdout
        assert stream is not None

        def consume() -> None:
            chunks.append(stream.read(self.limits.process_bytes + 1))

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        thread.join(min(remaining, self.limits.process_seconds))
        if thread.is_alive():
            self._stop(proc)
            thread.join(timeout=1)
            raise ReadError("Git process timeout")
        data = chunks[0]
        if len(data) > self.limits.process_bytes:
            self._stop(proc)
            raise ReadError("Git output limit exceeded")
        try:
            status = proc.wait(timeout=min(max(self.deadline - time.monotonic(), 0.001), self.limits.process_seconds))
        except subprocess.TimeoutExpired as exc:
            self._stop(proc)
            raise ReadError("Git process timeout") from exc
        if status:
            raise ReadError("Git object read failed")
        return data

    @staticmethod
    def _stop(proc: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()

    def entries(self) -> tuple[tuple[Entry, ...], tuple[str, ...]]:  # noqa: PLR0912
        data = self._git("ls-tree", "-r", "-t", "-z", "-l", "--full-tree", self.tree)
        # Check each child tree before using any path/blob relationship.
        tree_count = 0
        for record in data.split(b"\0"):
            if not record:
                continue
            fields = record.partition(b"\t")[0].split()
            if len(fields) != 4:
                raise ReadError("invalid tree listing")
            if fields[1] == b"tree":
                tree_count += 1
                if tree_count > self.limits.files:
                    raise ReadError("tree count limit exceeded")
                tree_oid = fields[2].decode("ascii", "strict")
                if not _SHA.fullmatch(tree_oid):
                    raise ReadError("invalid tree object ID")
                self._verify_object("tree", tree_oid, self._git("cat-file", "tree", tree_oid))
        result: list[Entry] = []
        issues: list[str] = []
        records = data.split(b"\0")
        for record in records:
            if not record:
                continue
            if len(result) >= self.limits.files:
                issues.append("file count limit exceeded")
                break
            try:
                head, raw_path = record.split(b"\t", 1)
                mode, obj_type, oid, size = head.split()
                if obj_type == b"tree":
                    continue
                path = validate_git_path(raw_path.decode("utf-8"))
                mode_s, oid_s = mode.decode("ascii"), oid.decode("ascii")
                if not _SHA.fullmatch(oid_s):
                    raise ValueError("invalid oid")
                kind = "SUBMODULE" if mode_s == "160000" else "SYMLINK" if mode_s == "120000" else "BLOB"
                if (kind == "SUBMODULE" and obj_type != b"commit") or (kind != "SUBMODULE" and obj_type != b"blob"):
                    raise ValueError("invalid object type")
                result.append(Entry(path, mode_s, oid_s, None if size == b"-" else int(size), kind))
            except (ValueError, UnicodeError):
                issues.append("invalid or unsupported tree entry")
        ordered = tuple(sorted(result, key=lambda e: e.path))
        self._entries = {entry.path: entry for entry in ordered}
        self._issues = tuple(issues)
        return ordered, self._issues

    def read(self, entry: Entry) -> bytes:
        if (
            self._entries is None
            or self._entries.get(entry.path) != entry
            or entry.kind != "BLOB"
            or entry.size is None
        ):
            raise ReadError("entry is not a readable tracked blob")
        if time.monotonic() > self.deadline:
            raise ReadError("overall time budget exceeded")
        cached = self._blob_cache.get(entry.oid)
        if cached is not None:
            if len(cached) != entry.size:
                raise ReadError("blob size mismatch")
            return cached
        if entry.size > self.limits.file_bytes or self.bytes_read + entry.size > self.limits.total_bytes:
            raise ReadError("blob byte budget exceeded")
        data = self._git("cat-file", "blob", entry.oid)
        if len(data) != entry.size:
            raise ReadError("blob size mismatch")
        self._verify_object("blob", entry.oid, data)
        self.bytes_read += len(data)
        self._blob_cache[entry.oid] = data
        return data


@dataclass(frozen=True)
class SnapshotOpenResult:
    reader: GitSnapshot | None
    coverage: Coverage
    issue: str | None


def open_snapshot(
    snapshot: AuthorizedSnapshot, git_executable: Path, limits: Limits | None = None
) -> SnapshotOpenResult:
    """Convert startup failures to an explicit unavailable status."""
    try:
        return SnapshotOpenResult(GitSnapshot(snapshot, git_executable, limits), Coverage.COMPLETE, None)
    except ReadError as exc:
        return SnapshotOpenResult(None, Coverage.UNAVAILABLE, str(exc))
    except (OSError, UnicodeError):
        return SnapshotOpenResult(None, Coverage.UNAVAILABLE, "repository metadata unavailable")
