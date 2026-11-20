"""Disposable test-only command Supervisor; no production configuration."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from aiagent_sandbox.command_execution import command_registration_factory
from aiagent_sandbox.source_rpc import SourceSessions
from aiagent_sandbox.supervisor import Limits, Supervisor, serve


def main() -> None:
    os.environ["AIAGENT_PROFILE"] = "test"
    root = Path(tempfile.mkdtemp(prefix="aiagent-command-rpc-"))
    os.chown(root, -1, 20001)
    root.chmod(0o710)
    source, output = root / "source", root / "output"
    source.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    supervisor = Supervisor(
        root / "cells",
        b"g" * 32,
        limits=Limits(cpu_s=60, memory_bytes=2 * 1024**3),
        require_netns=True,
        cell_uid=20001,
        cell_gid=20001,
    )
    sessions = SourceSessions(
        source,
        ticket_key=b"t" * 32,
        registration_factory=command_registration_factory(
            supervisor, source, output, b"c" * 32, ("/usr/local/bin/python3.13",)
        ),
    )
    asyncio.run(serve(supervisor, {"tool_gateway": b"r" * 32}, "tcp:0.0.0.0:7000", source_sessions=sessions))


if __name__ == "__main__":
    main()
