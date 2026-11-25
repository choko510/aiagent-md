"""Verify, or deliberately refresh, the vendored nihongo skill bundle.

The Skill is pinned to one upstream commit and shipped as read-only package data. The default mode runs during the
Docker build and verifies every checked-in file against its upstream Git blob SHA-1 and SHA-256, so an incomplete or
modified bundle never becomes an image. Nothing is downloaded at agent runtime, and no upstream file is executed:
the bundle contains only Markdown and the upstream MIT license. Upstream helper scripts (nihongo/scripts/*.py)
and the WEB.md onboarding page are excluded from the bundle entirely.

`--update` is the maintainer path when bumping the pin. It fetches that exact commit's archive, verifies every file
with the same digests, and only then replaces the destination (staged, then renamed). A failed download or a
mismatched file leaves the existing bundle untouched. Staging and backup directories live in a private
`.nihongo-work/<key>` area beside `skills/`, never inside the Runtime-visible skills tree. A crash between the two
renames is recovered on the next install/update from the leftover backup. A known legacy bundle is restored only for
the update path and still fails ordinary verification until the new pin is installed; the swap is rollback-safe, not
a single atomic directory replacement. Stale staging or backup directories make ordinary verification fail closed.

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

UPSTREAM_COMMIT = "eccf51724ef2feef9fb8a96cd7a64d298c7715d7"  # official Release v3.1.1 tag
UPSTREAM_URL = f"https://codeload.github.com/AnIT012/nihongo/tar.gz/{UPSTREAM_COMMIT}"
UPSTREAM_SKILL_PREFIX = f"nihongo-{UPSTREAM_COMMIT}/nihongo/"
UPSTREAM_LICENSE = f"nihongo-{UPSTREAM_COMMIT}/LICENSE"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

# Upstream ships helper scripts in nihongo/scripts/ and a separate WEB.md onboarding page. Neither is vendored,
# installed or executed here: the shipped bundle is declarative Markdown plus the MIT license only. WEB.md is
# excluded by exact name while reading the pinned archive; it is never an accepted bundle member.
UPSTREAM_EXCLUDED_DIRS = ("scripts/",)
UPSTREAM_EXCLUDED_FILES = ("WEB.md",)

# (Git blob SHA-1, SHA-256) of every one of the 25 adopted upstream files. Never accept "latest main" silently.
EXPECTED_FILES: dict[str, tuple[str, str]] = {
    "LICENSE": (
        "0941ee17a37b8d6559dd869e720bed1a210f8d71",
        "c0f14ffe335cc6f5d4370dd82d716aed7402fc39074bb164a739f29dd9b690f2",
    ),
    "ALWAYS.md": (
        "0c5f6e4e263c9c45b46ee482dd9fea3e9565a726",
        "71e65072df64f857f4e31c4ecff34aa966ba938cd44ed219bd2bea55ce99506e",
    ),
    "SKILL.md": (
        "0f42c077f19521d9690da74581a5d7425fb46177",
        "1117a827a7615906d8c2bbcc5a77cea3353213ae0aa89991434ef0f87c32df7a",
    ),
    "references/ai-patterns.md": (
        "3e02aa9d7f79d4bc63644ec520450ec9daa02277",
        "3feb063ba04b9093b1ce286c8e7243ba01009c471839074b351f050817d453a0",
    ),
    "references/business-email.md": (
        "77702e80a9b2a078e1a622396429ed3a2bb11b93",
        "135c6954b50ece8f478c18e50beafc1d3ef484da42d69a27fcde6e76ebf709e2",
    ),
    "references/conversation.md": (
        "958e3c004950ec206af8d0a33238b75a51137b5b",
        "1636c20bd703bd0588644c8bc8c666dde49bfc3bdb967f9db05a453617fd01da",
    ),
    "references/craft.md": (
        "afee8e94273409e8c7ec16dba5322c6f95e517ae",
        "4b657b870cb01bb7e83e7b849f208adf3012b08c9b92924bc09b0a769a58e1e5",
    ),
    "references/entry-sheet.md": (
        "357c7d100b99d2b7d0330b52fcac82a1422c07b4",
        "7dbdf55bb92d0db9ddabd9889cb97dbe68b0d553d8c29719fc8e9e95fa1de871",
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
        "32215d6aefb9ab395485dbc8f83eea03d45e6a99",
        "c4210710f00422086ffaaae89691f90f43aa0b4014f6ed5ca87dc20609d096b2",
    ),
    "references/marketing-copy.md": (
        "7a4df9972dba8ab4f0f36168ac2a68324d0eae57",
        "6bc27aaada9ff9e94615fb38ab6059c33a3cbbcf1c2be916631236193017c4ec",
    ),
    "references/media.md": (
        "ae25abcc05312402b959de6ad3771a301a4bb872",
        "94bbbab217f30aeeadd90e57f8a62b1e2e8fdebb7459d05de0b46a05cc0cf53d",
    ),
    "references/messaging.md": (
        "e8aab7f1605338f6b1fd466a80be9023095e2e61",
        "bb42a2c352bef47e3bbb0b5f48e562156f6b35dfd0ea00bd998407b49756be31",
    ),
    "references/notices.md": (
        "04eef3394f043a5acdf5e804ec648446212cb080",
        "558a530dcbed2cfc9f6ed4dc582cfb9ddaeea78f8ae08a8378407be9cc4ce8aa",
    ),
    "references/orthography.md": (
        "1965225280b4f5225e1e452336104e381b7d3eff",
        "c4d85f437721f519fa02892741a02775a51a152a457bb1f53034fe914c49e8e1",
    ),
    "references/personal-profile.md": (
        "f542d844a9000d654c12ac2708e39f2534cba481",
        "c7dce5bb3cfaf33a1c13c499b0958576aec77cdf188f46aa38c8b8d38c976c05",
    ),
    "references/register.md": (
        "59fdf708445e76ebeac671f2c6a4bbfc8df770e6",
        "3dad1a5f005a4efdf82dfa16f3936ea843b027556aa6bdd95d2088795ad4e6c2",
    ),
    "references/review.md": (
        "d3e2a38e0419f0140426866f2322b78f9ed79a86",
        "5523aa206f541feb3dbd3599bdf1f7e9aa264f990e8e363914aa1d4177ad7dc8",
    ),
    "references/slides-and-documents.md": (
        "4e4319dc9c8564dda9be6daad8a4b51a3556d4ec",
        "49a6535fe0d90f2e734983556223509a0e20c07d2302bb44687cfd119c5ff8a1",
    ),
    "references/speech.md": (
        "e2e9de29f2f3bc577b6d5dddb19c5825f8dfeff0",
        "221e51f1e4fb70a7ba8e3f6037b565ad2842b3d50d518f1ae2389c1d779a1be8",
    ),
    "references/technical-writing.md": (
        "9463353534d0a4a17269f901b3bfab8c0387e77e",
        "8399b4e5fbd249ed9d2bedd7590bac75cc77ec46e88c67618d8dd7ff9226ae8c",
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

# v2.3.1 bundles are valid only for crash recovery from an interrupted update, never as the current bundle.
# The value is (file count, canonical bundle fingerprint) where the fingerprint covers the exact path set and bytes.
LEGACY_BUNDLES: dict[str, tuple[int, str]] = {
    "61ca63f0499271495aefb9c7b3c4915f75960106": (
        24,
        "11331b49da5d8db19a631f89b305dcb5983037b66962f24472374ff893413b13",
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


def _read_declarative_bundle(destination: Path) -> dict[str, bytes]:
    """Read a declarative bundle while enforcing path, entry-type, hardlink and size limits.

    This helper deliberately does not compare against EXPECTED_FILES: legacy-bundle recovery needs the exact bytes
    first, then compares a canonical fingerprint against a separately pinned legacy version.
    """
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
        if path.stat(follow_symlinks=False).st_nlink > 1:
            raise ValueError(f"hardlink inside nihongo skill bundle: {path}")
        rel = path.relative_to(destination).as_posix()
        _checked_relpath(rel)
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"oversized nihongo skill file: {rel}")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("nihongo skill bundle exceeds size limit")
        files[rel] = data
    return files


def bundle_fingerprint(files: dict[str, bytes]) -> str:
    """Canonical SHA-256 over the exact relative-path set and each file's SHA-256."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[rel]).digest())
    return digest.hexdigest()


