"""Child-process entry points for fault-injection tests (real process death via os._exit, like SIGKILL/OOM:
no finally blocks, no ACK, server-side rollback of open transactions)."""

from __future__ import annotations

import os
import sys

from aiagent_core.db import Database


def relay_once(dsn: str, owner: str) -> None:
    from aiagent_core.relay import OutboxRelay

    OutboxRelay(Database(dsn), owner, lease_s=1.0).run_once()


def dequeue_and_die(dsn: str, worker: str) -> None:
    from aiagent_core.queue import QueuePort

    job = QueuePort(Database(dsn)).dequeue("run", worker)
    print(job.id if job else "none", flush=True)
    os._exit(137)  # killed while holding the job: no ack, no nack


if __name__ == "__main__":
    fn = {"relay_once": relay_once, "dequeue_and_die": dequeue_and_die}[sys.argv[1]]
    fn(*sys.argv[2:])
