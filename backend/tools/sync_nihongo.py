"""Verify, or deliberately refresh, the vendored nihongo skill bundle.

The Skill is pinned to one upstream commit and shipped as read-only package data. The default mode runs during the
Docker build and verifies every checked-in file against its upstream Git blob SHA-1 and SHA-256, so an incomplete or
modified bundle never becomes an image. Nothing is downloaded at agent runtime, and no upstream file is executed:
the bundle contains only Markdown and the upstream MIT license. Upstream helper scripts (nihongo/scripts/*.py) are
excluded from the bundle entirely.

`--update` is the maintainer path when bumping the pin. It fetches that exact commit's archive, verifies every file
with the same digests, and only then replaces the destination (staged, then renamed). A failed download or a
mismatched file leaves the existing bundle untouched. A crash between the two renames is recovered on the next
install/verify from the leftover `.nihongo-backup-*` directory; the swap is rollback-safe, not a single atomic
directory replacement.

Upstream: https://github.com/AnIT012/nihongo (MIT). Lite = ALWAYS.md, Full = SKILL.md + references/.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath

UPSTREAM_COMMIT = "e62ed520147c2f9b50cf93d46b9d0cecc9892565"  # v2.3.1, latest main commit at pin time
UPSTREAM_URL = f"https://codeload.github.com/AnIT012/nihongo/tar.gz/{UPSTREAM_COMMIT}"
UPSTREAM_SKILL_PREFIX = f"nihongo-{UPSTREAM_COMMIT}/nihongo/"
UPSTREAM_LICENSE = f"nihongo-{UPSTREAM_COMMIT}/LICENSE"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

# Upstream ships helper scripts in nihongo/scripts/. They are never vendored, installed or executed here: the
# shipped bundle is declarative Markdown plus the MIT license only.
UPSTREAM_EXCLUDED_DIRS = ("scripts/",)

# (Git blob SHA-1, SHA-256) of every upstream file. Never accept "latest main" silently.
EXPECTED_FILES: dict[str, tuple[str, str]] = {
    "ALWAYS.md": (
        "5d0fd20d9b62b1792589c4dfe8bf34b21529c95b",
        "58f2a39a8b6195bc506715883eb3e50233c190d6ea8fb51065c08aedb9500f82",
    ),
    "LICENSE": (
        "0941ee17a37b8d6559dd869e720bed1a210f8d71",
        "c0f14ffe335cc6f5d4370dd82d716aed7402fc39074bb164a739f29dd9b690f2",
    ),
    "SKILL.md": (
        "4dfc2d357293116f106a2072d1e3e2d47ff4b70b",
        "0f50b818c53ceecf3c39ee900bb602daadd4486bc8e626d0a8ba5012389e2e14",
    ),
    "references/ai-patterns.md": (
        "db78ae040a5d11f9021c5417c37879b6ee0729f4",
        "f58181d054a760b1e70e71ebddeacedb356a058a4059a799bec85204824fbd16",
    ),
    "references/business-email.md": (
        "69e370eae8e93ddc5477aab64d958401c89ffd43",
        "75bee399d4cbe604901fb1b10d477e69818b5219a92230b3cd7ef7c06c8ca235",
    ),
    "references/conversation.md": (
        "958e3c004950ec206af8d0a33238b75a51137b5b",
        "1636c20bd703bd0588644c8bc8c666dde49bfc3bdb967f9db05a453617fd01da",
    ),
    "references/craft.md": (
        "a748ab81d955b85973e18f32a6f42f096a6718de",
        "310755fa3f28c158eaf2fdc1a5d7e209bb9b97b29d25aee16eb9ff545d7a1a79",
    ),
    "references/entry-sheet.md": (
        "536b1c626541b59ab735f372ac5b76452e814f7e",
        "a8b36ded05ed602679b2ffe4f02b4396e31d2d339937bfe4b98fb7f2fa0eaf84",
    ),
    "references/essays-and-promotion.md": (
        "58c37d914ee8a5bc719e9d1d03ee8ca9b8823e5c",
        "c45863f10c4c3de8370a94edf9c1424f346f08291ab56259b58392f2f745f9f9",
    ),
    "references/keigo.md": (
        "2a812dfffeb28278c9f647f676eb7f06f6d61b23",
        "dce99964728f9cda0cdb53087894b3601e7b001102b503e7060033609695f8a1",
    ),
    "references/make-profile.md": (
        "0054a5acdd7a51649f4728a654405aa59f826bf9",
        "e557d02352394f68c0a16af2c267ff984385762aac5d94cec323be208f711c89",
    ),
    "references/media.md": (
        "ae25abcc05312402b959de6ad3771a301a4bb872",
        "94bbbab217f30aeeadd90e57f8a62b1e2e8fdebb7459d05de0b46a05cc0cf53d",
    ),
    "references/messaging.md": (
        "852630f38c510eead698e31a37ed7cf03535fade",
        "b3a086e46b1abe4360dbd2317550f46359aa5ead3dc1e94389cc585e9816d8a9",
    ),
    "references/notices.md": (
        "ce220f8b0e2b9ffa3af53a1101bc5525665a7ab0",
        "728aa530acf618de243be862fb7639ae645a3a8bdcacdc4dc90ce064bbcee8a3",
    ),
    "references/orthography.md": (
        "d6a142fe7592a5ecf1842194d03894e3a5e6f3ef",
        "189dc9424f6f489fb05720a8a5e4e5580da72a691511129daee904e7521deb11",
    ),
    "references/personal-profile.md": (
        "1318e3cfa2b566182816f6558a818ceee01a6d11",
        "713832f145329b60c5ffc077487fa80f8c96d7371a13d2285fe5980a5a834dfe",
    ),
    "references/review.md": (
        "c8ddf477cf023110f7b9e585949383e701b09d86",
        "fef6344b166e84069062e8f56712620dffb3aca534a04186907da908f66e2c44",
    ),
    "references/slides-and-documents.md": (
        "771239d8921ff6c8197c4dc427ded2bafd2ee62e",
        "50df37422ef9ccd9c16a01e3e16cc6742b6792201a52f4ab57e89619b3cb1831",
    ),
    "references/speech.md": (
        "e2e9de29f2f3bc577b6d5dddb19c5825f8dfeff0",
        "221e51f1e4fb70a7ba8e3f6037b565ad2842b3d50d518f1ae2389c1d779a1be8",
    ),
    "references/technical-writing.md": (
        "79cbf9deffcae0e756f0d2e31dabcdfecf6c8bd2",
        "084cecfa7e40e2374a5612ed5ed79d451e84dff648139cbf556ecfa95bc483ff",
    ),
    "references/titles.md": (
        "48b396fe785885418bb29832e64d6b977deb6b15",
        "17472943fc9e09e9b57a0a3d633c8a858a9c8bf27f1615d42a08c5aa5869d221",
    ),
    "references/translation.md": (
        "d707a04a790da5c0c10a28d1f79df1bc3f276dc0",
        "6995cee7dfb500eb19177e91eb5e8a5798c1b7b59b1d84c8bba7d026b029687b",
    ),
    "references/ui-copy.md": (
        "5457aff8f597d72b7a3df0805c5329d50f3773c5",
        "caa317fbff2cfc20775749b545c1a16c3adeca8f826fa5d6a121e83cba947efa",
    ),
}

DEFAULT_DESTINATION = Path(__file__).resolve().parents[1] / "packages/domain/src/aiagent_domain/skills/nihongo"


def digests(data: bytes) -> tuple[str, str]:
    """(Git blob SHA-1, SHA-256) so the pin matches both the upstream object and the exact bytes."""
    return (
        hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),  # noqa: S324 - Git object digest
        hashlib.sha256(data).hexdigest(),
    )


def verify_file(rel: str, data: bytes) -> None:
    if rel not in EXPECTED_FILES:
        raise ValueError(f"unexpected file in nihongo skill bundle: {rel}")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"oversized nihongo skill file: {rel}")
    blob, sha256 = digests(data)
    expected_blob, expected_sha256 = EXPECTED_FILES[rel]
    if blob != expected_blob:
        raise ValueError(f"upstream Git blob mismatch: {rel}")
    if sha256 != expected_sha256:
        raise ValueError(f"upstream SHA-256 mismatch: {rel}")


def _checked_relpath(rel: str) -> PurePosixPath:
    posix = PurePosixPath(rel)
    parts = posix.parts
    if not rel or rel.startswith("/") or ".." in parts or "\\" in rel or "\x00" in rel or str(posix) != rel:
        raise ValueError(f"invalid skill path: {rel!r}")
    if rel != "LICENSE" and not (rel in {"ALWAYS.md", "SKILL.md"} or rel.startswith("references/")):
        raise ValueError(f"invalid skill path: {rel!r}")
    if not (rel.endswith(".md") or rel == "LICENSE"):
        raise ValueError(f"unsupported skill file type: {rel!r}")
    return posix


def verify_tree(destination: Path) -> dict[str, bytes]:
    """Verify the checked-in bundle byte for byte; any missing, extra, linked or modified file fails the build."""
    if not destination.is_dir() or destination.is_symlink():
        raise ValueError(f"missing nihongo skill bundle: {destination}")
    files: dict[str, bytes] = {}
    total_bytes = 0
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink inside nihongo skill bundle: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsupported entry in nihongo skill bundle: {path}")
        rel = path.relative_to(destination).as_posix()
        _checked_relpath(rel)
        data = path.read_bytes()
        verify_file(rel, data)
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("nihongo skill bundle exceeds size limit")
        files[rel] = data
    missing = sorted(set(EXPECTED_FILES) - set(files))
    if missing:
        raise ValueError(f"incomplete nihongo skill bundle: {missing}")
    return files


def download_archive() -> bytes:
    req = urllib.request.Request(UPSTREAM_URL, headers={"User-Agent": "aiagent-md-build/1"})
    with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310 - fixed GitHub URL
        payload = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("nihongo archive exceeds size limit")
    return payload


def verified_archive_files(archive: bytes) -> dict[str, bytes]:
    """Extract only the pinned skill tree and license; never trust a member name, link or size."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("nihongo archive exceeds size limit")
    files: dict[str, bytes] = {}
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.name.startswith(UPSTREAM_SKILL_PREFIX) and member.name != UPSTREAM_LICENSE:
                continue
            rel = "LICENSE" if member.name == UPSTREAM_LICENSE else member.name[len(UPSTREAM_SKILL_PREFIX) :]
            if member.isdir():
                continue
            if rel.startswith(UPSTREAM_EXCLUDED_DIRS):
                continue  # upstream helper scripts are never shipped or run
            _checked_relpath(rel)
            if not member.isfile() or rel in files:
                raise ValueError(f"unexpected or unsafe upstream skill member: {member.name}")
            if member.size > MAX_FILE_BYTES or member.size < 0:
                raise ValueError(f"oversized upstream file: {rel}")
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"missing upstream file data: {rel}")
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != member.size:
                raise ValueError(f"invalid upstream file length: {rel}")
            verify_file(rel, data)
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("unpacked nihongo skill exceeds size limit")
            files[rel] = data
    if set(files) != set(EXPECTED_FILES):
        missing = sorted(set(EXPECTED_FILES) - set(files))
        raise ValueError(f"incomplete upstream skill bundle: {missing}")
    return files


