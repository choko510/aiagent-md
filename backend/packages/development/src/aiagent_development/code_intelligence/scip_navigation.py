"""Read-only SCIP navigation over an explicitly bound, immutable snapshot.

The manifest is supplied by trusted integration code; validation does not prove
indexer identity or semantic correctness. No external services are contacted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .git_reader import GitSnapshot
from .inventory import LOCKS, RepositoryInventory
from .models import Entry, Identity, ReadError
from .paths import display_path, validate_git_path
from .scip_ranges import ByteRange, convert_range
from .search import CodeSearchResult, search


class IndexStatus(StrEnum):
    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SourceProvenance:
    path: str
    blob_id: str
    digest: str


@dataclass(frozen=True)
class IndexManifest:
    identity: Identity
    tree: str
    index_digest: str
    indexer: str
    indexer_version: str
    protocol_version: int
    sources: tuple[SourceProvenance, ...]
    dependency_lock_digest: str | None = None
    toolchain_digest: str | None = None
    execution_evidence: str | None = None


@dataclass(frozen=True)
class SymbolLocation:
    symbol: str
    path: str
    span: ByteRange
    blob_id: str
    digest: str
    definition: bool
    positions: tuple[int, int, int, int]
    position_encoding: int
    confidence: str = "PRECISE_INDEX_REPORTED"


@dataclass(frozen=True)
class Implementation:
    implementing: str
    implemented: str
    document: str
    confidence: str = "PRECISE_INDEX_REPORTED"


@dataclass(frozen=True)
class ReferenceRelationship:
    source: str
    target: str
    document: str


@dataclass(frozen=True)
class SymbolGraph:
    manifest: IndexManifest
    locations: tuple[SymbolLocation, ...]
    implementations: tuple[Implementation, ...]
    status: IndexStatus
    issues: tuple[str, ...]
    reference_relationships: tuple[ReferenceRelationship, ...]
    provenance_trust: str = "UNVERIFIED_INDEXER_ORIGIN"
    coverage: str = "UNKNOWN"

    def related_files(self, symbol: str, *, local_path: str | None = None) -> tuple[str, ...]:
        """Files with indexed definition/reference occurrences for this symbol."""
        return tuple(
            sorted(
                {
                    x.path
                    for x in self.definitions(symbol, local_path=local_path)
                    + self.references(symbol, local_path=local_path)
                }
            )
        )

    def definitions(self, symbol: str, *, local_path: str | None = None) -> tuple[SymbolLocation, ...]:
        return self._locations(symbol, local_path, definition=True)

    def references(self, symbol: str, *, local_path: str | None = None) -> tuple[SymbolLocation, ...]:
        if symbol.startswith("local ") and local_path is None:
            raise ValueError("local symbol requires a document path")

        def key(name: str, path: str | None) -> tuple[str, str | None]:
            return (name, path if name.startswith("local ") else None)

        adjacency: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {}
        for rel in self.reference_relationships:
            left, right = key(rel.source, rel.document), key(rel.target, rel.document)
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        seen = {key(symbol, local_path)}
        pending = list(seen)
        while pending:
            for other in adjacency.get(pending.pop(), set()):
                if other not in seen:
                    seen.add(other)
                    pending.append(other)
        return tuple(x for x in self.locations if not x.definition and key(x.symbol, x.path) in seen)

    def _locations(self, symbol: str, local_path: str | None, *, definition: bool) -> tuple[SymbolLocation, ...]:
        if symbol.startswith("local ") and local_path is None:
            raise ValueError("local symbol requires a document path")
        return tuple(
            x
            for x in self.locations
            if x.symbol == symbol
            and x.definition == definition
            and (not symbol.startswith("local ") or x.path == local_path)
        )

    def find_implementations(self, symbol: str, *, local_path: str | None = None) -> tuple[Implementation, ...]:
        if symbol.startswith("local ") and local_path is None:
            raise ValueError("local symbol requires a document path")
        return tuple(
            x
            for x in self.implementations
            if x.implemented == symbol and (not symbol.startswith("local ") or x.document == local_path)
        )


@dataclass(frozen=True)
class LoadResult:
    status: IndexStatus
    graph: SymbolGraph | None
    issues: tuple[str, ...]


class ScopeMismatch(ReadError):
    """Authorization scope mismatch: never fall back to another repository."""


def _decode(data: bytes, timeout: float) -> dict[str, object]:
    if sys.platform != "linux":
        raise ReadError("isolated decoder unavailable on this platform")
    if len(data) > 4 * 1024 * 1024:
        raise ReadError("index byte limit exceeded")
    worker = Path(__file__).with_name("scip_decode_worker.py")
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", str(worker)],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env={"PATH": os.defpath, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadError("isolated decoder unavailable or timed out") from exc
    if proc.returncode < 0 or proc.returncode == 2 or len(proc.stdout) > 16 * 1024 * 1024:
        raise ReadError("isolated decoder resource limit unavailable")
    if proc.returncode:
        raise ReadError("invalid index")
    try:
        parsed = json.loads(proc.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ReadError("invalid decoder output") from exc
    if not isinstance(parsed, dict):
        raise ReadError("invalid decoder output")
    return parsed


def _safe_symbol(value: str) -> bool:
    return all(not unicodedata.category(char).startswith("C") and char not in "\u2028\u2029" for char in value)


def dependency_lock_fingerprint(reader: GitSnapshot, entries: tuple[Entry, ...]) -> str:
    """SHA-256 over a domain-separated, length-framed set of tracked lock blobs."""
    digest = hashlib.sha256(b"DEV13-locks-v1\0")
    for entry in sorted(entries, key=lambda item: item.path):
        if PurePosixPath(entry.path).name not in LOCKS:
            continue
        if entry.kind != "BLOB":
            raise ReadError("dependency lock is not a regular blob")
        raw = reader.read(entry)
        for field in (entry.path.encode("utf-8"), entry.oid.encode("ascii"), hashlib.sha256(raw).digest()):
            digest.update(len(field).to_bytes(4, "big"))
            digest.update(field)
    return digest.hexdigest()


def load_index(reader: GitSnapshot, manifest: IndexManifest, data: bytes) -> LoadResult:  # noqa: PLR0911, PLR0912, PLR0915
    actual = reader.snapshot.identity
    claimed = manifest.identity
    if (actual.tenant_id, actual.owner_id, actual.binding_id) != (
        claimed.tenant_id,
        claimed.owner_id,
        claimed.binding_id,
    ):
        raise ScopeMismatch("index authorization scope mismatch")
    if actual.commit != claimed.commit or actual.revision != claimed.revision or reader.tree != manifest.tree:
        return LoadResult(IndexStatus.STALE, None, ("snapshot revision mismatch",))
    if len(data) > 4 * 1024 * 1024:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("index byte limit exceeded",))
    if hashlib.sha256(data).hexdigest() != manifest.index_digest:
        return LoadResult(IndexStatus.INVALID, None, ("index digest mismatch",))
    remaining = reader.deadline - time.monotonic()
    if remaining <= 0:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("time budget exhausted",))
    try:
        index = _decode(data, min(remaining, reader.limits.process_seconds))
    except ReadError as exc:
        return LoadResult(
            IndexStatus.UNAVAILABLE if "unavailable" in str(exc) or "timed out" in str(exc) else IndexStatus.INVALID,
            None,
            (str(exc),),
        )
    if (
        index["version"] != manifest.protocol_version
        or index["tool"] != manifest.indexer
        or index["tool_version"] != manifest.indexer_version
        or index["version"] != 0
    ):
        return LoadResult(IndexStatus.INVALID, None, ("index metadata mismatch or unsupported version",))
    if index["encoding"] not in (0, 1, 2):
        return LoadResult(IndexStatus.INVALID, None, ("invalid source text encoding",))
    if index["encoding"] != 1:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("source text encoding is not explicit UTF-8",))
    try:
        entries, tree_issues = reader.entries()
    except (ReadError, UnicodeError, OSError):
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("snapshot tree unavailable",))
    tracked = {entry.path: entry for entry in entries}
    lock_entries = tuple(entry for entry in entries if PurePosixPath(entry.path).name in LOCKS)
    if manifest.dependency_lock_digest is not None:
        if tree_issues:
            return LoadResult(IndexStatus.UNAVAILABLE, None, ("snapshot tree is incomplete",))
        try:
            lock_digest = dependency_lock_fingerprint(reader, entries)
        except ReadError:
            return LoadResult(IndexStatus.UNAVAILABLE, None, ("dependency lock could not be verified",))
        if lock_digest != manifest.dependency_lock_digest:
            return LoadResult(IndexStatus.STALE, None, ("dependency lock digest mismatch",))
    expected = {s.path: s for s in manifest.sources}
    if len(expected) != len(manifest.sources):
        return LoadResult(IndexStatus.INVALID, None, ("duplicate source provenance",))
    docs = index["docs"]
    if not isinstance(docs, list):
        return LoadResult(IndexStatus.INVALID, None, ("invalid document list",))
    locations: list[SymbolLocation] = []
    relations: list[Implementation] = []
    reference_relations: list[ReferenceRelationship] = []
    issues = list(tree_issues)
    if index["external_relationships"]:
        issues.append("external symbol relationships not interpreted")
    if lock_entries and manifest.dependency_lock_digest is None:
        issues.append("dependency lock provenance unavailable")
    seen: set[str] = set()
    try:
        for doc in docs:
            if time.monotonic() > reader.deadline:
                raise ReadError("index validation timeout")
            path = validate_git_path(doc["path"])
            if path in seen:
                return LoadResult(IndexStatus.INVALID, None, ("duplicate document",))
            seen.add(path)
            name = PurePosixPath(path).name.casefold()
            if (
                name.startswith(".env")
                or name in {"credentials", "id_rsa", "id_ed25519"}
                or name.endswith((".pem", ".key", ".p12", ".pfx"))
            ):
                return LoadResult(IndexStatus.UNAVAILABLE, None, ("sensitive source withheld",))
            entry = tracked.get(path)
            source = expected.get(path)
            if entry is None and tree_issues:
                return LoadResult(IndexStatus.UNAVAILABLE, None, ("snapshot tree is incomplete",))
            if entry is None or entry.kind != "BLOB":
                return LoadResult(IndexStatus.INVALID, None, ("document is not a tracked regular blob",))
            if source is None or source.blob_id != entry.oid:
                return LoadResult(IndexStatus.STALE, None, ("source provenance mismatch",))
            raw = reader.read(entry)
            digest = hashlib.sha256(raw).hexdigest()
            if b"\x00" in raw or raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                return LoadResult(IndexStatus.UNAVAILABLE, None, ("binary or LFS source unavailable",))
            if digest != source.digest:
                return LoadResult(IndexStatus.STALE, None, ("source digest mismatch",))
            if doc["text_digest"] is not None and doc["text_digest"] != digest:
                return LoadResult(IndexStatus.STALE, None, ("indexed document text differs from source",))
            position_encoding = doc["encoding"]
            if position_encoding == 0 and raw.isascii():
                # All three unit encodings coincide for ASCII; coverage remains partial.
                position_encoding = 1
                issues.append(f"{display_path(path)}: unspecified position encoding (ASCII only)")
            elif position_encoding == 0:
                issues.append(f"{display_path(path)}: unspecified position encoding")
                continue
            elif position_encoding not in (1, 2, 3):
                return LoadResult(IndexStatus.INVALID, None, ("invalid position encoding",))
            for symbol, roles, coords in doc["occurrences"]:
                if time.monotonic() > reader.deadline:
                    raise ReadError("index validation timeout")
                if not symbol:
                    continue
                if roles < 0 or not _safe_symbol(symbol):
                    raise ValueError("invalid symbol or roles")
                if roles & ~127:
                    issues.append(f"{display_path(path)}: unknown symbol role")
                positions = tuple(coords)
                span = convert_range(raw, position_encoding, positions)
                locations.append(
                    SymbolLocation(symbol, path, span, entry.oid, digest, bool(roles & 1), positions, position_encoding)
                )
            for implementer, implemented in doc["relationships"]:
                if not _safe_symbol(implementer + implemented):
                    raise ValueError("invalid relationship")
                relations.append(Implementation(implementer, implemented, path))
            for left, right in doc["reference_relationships"]:
                if not _safe_symbol(left + right):
                    raise ValueError("invalid relationship")
                reference_relations.append(ReferenceRelationship(left, right, path))
            if doc["definition_relationships"]:
                issues.append(f"{display_path(path)}: definition relationships not interpreted")
    except ReadError:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("source or time budget unavailable",))
    except (ValueError, UnicodeError, KeyError, TypeError):
        return LoadResult(IndexStatus.INVALID, None, ("invalid document or range",))
    if set(expected) != seen:
        issues.append("source provenance and indexed document set differ")
    status = IndexStatus.PARTIAL if issues else IndexStatus.VALIDATED
    return LoadResult(
        status,
        SymbolGraph(manifest, tuple(locations), tuple(relations), status, tuple(issues), tuple(reference_relations)),
        tuple(issues),
    )


def fallback_text(
    reader: GitSnapshot, inv: RepositoryInventory, manifest: IndexManifest | None, data: bytes | None, query: str
) -> tuple[LoadResult | None, CodeSearchResult | None]:
    """Return a validated index or explicitly text-only same-snapshot fallback."""
    if inv.identity != reader.snapshot.identity:
        raise ScopeMismatch("inventory authorization scope mismatch")
    if manifest is not None:
        actual, claimed = reader.snapshot.identity, manifest.identity
        if (actual.tenant_id, actual.owner_id, actual.binding_id) != (
            claimed.tenant_id,
            claimed.owner_id,
            claimed.binding_id,
        ):
            raise ScopeMismatch("index authorization scope mismatch")
    result = load_index(reader, manifest, data) if manifest is not None and data is not None else None
    if result is not None and result.status == IndexStatus.VALIDATED and result.graph is not None:
        return result, None
    return result, search(reader, inv, query)


@dataclass(frozen=True)
class NavigationResult:
    index: LoadResult | None
    locations: tuple[SymbolLocation, ...]
    implementations: tuple[Implementation, ...]
    fallback: CodeSearchResult | None
    truncated: bool


def navigate(
    reader: GitSnapshot,
    inv: RepositoryInventory,
    manifest: IndexManifest | None,
    data: bytes | None,
    *,
    symbol: str,
    text_query: str,
    operation: str,
    local_path: str | None = None,
) -> NavigationResult:
    """Query index-reported relationships, using same-snapshot text when incomplete."""
    if operation not in {"definition", "reference", "implementation"}:
        raise ValueError("invalid navigation operation")
    if not symbol or len(symbol.encode("utf-8")) > 4096:
        raise ValueError("invalid symbol")
    if local_path is not None:
        validate_git_path(local_path)
    result, text = fallback_text(reader, inv, manifest, data, text_query)
    graph = result.graph if result else None
    locations: tuple[SymbolLocation, ...] = ()
    relations: tuple[Implementation, ...] = ()
    if graph is not None:
        if operation == "definition":
            locations = graph.definitions(symbol, local_path=local_path)
        elif operation == "reference":
            locations = graph.references(symbol, local_path=local_path)
        else:
            relations = graph.find_implementations(symbol, local_path=local_path)
    if text is None and not locations and not relations:
        text = search(reader, inv, text_query)
    truncated = len(locations) > reader.limits.hits or len(relations) > reader.limits.hits
    return NavigationResult(result, locations[: reader.limits.hits], relations[: reader.limits.hits], text, truncated)
