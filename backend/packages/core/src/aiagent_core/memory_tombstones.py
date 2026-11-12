"""Append-only, externally sealed tombstone log for Memory revocation.

PostgreSQL PITR restores an earlier DB state and can resurrect a Memory row whose
revocation lived later in WAL. The external log and its independently sealed head
must therefore live on storage NOT restored with the application database.

Format and guarantees:
* each JSONL event is HMAC-SHA256 chained (tests may use an unkeyed SHA chain);
* `<log>.head` stores the highest generation + chain head and its own MAC;
* readers fail closed if the log and sealed head disagree, a key is missing, or
  any event/sidecar MAC is invalid;
* append is serialized with an OS file lock across API processes;
* appends fsync event bytes, then atomically replace+fsync the head seal;
* an operator explicitly initializes the empty generation-0 log, and explicitly
  reseals only after a valid chain is inspected (crash recovery). Runtime code
  never recreates a missing log or sidecar.

Revocation order is external append+seal first, PostgreSQL update second. If DB
commit fails, the extra tombstone causes safe over-revocation and is replayed after
restore. WAL/backups may still retain old ciphertext; key destruction and retention
remain the physical-erasure boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from aiagent_core import config

GENESIS = "0" * 64
_EVENT_KINDS = frozenset({"created", "revoked", "updated", "consent_revoked"})
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class EventBody(TypedDict):
    generation: int
    kind: str
    tenant: str
    owner: str
    task: str
    memory: str
    revision: int
    recorded_at: str


class SealPayload(TypedDict):
    generation: int
    chain_head: str


class WatermarkPayload(TypedDict):
    generation: int
    chain_head: str
    last_event_at: str


class TombstoneError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tombstone:
    generation: int
    kind: str
    tenant_id: str
    owner_id: str
    task_id: str
    memory_id: str
    revision: int
    recorded_at: str
    prev: str
    chain_head: str


class TombstoneLog:
    """HMAC-chained JSONL plus separately sealed high-watermark sidecar."""

    def __init__(self, path: str | Path, *, integrity_key: bytes | None = None):
        self.path = Path(path)
        self.head_path = Path(str(self.path) + ".head")
        self.lock_path = Path(str(self.path) + ".lock")
        if integrity_key is not None and len(integrity_key) < 32:
            raise TombstoneError("tombstone integrity key must be at least 256 bits")
        self.integrity_key = integrity_key

    def _mac(self, value: object) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self.integrity_key is None:
            return hashlib.sha256(raw).hexdigest()
        return hmac.new(self.integrity_key, raw, hashlib.sha256).hexdigest()

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        # OS file locks serialize API replicas; the local lock also serializes
        # threads in one process on platforms whose byte-range lock is process-wide.
        lock_key = str(self.lock_path.resolve())
        with _LOCAL_LOCKS_GUARD:
            local_lock = _LOCAL_LOCKS.setdefault(lock_key, threading.RLock())
        with local_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+b") as f:
                if sys.platform == "win32":
                    import msvcrt

                    f.seek(0)
                    # Lock one byte from the start of the persistent lock file.
                    if self.lock_path.stat().st_size == 0:
                        f.write(b"\0")
                        f.flush()
                        os.fsync(f.fileno())
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _read_events_unlocked(self) -> list[Tombstone]:
        if not self.path.exists():
            return []
        out: list[Tombstone] = []
        prev = GENESIS
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                body: EventBody = {
                    "generation": int(raw["generation"]),
                    "kind": str(raw["kind"]),
                    "tenant": str(raw["tenant"]),
                    "owner": str(raw["owner"]),
                    "task": str(raw["task"]),
                    "memory": str(raw["memory"]),
                    "revision": int(raw["revision"]),
                    "recorded_at": str(raw["recorded_at"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise TombstoneError(f"tombstone log line {lineno} is malformed") from exc
            if raw.get("prev") != prev:
                raise TombstoneError(f"tombstone log predecessor broken at line {lineno}")
            expected = self._mac({"prev": prev, **body})
            if not hmac.compare_digest(str(raw.get("chain", "")), expected):
                raise TombstoneError(f"tombstone log HMAC chain broken at line {lineno}")
            if body["kind"] not in _EVENT_KINDS:
                raise TombstoneError(f"tombstone log line {lineno} has an unknown kind")
            out.append(
                Tombstone(
                    body["generation"],
                    body["kind"],
                    body["tenant"],
                    body["owner"],
                    body["task"],
                    body["memory"],
                    body["revision"],
                    body["recorded_at"],
                    prev,
                    expected,
                )
            )
            prev = expected
        if [e.generation for e in out] != list(range(1, len(out) + 1)):
            raise TombstoneError("tombstone generations must be contiguous from 1")
        return out

    def _seal_payload(self, entries: list[Tombstone]) -> SealPayload:
        return {
            "generation": entries[-1].generation if entries else 0,
            "chain_head": entries[-1].chain_head if entries else GENESIS,
        }

    def _read_seal_unlocked(self) -> SealPayload:
        if not self.head_path.exists():
            raise TombstoneError("external tombstone head seal is missing; Memory stays disabled")
        try:
            seal = json.loads(self.head_path.read_text(encoding="utf-8"))
            payload: SealPayload = {
                "generation": int(seal["generation"]),
                "chain_head": str(seal["chain_head"]),
            }
            expected = self._mac(payload)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TombstoneError("external tombstone head seal is malformed") from exc
        if not hmac.compare_digest(str(seal.get("mac", "")), expected):
            raise TombstoneError("external tombstone head seal authentication failed")
        return payload

    def _verify_seal_unlocked(self, entries: list[Tombstone]) -> tuple[int, str]:
        seal = self._read_seal_unlocked()
        actual = self._seal_payload(entries)
        if seal != actual:
            raise TombstoneError("external tombstone high-watermark does not match event log")
        return actual["generation"], actual["chain_head"]

    def _write_seal_unlocked(self, generation: int, chain_head: str) -> None:
        payload: SealPayload = {"generation": generation, "chain_head": chain_head}
        seal = json.dumps({**payload, "mac": self._mac(payload)}, sort_keys=True, separators=(",", ":")) + "\n"
        self.head_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.head_path.name + ".", dir=self.head_path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(seal.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.head_path)
            if os.name != "nt":
                dir_fd = os.open(self.head_path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)

    def initialize(self) -> None:
        """Create genesis log+seal once, under operator control; never overwrite state."""
        with self._lock():
            if self.path.exists() or self.head_path.exists():
                raise TombstoneError("tombstone log or seal already exists; refusing reinitialize")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "xb") as f:
                f.flush()
                os.fsync(f.fileno())
            self._write_seal_unlocked(0, GENESIS)

    def _read_unlocked(self) -> list[Tombstone]:
        entries = self._read_events_unlocked()
        self._verify_seal_unlocked(entries)
        return entries

    def read(self) -> list[Tombstone]:
        with self._lock():
            return self._read_unlocked()

    def read_events_for_reseal(self) -> list[Tombstone]:
        """Verify every event HMAC but intentionally ignore a stale/missing head seal.

        Exposed only for the explicit operator `memory-tombstone-reseal` recovery
        after reviewing a crash-consistent log tail. Never called by API/read paths.
        """
        with self._lock():
            return self._read_events_unlocked()

    def reseal_validated(self) -> tuple[int, str]:
        """Recover exactly one fsynced event whose atomic head replacement was interrupted.

        A missing seal, a seal ahead of the log, or a multi-generation discrepancy
        could mean the external log was rolled back/truncated. Those conditions are
        NOT repaired from the remaining log; restore the latest external backup and
        keep Memory disabled until its authenticated high-watermark is recovered.
        """
        with self._lock():
            entries = self._read_events_unlocked()
            seal = self._read_seal_unlocked()
            payload = self._seal_payload(entries)
            event_generation = payload["generation"]
            sealed_generation = seal["generation"]
            if event_generation == sealed_generation and payload["chain_head"] == seal["chain_head"]:
                return event_generation, str(payload["chain_head"])
            if event_generation != sealed_generation + 1:
                raise TombstoneError(
                    "log/seal generation gap is not a single interrupted append; restore external log backup"
                )
            if sealed_generation > 0 and entries[sealed_generation - 1].chain_head != seal["chain_head"]:
                raise TombstoneError("previously sealed tombstone prefix changed; restore external log backup")
            self._write_seal_unlocked(event_generation, str(payload["chain_head"]))
            return event_generation, str(payload["chain_head"])

    def head(self) -> tuple[int, str]:
        entries = self.read()
        payload = self._seal_payload(entries)
        return payload["generation"], payload["chain_head"]

    def append(
        self,
        *,
        kind: str,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        memory_id: str,
        revision: int,
        recorded_at: str,
    ) -> Tombstone:
        if kind not in _EVENT_KINDS:
            raise TombstoneError("unknown tombstone kind")
        with self._lock():
            entries = self._read_unlocked()
            generation = len(entries) + 1
            prev = entries[-1].chain_head if entries else GENESIS
            body = {
                "generation": generation,
                "kind": kind,
                "tenant": tenant_id,
                "owner": owner_id,
                "task": task_id,
                "memory": memory_id,
                "revision": revision,
                "recorded_at": recorded_at,
            }
            head = self._mac({"prev": prev, **body})
            line = json.dumps({**body, "prev": prev, "chain": head}, sort_keys=True, separators=(",", ":"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as f:
                f.write((line + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            # Seal after log fsync. If this fails, the next operation fails closed
            # until an operator validates the HMAC chain and explicitly reseals it.
            self._write_seal_unlocked(generation, head)
            return Tombstone(
                generation, kind, tenant_id, owner_id, task_id, memory_id, revision, recorded_at, prev, head
            )


def log_from_env(*, require_initialized: bool = True) -> TombstoneLog:
    path = os.environ.get("AIAGENT_MEMORY_TOMBSTONE_LOG")
    if not path:
        raise TombstoneError("AIAGENT_MEMORY_TOMBSTONE_LOG is not configured")
    key_setting = config.secret("AIAGENT_MEMORY_TOMBSTONE_HMAC_KEY", required=config.profile() != "test")
    key = None
    if key_setting is not None:
        try:
            key = bytes.fromhex(key_setting.reveal())
        except ValueError as exc:
            raise TombstoneError("tombstone HMAC key must be hex encoded") from exc
        if len(key) < 32:
            raise TombstoneError("tombstone HMAC key must be at least 256 bits")
    log = TombstoneLog(path, integrity_key=key)
    if require_initialized and config.profile() != "test" and (not log.path.is_file() or not log.head_path.is_file()):
        raise TombstoneError("external tombstone log and head seal must be operator-initialized")
    return log


def watermark_payload(entries: list[Tombstone]) -> WatermarkPayload:
    if not entries:
        return {"generation": 0, "chain_head": GENESIS, "last_event_at": "1970-01-01T00:00:00+00:00"}
    last = entries[-1]
    return {"generation": last.generation, "chain_head": last.chain_head, "last_event_at": last.recorded_at}
