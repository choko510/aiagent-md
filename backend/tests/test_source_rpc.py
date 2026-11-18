"""Real Unix-socket source staging, without claiming production authority."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from aiagent_development.code_intelligence.git_proof import build_git_tree_proof
from aiagent_development.code_intelligence.git_reader import GitSnapshot
from aiagent_development.code_intelligence.models import AuthorizedSnapshot, Identity
from aiagent_development.code_intelligence.projection import prepare_projection
from aiagent_development.code_intelligence.projection_stream import ProjectionEncoder
from aiagent_domain.rpcwire import decode_length, encode_frame, sign_request, verify_response
from aiagent_gateways.source_transfer import send_source
from aiagent_sandbox.binary_transfer import encode_chunks
from aiagent_sandbox.source_rpc import SourceSession, SourceSessions
from aiagent_sandbox.supervisor import serve

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Unix socket and POSIX staging required")


async def test_authenticated_socket_stages_git_projection_and_rejects_replay(tmp_path: Path, monkeypatch) -> None:
    git = shutil.which("git")
    assert git
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> bytes:
        return subprocess.check_output([git, "-C", str(repo), *args], stderr=subprocess.DEVNULL)

    run("init")
    run("config", "user.name", "Test")
    run("config", "user.email", "test@example.invalid")
    (repo / "a.py").write_bytes(b"value = 42\n")
    run("add", "a.py")
    run("commit", "-m", "test")
    identity = Identity("tenant", "owner", "repository", "revision", run("rev-parse", "HEAD").decode().strip())
    snapshot = GitSnapshot(AuthorizedSnapshot(identity, repo), Path(git))
    manifest = prepare_projection(snapshot, identity, snapshot.tree)
    proof = build_git_tree_proof(snapshot)
    encoder = ProjectionEncoder(snapshot, manifest)
    chunks = list(encoder.chunks())
    assert encoder.receipt is not None
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    seen: list[bytes] = []

    def inspect(fd: int) -> None:
        child = os.open("a.py", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            seen.append(os.read(child, 100))
        finally:
            os.close(child)

    key = b"k" * 32
    session = SourceSession(
        "session",
        "execution",
        "attempt",
        "a" * 64,
        "snapshot",
        key,
        manifest,
        encoder.receipt,
        proof,
        identity,
        time.monotonic() + 15,
        lambda: True,
        inspect,
    )
    sessions = SourceSessions(parent)
    sessions.register(session)
    socket = tmp_path / "supervisor.sock"
    server = asyncio.create_task(serve(None, {"tool_gateway": key}, f"unix:{socket}", source_sessions=sessions))  # type: ignore[arg-type]
    try:
        for _ in range(100):
            if socket.exists():
                break
            await asyncio.sleep(0.01)
        await send_source(
            f"unix:{socket}",
            key,
            session.session_id,
            key,
            session.transfer_binding(),
            chunks,
            deadline=session.deadline,
        )
        assert seen == [b"value = 42\n"]
        assert not list(parent.iterdir())
        with pytest.raises((RuntimeError, ConnectionError, asyncio.IncompleteReadError)):
            await send_source(
                f"unix:{socket}",
                key,
                session.session_id,
                key,
                session.transfer_binding(),
                chunks,
                deadline=session.deadline,
            )
        assert len(seen) == 1
        for name, changed_key, changed_binding, active in (
            ("bad-key", b"x" * 32, None, True),
            ("bad-binding", key, b"x" * 32, True),
            ("revoked", key, None, False),
        ):
            candidate = replace(session, session_id=name, active=lambda: active)
            sessions.register(candidate)
            with pytest.raises((RuntimeError, ConnectionError, asyncio.IncompleteReadError)):
                await send_source(
                    f"unix:{socket}",
                    key,
                    name,
                    changed_key,
                    changed_binding or candidate.transfer_binding(),
                    chunks,
                    deadline=candidate.deadline,
                )
            assert len(seen) == 1
            assert not list(parent.iterdir())
        for name, extra, incomplete in (("extra", True, False), ("truncated", False, True)):
            candidate = replace(session, session_id=name)
            sessions.register(candidate)
            reader, writer = await asyncio.open_unix_connection(socket)
            request = sign_request(key, "tool_gateway", "source.receive", {"sessionId": name})
            writer.write(encode_frame(request))
            wire = b"".join(encode_chunks(key, candidate.transfer_binding(), chunks))
            writer.write(wire[:-1] if incomplete else wire + (b"x" if extra else b""))
            writer.write_eof()
            await writer.drain()
            header = await reader.readexactly(4)
            import json

            frame = json.loads(await reader.readexactly(decode_length(header)))
            response = verify_response(key, request["nonce"], frame)
            assert response["code"] == "GRANT_SOURCE_TRANSFER_REJECTED"
            writer.close()
            await writer.wait_closed()
            assert len(seen) == 1
            assert not list(parent.iterdir())
        # A slow verifier must not block the event loop or outlive revocation.
        import threading
        from contextlib import contextmanager

        from aiagent_development.code_intelligence.models import ReadError
        from aiagent_sandbox import source_rpc

        verifying = threading.Event()
        live = [True]

        @contextmanager
        def slow_verifier(*args, **kwargs):
            verifying.set()
            while kwargs["active"]():
                time.sleep(0.005)
            raise ReadError("revoked during verification")
            yield  # pragma: no cover

        with monkeypatch.context() as patch:
            patch.setattr(source_rpc, "stage_git_projection", slow_verifier)
            candidate = replace(session, session_id="revoke-verification", active=lambda: live[0])
            sessions.register(candidate)
            client = asyncio.create_task(
                send_source(
                    f"unix:{socket}",
                    key,
                    candidate.session_id,
                    key,
                    candidate.transfer_binding(),
                    chunks,
                    deadline=candidate.deadline,
                )
            )
            assert await asyncio.to_thread(verifying.wait, 3)
            live[0] = False
            with pytest.raises(RuntimeError, match="rejected"):
                await asyncio.wait_for(client, timeout=3)
            assert not list(parent.iterdir())

        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def waiting(fd: int) -> None:
            assert os.fstat(fd).st_ino
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        candidate = replace(session, session_id="shutdown", on_staged=waiting)
        sessions.register(candidate)
        client = asyncio.create_task(
            send_source(
                f"unix:{socket}",
                key,
                candidate.session_id,
                key,
                candidate.transfer_binding(),
                chunks,
                deadline=candidate.deadline,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await asyncio.gather(client, return_exceptions=True)
        assert cleaned.is_set()
        assert not list(parent.iterdir())
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