def recover_leftover_backup(destination: Path) -> None:
    """Recover a crash between the backup rename and the staged rename.

    The previous bundle is verified before it is restored or discarded, so an arbitrary leftover directory is never
    trusted. An ambiguous set of backups without a destination is refused rather than guessed.
    """
    backups = sorted(destination.parent.glob(".nihongo-backup-*"))
    if not backups:
        return
    if destination.exists():
        try:
            verify_tree(destination)
        except (OSError, ValueError) as exc:
            raise RuntimeError("destination is not the pinned nihongo bundle; refusing to discard its backup") from exc
        for backup in backups:
            shutil.rmtree(backup, ignore_errors=True)
        return
    if len(backups) > 1:
        raise RuntimeError(f"ambiguous nihongo backups; manual recovery required: {backups}")
    try:
        verify_tree(backups[0])
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"leftover nihongo backup is not a verified bundle: {backups[0]}") from exc
    backups[0].rename(destination)


def install(files: dict[str, bytes], destination: Path) -> None:
    """Stage verified files, keep the old bundle until the swap succeeds, and roll back if the swap fails.

    A directory rename cannot replace a non-empty directory, so the old bundle is moved aside first. The window
    between that move and the staged rename is covered by a rollback: if the swap raises, the previous verified
    bundle is restored. If the process dies in that window, the next install/verify restores the verified backup
    before proceeding, so the destination is not left missing.
    """
    if set(files) != set(EXPECTED_FILES):
        raise ValueError("unverified skill bundle")
    for rel, data in files.items():
        verify_file(rel, data)
    recover_leftover_backup(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".nihongo-stage-", dir=destination.parent))
    backup: Path | None = None
    try:
        for rel, data in files.items():
            path = staging.joinpath(*_checked_relpath(rel).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if destination.is_symlink():
            raise ValueError("refusing to replace a symlink")
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=".nihongo-backup-", dir=destination.parent))
            backup.rmdir()
            destination.rename(backup)
        try:
            staging.rename(destination)
        except BaseException:
            if backup is not None and not destination.exists():
                try:
                    backup.rename(destination)  # restore the previously verified bundle
                except OSError:
                    pass  # keep the backup on disk for manual recovery
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def update(destination: Path, fetch: Callable[[], bytes] = download_archive) -> dict[str, bytes]:
    """Fetch, verify, then install with rollback and crash recovery; a failure leaves the bundle untouched."""
    files = verified_archive_files(fetch())
    install(files, destination)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify or refresh the pinned nihongo skill bundle")
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument(
        "--update",
        action="store_true",
        help="fetch the pinned upstream commit and replace the bundle after verification (maintainer only)",
    )
    args = parser.parse_args()
    recover_leftover_backup(args.destination)
    if args.update:
        files = update(args.destination)
        print(f"Verified and installed upstream nihongo {UPSTREAM_COMMIT} ({len(files)} files)")
        return
    files = verify_tree(args.destination)
    print(f"Verified packaged nihongo {UPSTREAM_COMMIT} ({len(files)} files)")


if __name__ == "__main__":
    main()
