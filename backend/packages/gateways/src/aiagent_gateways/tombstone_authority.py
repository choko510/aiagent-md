"""External tombstone authority for Memory egress admission.

`memory_tombstones.TombstoneLog` is the append-only record of Memory revocation
that survives a PostgreSQL PITR restore. The API appends to it before committing
its database effect. Before this module existed the Gateway trusted the database
alone, so a deployment whose external log was missing, rolled back, or ahead of
the database could still send owner-approved Memory bytes to a provider. The
external log is now an admission authority, checked inside the same transaction
that reserves the attempt and records the Context dispatch.

Three failure classes are denied:

* the log/seal/HMAC key is unreadable or inconsistent (fail closed, never
  "assume the database is right");
* the log is behind the committed coverage watermark, or its history at that
  generation names a different chain head (rolled back / diverged volume);
* the log holds a revocation generation the database never committed (an append
  whose transaction failed must not be treated as already applied).

A `created` append needs no coverage: revocation is permanent for a row and
permanently bumps its revision, so a later create cannot revive it.
"""

from __future__ import annotations

import psycopg
from aiagent_core.control import Claim
from aiagent_core.db import Row
from aiagent_core.memory_tombstones import TombstoneError, TombstoneLog, TombstoneSnapshot
from aiagent_domain.rpcwire import RpcError

DENIED = "MEMORY_TOMBSTONE_UNAVAILABLE"


def verify(c: psycopg.Connection[Row], claim: Claim, log: TombstoneLog | None) -> TombstoneSnapshot:
    """Return the verified snapshot or raise RpcError(DENIED). Never returns unverified state."""
    if log is None:
        # No configured authority: Memory egress is disabled rather than trusted.
        raise RpcError(DENIED)
    try:
        snapshot = log.snapshot_readonly()
    except TombstoneError as exc:
        raise RpcError(DENIED, type(exc).__name__) from None
    row = c.execute(
        "SELECT * FROM agent.memory_tombstone_coverage_read(%s,%s,%s,%s,%s,%s)",
        (claim.tenant_id, claim.owner_user_id, claim.task_id, claim.run_id, claim.lease_epoch, claim.worker_ref),
    ).fetchone()
    if row is None:
        raise RpcError(DENIED)
    covered = int(row["covered_revocation_generation"])
    if snapshot.max_revocation_generation > covered:
        # A revocation exists outside the database: re-run reconciliation.
        raise RpcError(DENIED, "uncovered revocation")
    if covered > 0 and snapshot.head_at(covered) != str(row["chain_head"]):
        # The log no longer contains the history the database committed.
        raise RpcError(DENIED, "tombstone history diverged")
    return snapshot
