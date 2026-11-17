"""Migration runner (10-01 §20.6): advisory lock, immutable checksums, one transaction per migration.

* version present + same checksum  -> skip
* version present + other checksum -> fail closed (applied migrations are immutable)
* version absent                   -> apply up.sql and record version/checksum/control in the SAME transaction
* `down` is development/test only (AIAGENT_ALLOW_DOWN=1); production upgrades are forward-only expand/contract.

Usage: python -m aiagent_core.migrate [up|down --to N|status|check] (DSN from AIAGENT_MIGRATE_DSN[_FILE])
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .config import secret
from .db import Row, connect

# Schema version this code base requires and the newest it understands.
APP_SCHEMA_VERSION = 13
APP_MIN_READER = 5
APP_MIN_WRITER = 5
LOCK_KEY = 0x61696167656E74  # 'aiagent'
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.up\.sql$")

# Exact pre-squash PR #92 migration lineage (versions 0006..0016) replaced by 0006_step4_context_memory.
LEGACY_PR92_MIGRATIONS: dict[int, tuple[str, str]] = {
    6: ("context_artifacts", "9ad233265000e932e61c79e59288cac1379323402906866c093786e335349845"),
    7: ("context_artifact_scope", "1ee3df0f48ef3d6821339f368b7a39d4e974013260762e87c3d405facc998096"),
    8: ("memory_scope", "c6276c5c78fd801a2567040562b4b121be304b81506768359faeb791bebb7a53"),
    9: ("context_dispatch", "4f6f295c04cd9f83fa581c32b29e2c9535c0ac785ee69203168916004d3d552e"),
    10: ("context_unknown_barrier", "73c092cb355402f8027de6819e7d61e571c92dbd775d9a61940306f5b8fb8fb1"),
    11: ("context_authority", "f663be61310ed9b979ca63ace65ae344904405084a0d856cbdefbaeb34ac385f"),
    12: ("memory_history", "8cb1c300c9b40b55b1f3d7dc00816d286e534e553e4533493ab9cbae4a06360e"),
    13: ("context_policy", "7806169f007510afe061345a08357e3dad26cb27fad5bfa8a27764ba9cf901b8"),
    14: ("context_admission_locks", "b268866eee9be97f75011e11fc5ae318bdbde71dad0fe48a5bdaea6234d8d3aa"),
    15: ("memory_consent_history", "31073917fbdf770f3cdcba4652f9c1142b751dd7cd795660c61ded970dd1eabb"),
    16: ("memory_tombstone_coverage", "7ff9f13c19e839b01972700c3c7c33631ce670a0a53512012e8203f30d8aa7d8"),
}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Path
    down: Path | None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up.read_bytes()).hexdigest()


def default_dir() -> Path:
    env = os.environ.get("AIAGENT_MIGRATIONS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "migrations" / "0001_core.up.sql").exists():
            return p / "migrations"
    raise MigrationError("migrations directory not found (set AIAGENT_MIGRATIONS_DIR)")


def discover(directory: Path | None = None) -> list[Migration]:
    d = directory or default_dir()
    out = []
    for p in sorted(d.glob("*.up.sql")):
        m = _NAME.match(p.name)
        if not m:
            raise MigrationError(f"bad migration file name {p.name}")
        down = p.with_name(p.name.replace(".up.sql", ".down.sql"))
        out.append(Migration(int(m.group(1)), m.group(2), p, down if down.exists() else None))
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise MigrationError("migration versions must be contiguous from 0001")
    return out


_META = """
CREATE SCHEMA IF NOT EXISTS aiagent_meta;
CREATE TABLE IF NOT EXISTS aiagent_meta.schema_migrations (
  version integer PRIMARY KEY CHECK (version > 0),
  checksum_sha256 char(64) NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
  name text NOT NULL,
  phase text NOT NULL DEFAULT 'transactional' CHECK (phase IN ('transactional','postcommit')),
  applied_at timestamptz NOT NULL DEFAULT now(),
  app_min_reader integer NOT NULL,
  app_min_writer integer NOT NULL
);
"""


def _applied(conn: psycopg.Connection[Row]) -> dict[int, Row]:
    rows = conn.execute("SELECT * FROM aiagent_meta.schema_migrations ORDER BY version").fetchall()
    return {r["version"]: r for r in rows}


def _set_control(conn: psycopg.Connection[Row], version: int) -> None:
    if conn.execute("SELECT to_regclass('agent.schema_control') AS t").fetchone()["t"] is None:  # type: ignore[index]
        return
    cur = conn.execute(
        "UPDATE agent.schema_control SET schema_version=%s, min_compatible_reader=%s, min_compatible_writer=%s,"
        " changed_at=now() WHERE singleton",
        (version, min(version, APP_MIN_READER), min(version, APP_MIN_WRITER)),
    )
    if cur.rowcount != 1:
        raise MigrationError("schema_control CAS affected 0 rows")


def _maybe_reconcile_legacy_pr92(
    conn: psycopg.Connection[Row], migrations: list[Migration], done: dict[int, Row]
) -> dict[int, Row]:
    """Reconcile a verified pre-squash PR #92 database (0006..0016) non-destructively into 0006_step4_context_memory."""
    if sorted(done) != list(range(1, 17)):
        return done
    m6 = next((m for m in migrations if m.version == 6 and m.name == "step4_context_memory"), None)
    if m6 is None:
        return done
    for m in migrations:
        if m.version <= 5 and done[m.version]["checksum_sha256"] != m.checksum:
            raise MigrationError(f"checksum mismatch for applied migration {m.version:04d}_{m.name}")
    for ver, (legacy_name, legacy_sha) in LEGACY_PR92_MIGRATIONS.items():
        row = done[ver]
        if row["name"] != legacy_name or row["checksum_sha256"] != legacy_sha:
            raise MigrationError(f"checksum mismatch for legacy migration {ver:04d}_{legacy_name}")
    with conn.transaction():
        conn.execute("DROP FUNCTION IF EXISTS agent.lock_memory_epoch(uuid, uuid, uuid, bigint)")
        conn.execute("DROP FUNCTION IF EXISTS agent.revoke_context_artifact(uuid, uuid, uuid, uuid, uuid)")
        conn.execute("DELETE FROM aiagent_meta.schema_migrations WHERE version BETWEEN 6 AND 16")
        conn.execute(
            "INSERT INTO aiagent_meta.schema_migrations(version, checksum_sha256, name, app_min_reader,"
            " app_min_writer) VALUES (%s,%s,%s,%s,%s)",
            (m6.version, m6.checksum, m6.name, APP_MIN_READER, APP_MIN_WRITER),
        )
        _set_control(conn, m6.version)
    return _applied(conn)


