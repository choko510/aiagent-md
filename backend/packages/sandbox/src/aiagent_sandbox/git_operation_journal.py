"""Durable, authenticated local fence for an isolated Git provider.

The journal does not grant authority or prove a CAS happened. A missing or
incomplete record must remain UNKNOWN; never infer success from Git HEAD.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from aiagent_development.code_intelligence.projection_stage import _open_trusted_parent
from aiagent_domain import canonical_json, is_uuid7

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_OID = re.compile(r"[a-f0-9]{40}\Z")
_DOMAIN = b"aiagent-git-operation-journal-v1\0"
_MAX_RECORDS = 12_000


class GitJournalError(RuntimeError):
    """The operation must remain unknown and must not be re-executed."""


def verify_confirmed_journal(key: bytes, envelope: Any, prepared: dict[str, Any]) -> None:
    """Authenticate the immutable confirmation corresponding to a verified preparation."""
    try:
        if len(key) < 32 or not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
            raise ValueError
        payload, mac = envelope["payload"], envelope["mac"]
        if not isinstance(payload, dict) or not isinstance(mac, str):
            raise ValueError
        expected = {**prepared, "stage": "confirmed"}
        signature = hmac.new(key, _DOMAIN + canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
        if payload != expected or not hmac.compare_digest(mac, signature):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise GitJournalError("confirmed journal authentication failed") from exc


def verify_prepared_journal(
    key: bytes,
    envelope: Any,
    *,
    operation: str,
    attempt: str,
    binding: str,
    generation: int,
    branch: str,
    request_sha256: str,
    parent: str,
) -> dict[str, Any]:
    """Authenticate exact pre-CAS evidence; this never asserts a CAS occurred."""
    try:
        if len(key) < 32 or not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
            raise ValueError
        payload, mac = envelope["payload"], envelope["mac"]
        if not isinstance(payload, dict) or not isinstance(mac, str):
            raise ValueError
        expected_fields = {
            "version",
            "operation",
            "stage",
            "request_sha256",
            "attempt",
            "binding",
            "generation",
            "branch",
            "parent",
            "commit",
            "tree",
        }
        mac_expected = hmac.new(key, _DOMAIN + canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
        if (
            set(payload) != expected_fields
            or not hmac.compare_digest(mac, mac_expected)
            or payload["version"] != 1
            or payload["stage"] != "prepared"
            or payload["operation"] != operation
            or payload["attempt"] != attempt
            or payload["binding"] != binding
            or payload["generation"] != str(generation)
            or payload["branch"] != branch
            or payload["request_sha256"] != request_sha256
            or payload["parent"] != parent
            or not is_uuid7(operation)
            or not is_uuid7(attempt)
            or not is_uuid7(binding)
            or type(generation) is not int
            or generation < 1
            or not _SHA256.fullmatch(request_sha256)
            or any(
                not isinstance(payload[k], str) or _OID.fullmatch(payload[k]) is None
                for k in ("parent", "commit", "tree")
            )
            or payload["commit"] == parent
        ):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError) as exc:
        raise GitJournalError("prepared journal authentication failed") from exc


class GitOperationJournal:
    """One-shot records in an externally provisioned, persistent private directory."""

    def __init__(self, directory: Path, key: bytes, *, max_records: int = _MAX_RECORDS) -> None:
        if (
            sys.platform != "linux"
            or len(key) < 32
            or type(max_records) is not int
            or not 3 <= max_records <= _MAX_RECORDS
        ):
            raise GitJournalError("journal unavailable")
        try:
            fd = _open_trusted_parent(directory)
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                os.close(fd)
                raise GitJournalError("journal directory is not private")
        except OSError as exc:
            raise GitJournalError("journal unavailable") from exc
        self._fd, self._key, self._max_records = fd, key, max_records

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def _name(self, operation: str, stage: str) -> str:
        if not is_uuid7(operation) or stage not in {"admitted", "prepared", "confirmed"}:
            raise GitJournalError("invalid journal identity")
        return operation + "." + stage

    def _write(self, operation: str, stage: str, fields: dict[str, str]) -> None:
        name = self._name(operation, stage)
        payload = {"version": 1, "operation": operation, "stage": stage, **fields}
        body = canonical_json(payload).encode("utf-8")
        proof = hmac.new(self._key, _DOMAIN + body, hashlib.sha256).hexdigest()
        data = canonical_json({"payload": payload, "mac": proof}).encode("utf-8")
        if len(data) > 4096:
            raise GitJournalError("journal record too large")
        import fcntl

        lock_fd = -1
        try:
            # Every writer opens its own directory description so flock also
            # serializes threads and distinct worker processes. A contended or
            # full journal fails closed; no prior replay evidence is removed.
            lock_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(lock_fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise GitJournalError("journal directory is not private")
            with os.scandir(lock_fd) as entries:
                for count, _entry in enumerate(entries, start=1):
                    if count >= self._max_records:
                        raise GitJournalError("journal capacity exhausted")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
            try:
                view = memoryview(data)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError("short journal write")
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self._fd)
        except OSError as exc:
            # A partial record intentionally remains as a replay fence.
            raise GitJournalError("journal write failed or operation already exists") from exc
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)

    def read(self, operation: str, stage: str) -> dict[str, Any]:
        name = self._name(operation, stage)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or not 1 <= info.st_size <= 4096
                ):
                    raise GitJournalError("journal record invalid")
                data = os.read(fd, 4097)
                if len(data) != info.st_size:
                    raise GitJournalError("journal record incomplete")
            finally:
                os.close(fd)
            envelope = json.loads(data)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError
            payload, mac = envelope["payload"], envelope["mac"]
            if not isinstance(payload, dict) or not isinstance(mac, str):
                raise ValueError
            body = canonical_json(payload).encode("utf-8")
            expected = hmac.new(self._key, _DOMAIN + body, hashlib.sha256).hexdigest()
            if (
                canonical_json(envelope).encode("utf-8") != data
                or not hmac.compare_digest(mac, expected)
                or payload.get("version") != 1
                or payload.get("operation") != operation
                or payload.get("stage") != stage
            ):
                raise ValueError
            return payload
        except (OSError, ValueError, TypeError) as exc:
            raise GitJournalError("journal record unavailable or invalid") from exc

    def export_prepared(self, operation: str) -> dict[str, Any]:
        """Export authenticated bytes only after the persisted record revalidates."""
        payload = self.read(operation, "prepared")
        mac = hmac.new(self._key, _DOMAIN + canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
        return {"payload": payload, "mac": mac}

    def export_confirmed(self, operation: str) -> dict[str, Any]:
        """Export a revalidated confirmed record without asserting canonical success."""
        payload = self.read(operation, "confirmed")
        mac = hmac.new(self._key, _DOMAIN + canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
        return {"payload": payload, "mac": mac}

    def admit(
        self, operation: str, request_sha256: str, attempt: str, binding: str, generation: int, branch: str
    ) -> None:
        if (
            not _SHA256.fullmatch(request_sha256)
            or not is_uuid7(attempt)
            or not is_uuid7(binding)
            or type(generation) is not int
            or generation < 1
            or not isinstance(branch, str)
            or not 1 <= len(branch) <= 200
        ):
            raise GitJournalError("invalid request digest")
        self._write(
            operation,
            "admitted",
            {
                "request_sha256": request_sha256,
                "attempt": attempt,
                "binding": binding,
                "generation": str(generation),
                "branch": branch,
            },
        )

    def prepare(self, operation: str, request_sha256: str, parent: str, commit: str, tree: str) -> None:
        admitted = self.read(operation, "admitted")
        if (
            set(admitted)
            != {"version", "operation", "stage", "request_sha256", "attempt", "binding", "generation", "branch"}
            or admitted.get("request_sha256") != request_sha256
            or not is_uuid7(admitted.get("attempt"))
            or not is_uuid7(admitted.get("binding"))
            or any(_OID.fullmatch(value) is None for value in (parent, commit, tree))
            or commit == parent
        ):
            raise GitJournalError("journal preparation mismatch")
        self._write(
            operation,
            "prepared",
            {
                "request_sha256": request_sha256,
                "attempt": admitted["attempt"],
                "binding": admitted["binding"],
                "generation": admitted["generation"],
                "branch": admitted["branch"],
                "parent": parent,
                "commit": commit,
                "tree": tree,
            },
        )

    def confirm(self, operation: str, request_sha256: str, parent: str, commit: str, tree: str) -> None:
        admitted = self.read(operation, "admitted")
        expected = {
            "version": 1,
            "operation": operation,
            "stage": "prepared",
            "request_sha256": request_sha256,
            "attempt": admitted.get("attempt"),
            "binding": admitted.get("binding"),
            "generation": admitted.get("generation"),
            "branch": admitted.get("branch"),
            "parent": parent,
            "commit": commit,
            "tree": tree,
        }
        if self.read(operation, "prepared") != expected:
            raise GitJournalError("journal confirmation mismatch")
        self._write(
            operation,
            "confirmed",
            {
                "request_sha256": request_sha256,
                "attempt": admitted["attempt"],
                "binding": admitted["binding"],
                "generation": admitted["generation"],
                "branch": admitted["branch"],
                "parent": parent,
                "commit": commit,
                "tree": tree,
            },
        )
