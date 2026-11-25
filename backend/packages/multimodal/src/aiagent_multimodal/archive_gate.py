"""Archive security gate for ZIP-based Office formats (15-01 §7.1).

The central directory is treated as an untrusted claim. Every entry is
decompressed under a byte counter and CRC-verified; the gate stops as soon as a
ceiling is crossed. Files are never extracted to the filesystem, so a
traversal-bearing name cannot create a path outside the allowlisted namespace.

Policy covers ZIP Slip, absolute/UNC/drive paths, NTFS alternate data streams,
NUL, symlinks/hardlinks/device files, duplicate names (casefold + NFKC),
nested archives, ZIP64 anomalies, CRC mismatches, entry count, per-entry and
total expanded bytes, compression ratio and depth.
"""

from __future__ import annotations

import io
import re
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from .errors import MmiError
from .limits import MmiLimits

_DRIVE = re.compile(r"^[A-Za-z]:")
_ADS = re.compile(r":")
_ALLOWED_COMPRESSION: Final[frozenset[int]] = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    name: str
    uncompressed_bytes: int
    compressed_bytes: int
    crc32: int
    is_dir: bool
    nested_archive: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    entries: tuple[ArchiveEntry, ...]
    expanded_bytes: int
    max_entry_bytes: int
    max_ratio: float
    _names: frozenset[str] = field(default_factory=frozenset)

    def has(self, name: str) -> bool:
        return name in self._names

    @property
    def names(self) -> frozenset[str]:
        return self._names


def inspect_archive(data: bytes, limits: MmiLimits, *, max_entries: int | None = None) -> ArchiveReport:
    """Fully validate a ZIP container under hard byte limits."""
    if not data:
        raise MmiError("BAD_ARCHIVE", "empty archive")
    entry_cap = limits.max_zip_entries if max_entries is None else min(max_entries, limits.max_zip_entries)
    seen: dict[str, str] = {}
    entries: list[ArchiveEntry] = []
    total = 0
    max_entry = 0
    max_ratio = 0.0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > entry_cap:
                raise MmiError("EXPANSION_LIMIT", "archive entry count exceeds limit")
            for info in infos:
                _validate_entry(info, seen, limits)
            for info in infos:
                if info.is_dir():
                    entries.append(ArchiveEntry(info.filename, 0, info.compress_size, info.CRC, True))
                    continue
                _validate_size(info, limits)
                if (info.compress_size == 0 and info.file_size > 0) or info.file_size > limits.max_zip64_declared_bytes:
                    raise MmiError("BAD_ARCHIVE", "implausible ZIP64 size declaration")
                expanded, crc, nested = _inflate_entry(zf, info, limits, total)
                total += expanded
                if total > limits.max_zip_uncompressed_bytes:
                    raise MmiError("EXPANSION_LIMIT", "archive expands beyond the total limit")
                max_entry = max(max_entry, expanded)
                ratio = expanded / max(info.compress_size, 1)
                max_ratio = max(max_ratio, ratio)
                if expanded > 0 and ratio > limits.max_zip_ratio:
                    raise MmiError("EXPANSION_LIMIT", "archive compression ratio exceeds limit")
                if crc != info.CRC:
                    raise MmiError("BAD_ARCHIVE", "archive entry CRC mismatch")
                if nested:
                    raise MmiError("BAD_ARCHIVE", "nested archive entry is not allowed")
                entries.append(ArchiveEntry(info.filename, expanded, info.compress_size, info.CRC, False, nested))
    except MmiError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, ValueError) as exc:
        raise MmiError("BAD_ARCHIVE", f"malformed zip container ({type(exc).__name__})") from exc
    return ArchiveReport(
        entries=tuple(entries),
        expanded_bytes=total,
        max_entry_bytes=max_entry,
        max_ratio=max_ratio,
        _names=frozenset(e.name for e in entries),
    )


