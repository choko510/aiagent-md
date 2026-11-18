"""Supervisor-side fixed SCIP recipe adapter for an authenticated source session.

This adapter does not authenticate a VM/image or configure production keys. It
must only be installed behind canonical Gateway heartbeat and Cell isolation.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiagent_development.code_intelligence.indexer_recipe import (
    PYTHON_SCIP_GRANT_ARGV,
    measure_python_scip_toolchain,
    python_scip_argv,
)
from aiagent_development.code_intelligence.projection_stage import cell_source_traversal

from .capture_signing import validate_capture_binding
from .grant import GrantError, sign, verify
from .projected_output import projected_output
from .scip_authority import verify_index_binding
from .source_rpc import CapturedArtifact, SourceAuthority
from .supervisor import Supervisor


def scip_registration_factory(
    supervisor: Supervisor,
    source_parent: Path,
    output_parent: Path,
    capture_parent: Path,
    receipt_key: bytes,
    *,
    heartbeat_seconds: float = 3.0,
) -> Any:
    """Create a server-selected consumer; RPC callers cannot choose code or paths."""
    if not supervisor.require_netns or supervisor.cell_uid in (None, 0) or len(receipt_key) < 32:
        raise ValueError("projected execution isolation unavailable")
    toolchain_digest = measure_python_scip_toolchain()

    def factory(ticket: dict[str, Any]) -> Any:
        binding, token = ticket.get("_execution_binding"), ticket.get("_grant")
        if not isinstance(binding, dict) or not isinstance(token, dict):
            raise GrantError("INDEX_AUTHORITY_REQUIRED")
        verify_index_binding(ticket, binding)
        if binding.get("toolchain_sha256") != toolchain_digest:
            raise GrantError("INDEX_TOOLCHAIN_MISMATCH")
        grant = verify(supervisor.grant_key, token, list(PYTHON_SCIP_GRANT_ARGV), ".")
        validate_capture_binding(receipt_key, grant, binding)
        if (
            grant.execution_id != ticket["execution"]
            or grant.attempt_id != ticket["attempt"]
            or grant.execution_binding_sha256 != ticket["binding_sha256"]
            or grant.expires > ticket["expires_at"]
            or type(grant.wall_ms) is not int
            or not 1 <= grant.wall_ms <= 30000
            or type(grant.memory_bytes) is not int
            or not 1 <= grant.memory_bytes <= 2 * 1024**3
        ):
            raise GrantError("INDEX_AUTHORITY_INVALID")
        authority = SourceAuthority(ticket["expires_at"], heartbeat_seconds=heartbeat_seconds)

        async def run(source_fd: int) -> CapturedArtifact:
            if not authority():
                raise GrantError("INDEX_AUTHORITY_REVOKED")
            assert supervisor.cell_uid is not None
            gid = supervisor.cell_gid if supervisor.cell_gid is not None else supervisor.cell_uid
            with cell_source_traversal(source_parent, cell_gid=gid):
                with projected_output(output_parent, grant, cell_uid=supervisor.cell_uid, cell_gid=gid) as output_fd:
                    argv = list(python_scip_argv(source_fd, output_fd))
                    # The Gateway signed the fixed recipe marker. Only the
                    # trusted Supervisor derives the descriptor-specific argv.
                    actual_token = sign(supervisor.grant_key, replace(grant, argv=tuple(argv)))
                    deadline = time.monotonic() + max(0, min(grant.expires, ticket["expires_at"]) - time.time())
                    async with supervisor._execute_and_capture(
                        actual_token,
                        argv,
                        ".",
                        source_fd=source_fd,
                        output_parent=output_parent,
                        prepared_output_fd=output_fd,
                        source_cwd=True,
                        binding=binding,
                        receipt_key=receipt_key,
                        private_parent=capture_parent,
                        deadline=deadline,
                        heartbeat_seconds=heartbeat_seconds,
                        revoked=authority.revoked,
                    ) as (_result, fd, proof):
                        size = os.fstat(fd).st_size
                        if not 1 <= size <= 4 * 1024 * 1024 or not authority():
                            raise GrantError("INDEX_CAPTURE_INVALID")
                        stopped = threading.Event()

                        def read_capture() -> bytes:
                            data = bytearray()
                            while len(data) < size:
                                if stopped.is_set() or not authority():
                                    raise GrantError("INDEX_AUTHORITY_REVOKED")
                                block = os.pread(fd, min(65536, size - len(data)), len(data))
                                if not block:
                                    raise GrantError("INDEX_CAPTURE_INVALID")
                                data.extend(block)
                            return bytes(data)

                        worker = asyncio.create_task(asyncio.to_thread(read_capture))
                        try:
                            data = await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            stopped.set()
                            await asyncio.gather(worker, return_exceptions=True)
                            raise
                        if not authority():
                            raise GrantError("INDEX_AUTHORITY_REVOKED")
                        return CapturedArtifact(data, proof)

        return authority, run

    return factory
