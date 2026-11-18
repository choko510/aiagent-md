"""Opt-in source transfer receiver; session registration is not authority.

The production authority bridge is not implemented. In particular, callers must
not register a session from unverified RPC fields or treat staging as permission
to execute. The session registry is process-local and deliberately not enabled
by the supervisor entry point.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import os
import stat
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiagent_development.code_intelligence.git_proof import GitTreeProof, TreeObject
from aiagent_development.code_intelligence.models import Coverage, Identity
from aiagent_development.code_intelligence.projection import (
    ProjectionFile,
    ProjectionManifest,
    projection_manifest_digest,
)
from aiagent_development.code_intelligence.projection_stage import _open_trusted_parent, stage_git_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt

from .binary_transfer import receive_async
from .source_ticket import source_binding, source_transfer_key, verify_source_ticket


@dataclass(frozen=True)
class SourceSession:
    session_id: str
    execution_id: str
    attempt_id: str
    binding_sha256: str
    snapshot_id: str
    key: bytes
    manifest: ProjectionManifest
    receipt: ProjectionStreamReceipt
    proof: GitTreeProof
    identity: Identity
    deadline: float
    active: Callable[[], bool]
    on_staged: Callable[[int], Awaitable[None] | None]

    def transfer_binding(self) -> bytes:
        return source_binding(
            self.session_id,
            self.execution_id,
            self.attempt_id,
            self.binding_sha256,
            self.snapshot_id,
            self.receipt.byte_length,
            self.receipt.sha256,
        )


class SourceSessions:
    """One-shot in-memory staging sessions supplied by a trusted integration."""

    def __init__(
        self,
        parent: Path,
        *,
        ticket_key: bytes | None = None,
        registration_factory: Callable[
            [dict[str, Any]], tuple[Callable[[], bool], Callable[[int], Awaitable[None] | None]]
        ]
        | None = None,
    ) -> None:
        self.parent = parent
        self._ticket_key = ticket_key
        self._registration_factory = registration_factory
        self._pending: dict[str, SourceSession] = {}
        self._used: set[str] = set()

    def register_authorized(self, body: dict[str, Any], consume: Callable[[str], None]) -> dict[str, str]:
        """Register only a signed DB-issued ticket and a server-selected consumer.

        The configured factory must supply a live, fail-closed authority bridge.
        Registration is unavailable without that integration; it is never supplied
        by request data. The Supervisor journal provides a restart replay fence.
        """
        if (
            self._ticket_key is None
            or self._registration_factory is None
            or set(body) != {"ticket", "files", "total_bytes", "commit_proof", "trees"}
        ):
            raise ValueError("source registration unavailable")
        p = verify_source_ticket(self._ticket_key, body["ticket"])
        files, trees, total = body["files"], body["trees"], body["total_bytes"]
        if not isinstance(files, list) or len(files) > 10000 or not isinstance(trees, list) or len(trees) > 10000:
            raise ValueError("invalid projection metadata")
        if type(total) is not int or total < 0 or type(body["commit_proof"]) is not str:
            raise ValueError("invalid projection metadata")
        try:
            entries = []
            for f in files:
                if not isinstance(f, dict) or set(f) != {"path", "blob_id", "sha256", "size"}:
                    raise ValueError("invalid projection metadata")
                if any(type(f[k]) is not str for k in ("path", "blob_id", "sha256")) or type(f["size"]) is not int:
                    raise ValueError("invalid projection metadata")
                entries.append(ProjectionFile(f["path"], f["blob_id"], f["sha256"], f["size"]))
            objects = []
            for tree in trees:
                if not isinstance(tree, dict) or set(tree) != {"oid", "data"}:
                    raise ValueError("invalid Git proof")
                if type(tree["oid"]) is not str or type(tree["data"]) is not str:
                    raise ValueError("invalid Git proof")
                objects.append(TreeObject(tree["oid"], base64.b64decode(tree["data"], validate=True)))
            proof = GitTreeProof(base64.b64decode(body["commit_proof"], validate=True), tuple(objects))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid source registration") from exc
        identity = Identity(p["tenant"], p["owner"], p["repository_binding"], p["revision"], p["commit"])
        manifest = ProjectionManifest(identity, p["tree"], tuple(entries), total, Coverage.PARTIAL, ())
        if projection_manifest_digest(manifest) != p["manifest_sha256"]:
            raise ValueError("source manifest mismatch")
        active, staged = self._registration_factory(p)
        if not active():
            raise ValueError("source authority unavailable")
        receipt = ProjectionStreamReceipt(p["source_sha256"], p["source_bytes"], len(entries))
        session = SourceSession(
            p["session"],
            p["execution"],
            p["attempt"],
            p["binding_sha256"],
            p["snapshot"],
            source_transfer_key(self._ticket_key, p["session"]),
            manifest,
            receipt,
            proof,
            identity,
            time.monotonic() + max(0, p["expires_at"] - time.time()),
            active,
            staged,
        )
        # Both fences are persisted before acceptance. An interrupted registration
        # loses availability rather than permitting a second execution.
        consume("source-session:" + p["session"])
        consume("source-execution:" + p["execution"])
        if not active():
            raise ValueError("source authority revoked")
        self.register(session)
        return {"sessionId": session.session_id}

    def register(self, session: SourceSession) -> None:
        if (
            not session.session_id
            or len(session.session_id) > 128
            or session.session_id in self._pending
            or session.session_id in self._used
            or len(session.key) < 32
            or not time.monotonic() < session.deadline <= time.monotonic() + 300
        ):
            raise ValueError("invalid source session")
        self._pending[session.session_id] = session

    async def receive(self, body: dict[str, Any], reader: asyncio.StreamReader) -> dict[str, Any]:
        if set(body) != {"sessionId"} or type(body["sessionId"]) is not str:
            raise ValueError("invalid source session")
        session_id = body["sessionId"]
        session = self._pending.pop(session_id, None)
        if session is None:
            raise ValueError("unknown source session")
        self._used.add(session_id)
        parent_fd = _open_trusted_parent(self.parent)
        try:
            info = os.fstat(parent_fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("source parent is not private")
            return await self._receive_session(session, reader, parent_fd)
        finally:
            os.close(parent_fd)

    async def _receive_session(
        self, session: SourceSession, reader: asyncio.StreamReader, parent_fd: int
    ) -> dict[str, Any]:
        with tempfile.TemporaryFile(dir=f"/proc/self/fd/{parent_fd}") as quarantine:
            await receive_async(
                reader,
                quarantine,
                key=session.key,
                binding=session.transfer_binding(),
                expected_size=session.receipt.byte_length,
                expected_sha256=session.receipt.sha256,
                deadline=session.deadline,
                active=session.active,
            )
            # A half-close marks the end of this dedicated data mode. Any extra
            # bytes are rejected before exposing the staged directory.
            while True:
                remaining = session.deadline - time.monotonic()
                if remaining <= 0 or not session.active():
                    raise ValueError("source session expired or revoked")
                try:
                    extra = await asyncio.wait_for(reader.read(1), timeout=min(remaining, 0.1))
                    break
                except TimeoutError:
                    continue
            if extra:
                raise ValueError("extra source data")
            quarantine.seek(0)
            stopped = threading.Event()

            def active() -> bool:
                return not stopped.is_set() and session.active()

            stage = stage_git_projection(
                self.parent,
                quarantine,
                session.manifest,
                session.receipt,
                session.proof,
                session.identity,
                deadline=session.deadline,
                active=active,
            )
            worker = asyncio.create_task(asyncio.to_thread(stage.__enter__))
            try:
                staged = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Do not close the quarantine while its worker still reads it.
                # Cooperative checkpoints stop proof verification and staging.
                stopped.set()
                try:
                    await worker
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(stage.__exit__, None, None, None)
                raise
            try:
                if not session.active() or time.monotonic() >= session.deadline:
                    raise ValueError("source session revoked")
                result = session.on_staged(staged.root_fd)
                if inspect.isawaitable(result):
                    await result
                if not session.active() or time.monotonic() >= session.deadline:
                    raise ValueError("source session revoked")
            finally:
                stopped.set()
                await asyncio.to_thread(stage.__exit__, None, None, None)
        return {"staged": True}
