"""Read-only SCIP navigation over an explicitly bound, immutable snapshot.

The manifest is supplied by trusted integration code; validation does not prove
indexer identity or semantic correctness. No external services are contacted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import chain
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Generic, TypeVar

from .git_reader import GitSnapshot
from .inventory import LOCKS, RepositoryInventory
from .models import Entry, Identity, ReadError
from .paths import display_path, validate_git_path
from .scip_ranges import ByteRange, PositionBudgetExceeded, PositionIndex
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
    document: str | None
    confidence: str = "PRECISE_INDEX_REPORTED"


@dataclass(frozen=True)
class ReferenceRelationship:
    source: str
    target: str
    document: str | None


_T = TypeVar("_T")
_Key = tuple[str, str | None]


@dataclass(frozen=True)
class GraphMatches(Generic[_T]):  # noqa: UP046
    items: tuple[_T, ...]
    truncated: bool


class GraphTimeExceeded(ReadError):
    """A graph search or index build exceeded the snapshot/query deadline."""


class GraphLimitExceeded(ReadError):
    """The tuple-returning convenience API cannot return a complete result."""


@dataclass(frozen=True)
class SymbolGraph:
    manifest: IndexManifest
    locations: tuple[SymbolLocation, ...]
    implementations: tuple[Implementation, ...]
    status: IndexStatus
    issues: tuple[str, ...]
    reference_relationships: tuple[ReferenceRelationship, ...]
    definition_relationships: tuple[ReferenceRelationship, ...]
    deadline: float
    max_results: int
    provenance_trust: str = "UNVERIFIED_INDEXER_ORIGIN"
    coverage: str = "UNKNOWN"
    _defs: Mapping[_Key, tuple[SymbolLocation, ...]] = field(init=False, repr=False, compare=False)
    _refs: Mapping[_Key, tuple[SymbolLocation, ...]] = field(init=False, repr=False, compare=False)
    _impls: Mapping[_Key, tuple[Implementation, ...]] = field(init=False, repr=False, compare=False)
    _def_links: Mapping[_Key, tuple[_Key, ...]] = field(init=False, repr=False, compare=False)
    _ref_links: Mapping[_Key, tuple[_Key, ...]] = field(init=False, repr=False, compare=False)

    @staticmethod
    def _symbol_key(name: str, path: str | None) -> _Key:
        return name, path if name.startswith("local ") else None

    def _check(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise GraphTimeExceeded("graph search deadline exceeded")

    def __post_init__(self) -> None:
        if not math.isfinite(self.deadline) or self.max_results <= 0 or self.max_results > 100000:
            raise ValueError("invalid graph limits")
        if any(
            len(items) > 50000
            for items in (
                self.locations,
                self.implementations,
                self.reference_relationships,
                self.definition_relationships,
            )
        ):
            raise ValueError("graph input limit exceeded")
        definitions: dict[_Key, list[SymbolLocation]] = {}
        references: dict[_Key, list[SymbolLocation]] = {}
        implementations: dict[_Key, list[Implementation]] = {}
        definition_links: dict[_Key, list[_Key]] = {}
        reference_links: dict[_Key, list[_Key]] = {}
        for item in self.locations:
            self._check(self.deadline)
            target = definitions if item.definition else references
            target.setdefault(self._symbol_key(item.symbol, item.path), []).append(item)
        for implementation in self.implementations:
            self._check(self.deadline)
            implementations.setdefault(
                self._symbol_key(implementation.implemented, implementation.document), []
            ).append(implementation)
        for rel in self.definition_relationships:
            self._check(self.deadline)
            left, right = self._symbol_key(rel.source, rel.document), self._symbol_key(rel.target, rel.document)
            definition_links.setdefault(left, []).append(right)
        for rel in self.reference_relationships + self.definition_relationships:
            self._check(self.deadline)
            left, right = self._symbol_key(rel.source, rel.document), self._symbol_key(rel.target, rel.document)
            reference_links.setdefault(left, []).append(right)
            reference_links.setdefault(right, []).append(left)
        for attr, data in (
            ("_defs", definitions),
            ("_refs", references),
            ("_impls", implementations),
            ("_def_links", definition_links),
            ("_ref_links", reference_links),
        ):
            frozen = {}
            for key, values in data.items():
                self._check(self.deadline)
                frozen[key] = tuple(values)
            object.__setattr__(self, attr, MappingProxyType(frozen))
        self._check(self.deadline)

    def _limits(self, deadline: float | None, limit: int | None) -> tuple[float, int]:
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("invalid query deadline")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise ValueError("invalid query result limit")
        end = min(self.deadline, deadline) if deadline is not None else self.deadline
        self._check(end)
        return end, min(self.max_results, limit) if limit is not None else self.max_results

    def _walk(
        self, initial: tuple[_Key, ...], links: Mapping[_Key, tuple[_Key, ...]], deadline: float
    ) -> Iterator[_Key]:
        seen: set[_Key] = set()
        pending: list[_Key] = []
        for key in initial:
            self._check(deadline)
            if key not in seen:
                seen.add(key)
                pending.append(key)
        pending.reverse()
        while pending:
            self._check(deadline)
            key = pending.pop()
            yield key
            for other in links.get(key, ()):
                self._check(deadline)
                if other not in seen:
                    seen.add(other)
                    pending.append(other)

    def _collect(
        self,
        initial: tuple[_Key, ...],
        links: Mapping[_Key, tuple[_Key, ...]],
        buckets: Mapping[_Key, tuple[_T, ...]],
        deadline: float,
        limit: int,
    ) -> GraphMatches[_T]:
        result: list[_T] = []
        for key in self._walk(initial, links, deadline):
            for item in buckets.get(key, ()):
                self._check(deadline)
                if len(result) == limit:
                    partial = tuple(result)
                    self._check(deadline)
                    return GraphMatches(partial, True)
                result.append(item)
        self._check(deadline)
        complete = tuple(result)
        self._check(deadline)
        return GraphMatches(complete, False)

    def _key(self, symbol: str, local_path: str | None) -> _Key:
        if not isinstance(symbol, str) or not symbol or len(symbol) > 4096 or len(symbol.encode("utf-8")) > 4096:
            raise ValueError("invalid graph symbol")
        if local_path is not None:
            if not isinstance(local_path, str) or len(local_path) > 4096:
                raise ValueError("invalid local path")
            validate_git_path(local_path)
        if symbol.startswith("local ") and local_path is None:
            raise ValueError("local symbol requires a document path")
        return self._symbol_key(symbol, local_path)

    def query_definitions(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> GraphMatches[SymbolLocation]:
        end, cap = self._limits(deadline, limit)
        return self._collect((self._key(symbol, local_path),), self._def_links, self._defs, end, cap)

    def query_references(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> GraphMatches[SymbolLocation]:
        end, cap = self._limits(deadline, limit)
        return self._collect((self._key(symbol, local_path),), self._ref_links, self._refs, end, cap)

    def query_implementations(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> GraphMatches[Implementation]:
        end, cap = self._limits(deadline, limit)
        return self._collect((self._key(symbol, local_path),), {}, self._impls, end, cap)

    @staticmethod
    def _complete(matches: GraphMatches[_T]) -> tuple[_T, ...]:
        if matches.truncated:
            raise GraphLimitExceeded("graph result limit exceeded; use query_* for partial results")
        return matches.items

    def definitions(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> tuple[SymbolLocation, ...]:
        return self._complete(self.query_definitions(symbol, local_path=local_path, deadline=deadline, limit=limit))

    def references(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> tuple[SymbolLocation, ...]:
        return self._complete(self.query_references(symbol, local_path=local_path, deadline=deadline, limit=limit))

    def find_implementations(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> tuple[Implementation, ...]:
        return self._complete(self.query_implementations(symbol, local_path=local_path, deadline=deadline, limit=limit))

    def query_related_files(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> GraphMatches[str]:
        end, cap = self._limits(deadline, limit)
        definitions = self.query_definitions(symbol, local_path=local_path, deadline=end, limit=cap)
        references = self.query_references(symbol, local_path=local_path, deadline=end, limit=cap)
        implementations = self.query_implementations(symbol, local_path=local_path, deadline=end, limit=cap)
        roots: list[_Key] = []
        for rel in implementations.items:
            self._check(end)
            roots.append(self._symbol_key(rel.implementing, rel.document))
        impl_defs = self._collect(tuple(roots), self._def_links, self._defs, end, cap)
        seen_paths: set[str] = set()
        ordered: list[str] = []
        for item in chain(definitions.items, references.items, impl_defs.items):
            self._check(end)
            if item.path not in seen_paths:
                if len(ordered) == cap:
                    partial = tuple(ordered)
                    self._check(end)
                    return GraphMatches(partial, True)
                seen_paths.add(item.path)
                ordered.append(item.path)
        self._check(end)
        incomplete = definitions.truncated or references.truncated or implementations.truncated or impl_defs.truncated
        complete = tuple(ordered)
        self._check(end)
        return GraphMatches(complete, incomplete)

    def related_files(
        self, symbol: str, *, local_path: str | None = None, deadline: float | None = None, limit: int | None = None
    ) -> tuple[str, ...]:
        return self._complete(self.query_related_files(symbol, local_path=local_path, deadline=deadline, limit=limit))


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
        for part in (entry.path.encode("utf-8"), entry.oid.encode("ascii"), hashlib.sha256(raw).digest()):
            digest.update(len(part).to_bytes(4, "big"))
            digest.update(part)
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
    if len(manifest.sources) > reader.limits.files:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("source provenance count limit exceeded",))
    expected = {s.path: s for s in manifest.sources}
    if len(expected) != len(manifest.sources):
        return LoadResult(IndexStatus.INVALID, None, ("duplicate source provenance",))
    docs = index["docs"]
    if not isinstance(docs, list):
        return LoadResult(IndexStatus.INVALID, None, ("invalid document list",))
    locations: list[SymbolLocation] = []
    relations: list[Implementation] = []
    reference_relations: list[ReferenceRelationship] = []
    definition_relations: list[ReferenceRelationship] = []
    issues = list(tree_issues)
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
            if raw.startswith(b"\xef\xbb\xbf"):
                return LoadResult(IndexStatus.INVALID, None, ("ambiguous UTF-8 BOM",))
            if b"\x00" in raw or raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                return LoadResult(IndexStatus.UNAVAILABLE, None, ("binary or LFS source unavailable",))
            if digest != source.digest:
                return LoadResult(IndexStatus.STALE, None, ("source digest mismatch",))
            if doc["text_digest"] is not None and doc["text_digest"] != digest:
                return LoadResult(IndexStatus.STALE, None, ("indexed document text differs from source",))
            positions_index = PositionIndex(raw, deadline=reader.deadline)
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
            unknown_roles = False
            for symbol, roles, coords in doc["occurrences"]:
                if time.monotonic() > reader.deadline:
                    raise ReadError("index validation timeout")
                if not symbol:
                    continue
                if roles < 0 or not _safe_symbol(symbol):
                    raise ValueError("invalid symbol or roles")
                if roles & ~127:
                    unknown_roles = True
                positions = tuple(coords)
                span = positions_index.convert(position_encoding, positions)
                locations.append(
                    SymbolLocation(symbol, path, span, entry.oid, digest, bool(roles & 1), positions, position_encoding)
                )
            if unknown_roles:
                issues.append(f"{display_path(path)}: unknown symbol role")
            for implementer, implemented in doc["relationships"]:
                if not _safe_symbol(implementer + implemented):
                    raise ValueError("invalid relationship")
                relations.append(Implementation(implementer, implemented, path))
            for left, right in doc["reference_relationships"]:
                if not _safe_symbol(left + right):
                    raise ValueError("invalid relationship")
                reference_relations.append(ReferenceRelationship(left, right, path))
            for left, right in doc["definition_relationships"]:
                if not _safe_symbol(left + right):
                    raise ValueError("invalid relationship")
                definition_relations.append(ReferenceRelationship(left, right, path))
        external_relationships = index["external_relationships"]
        if not isinstance(external_relationships, list):
            raise ValueError("invalid external relationship list")
        for left, right, is_definition, is_reference, is_implementation in external_relationships:
            if not _safe_symbol(left + right):
                raise ValueError("invalid external relationship")
            # An external SymbolInformation has no document, hence it cannot
            # safely bind a document-scoped local symbol.
            if left.startswith("local ") or right.startswith("local "):
                issues.append("external relationship has unresolved local scope")
                continue
            if is_definition:
                definition_relations.append(ReferenceRelationship(left, right, None))
            if is_reference:
                reference_relations.append(ReferenceRelationship(left, right, None))
            if is_implementation:
                relations.append(Implementation(left, right, None))
    except PositionBudgetExceeded as exc:
        return LoadResult(IndexStatus.UNAVAILABLE, None, (str(exc),))
    except ReadError:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("source or time budget unavailable",))
    except (ValueError, UnicodeError, KeyError, TypeError):
        return LoadResult(IndexStatus.INVALID, None, ("invalid document or range",))
    if set(expected) != seen:
        issues.append("source provenance and indexed document set differ")
    # Never resolve relationship endpoints against another repository. Record
    # incompleteness if the current index has no occurrence for an endpoint.
    local_symbols = {(item.symbol, item.path if item.symbol.startswith("local ") else None) for item in locations}
    edges = [(r.source, r.target, r.document) for r in reference_relations + definition_relations]
    edges.extend((r.implementing, r.implemented, r.document) for r in relations)
    for left, right, document in edges:
        left_key = (left, document if left.startswith("local ") else None)
        right_key = (right, document if right.startswith("local ") else None)
        if left_key not in local_symbols or right_key not in local_symbols:
            issues.append("relationship has unresolved in-snapshot endpoint")
            break
    definition_symbols = {
        (item.symbol, item.path if item.symbol.startswith("local ") else None) for item in locations if item.definition
    }
    # Resolve definition aliases backwards in linear time, including chains and
    # cycles, without looking outside this snapshot's verified occurrences.
    reverse_definitions: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {}
    for relation in definition_relations:
        source_key = (relation.source, relation.document if relation.source.startswith("local ") else None)
        target_key = (relation.target, relation.document if relation.target.startswith("local ") else None)
        reverse_definitions.setdefault(target_key, set()).add(source_key)
    resolved_symbols = set(definition_symbols)
    pending = list(resolved_symbols)
    while pending:
        if time.monotonic() >= reader.deadline:
            return LoadResult(IndexStatus.UNAVAILABLE, None, ("relationship time budget exceeded",))
        for source_key in reverse_definitions.get(pending.pop(), set()):
            if source_key not in resolved_symbols:
                resolved_symbols.add(source_key)
                pending.append(source_key)
    if any(
        (r.target, r.document if r.target.startswith("local ") else None) not in resolved_symbols
        for r in definition_relations
    ):
        issues.append("definition relationship has no in-snapshot target definition")
    if any(
        (r.implementing, r.document if r.implementing.startswith("local ") else None) not in resolved_symbols
        or (r.implemented, r.document if r.implemented.startswith("local ") else None) not in resolved_symbols
        for r in relations
    ):
        issues.append("implementation relationship has an unresolved in-snapshot definition")
    if any(
        (r.source, r.document if r.source.startswith("local ") else None) not in resolved_symbols
        or (r.target, r.document if r.target.startswith("local ") else None) not in resolved_symbols
        for r in reference_relations
    ):
        issues.append("reference relationship has an unresolved in-snapshot definition")
    status = IndexStatus.PARTIAL if issues else IndexStatus.VALIDATED
    try:
        graph = SymbolGraph(
            manifest,
            tuple(locations),
            tuple(relations),
            status,
            tuple(issues),
            tuple(reference_relations),
            tuple(definition_relations),
            reader.deadline,
            reader.limits.hits,
        )
    except GraphTimeExceeded:
        return LoadResult(IndexStatus.UNAVAILABLE, None, ("graph indexing deadline exceeded",))
    return LoadResult(status, graph, tuple(issues))


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
    query_status: str = "COMPLETE"
    query_issue: str | None = None


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
    truncated = False
    try:
        if graph is not None:
            if operation == "definition":
                matches = graph.query_definitions(symbol, local_path=local_path)
                locations, truncated = matches.items, matches.truncated
            elif operation == "reference":
                matches = graph.query_references(symbol, local_path=local_path)
                locations, truncated = matches.items, matches.truncated
            else:
                implementations = graph.query_implementations(symbol, local_path=local_path)
                relations, truncated = implementations.items, implementations.truncated
    except GraphTimeExceeded as exc:
        return NavigationResult(result, (), (), text, False, "UNAVAILABLE", str(exc))
    if text is None and not locations and not relations:
        text = search(reader, inv, text_query)
    status = "COMPLETE"
    if truncated or result is None or result.status != IndexStatus.VALIDATED:
        status = "PARTIAL"
    if text is not None:
        status = "UNAVAILABLE" if text.coverage.value == "UNAVAILABLE" else "PARTIAL"
    return NavigationResult(result, locations, relations, text, truncated, status)
