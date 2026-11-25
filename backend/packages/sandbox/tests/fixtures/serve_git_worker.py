"""Docker fixture only: heartbeats are not backed by canonical DB authority."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiagent_development.git_commit_worker import IsolatedGitCommitWorker
from aiagent_sandbox.git_cas_fence_client import RpcGitCasAdmitter
from aiagent_sandbox.git_operation_journal import GitOperationJournal
from aiagent_sandbox.git_worker_rpc import GitWorkerAuthority, GitWorkerService, build_git_worker_server


async def main() -> None:
    journal = GitOperationJournal(Path("/journal"), b"j" * 32)
    if os.environ.get("FIXTURE_CRASH_AFTER_CAS") == "1":
        original_confirm = journal.confirm

        def crash_after_confirm(*args: object) -> None:
            original_confirm(*args)  # type: ignore[arg-type]
            os._exit(137)

        journal.confirm = crash_after_confirm  # type: ignore[method-assign]
    service = GitWorkerService(
        IsolatedGitCommitWorker(Path("/repo"), Path("/usr/bin/git"), "feat/fixture"),
        journal,
        tenant=os.environ["FIXTURE_TENANT"],
        binding=os.environ["FIXTURE_BINDING"],
        branch="feat/fixture",
        grant_key=b"g" * 32,
        transfer_key=b"t" * 32,
        result_key=b"s" * 32,
        worker_id="fixture-worker",
        authority=GitWorkerAuthority(5),
        cas_admitter=(
            RpcGitCasAdmitter("unix:/bridge/fence.sock", b"f" * 32)
            if os.environ.get("FIXTURE_GIT_FENCE") == "1"
            else None
        ),
    )
    server = build_git_worker_server("unix:/bridge/worker.sock", b"r" * 32, service, allowed_uid=os.getuid())
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()
        journal.close()


if __name__ == "__main__":
    asyncio.run(main())
