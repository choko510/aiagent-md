"""Relay process: Transactional Outbox relay + Scheduler tick + poison-job quarantine, with LISTEN-free polling
(bounded interval) and graceful shutdown. `python -m aiagent_core.relay_main`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket

from . import config
from .db import Database
from .logging import log, setup
from .migrate import check_compatible
from .queue import QueuePort
from .relay import OutboxRelay
from .scheduler import Scheduler
from .service import Lifecycle

logger = logging.getLogger("aiagent.relay")


async def main_async() -> None:
    setup("relay")
    config.require_isolation("relay")
    db = Database(config.secret("AIAGENT_RELAY_DSN").reveal(), max_size=4, name="relay")  # type: ignore[union-attr]
    with db.conn() as c:
        check_compatible(c)
    owner = os.environ.get("AIAGENT_WORKER_ID", f"relay:{socket.gethostname()}:{os.getpid()}")
    relay, sched, queue = OutboxRelay(db, owner), Scheduler(db), QueuePort(db)
    life = Lifecycle("relay", health_port=config.env_int("AIAGENT_HEALTH_PORT", 0) or None,
                     health_host=os.environ.get("AIAGENT_HEALTH_HOST", "127.0.0.1"))

    async def ready() -> tuple[bool, str]:
        return await asyncio.to_thread(db.ping), "db"

    life.checks["database"] = ready
    life.install_signals()
    await life.start_health()
    interval = max(0.5, min(5.0, float(os.environ.get("AIAGENT_RELAY_INTERVAL_S", "1"))))
    while not life.stopping.is_set():
        try:
            fired = await asyncio.to_thread(sched.tick)
            sent = await asyncio.to_thread(relay.run_once)
            dead = await asyncio.to_thread(queue.dead_letter_exhausted, "run")
            if fired or sent or dead:
                log(logger, logging.INFO, "relay tick", fired=len(fired), dispatched=sent, dead_lettered=dead)
        except Exception as e:  # DB outage: keep the process, report not-ready, retry (JQ-T13)
            log(logger, logging.WARNING, "relay tick failed", exc=type(e).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(life.stopping.wait(), timeout=interval)
    await life.close()
    db.close()


if __name__ == "__main__":
    asyncio.run(main_async())
