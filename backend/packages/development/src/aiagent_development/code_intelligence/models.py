"""Internal, non-canonical value objects for scoped diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Coverage(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    TEXT_ONLY = "TEXT_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    owner_id: str
    binding_id: str
    revision: str
    commit: str


@dataclass(frozen=True)
class AuthorizedSnapshot:
    """Only a trusted integration may select this path and identity; not an auth token."""

    identity: Identity
    repository: Path


class SnapshotPort(Protocol):
    def authorized_snapshot(self) -> AuthorizedSnapshot: ...


@dataclass(frozen=True)
class Limits:
    files: int = 10000
    file_bytes: int = 1024 * 1024
    total_bytes: int = 32 * 1024 * 1024
    hits: int = 100
    excerpt_bytes: int = 160
    process_bytes: int = 8 * 1024 * 1024
    process_seconds: float = 5.0
    total_seconds: float = 30.0

    def __post_init__(self) -> None:
        if min(self.files, self.file_bytes, self.total_bytes, self.hits, self.excerpt_bytes, self.process_bytes) <= 0:
            raise ValueError("limits must be positive")
        if not 0 < self.process_seconds <= 300 or not 0 < self.total_seconds <= 3600:
            raise ValueError("invalid time limit")
        if (
            self.process_bytes > 64 * 1024 * 1024
            or self.files > 100000
            or self.file_bytes > 64 * 1024 * 1024
            or self.total_bytes > 1024 * 1024 * 1024
            or self.hits > 100000
            or self.excerpt_bytes > 65536
        ):
            raise ValueError("limit exceeds implementation ceiling")


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    oid: str
    size: int | None
    kind: str


class ReadError(RuntimeError):
    """Failure is intentionally free of Git stderr and repository content."""
