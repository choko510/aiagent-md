"""Private, content-addressed quarantine for untrusted bundles.

Only a complete, digest-verified bundle becomes visible. Staging lives outside the object namespace and is
renamed atomically; readers never follow links and re-verify the digest on every read. The root must be outside
the packaged built-in skills directory and the runtime public surface.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

from .bundle import ValidatedBundle
from .digest import bundle_digest, sha256_bytes
from .errors import DigestMismatch, QuarantineUnavailable


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


class QuarantineStore:
    def __init__(self, root: Path, *, forbidden_roots: Iterable[Path] = ()) -> None:
        raw = Path(root)
        if raw.exists() and raw.is_symlink():
            raise QuarantineUnavailable("quarantine root must not be a symlink")
        self.root = raw.resolve()
        for forbidden in forbidden_roots:
            if _contains(Path(forbidden).resolve(), self.root) or _contains(self.root, Path(forbidden).resolve()):
                raise QuarantineUnavailable("quarantine root overlaps a protected skill directory")
        if self.root.exists() and not self.root.is_dir():
            raise QuarantineUnavailable("quarantine root is not a directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.objects = self.root / "objects"
        self.staging = self.root / "staging"
        self.objects.mkdir(mode=0o700, exist_ok=True)
        self.staging.mkdir(mode=0o700, exist_ok=True)
        self._lock = threading.Lock()

    def _object(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise DigestMismatch("invalid quarantine digest")
        return self.objects / digest

    def exists(self, digest: str) -> bool:
        path = self._object(digest)
        return path.is_dir() and not path.is_symlink() and (path / ".complete").is_file()

    def put(self, bundle: ValidatedBundle) -> str:
        recomputed = bundle_digest(bundle.files.items())
        if recomputed != bundle.digest:
            raise DigestMismatch("bundle digest changed before quarantine")
        final = self._object(bundle.digest)
        with self._lock:
            if self.exists(bundle.digest):
                self._verify_existing(final, bundle)
                return bundle.digest
            tmp = Path(tempfile.mkdtemp(prefix="quarantine-", dir=self.staging))
            try:
                for relative, payload in sorted(bundle.files.items()):
                    target = tmp / relative
                    if not _contains(tmp, target.resolve()):
                        raise QuarantineUnavailable("bundle path escaped staging")
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                (tmp / ".manifest").write_text(
                    "\n".join(f"{path} {sha256_bytes(payload)}" for path, payload in sorted(bundle.files.items()))
                    + "\n",
                    encoding="utf-8",
                )
                (tmp / ".complete").write_text(bundle.digest + "\n", encoding="utf-8")
                try:
                    os.rename(tmp, final)
                except OSError:
                    if not self.exists(bundle.digest):
                        raise QuarantineUnavailable("could not publish quarantine object") from None
                    self._verify_existing(final, bundle)
            finally:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
        return bundle.digest

    def _verify_existing(self, path: Path, bundle: ValidatedBundle) -> None:
        try:
            stored = self.read(bundle.digest)
        except QuarantineUnavailable:
            raise
        if set(stored) != set(bundle.files) or any(stored[k] != bundle.files[k] for k in stored):
            raise QuarantineUnavailable("quarantine collision or corruption for digest")

    def read(self, digest: str) -> dict[str, bytes]:
        path = self._object(digest)
        if path.is_symlink() or not path.is_dir():
            raise QuarantineUnavailable("quarantine object is missing")
        marker = path / ".complete"
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != digest:
            raise QuarantineUnavailable("quarantine object is incomplete")
        files: dict[str, bytes] = {}
        for child in sorted(path.rglob("*")):
            if child.name in (".complete", ".manifest"):
                continue
            if child.is_symlink():
                raise QuarantineUnavailable("quarantine object contains a symlink")
            if child.is_dir():
                continue
            if not child.is_file():
                raise QuarantineUnavailable("quarantine object contains a special file")
            relative = child.relative_to(path).as_posix()
            if not _contains(path.resolve(), child.resolve()):
                raise QuarantineUnavailable("quarantine object path escaped its root")
            files[relative] = child.read_bytes()
        actual = bundle_digest(files.items())
        if actual != digest:
            raise DigestMismatch("quarantine object digest verification failed")
        return files

    def delete(self, digest: str, *, referenced: bool = False) -> None:
        if referenced:
            raise QuarantineUnavailable("refusing to delete a quarantined object still referenced")
        path = self._object(digest)
        if not path.exists():
            return
        if path.is_symlink():
            raise QuarantineUnavailable("refusing to delete through a symlink")
        shutil.rmtree(path)

    def cleanup_staging(self) -> int:
        removed = 0
        for entry in self.staging.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