def _verify_bundle(destination: Path) -> dict[str, bytes]:
    """Verify one directory's bytes without inspecting recovery work directories."""
    files = _read_declarative_bundle(destination)
    for rel, data in files.items():
        verify_file(rel, data)
    missing = sorted(set(EXPECTED_FILES) - set(files))
    if missing:
        raise ValueError(f"incomplete nihongo skill bundle: {missing}")
    return files


def _work_root(destination: Path) -> Path:
    """Private staging/backup area outside the publicly exposed skills tree.

    A destination-specific key prevents tests and two checkouts from sharing recovery state. The directory lives
    beside skills rather than inside it, so the Runtime filesystem route and packaged_skill_texts cannot discover
    an unverified staged copy.
    """
    key = hashlib.sha256(str(destination.resolve()).encode("utf-8")).hexdigest()[:16]
    return destination.parent.parent / ".nihongo-work" / key


def _staging_dirs(destination: Path) -> list[Path]:
    paths = list(destination.parent.glob(".nihongo-stage-*"))
    work = _work_root(destination)
    if work.is_dir():
        paths.extend(work.glob(".nihongo-stage-*"))
    return sorted(paths)


def _backup_dirs(destination: Path) -> list[Path]:
    paths = list(destination.parent.glob(".nihongo-backup-*"))
    work = _work_root(destination)
    if work.is_dir():
        paths.extend(work.glob(".nihongo-backup-*"))
    return sorted(paths)


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise RuntimeError(f"failed to remove nihongo work directory; preserved for manual recovery: {path}") from exc


