"""In-memory, fail-closed ZIP bundle inspection.

We never extract to the host filesystem. The central directory is validated, every entry is decompressed in
bounded chunks, and the resulting immutable file map is content-addressed. Windows and Linux path semantics are
both rejected when a name is ambiguous: we accept one canonical POSIX relative-path form only.
"""

from __future__ import annotations

import io
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NoReturn

from .contracts import SkillFile
from .digest import bundle_digest, sha256_bytes
from .errors import ArchiveRejected, BundleInvalid
from .manifest import media_type

MAX_BUNDLE_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_FILES = 64
MAX_ENTRIES = 128
MAX_DEPTH = 8
MAX_PATH_BYTES = 240
MAX_COMPRESSION_RATIO = 100
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024

_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".com",
        ".msi",
        ".scr",
        ".cpl",
        ".sys",
        ".pyc",
        ".pyo",
        ".class",
        ".jar",
        ".war",
        ".wasm",
        ".node",
        ".o",
        ".a",
        ".lib",
    }
)
_ARCHIVE_SUFFIXES = frozenset(
    {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".cab", ".iso", ".img", ".dmg"}
)
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)
_BINARY_MAGIC = (b"\x7fELF", b"MZ", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\x00asm")
_ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")
_RESERVED_ROOT_NAMES = frozenset({".complete", ".manifest"})


@dataclass(frozen=True)
class BundleLimits:
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    max_file_bytes: int = MAX_FILE_BYTES
    max_files: int = MAX_FILES
    max_entries: int = MAX_ENTRIES
    max_depth: int = MAX_DEPTH
    max_path_bytes: int = MAX_PATH_BYTES
    max_compression_ratio: int = MAX_COMPRESSION_RATIO
    max_archive_bytes: int = MAX_ARCHIVE_BYTES
    max_skill_md_bytes: int = MAX_SKILL_MD_BYTES


@dataclass(frozen=True)
class ValidatedBundle:
    files: dict[str, bytes]
    entries: tuple[SkillFile, ...]
    digest: str
    archived_bytes: int

    def skill_md(self) -> bytes:
        return self.files["SKILL.md"]


def _reject(code: str, detail: str, **context: object) -> NoReturn:
    raise ArchiveRejected(detail, reason=code, **context)


def normalize_member_path(raw: str, limits: BundleLimits) -> str:
    if not isinstance(raw, str) or not raw:
        _reject("EMPTY_PATH", "archive entry has an empty name")
    if "\x00" in raw:
        _reject("NUL_IN_PATH", "archive entry name contains NUL")
    if "\\" in raw:
        _reject("BACKSLASH_PATH", "backslash paths are not accepted")
    if raw.startswith("/") or raw.startswith("//"):
        _reject("ABSOLUTE_PATH", "absolute paths are not accepted")
    if len(raw.encode("utf-8")) > limits.max_path_bytes:
        _reject("PATH_TOO_LONG", "archive entry path is too long")
    if not unicodedata.is_normalized("NFC", raw):
        _reject("NON_NFC_PATH", "archive entry path is not NFC normalized")
    path = raw.rstrip("/")
    if not path:
        _reject("EMPTY_PATH", "archive entry has an empty name")
    if ":" in path:
        _reject("ADS_OR_DRIVE_PATH", "colon is not allowed in an archive entry path")
    parts = path.split("/")
    if len(parts) > limits.max_depth:
        _reject("TOO_DEEP", "archive entry path is too deep")
    for part in parts:
        if part in ("", ".", ".."):
            _reject("PATH_TRAVERSAL", "archive entry path contains an unsafe segment")
        if part.casefold() in _RESERVED_ROOT_NAMES:
            _reject("RESERVED_PATH", "archive uses a quarantine-internal marker name")
        if part != part.strip() or part.endswith("."):
            _reject("WINDOWS_TRAILING", "archive entry path has a trailing space or dot")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in part):
            _reject("CONTROL_CHARACTER", "archive entry path contains a control character")
        if unicodedata.category(part[0]) == "Cf" or any(unicodedata.category(ch) == "Cf" for ch in part):
            _reject("FORMAT_CHARACTER", "archive entry path contains a format character")
        base = part.split(".", 1)[0].casefold()
        if base in _WINDOWS_RESERVED:
            _reject("WINDOWS_RESERVED_NAME", "archive entry path uses a reserved Windows name")
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in _ARCHIVE_SUFFIXES:
        _reject("NESTED_ARCHIVE", "nested archives are not supported")
    if suffix in _EXECUTABLE_SUFFIXES:
        _reject("EXECUTABLE_FILE", "executable payloads are not supported")
    if path.casefold().startswith(".git/") or path.casefold() == ".git":
        _reject("GIT_METADATA", "git metadata is not accepted in a Skill bundle")
    return path


