"""Opt-in command execution inside a projected Cell; not a production adapter.

Only a trusted configuration may choose the executable allowlist. Production
remains disabled until full toolchain/image and source-disclosure gates exist.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from aiagent_development.code_intelligence.indexer_recipe import measure_command_toolchain
from aiagent_development.code_intelligence.projection_stage import cell_source_traversal

from .capture_signing import validate_capture_binding
from .command_authority import verify_command_binding
from .command_receipt import sign_command_receipt
from .grant import GrantError, verify
from .projected_output import projected_output
from .source_rpc import CapturedArtifact, SourceAuthority
from .supervisor import Supervisor


def command_registration_factory(
    supervisor: Supervisor,
    source_parent: Path,
    output_parent: Path,
    receipt_key: bytes,
    allowed_executables: tuple[str, ...],
    *,
    heartbeat_seconds: float = 3.0,
) -> Any:
    """Prepare a bounded test/development-only factory with a fixed binary allowlist."""
    if os.environ.get("AIAGENT_PROFILE", "production") not in ("test", "development"):
        raise RuntimeError("command execution requires production toolchain attestation")
    if (
        not supervisor.require_netns
        or supervisor.cell_uid in (None, 0)
        or len(receipt_key) < 32
        or not allowed_executables
        or len(allowed_executables) > 32
        or len(set(allowed_executables)) != len(allowed_executables)
    ):
        raise ValueError("command execution isolation unavailable")
    measured = {path: measure_command_toolchain(path) for path in allowed_executables}

    def factory(ticket: dict[str, Any]) -> Any:
        binding, token, descriptor = (
            ticket.get("_execution_binding"),
            ticket.get("_grant"),
            ticket.get("_command_descriptor"),
        )
        if not isinstance(binding, dict) or not isinstance(token, dict) or not isinstance(descriptor, dict):
            raise GrantError("COMMAND_AUTHORITY_REQUIRED")
        verify_command_binding(ticket, binding, descriptor)
        argv = descriptor["argv"]
        if argv[0] not in measured or binding.get("toolchain_sha256") != measured[argv[0]]:
            raise GrantError("COMMAND_TOOLCHAIN_MISMATCH")
        grant = verify(supervisor.grant_key, token, argv, ".")
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
            raise GrantError("COMMAND_AUTHORITY_INVALID")
        authority = SourceAuthority(ticket["expires_at"], heartbeat_seconds=heartbeat_seconds)

        async def run(source_fd: int) -> CapturedArtifact:
            if not authority():
                raise GrantError("COMMAND_AUTHORITY_REVOKED")
            assert supervisor.cell_uid is not None and grant.execution_id is not None
            gid = supervisor.cell_gid if supervisor.cell_gid is not None else supervisor.cell_uid
            with cell_source_traversal(source_parent, cell_gid=gid):
                with projected_output(output_parent, grant, cell_uid=supervisor.cell_uid, cell_gid=gid) as output_fd:
                    result = await supervisor._execute(
                        token,
                        argv,
                        ".",
                        source_fd=source_fd,
                        output_fd=output_fd,
                        output_path=output_parent / grant.execution_id,
                        source_cwd=True,
                        revoked=authority.revoked,
                        heartbeat_seconds=heartbeat_seconds,
                    )
                    if not authority() or authority.revoked.is_set():
                        raise GrantError("COMMAND_AUTHORITY_REVOKED")
                    body, proof = sign_command_receipt(receipt_key, grant, binding, descriptor, result)
                    if not authority():
                        raise GrantError("COMMAND_AUTHORITY_REVOKED")
                    return CapturedArtifact(body, proof)

        # Proof-derived completeness is required for command success evidence.
        return authority, run, True

    return factory