def _discard_stale_staging(destination: Path) -> None:
    for path in _staging_dirs(destination):
        _remove_tree(path)


def verify_tree(destination: Path) -> dict[str, bytes]:
    """Verify the checked-in bundle byte for byte; stale recovery directories fail closed."""
    stale = [*_staging_dirs(destination), *_backup_dirs(destination)]
    if stale:
        raise RuntimeError(f"unrecovered nihongo work directory; remove it manually: {stale}")
    return _verify_bundle(destination)


def legacy_bundle_pin(destination: Path) -> str | None:
    """Return the pin when a complete, known legacy bundle is present; never trust an arbitrary directory."""
    try:
        files = _read_declarative_bundle(destination)
    except (OSError, ValueError):
        return None
    fingerprint = bundle_fingerprint(files)
    for pin, (count, expected) in LEGACY_BUNDLES.items():
        if len(files) == count and fingerprint == expected:
            return pin
    return None


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
    excluded: set[str] = set()
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.name.startswith(UPSTREAM_SKILL_PREFIX) and member.name != UPSTREAM_LICENSE:
                continue
            rel = "LICENSE" if member.name == UPSTREAM_LICENSE else member.name[len(UPSTREAM_SKILL_PREFIX) :]
            if rel in UPSTREAM_EXCLUDED_FILES:
                if not member.isfile() or rel in excluded:
                    raise ValueError(f"unsafe excluded upstream skill member: {member.name}")
                excluded.add(rel)
                continue  # WEB.md is onboarding content, not part of the static bundle
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


