"""Opt-in Git worker RPC building blocks; no production entry point is enabled.

The caller must provide an independently verified live-authority guard and OS
isolation. A signed request alone does not authorize an external effect.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiagent_core import config
from aiagent_core.rpc import Endpoint, RpcServer
from aiagent_development.git_commit_worker import IsolatedGitCommitWorker
from aiagent_domain import canonical_json, is_uuid7
from aiagent_domain.rpcwire import RpcError

from .binary_transfer import MAX_CHUNK, receive_chunks
from .git_operation_journal import GitJournalError, GitOperationJournal

_GRANT_DOMAIN = b"aiagent-git-worker-grant-v1\0"
_RESULT_DOMAIN = b"aiagent-git-worker-result-v1\0"
_TRANSFER_DOMAIN = b"aiagent-git-patch-transfer-v1\0"
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_OID = re.compile(r"[a-f0-9]{40}\Z")
_FIELDS = frozenset(
    {
        "operation",
        "tenant",
        "owner",
        "task",
        "run",
        "workspace",
        "goal",
        "lease",
        "restore",
        "worker_ref",
        "intent",
        "attempt",
        "effect_key",
        "binding",
        "patch_id",
        "cycle_id",
        "plan_ref",
        "generation",
        "branch",
        "parent",
        "patch_sha256",
        "patch_bytes",
        "paths",
        "message",
        "request_sha256",
        "expires_at",
    }
)


def sign_git_worker_grant(key: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    """Sign a request already authorized by the canonical Gateway owner."""
    if len(key) < 32 or set(payload) != _FIELDS:
        raise ValueError("invalid Git worker grant")
    body = canonical_json(payload).encode("utf-8")
    return {"payload": payload, "mac": hmac.new(key, _GRANT_DOMAIN + body, hashlib.sha256).hexdigest()}


def git_transfer_key(key: bytes, operation: str) -> bytes:
    if len(key) < 32 or not is_uuid7(operation):
        raise ValueError("invalid Git transfer identity")
    return hmac.digest(key, _TRANSFER_DOMAIN + operation.encode("ascii"), "sha256")


def git_transfer_binding(payload: dict[str, Any]) -> bytes:
    return hashlib.sha256(_TRANSFER_DOMAIN + canonical_json(payload).encode("utf-8")).digest()


def verify_git_worker_result(key: bytes, envelope: Any, expected: dict[str, Any], worker_id: str) -> dict[str, Any]:
    """Authenticate the worker result; callers must separately verify Git objects and DB authority."""
    if len(key) < 32 or not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
        raise ValueError("invalid Git worker result")
    result, mac = envelope["payload"], envelope["mac"]
    if (
        not isinstance(result, dict)
        or set(result) != _FIELDS | {"commit", "tree", "cas", "worker_id"}
        or not isinstance(mac, str)
    ):
        raise ValueError("invalid Git worker result")
    expected_mac = hmac.new(key, _RESULT_DOMAIN + canonical_json(result).encode(), hashlib.sha256).hexdigest()
    if (
        not hmac.compare_digest(mac, expected_mac)
        or set(expected) != _FIELDS
        or any(result[name] != value for name, value in expected.items())
        or result["worker_id"] != worker_id
        or result["cas"] != "CONFIRMED"
        or not isinstance(result["commit"], str)
        or not _OID.fullmatch(result["commit"])
        or not isinstance(result["tree"], str)
        or not _OID.fullmatch(result["tree"])
    ):
        raise ValueError("invalid Git worker result")
    return result


@dataclass
class _Session:
    payload: dict[str, Any]
    deadline: float
    frames: list[bytes] = field(default_factory=list)
    bytes_received: int = 0


class GitWorkerService:
    """One-operation worker with authenticated framed input and durable pre-CAS fence."""

    def __init__(
        self,
        worker: IsolatedGitCommitWorker,
        journal: GitOperationJournal,
        *,
        tenant: str,
        binding: str,
        branch: str,
        grant_key: bytes,
        transfer_key: bytes,
        result_key: bytes,
        worker_id: str,
        authority: Callable[[dict[str, Any]], bool],
    ) -> None:
        if (
            config.profile() == "production"
            or not isinstance(worker, IsolatedGitCommitWorker)
            or worker._ref != "refs/heads/" + branch
            or not is_uuid7(tenant)
            or not is_uuid7(binding)
            or not branch
            or not worker_id
            or not callable(authority)
            or any(len(key) < 32 for key in (grant_key, transfer_key, result_key))
            or len({grant_key, transfer_key, result_key}) != 3
        ):
            raise ValueError("Git worker isolation or key configuration unavailable")
        self._worker, self._journal = worker, journal
        self._tenant, self._binding, self._branch, self._worker_id = tenant, binding, branch, worker_id
        self._grant_key, self._transfer_key, self._result_key = grant_key, transfer_key, result_key
        self._authority = authority
        self._sessions: dict[str, _Session] = {}

    def _active(self, session: _Session) -> bool:
        return time.monotonic() < session.deadline and self._authority(session.payload)

    def begin(self, envelope: Any) -> str:
        try:
            for operation, existing in tuple(self._sessions.items()):
                if time.monotonic() >= existing.deadline:
                    self._sessions.pop(operation, None)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError
            p, mac = envelope["payload"], envelope["mac"]
            if not isinstance(p, dict) or set(p) != _FIELDS or not isinstance(mac, str):
                raise ValueError
            expected = sign_git_worker_grant(self._grant_key, p)["mac"]
            if (
                not hmac.compare_digest(mac, expected)
                or any(
                    not is_uuid7(p[k])
                    for k in (
                        "operation",
                        "tenant",
                        "owner",
                        "task",
                        "run",
                        "workspace",
                        "intent",
                        "attempt",
                        "binding",
                        "patch_id",
                    )
                )
                or p["tenant"] != self._tenant
                or p["binding"] != self._binding
                or p["branch"] != self._branch
                or p["effect_key"] != p["tenant"] + ":" + p["intent"]
                or type(p["generation"]) is not int
                or p["generation"] < 1
                or type(p["lease"]) is not int
                or p["lease"] < 1
                or type(p["goal"]) is not int
                or p["goal"] < 1
                or type(p["restore"]) is not int
                or p["restore"] < 1
                or not isinstance(p["worker_ref"], str)
                or not 1 <= len(p["worker_ref"]) <= 256
                or not isinstance(p["cycle_id"], str)
                or not 1 <= len(p["cycle_id"]) <= 200
                or not isinstance(p["plan_ref"], str)
                or not 1 <= len(p["plan_ref"]) <= 256
                or type(p["expires_at"]) is not int
                or not time.time() < p["expires_at"] <= time.time() + 60
                or type(p["patch_bytes"]) is not int
                or not 1 <= p["patch_bytes"] <= 4 * 1024 * 1024
                or not isinstance(p["patch_sha256"], str)
                or not _HEX.fullmatch(p["patch_sha256"])
                or not isinstance(p["request_sha256"], str)
                or not _HEX.fullmatch(p["request_sha256"])
                or not isinstance(p["parent"], str)
                or not _OID.fullmatch(p["parent"])
                or not isinstance(p["paths"], list)
                or not 1 <= len(p["paths"]) <= 512
                or not all(isinstance(path, str) for path in p["paths"])
                or not isinstance(p["message"], str)
                or not p["message"].strip()
                or len(p["message"].encode("utf-8")) > 256
                or len(self._sessions) >= 1
            ):
                raise ValueError
            session = _Session(dict(p), time.monotonic() + max(0, p["expires_at"] - time.time()))
            if not self._active(session):
                raise ValueError
            self._journal.admit(
                p["operation"], p["request_sha256"], p["attempt"], p["binding"], p["generation"], p["branch"]
            )
            self._sessions[p["operation"]] = session
            return str(p["operation"])
        except (ValueError, TypeError, KeyError, GitJournalError) as exc:
            raise RpcError("GIT_ADMISSION_DENIED") from exc

    def chunk(self, operation: str, encoded: str) -> None:
        try:
            session = self._sessions[operation]
            if not self._active(session) or not isinstance(encoded, str) or len(encoded) > 90000:
                raise ValueError
            frame = base64.b64decode(encoded, validate=True)
            sequence, size = struct.unpack(">QI", frame[:12])
            remaining = session.payload["patch_bytes"] - session.bytes_received
            expected = min(MAX_CHUNK, remaining)
            if sequence != len(session.frames) or size != expected or size <= 0 or len(frame) != size + 44:
                raise ValueError
            session.frames.append(frame)
            session.bytes_received += size
        except (ValueError, TypeError, KeyError, struct.error) as exc:
            raise RpcError("GIT_TRANSFER_INVALID") from exc

    def execute(self, operation: str, encoded_end: str) -> dict[str, Any]:
        try:
            session = self._sessions.pop(operation)
            if not self._active(session) or session.bytes_received != session.payload["patch_bytes"]:
                raise ValueError
            if not isinstance(encoded_end, str) or len(encoded_end) > 64:
                raise ValueError
            end = base64.b64decode(encoded_end, validate=True)
            if len(end) != 44:
                raise ValueError
            p = session.payload
            source = io.BytesIO(b"".join([*session.frames, end]))
            target = io.BytesIO()
            receive_chunks(
                source,
                target,
                key=git_transfer_key(self._transfer_key, operation),
                binding=git_transfer_binding(p),
                expected_size=p["patch_bytes"],
                expected_sha256=p["patch_sha256"],
                deadline=session.deadline,
                active=lambda: self._active(session),
            )
            tree: list[str] = []

            def before_cas(commit: str, tree_id: str) -> None:
                if not self._active(session):
                    raise GitJournalError("Git authority expired")
                self._journal.prepare(operation, p["request_sha256"], p["parent"], commit, tree_id)
                if not self._active(session):
                    raise GitJournalError("Git authority expired")
                tree.append(tree_id)

            commit = self._worker.commit(
                p["parent"],
                target.getvalue(),
                p["patch_sha256"],
                tuple(p["paths"]),
                p["message"],
                before_cas=before_cas,
            )
            self._journal.confirm(operation, p["request_sha256"], p["parent"], commit, tree[0])
            result = {**p, "commit": commit, "tree": tree[0], "cas": "CONFIRMED", "worker_id": self._worker_id}
            mac = hmac.new(
                self._result_key, _RESULT_DOMAIN + canonical_json(result).encode(), hashlib.sha256
            ).hexdigest()
            return {"payload": result, "mac": mac}
        except Exception as exc:
            # Includes a lost reply or a failure after CAS: caller must reconcile.
            raise RpcError("GIT_RESULT_UNKNOWN") from exc


def build_git_worker_server(endpoint: str, rpc_key: bytes, service: GitWorkerService, *, allowed_uid: int) -> RpcServer:
    """Explicit opt-in server, requiring external OS isolation and a live guard."""
    import asyncio

    if (
        len(rpc_key) < 32
        or config.profile() == "production"
        or not endpoint.startswith("unix:/")
        or type(allowed_uid) is not int
        or allowed_uid <= 0
        or rpc_key in (service._grant_key, service._transfer_key, service._result_key)
    ):
        raise ValueError("Git worker unavailable")
    server = RpcServer("git_worker", Endpoint(endpoint), {"tool_gateway": rpc_key})
    server.allowed_uids["tool_gateway"] = {allowed_uid}
    lock = asyncio.Lock()

    async def begin(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        async with lock:
            return {"operation": await asyncio.to_thread(service.begin, body.get("grant"))}

    async def chunk(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        operation, frame = body.get("operation"), body.get("frame")
        if not isinstance(operation, str) or not isinstance(frame, str):
            raise RpcError("BAD_REQUEST")
        async with lock:
            await asyncio.to_thread(service.chunk, operation, frame)
        return {"accepted": True}

    async def execute(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        operation, end = body.get("operation"), body.get("end")
        if not isinstance(operation, str) or not isinstance(end, str):
            raise RpcError("BAD_REQUEST")
        async with lock:
            return await asyncio.to_thread(service.execute, operation, end)

    server.register("git.begin", begin, {"tool_gateway"})
    server.register("git.chunk", chunk, {"tool_gateway"})
    server.register("git.execute", execute, {"tool_gateway"})
    return server
