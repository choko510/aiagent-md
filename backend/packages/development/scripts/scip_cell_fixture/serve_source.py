"""Disposable test-only Supervisor endpoint; never use these keys in deployment."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from aiagent_sandbox import binary_transfer
from aiagent_sandbox.supervisor import main as supervisor_main

from aiagent_development.code_intelligence import artifact_capture


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="aiagent-scip-rpc-"))
    os.chown(root, -1, 20001)
    root.chmod(0o710)
    source, output, capture, journal, keys = (
        root / item for item in ("source", "output", "capture", "journal", "keys")
    )
    for path in (source, output, capture, journal, keys):
        path.mkdir(mode=0o700)
    if os.environ.get("AIAGENT_SCIP_PAUSE_CAPTURE") == "1":
        # Test-only barrier: keep the real capture in progress until revocation.
        original_copy = artifact_capture._copy

        def paused_copy(source_fd, temp_fd, size, deadline, active):
            (root / "capture-started").touch()
            while active() and time.monotonic() < deadline:
                time.sleep(0.01)
            return original_copy(source_fd, temp_fd, size, deadline, active)

        artifact_capture._copy = paused_copy
    if os.environ.get("AIAGENT_SCIP_PAUSE_ARTIFACT") == "1":
        # Test-only barrier after the first real authenticated Artifact frame.
        original_encode = binary_transfer.encode_chunks

        def paused_encode(*args, **kwargs):
            for sequence, frame in enumerate(original_encode(*args, **kwargs)):
                yield frame
                if sequence == 0:
                    (root / "artifact-transfer-started").touch()
                    while True:
                        time.sleep(0.01)

        binary_transfer.encode_chunks = paused_encode
    for name, value in (("grant", b"g"), ("rpc", b"r"), ("ticket", b"t"), ("capture", b"c")):
        path = keys / name
        path.write_text((value * 32).hex())
        path.chmod(0o400)
    os.environ.update(
        {
            "AIAGENT_SANDBOX_GRANT_KEY_FILE": str(keys / "grant"),
            "AIAGENT_SANDBOX_RPC_KEY_FILE": str(keys / "rpc"),
            "AIAGENT_SANDBOX_SOURCE_TICKET_KEY_FILE": str(keys / "ticket"),
            "AIAGENT_SANDBOX_CAPTURE_KEY_FILE": str(keys / "capture"),
            "AIAGENT_SANDBOX_SOURCE_MODE": "enabled",
            "AIAGENT_SANDBOX_SOURCE_ROOT": str(root),
            "AIAGENT_SANDBOX_REPLAY_JOURNAL": str(journal / "consumed"),
            "AIAGENT_SANDBOX_ROOT": str(root / "cells"),
            "AIAGENT_SANDBOX_LISTEN": "tcp:0.0.0.0:7000",
            "AIAGENT_SANDBOX_CELL_UID": "20001",
            "AIAGENT_SANDBOX_MEMORY_LIMIT_BYTES": str(2 * 1024**3),
            "AIAGENT_SANDBOX_CPU_LIMIT_SECONDS": "60",
        }
    )
    supervisor_main()


if __name__ == "__main__":
    main()