def recover_leftover_backup(destination: Path, *, allow_legacy: bool = False) -> str | None:
    """Recover a crash between the backup rename and the staged rename.

    A backup is verified before it is restored. The current pin is always accepted. A known v2.3.1 bundle is accepted
    only when allow_legacy is explicit (the update path), because restoring it means the next ordinary build must
    still fail until the new pin is installed. Unknown or modified backups are refused and left on disk for manual
    recovery. Stale staging is discarded only on the update path; ordinary verification fails closed instead. When
    the destination already exists it must verify against the current pin before stale backups are discarded.
    """
    if allow_legacy:
        _discard_stale_staging(destination)
    backups = _backup_dirs(destination)
    if not backups:
        return None
    if destination.exists():
        try:
            _verify_bundle(destination)
        except (OSError, ValueError) as exc:
            raise RuntimeError("destination is not the pinned nihongo bundle; refusing to discard its backup") from exc
        for backup in backups:
            _remove_tree(backup)
        return None
    if len(backups) > 1:
        raise RuntimeError(f"ambiguous nihongo backups; manual recovery required: {backups}")
    backup = backups[0]
    try:
        _verify_bundle(backup)
    except (OSError, ValueError) as current_error:
        legacy_pin = legacy_bundle_pin(backup)
        if legacy_pin is None:
            raise RuntimeError(f"leftover nihongo backup is not a verified bundle: {backup}") from current_error
        if not allow_legacy:
            raise RuntimeError(
                f"leftover nihongo backup is verified legacy {legacy_pin}, not the current pin; "
                "run --update or restore it manually (backup preserved)"
            ) from current_error
        backup.rename(destination)
        return legacy_pin
    backup.rename(destination)
    return None


def install(files: dict[str, bytes], destination: Path, *, allow_legacy_destination: bool = False) -> None:
    """Stage verified files, keep the old bundle until the swap succeeds, and roll back if the swap fails.

    A directory rename cannot replace a non-empty directory, so the old bundle is moved aside first. The window
    between that move and the staged rename is covered by a rollback: if the swap raises, the previous verified
    bundle is restored. If the process dies in that window, the next install/verify restores the verified backup
    before proceeding, so the destination is not left missing. ``allow_legacy_destination`` is used only by
    the update path after it has restored a known legacy backup; the legacy bundle is then replaced, never
    accepted as the current pin.
    """
    if set(files) != set(EXPECTED_FILES):
        raise ValueError("unverified skill bundle")
    for rel, data in files.items():
        verify_file(rel, data)
    if not (allow_legacy_destination and legacy_bundle_pin(destination) is not None):
        recover_leftover_backup(destination, allow_legacy=True)
    _discard_stale_staging(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = _work_root(destination)
    work.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".nihongo-stage-", dir=work))
    backup: Path | None = None
    try:
        for rel, data in files.items():
            path = staging.joinpath(*_checked_relpath(rel).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if destination.is_symlink():
            raise ValueError("refusing to replace a symlink")
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=".nihongo-backup-", dir=work))
            backup.rmdir()
            destination.rename(backup)
        try:
            staging.rename(destination)
        except BaseException:
            if backup is not None and not destination.exists():
                try:
                    backup.rename(destination)  # restore the previously verified bundle
                except OSError as restore_error:
                    raise RuntimeError(
                        f"failed to swap nihongo and failed to restore; backup preserved: {backup}"
                    ) from restore_error
            raise
        if backup is not None:
            _remove_tree(backup)
            backup = None
    finally:
        if staging.exists():
            _remove_tree(staging)


def update(destination: Path, fetch: Callable[[], bytes] = download_archive) -> dict[str, bytes]:
    """Fetch, verify, then install with rollback and crash recovery; a failure leaves the bundle untouched."""
    recover_leftover_backup(destination, allow_legacy=True)
    files = verified_archive_files(fetch())
    install(files, destination, allow_legacy_destination=True)
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
    recover_leftover_backup(args.destination, allow_legacy=args.update)
    if args.update:
        files = update(args.destination)
        print(f"Verified and installed upstream nihongo {UPSTREAM_COMMIT} ({len(files)} files)")
        return
    files = verify_tree(args.destination)
    print(f"Verified packaged nihongo {UPSTREAM_COMMIT} ({len(files)} files)")


if __name__ == "__main__":
    main()
