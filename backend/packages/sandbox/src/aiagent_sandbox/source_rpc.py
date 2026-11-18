"""Opt-in source transfer receiver; session registration is not authority.

The production authority bridge is not implemented. In particular, callers must
not register a session from unverified RPC fields or treat staging as permission
to execute. The session registry is process-local and deliberately not enabled
by the supervisor entry point.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiagent_development.code_intelligence.git_proof import GitTreeProof
from aiagent_development.code_intelligence.models import Identity
from aiagent_development.code_intelligence.projection import ProjectionManifest
from aiagent_development.code_intelligence.projection_stage import _open_trusted_parent, stage_git_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionStreamReceipt

from .binary_transfer import receive_async


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
    on_staged: Callable[[int], None]

    def transfer_binding(self) -> bytes:
        payload = {
            "version": "dev13-source-session-v1",
            "session": self.session_id,
            "direction": "gateway-to-supervisor",
            "execution": self.execution_id,
            "attempt": self.attempt_id,
            "binding": self.binding_sha256,
            "snapshot": self.snapshot_id,
            "size": self.receipt.byte_length,
            "sha256": self.receipt.sha256,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()


class SourceSessions:
    """One-shot in-memory staging sessions supplied by a trusted integration."""

    def __init__(self, parent: Path) -> None:
        self.parent = parent
        self._pending: dict[str, SourceSession] = {}
        self._used: set[str] = set()

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
            with stage_git_projection(
                self.parent,
                quarantine,
                session.manifest,
                session.receipt,
                session.proof,
                session.identity,
                deadline=session.deadline,
            ) as staged:
                if not session.active() or time.monotonic() >= session.deadline:
                    raise ValueError("source session revoked")
                session.on_staged(staged.root_fd)
                if not session.active() or time.monotonic() >= session.deadline:
                    raise ValueError("source session revoked")
        return {"staged": True}