def up(dsn: str, directory: Path | None = None, *, target: int | None = None) -> list[int]:
    migrations = discover(directory)
    applied_now: list[int] = []
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute(_META)
            done = _applied(conn)
            if target is None or target >= 6:
                done = _maybe_reconcile_legacy_pr92(conn, migrations, done)
            for v in sorted(done):
                if v > len(migrations):
                    raise MigrationError(f"database has migration {v} unknown to this code (downgrade refused)")
            for m in migrations:
                if target is not None and m.version > target:
                    break
                row = done.get(m.version)
                if row is not None:
                    if row["checksum_sha256"] != m.checksum:
                        raise MigrationError(f"checksum mismatch for applied migration {m.version:04d}_{m.name}")
                    continue
                with conn.transaction():
                    conn.execute(m.up.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO aiagent_meta.schema_migrations(version, checksum_sha256, name, app_min_reader,"
                        " app_min_writer) VALUES (%s,%s,%s,%s,%s)",
                        (m.version, m.checksum, m.name, APP_MIN_READER, APP_MIN_WRITER),
                    )
                    _set_control(conn, m.version)
                applied_now.append(m.version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


def down(dsn: str, to: int, directory: Path | None = None) -> list[int]:
    if os.environ.get("AIAGENT_ALLOW_DOWN") != "1":
        raise MigrationError("down migrations are disabled (development/test only: AIAGENT_ALLOW_DOWN=1)")
    migrations = {m.version: m for m in discover(directory)}
    reverted: list[int] = []
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute(_META)
            for v in sorted(_applied(conn), reverse=True):
                if v <= to:
                    break
                m = migrations[v]
                if m.down is None:
                    raise MigrationError(f"migration {v} has no down script")
                with conn.transaction():
                    conn.execute(m.down.read_text(encoding="utf-8"))
                    conn.execute("DELETE FROM aiagent_meta.schema_migrations WHERE version=%s", (v,))
                    _set_control(conn, v - 1)
                reverted.append(v)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return reverted


def check_compatible(conn: psycopg.Connection[Row], *, writer: bool = True) -> None:
    """Readiness gate for every service: refuse to run against an incompatible schema (fail closed)."""
    row = conn.execute(
        "SELECT schema_version, min_compatible_reader, min_compatible_writer FROM agent.schema_control"
    ).fetchone()
    if row is None:
        raise MigrationError("schema_control missing")
    if row["schema_version"] < APP_SCHEMA_VERSION:
        raise MigrationError(f"schema version {row['schema_version']} < required {APP_SCHEMA_VERSION}")
    floor = row["min_compatible_writer"] if writer else row["min_compatible_reader"]
    if floor > APP_SCHEMA_VERSION:
        raise MigrationError("application is older than the schema's minimum compatible version")


def status(dsn: str, directory: Path | None = None) -> list[tuple[int, str, str]]:
    out = []
    with connect(dsn) as conn:
        conn.execute(_META)
        done = _applied(conn)
    for m in discover(directory):
        row = done.get(m.version)
        st = "pending" if row is None else ("applied" if row["checksum_sha256"] == m.checksum else "CHECKSUM_MISMATCH")
        out.append((m.version, m.name, st))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aiagent-migrate")
    ap.add_argument("cmd", choices=["up", "down", "status", "check"])
    ap.add_argument("--to", type=int, default=None)
    a = ap.parse_args(argv)
    s = secret("AIAGENT_MIGRATE_DSN")
    assert s is not None
    dsn = s.reveal()
    try:
        if a.cmd == "up":
            print("applied:", up(dsn, target=a.to) or "none")
        elif a.cmd == "down":
            if a.to is None:
                raise MigrationError("down requires --to")
            print("reverted:", down(dsn, a.to) or "none")
        elif a.cmd == "status":
            for v, n, st in status(dsn):
                print(f"{v:04d}_{n}: {st}")
        else:
            with connect(dsn) as conn:
                check_compatible(conn)
            print("compatible")
    except MigrationError as e:
        print(f"migration error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