def _entry_kind(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    if mode:
        file_type = stat.S_IFMT(mode)
        if file_type == 0:
            # Python and many Windows tools store only permission bits (e.g. 0o600) without S_IFREG.
            pass
        elif stat.S_ISLNK(mode):
            _reject("SYMLINK", "symbolic links are not accepted")
        elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            _reject("SPECIAL_FILE", "device/FIFO/socket entries are not accepted")
        elif stat.S_ISDIR(mode):
            return "dir"
        elif not stat.S_ISREG(mode):
            _reject("SPECIAL_FILE", "only regular files and directories are accepted")
    if info.is_dir():
        return "dir"
    return "file"


def _validate_path_kinds(entries: list[tuple[str, str]]) -> None:
    """Reject duplicate, case-aliased, marker, and file/directory prefix paths before extraction."""
    folded_kind: dict[str, str] = {}
    folded_actual: dict[str, str] = {}
    for path, kind in entries:
        folded = path.casefold()
        previous = folded_kind.get(folded)
        if previous is not None:
            if previous == kind:
                _reject("DUPLICATE_ENTRY", "duplicate archive entry")
            _reject("DUPLICATE_ENTRY", "file and directory entries share an archive path")
        folded_kind[folded] = kind
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            folded_prefix = "/".join(parts[:index]).casefold()
            actual_prefix = "/".join(parts[:index])
            prior = folded_actual.get(folded_prefix)
            if prior is not None and prior != actual_prefix:
                _reject("CASE_COLLISION", "case-insensitive archive path collision")
            folded_actual[folded_prefix] = actual_prefix
    for folded, _kind in folded_kind.items():
        parts = folded.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if folded_kind.get(ancestor) == "file":
                _reject("PATH_PREFIX_COLLISION", "archive contains a file/directory prefix collision")


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limits: BundleLimits) -> bytes:
    if info.flag_bits & 0x1:
        _reject("ENCRYPTED", "encrypted archive entries are not supported")
    if info.file_size > limits.max_file_bytes:
        _reject("FILE_TOO_LARGE", "archive entry exceeds the per-file limit")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        _reject("COMPRESSION_UNSUPPORTED", "unsupported archive compression method")
    if info.file_size > 0 and info.compress_size == 0:
        _reject("ZIP_BOMB", "archive entry declares non-zero size with no compressed bytes")
    if info.compress_size > 0 and info.file_size / info.compress_size > limits.max_compression_ratio:
        _reject("COMPRESSION_RATIO", "archive entry exceeds the compression-ratio limit")
    out = bytearray()
    try:
        with zf.open(info) as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                out.extend(chunk)
                if len(out) > limits.max_file_bytes:
                    _reject("FILE_TOO_LARGE", "archive entry exceeds the per-file limit")
    except zipfile.BadZipFile as exc:
        _reject("CORRUPT_ENTRY", "archive entry failed CRC/decompression", problem=str(exc)[:100])
    if len(out) != info.file_size:
        _reject("SIZE_MISMATCH", "archive entry size differs from its declared size")
    data = bytes(out)
    if data.startswith(_BINARY_MAGIC):
        _reject("EXECUTABLE_FILE", "binary executable payloads are not supported")
    if data.startswith(_ARCHIVE_MAGIC):
        _reject("NESTED_ARCHIVE_MAGIC", "archive payloads are not supported inside a Skill bundle")
    return data


def inspect_zip(data: bytes, *, limits: BundleLimits | None = None, require_entry: bool = True) -> ValidatedBundle:
    limits = limits or BundleLimits()
    if len(data) > limits.max_archive_bytes:
        _reject("ARCHIVE_TOO_LARGE", "archive exceeds the archive byte limit")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        _reject("NOT_A_ZIP", "bundle is not a valid ZIP archive", problem=str(exc)[:100])
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            _reject("TOO_MANY_ENTRIES", "archive has too many entries")
        declared_total = 0
        files: dict[str, bytes] = {}
        described: list[tuple[str, str, zipfile.ZipInfo]] = []
        for info in infos:
            if "\x00" in info.orig_filename:
                _reject("NUL_IN_PATH", "archive entry name contains NUL")
            path = normalize_member_path(info.filename, limits)
            kind = _entry_kind(info)
            described.append((path, kind, info))
        _validate_path_kinds([(path, kind) for path, kind, _info in described])
        for path, kind, info in described:
            if kind == "dir":
                continue
            declared_total += info.file_size
            if declared_total > limits.max_bundle_bytes:
                _reject("BUNDLE_TOO_LARGE", "uncompressed bundle exceeds the byte limit")
            files[path] = _read_entry(zf, info, limits)
        if len(files) > limits.max_files:
            _reject("TOO_MANY_FILES", "archive has too many files")
        if require_entry:
            if "SKILL.md" not in files:
                _reject("MISSING_SKILL_MD", "SKILL.md is required at the bundle root")
            if len(files["SKILL.md"]) > limits.max_skill_md_bytes:
                _reject("SKILL_MD_TOO_LARGE", "SKILL.md exceeds the 64KiB limit")
        entries = tuple(
            SkillFile(path, sha256_bytes(payload), len(payload), media_type(path))
            for path, payload in sorted(files.items())
        )
        return ValidatedBundle(
            files=files,
            entries=entries,
            digest=bundle_digest(files.items()),
            archived_bytes=sum(len(payload) for payload in files.values()),
        )


def strip_single_root(files: dict[str, bytes]) -> dict[str, bytes]:
    """Strip one wrapper directory (GitHub archives) only when every path shares that first segment."""
    if not files or "SKILL.md" in files:
        return files
    roots = {path.split("/", 1)[0] for path in files}
    if len(roots) != 1:
        return files
    roots.pop()
    if not all("/" in path for path in files):
        return files
    stripped = {path.split("/", 1)[1]: payload for path, payload in files.items()}
    if "SKILL.md" not in stripped:
        return files
    return stripped


def assert_expected_digest(bundle: ValidatedBundle, expected: str) -> None:
    if bundle.digest != expected:
        raise BundleInvalid("bundle digest does not match the expected immutable digest")