def read_entry(data: bytes, name: str, limits: MmiLimits, *, max_bytes: int | None = None) -> bytes:
    """Read one named part with a second, independent byte ceiling."""
    cap = limits.max_zip_entry_bytes if max_bytes is None else min(max_bytes, limits.max_zip_entry_bytes)
    _validate_name(name, limits)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo(name)
            if info.is_dir() or info.file_size > cap:
                raise MmiError("EXPANSION_LIMIT", "archive part is too large")
            return _read_bounded(zf.open(info), cap, name)
    except MmiError:
        raise
    except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise MmiError("BAD_ARCHIVE", f"archive part unavailable ({type(exc).__name__})") from exc


def entry_names(data: bytes, limits: MmiLimits) -> tuple[str, ...]:
    """List names without inflating payloads (safe for format detection)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > limits.max_zip_entries:
                raise MmiError("EXPANSION_LIMIT", "archive entry count exceeds limit")
            seen: dict[str, str] = {}
            for info in infos:
                _validate_entry(info, seen, limits)
            return tuple(i.filename for i in infos)
    except MmiError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, ValueError) as exc:
        raise MmiError("BAD_ARCHIVE", f"malformed zip container ({type(exc).__name__})") from exc


def _validate_entry(info: zipfile.ZipInfo, seen: dict[str, str], limits: MmiLimits) -> None:
    _validate_name(info.filename, limits)
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
        raise MmiError("BAD_ARCHIVE", "archive contains a non-regular entry (symlink/device)")
    if info.flag_bits & 0x1:
        raise MmiError("BAD_ARCHIVE", "encrypted archive entry is not allowed")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise MmiError("BAD_ARCHIVE", "unsupported archive compression method")
    key = unicodedata.normalize("NFKC", info.filename.rstrip("/")).casefold()
    if key in seen:
        raise MmiError("BAD_ARCHIVE", "duplicate or colliding archive entry name")
    seen[key] = info.filename
    depth = len([p for p in info.filename.rstrip("/").split("/") if p])
    if depth > limits.max_zip_depth:
        raise MmiError("BAD_ARCHIVE", "archive directory depth exceeds limit")


def _validate_name(name: str, limits: MmiLimits) -> None:
    if not name or "\x00" in name or len(name) > 512:
        raise MmiError("BAD_ARCHIVE", "invalid archive entry name")
    if "\\" in name or name.startswith("/") or name.startswith("//"):
        raise MmiError("BAD_ARCHIVE", "absolute or non-portable archive path")
    if _DRIVE.match(name):
        raise MmiError("BAD_ARCHIVE", "archive path contains a drive letter")
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MmiError("BAD_ARCHIVE", "archive path contains traversal or empty segment")
    if _ADS.search(name):
        raise MmiError("BAD_ARCHIVE", "archive path contains an alternate data stream")
    if len(parts) > limits.max_zip_depth:
        raise MmiError("BAD_ARCHIVE", "archive directory depth exceeds limit")


def _validate_size(info: zipfile.ZipInfo, limits: MmiLimits) -> None:
    if info.file_size > limits.max_zip_entry_bytes:
        raise MmiError("EXPANSION_LIMIT", "archive entry exceeds the per-entry limit")


def _inflate_entry(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, limits: MmiLimits, already: int
) -> tuple[int, int, bool]:
    crc = 0
    size = 0
    head = b""
    try:
        with zf.open(info, "r") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limits.max_zip_entry_bytes or already + size > limits.max_zip_uncompressed_bytes:
                    raise MmiError("EXPANSION_LIMIT", "archive expands beyond limit while streaming")
                crc = zlib.crc32(chunk, crc)
                if len(head) < 4:
                    head += chunk[: 4 - len(head)]
    except MmiError:
        raise
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as exc:
        raise MmiError("BAD_ARCHIVE", f"archive inflate failed ({type(exc).__name__})") from exc
    nested = head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    return size, crc & 0xFFFFFFFF, nested


def _read_bounded(handle: Iterable[bytes], cap: int, name: str) -> bytes:
    out = bytearray()
    for chunk in handle:
        out.extend(chunk)
        if len(out) > cap:
            raise MmiError("EXPANSION_LIMIT", f"archive part {name!r} exceeds limit")
    return bytes(out)
