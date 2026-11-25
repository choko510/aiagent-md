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
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
_HEAD_DOMAIN = b"aiagent-git-head-grant-v1\0"
_HEAD_FIELDS = frozenset(
    {
        "tenant",
        "owner",
        "workspace",
        "task",
        "run",
        "goal",
        "lease",
        "restore",
        "worker_ref",
        "binding",
        "generation",
        "branch",
        "expires_at",
    }
)
_RESULT_DOMAIN = b"aiagent-git-worker-result-v1\0"
_RECOVERY_DOMAIN = b"aiagent-git-recovery-read-v1\0"
_RECOVERY_ATTEST_DOMAIN = b"aiagent-git-recovery-observation-v1\0"
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


def sign_git_recovery_grant(key: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    """Sign a short-lived read-only recovery request after canonical authorization."""
    if len(key) < 32 or set(payload) != _FIELDS:
        raise ValueError("invalid Git recovery grant")
    body = canonical_json(payload).encode("utf-8")
    return {"payload": payload, "mac": hmac.new(key, _RECOVERY_DOMAIN + body, hashlib.sha256).hexdigest()}


def verify_git_recovery_observation(key: bytes, envelope: Any, expected: dict[str, Any]) -> None:
    """Verify a read-only observation; it is not a canonical success receipt."""
    if len(key) < 32 or not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
        raise ValueError("Git recovery observation invalid")
    payload, mac = envelope["payload"], envelope["mac"]
    if not isinstance(payload, dict) or not isinstance(mac, str) or payload != expected:
        raise ValueError("Git recovery observation invalid")
    signature = hmac.new(key, _RECOVERY_ATTEST_DOMAIN + canonical_json(payload).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, signature):
        raise ValueError("Git recovery observation invalid")


def sign_git_head_grant(key: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    """Sign a read-only branch request after canonical scope validation."""
    if len(key) < 32 or set(payload) != _HEAD_FIELDS:
        raise ValueError("invalid Git head grant")
    body = canonical_json(payload).encode("utf-8")
    return {"payload": payload, "mac": hmac.new(key, _HEAD_DOMAIN + body, hashlib.sha256).hexdigest()}


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
        or result["commit"] == result["parent"]
    ):
        raise ValueError("invalid Git worker result")
    return result


@dataclass
class _Session:
    payload: dict[str, Any]
    deadline: float
    frames: list[bytes] = field(default_factory=list)
    bytes_received: int = 0


class GitWorkerAuthority:
    """Fresh authenticated Gateway heartbeats; no self-renewal or implicit allow."""

    def __init__(self, heartbeat_seconds: float = 2.0) -> None:
        if not 0.1 <= heartbeat_seconds <= 5:
            raise ValueError("invalid Git heartbeat interval")
        self._period = heartbeat_seconds
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def register(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            for op, state in tuple(self._states.items()):
                if time.time() >= state["expires"]:
                    self._states.pop(op, None)
            operation = payload["operation"]
            if operation in self._states or len(self._states) >= 1024 or time.time() >= payload["expires_at"]:
                return False
            self._states[operation] = {
                "attempt": payload["attempt"],
                "request": payload["request_sha256"],
                "identity": hashlib.sha256(
                    canonical_json({k: v for k, v in payload.items() if k != "expires_at"}).encode()
                ).hexdigest(),
                "expires": payload["expires_at"],
                "deadline": time.monotonic() + self._period,
                "renewed": False,
                "revoked": False,
            }
            return True

    def _matching(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        operation = payload.get("operation")
        if not isinstance(operation, str):
            return None
        state = self._states.get(operation)
        if (
            state is None
            or state["attempt"] != payload.get("attempt")
            or state["request"] != payload.get("request_sha256")
            or state["identity"]
            != hashlib.sha256(
                canonical_json({k: v for k, v in payload.items() if k != "expires_at"}).encode()
            ).hexdigest()
        ):
            return None
        if state["revoked"] or time.time() >= state["expires"] or time.monotonic() >= state["deadline"]:
            state["revoked"] = True
            return None
        return state

    def __call__(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            state = self._matching(payload)
            return bool(state is not None and state["renewed"])

    def renew(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            state = self._matching(payload)
            if state is None:
                return False
            state["renewed"] = True
            state["deadline"] = time.monotonic() + self._period
            return True

    def cancel(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            state = self._matching(payload)
            if state is None:
                return False
            state["revoked"] = True
            return True

    @contextmanager
    def cas_guard(self, payload: dict[str, Any]) -> Iterator[None]:
        """Serialize local watchdog revocation with the ref update only.

        This is not a fence against a concurrent canonical DB revocation.
        """
        with self._lock:
            state = self._matching(payload)
            if state is None or not state["renewed"]:
                raise GitJournalError("Git authority unavailable")
            yield


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
        cas_admitter: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
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
            or (config.profile() == "development" and not isinstance(authority, GitWorkerAuthority))
            or any(len(key) < 32 for key in (grant_key, transfer_key, result_key))
            or len({grant_key, transfer_key, result_key}) != 3
        ):
            raise ValueError("Git worker isolation or key configuration unavailable")
        self._worker, self._journal = worker, journal
        self._tenant, self._binding, self._branch, self._worker_id = tenant, binding, branch, worker_id
        self._grant_key, self._transfer_key, self._result_key = grant_key, transfer_key, result_key
        self._authority = authority
        self._cas_admitter = cas_admitter
        self._sessions: dict[str, _Session] = {}
        self._live: dict[str, _Session] = {}

    def _active(self, session: _Session) -> bool:
        return time.monotonic() < session.deadline and self._authority(session.payload)

    def head(self, envelope: Any) -> dict[str, str]:
        """Read the configured branch only; never treat the result as a receipt."""
        try:
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError
            payload, mac = envelope["payload"], envelope["mac"]
            if (
                not isinstance(payload, dict)
                or set(payload) != _HEAD_FIELDS
                or not isinstance(mac, str)
                or not hmac.compare_digest(mac, sign_git_head_grant(self._grant_key, payload)["mac"])
                or payload["tenant"] != self._tenant
                or payload["binding"] != self._binding
                or payload["branch"] != self._branch
                or any(not is_uuid7(payload[k]) for k in ("tenant", "owner", "workspace", "task", "run", "binding"))
                or any(
                    type(payload[k]) is not int or payload[k] < 1 for k in ("goal", "lease", "restore", "generation")
                )
                or not isinstance(payload["worker_ref"], str)
                or not payload["worker_ref"]
                or type(payload["expires_at"]) is not int
                or not time.time() < payload["expires_at"] <= time.time() + 10
            ):
                raise ValueError
            head = self._worker.head()
            if time.time() >= payload["expires_at"]:
                raise ValueError
            return {"binding": self._binding, "branch": self._branch, "head": head, "worker_id": self._worker_id}
        except Exception as exc:
            raise RpcError("GIT_HEAD_UNAVAILABLE") from exc

    def begin(self, envelope: Any) -> str:
        try:
            for operation, existing in tuple(self._live.items()):
                if time.monotonic() >= existing.deadline:
                    self._sessions.pop(operation, None)
                    self._live.pop(operation, None)
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
                or len(self._live) >= 8
            ):
                raise ValueError
            session = _Session(dict(p), time.monotonic() + max(0, p["expires_at"] - time.time()))
            if isinstance(self._authority, GitWorkerAuthority):
                if not self._authority.register(p):
                    raise ValueError
            elif not self._active(session):
                raise ValueError
            self._journal.admit(
                p["operation"], p["request_sha256"], p["attempt"], p["binding"], p["generation"], p["branch"]
            )
            self._sessions[p["operation"]] = session
            self._live[p["operation"]] = session
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

    def heartbeat(self, operation: str, attempt: str, request_sha256: str) -> bool:
        session = self._live.get(operation)
        if (
            session is None
            or not isinstance(self._authority, GitWorkerAuthority)
            or session.payload["attempt"] != attempt
            or session.payload["request_sha256"] != request_sha256
        ):
            return False
        return self._authority.renew(session.payload)

    def cancel(self, operation: str, attempt: str, request_sha256: str) -> bool:
        session = self._live.get(operation)
        if (
            session is None
            or not isinstance(self._authority, GitWorkerAuthority)
            or session.payload["attempt"] != attempt
            or session.payload["request_sha256"] != request_sha256
        ):
            return False
        self._sessions.pop(operation, None)
        return self._authority.cancel(session.payload)

    def execute(self, operation: str, encoded_end: str) -> dict[str, Any]:
        # A heartbeat alone cannot serialize revocation with the Git ref update.
        # Until a durable CAS fence is implemented, only isolated test fixtures
        # may exercise this method; development and production remain closed.
        if config.profile() != "test":
            raise RpcError("GIT_CAS_FENCE_UNAVAILABLE")
        session: _Session | None = None
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
                if self._cas_admitter is not None:
                    self._cas_admitter(p, self._journal.export_prepared(operation))
                if not self._active(session):
                    raise GitJournalError("Git authority expired")
                tree.append(tree_id)

            authority = self._authority
            commit = self._worker.commit(
                p["parent"],
                target.getvalue(),
                p["patch_sha256"],
                tuple(p["paths"]),
                p["message"],
                before_cas=before_cas,
                cas_guard=(lambda: authority.cas_guard(p)) if isinstance(authority, GitWorkerAuthority) else None,
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
        finally:
            if session is not None:
                session.frames.clear()

    def inspect(self, envelope: Any) -> dict[str, Any]:
        """Read bounded Git object evidence for a journal-confirmed operation."""
        try:
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError
            p, mac = envelope["payload"], envelope["mac"]
            if not isinstance(p, dict) or set(p) != _FIELDS or not isinstance(mac, str):
                raise ValueError
            if (
                not hmac.compare_digest(mac, sign_git_worker_grant(self._grant_key, p)["mac"])
                or p["tenant"] != self._tenant
                or p["binding"] != self._binding
                or p["branch"] != self._branch
                or type(p["expires_at"]) is not int
                or not time.time() < p["expires_at"] <= time.time() + 60
                or not self._authority(p)
            ):
                raise ValueError
            prepared = self._journal.read(p["operation"], "prepared")
            confirmed = self._journal.read(p["operation"], "confirmed")
            if (
                {**prepared, "stage": "confirmed"} != confirmed
                or any(
                    prepared.get(k) != p[v]
                    for k, v in (
                        ("operation", "operation"),
                        ("attempt", "attempt"),
                        ("binding", "binding"),
                        ("branch", "branch"),
                        ("parent", "parent"),
                        ("request_sha256", "request_sha256"),
                    )
                )
                or prepared.get("generation") != str(p["generation"])
            ):
                raise ValueError
            commit_body, tree_body, diff_sha = self._worker.inspect_commit(
                p["parent"], prepared["commit"], prepared["tree"]
            )
            if diff_sha != p["patch_sha256"] or not self._authority(p):
                raise ValueError
            return {
                "operation": p["operation"],
                "worker_id": self._worker_id,
                "commit": prepared["commit"],
                "tree": prepared["tree"],
                "parent": p["parent"],
                "diff_sha256": diff_sha,
                "commit_object": base64.b64encode(commit_body).decode("ascii"),
                "tree_object": base64.b64encode(tree_body).decode("ascii"),
            }
        except Exception as exc:
            raise RpcError("GIT_OBJECT_UNAVAILABLE") from exc

    def recover_inspect(self, envelope: Any) -> dict[str, Any]:
        """Test-only read of confirmed journal and Git objects after worker restart.

        This is forensic data, not a signed effect receipt or a state transition.
        The Gateway must separately authorize the caller and must retain UNKNOWN
        unless a canonical reconciliation contract validates the complete proof.
        """
        try:
            if config.profile() != "test" or not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError
            p, mac = envelope["payload"], envelope["mac"]
            if (
                not isinstance(p, dict)
                or set(p) != _FIELDS
                or not isinstance(mac, str)
                or not hmac.compare_digest(mac, sign_git_recovery_grant(self._grant_key, p)["mac"])
                or p["tenant"] != self._tenant
                or p["binding"] != self._binding
                or p["branch"] != self._branch
                or type(p["expires_at"]) is not int
                or not time.time() < p["expires_at"] <= time.time() + 10
            ):
                raise ValueError
            prepared = self._journal.read(p["operation"], "prepared")
            confirmed = self._journal.read(p["operation"], "confirmed")
            if (
                {**prepared, "stage": "confirmed"} != confirmed
                or any(
                    prepared.get(k) != p[v]
                    for k, v in (
                        ("operation", "operation"),
                        ("attempt", "attempt"),
                        ("binding", "binding"),
                        ("branch", "branch"),
                        ("parent", "parent"),
                        ("request_sha256", "request_sha256"),
                    )
                )
                or prepared.get("generation") != str(p["generation"])
            ):
                raise ValueError
            commit_body, tree_body, diff_sha = self._worker.inspect_commit(
                p["parent"], prepared["commit"], prepared["tree"]
            )
            if diff_sha != p["patch_sha256"] or time.time() >= p["expires_at"]:
                raise ValueError
            prepared_envelope = self._journal.export_prepared(p["operation"])
            confirmed_envelope = self._journal.export_confirmed(p["operation"])
            journal_sha = hashlib.sha256(canonical_json(prepared_envelope).encode()).hexdigest()
            observation = {
                "version": 1,
                "operation": p["operation"],
                "tenant": p["tenant"],
                "run": p["run"],
                "lease": p["lease"],
                "intent": p["intent"],
                "attempt": p["attempt"],
                "binding": p["binding"],
                "generation": p["generation"],
                "branch": p["branch"],
                "parent": p["parent"],
                "commit": prepared["commit"],
                "tree": prepared["tree"],
                "patch_sha256": p["patch_sha256"],
                "request_sha256": p["request_sha256"],
                "prepared_journal_sha256": journal_sha,
                "worker_id": self._worker_id,
                "expires_at": p["expires_at"],
            }
            observation_mac = hmac.new(
                self._result_key, _RECOVERY_ATTEST_DOMAIN + canonical_json(observation).encode(), hashlib.sha256
            ).hexdigest()
            return {
                "operation": p["operation"],
                "worker_id": self._worker_id,
                "commit": prepared["commit"],
                "tree": prepared["tree"],
                "parent": p["parent"],
                "diff_sha256": diff_sha,
                "prepared_journal_sha256": journal_sha,
                "prepared_journal": prepared_envelope,
                "confirmed_journal": confirmed_envelope,
                "observation": {"payload": observation, "mac": observation_mac},
                "commit_object": base64.b64encode(commit_body).decode("ascii"),
                "tree_object": base64.b64encode(tree_body).decode("ascii"),
            }
        except Exception as exc:
            raise RpcError("GIT_RECOVERY_UNAVAILABLE") from exc


def build_git_worker_server(
    endpoint: str, rpc_key: bytes, service: GitWorkerService, *, allowed_uid: int, enable_test_recovery: bool = False
) -> RpcServer:
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
            task = asyncio.create_task(asyncio.to_thread(service.execute, operation, end))
        return await task

    async def inspect(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        async with lock:
            task = asyncio.create_task(asyncio.to_thread(service.inspect, body.get("grant")))
        return await task

    async def recover_inspect(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        async with lock:
            task = asyncio.create_task(asyncio.to_thread(service.recover_inspect, body.get("grant")))
        return await task

    async def head(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(service.head, body.get("grant"))

    async def heartbeat(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        operation, attempt, digest = body.get("operation"), body.get("attempt"), body.get("request_sha256")
        if not isinstance(operation, str) or not isinstance(attempt, str) or not isinstance(digest, str):
            raise RpcError("BAD_REQUEST")
        return {"renewed": await asyncio.to_thread(service.heartbeat, operation, attempt, digest)}

    async def cancel(caller: str, body: dict[str, Any]) -> dict[str, Any]:
        operation, attempt, digest = body.get("operation"), body.get("attempt"), body.get("request_sha256")
        if not isinstance(operation, str) or not isinstance(attempt, str) or not isinstance(digest, str):
            raise RpcError("BAD_REQUEST")
        return {"cancelled": await asyncio.to_thread(service.cancel, operation, attempt, digest)}

    server.register("git.begin", begin, {"tool_gateway"})
    server.register("git.chunk", chunk, {"tool_gateway"})
    server.register("git.execute", execute, {"tool_gateway"})
    server.register("git.inspect", inspect, {"tool_gateway"})
    if enable_test_recovery:
        if config.profile() != "test":
            raise ValueError("Git recovery inspection is test-only")
        server.register("git.recovery_inspect", recover_inspect, {"tool_gateway"})
    server.register("git.head", head, {"tool_gateway"})
    server.register("git.heartbeat", heartbeat, {"tool_gateway"})
    server.register("git.cancel", cancel, {"tool_gateway"})
    return server
