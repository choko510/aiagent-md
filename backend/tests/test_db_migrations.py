"""STEP 3-A: formal migrations on real PostgreSQL - apply, re-apply, checksum, rollback, concurrency, privileges."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import psycopg
import pytest
from aiagent_core import admin, migrate
from aiagent_core.db import connect
from conftest import PgEnv, drop_database, make_database

pytestmark = pytest.mark.pg


def test_reapply_is_noop(pg: PgEnv) -> None:
    assert migrate.up(pg.owner) == []
    assert all(st == "applied" for _, _, st in migrate.status(pg.owner))
    with connect(pg.owner) as c:
        migrate.check_compatible(c)


def test_checksum_mismatch_fails_closed(pg: PgEnv, tmp_path: Path) -> None:
    d = tmp_path / "m"
    shutil.copytree(migrate.default_dir(), d)
    p = d / "0005_roles_grants_rls.up.sql"
    p.write_bytes(p.read_bytes() + b"\n-- tampered\n")
    with pytest.raises(migrate.MigrationError, match="checksum mismatch"):
        migrate.up(pg.owner, d)


def test_failed_migration_rolls_back_atomically(pg: PgEnv, tmp_path: Path) -> None:
    d = tmp_path / "m"
    shutil.copytree(migrate.default_dir(), d)
    (d / "0010_broken.up.sql").write_text("CREATE TABLE agent.half_done(x int);\nSELECT * FROM no_such_table;\n")
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate.up(pg.owner, d)
    with connect(pg.owner) as c:
        assert c.execute("SELECT to_regclass('agent.half_done') AS t").fetchone()["t"] is None
        assert c.execute("SELECT max(version) AS v FROM aiagent_meta.schema_migrations").fetchone()["v"] == 9
        assert c.execute("SELECT schema_version FROM agent.schema_control").fetchone()["schema_version"] == 9


def test_upgrade_from_previous_version_and_unknown_future_refused(tmp_path: Path) -> None:
    env = make_database("aiagent_up")
    try:
        # simulate an old deployment at version 3, then upgrade
        migrate_dir = migrate.default_dir()
        import os

        os.environ["AIAGENT_ALLOW_DOWN"] = "1"
        assert migrate.down(env.owner, 3) == [9, 8, 7, 6, 5, 4]
        assert migrate.up(env.owner) == [4, 5, 6, 7, 8, 9]
        with connect(env.owner) as c:
            c.execute(
                "INSERT INTO aiagent_meta.schema_migrations(version, checksum_sha256, name, app_min_reader,"
                " app_min_writer) VALUES (99, repeat('a',64), 'future', 99, 99)"
            )
        with pytest.raises(migrate.MigrationError, match="unknown to this code"):
            migrate.up(env.owner, migrate_dir)
    finally:
        os.environ.pop("AIAGENT_ALLOW_DOWN", None)
        drop_database(env.name)


def test_concurrent_runners_apply_once() -> None:
    import secrets

    from conftest import PG

    name = f"aiagent_cc_{secrets.token_hex(4)}"
    with connect(PG) as c:  # type: ignore[arg-type]
        c.execute(f'CREATE DATABASE "{name}"')
    from conftest import _with_db

    dsn = _with_db(PG, name)  # type: ignore[arg-type]
    results: list[list[int]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(migrate.up(dsn))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    ts = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    try:
        assert not errors
        assert sorted(len(r) for r in results) == [0, 0, 0, 9]
    finally:
        drop_database(name)


def test_down_requires_explicit_opt_in(pg: PgEnv) -> None:
    with pytest.raises(migrate.MigrationError, match="disabled"):
        migrate.down(pg.owner, 0)


def test_framework_migrations_pinned() -> None:
    from langgraph.checkpoint.postgres.base import MIGRATIONS

    assert len(MIGRATIONS) == 10, "langgraph-checkpoint-postgres changed its schema: add a new aiagent migration"


def _deny(dsn: str, sql: str, args: tuple[object, ...] = ()) -> None:
    with connect(dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(sql, args)


def test_least_privilege(pg: PgEnv, tenant: dict[str, str]) -> None:
    d = pg.dsn
    for role in ("api", "runtime", "model_gateway", "tool_gateway", "relay", "sentinel", "checkpointer"):
        _deny(d[role], "CREATE TABLE agent.x(i int)")
    _deny(d["checkpointer"], "CREATE TABLE lg_checkpoint.x(i int)")
    _deny(d["runtime"], "CREATE TABLE lg_checkpoint.x(i int)")
    _deny(d["checkpointer"], "SELECT * FROM agent.runs")  # framework login sees no canonical data
    _deny(d["runtime"], "SELECT * FROM lg_checkpoint.checkpoints")  # and runtime login no framework blobs
    _deny(d["runtime"], "INSERT INTO agent.run_checkpoints(tenant_id) VALUES (gen_random_uuid())")  # HEAD by gateways
    _deny(d["runtime"], "UPDATE agent.approval_records SET decision_code='APPROVED'")  # no self-approval
    _deny(d["runtime"], "UPDATE agent.tool_intents SET status_code='DONE'")  # ledger by Tool GW
    _deny(d["model_gateway"], "UPDATE agent.runs SET status_code='SUCCEEDED'")
    # a compromised worker or API cannot raise its own budget, widen model policy or move fences/HEAD
    for col in (
        "token_budget = 10000000",
        "tokens_used = 0",
        "allowed_models = '{x}'",
        "restore_incarnation = 99",
        "max_model_calls = 9999",
    ):
        _deny(d["runtime"], f"UPDATE agent.runs SET {col}")
        _deny(d["api"], f"UPDATE agent.runs SET {col}")
    _deny(d["runtime"], "UPDATE agent.runs SET head_revision = 0")
    _deny(d["api"], "UPDATE agent.runs SET lease_epoch = 0")  # column grants
    _deny(d["model_gateway"], "SELECT * FROM agent.approval_records")
    _deny(d["sentinel"], "UPDATE agent.tool_intents SET status_code='DONE'")
    _deny(d["api"], "UPDATE agent.tool_intents SET status_code='DONE'")
    _deny(d["api"], "SELECT * FROM queue.jobs")
    _deny(d["tool_gateway"], "SELECT * FROM agent.api_credentials")
    _deny(d["api"], "SELECT * FROM agent.api_credentials")  # only via resolve_api_token()


def test_rls_api_without_tenant_sees_nothing(pg: PgEnv, tenant: dict[str, str]) -> None:
    with connect(pg.dsn["api"]) as c:
        assert c.execute("SELECT count(*) AS n FROM agent.workspaces").fetchone()["n"] == 0
        c.execute("SELECT set_config('aiagent.tenant_id', %s, false)", (tenant["tenantId"],))
        assert c.execute("SELECT count(*) AS n FROM agent.workspaces").fetchone()["n"] == 1
        other = admin.provision_tenant(pg.owner)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # WITH CHECK: cannot write into another tenant
            c.execute(
                "INSERT INTO agent.audit_records(tenant_id, id, action_code, target_type) VALUES (%s, gen_random_uuid(), 'x', 'y')",
                (other["tenantId"],),
            )


def test_append_only_head_and_events_even_for_owner(pg: PgEnv, tenant: dict[str, str]) -> None:
    from aiagent_core.commands import Scope, submit_task
    from aiagent_core.db import Database

    api = Database(pg.dsn["api"])
    submit_task(api, Scope(tenant["tenantId"], tenant["userId"], tenant["workspaceId"]), "seed")
    api.close()
    with connect(pg.owner) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM agent.run_checkpoints WHERE tenant_id=%s", (tenant["tenantId"],))
        for t in ("run_checkpoints", "domain_events", "task_goal_revisions"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"UPDATE agent.{t} SET tenant_id = tenant_id")


def test_upgrade_from_legacy_pr92_db_preserves_data_and_refuses_tampered() -> None:
    from aiagent_core.commands import Scope, submit_task
    from aiagent_core.db import Database

    env = make_database("aiagent_legacy92")
    try:
        import os

        os.environ["AIAGENT_ALLOW_DOWN"] = "1"
        assert migrate.down(env.owner, 6) == [9, 8, 7]
        seeded_tenant = admin.provision_tenant(env.owner)
        api_db = Database(env.dsn["api"])
        try:
            task = submit_task(
                api_db,
                Scope(seeded_tenant["tenantId"], seeded_tenant["userId"], seeded_tenant["workspaceId"]),
                "legacy-92-preserve",
            )
        finally:
            api_db.close()

        # Simulate legacy PR #92 metadata rows 0006..0016 + persisted consent data before upgrade.
        with connect(env.owner) as c:
            with c.transaction():
                c.execute(
                    "INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id, source_epoch)"
                    " VALUES (%s, %s, %s, 1)",
                    (seeded_tenant["tenantId"], seeded_tenant["userId"], task["taskId"]),
                )
                c.execute("DELETE FROM aiagent_meta.schema_migrations WHERE version >= 6")
                for ver, (name, sha) in migrate.LEGACY_PR92_MIGRATIONS.items():
                    c.execute(
                        "INSERT INTO aiagent_meta.schema_migrations(version, checksum_sha256, name, app_min_reader,"
                        " app_min_writer) VALUES (%s,%s,%s,5,5)",
                        (ver, sha, name),
                    )
                c.execute("UPDATE agent.schema_control SET schema_version = 16 WHERE singleton")

        # Tampered legacy checksum must still fail closed.
        with connect(env.owner) as c:
            with c.transaction():
                c.execute(
                    "UPDATE aiagent_meta.schema_migrations SET checksum_sha256 = repeat('0', 64) WHERE version = 11"
                )
        with pytest.raises(migrate.MigrationError, match="checksum mismatch for legacy migration"):
            migrate.up(env.owner)

        # Restore genuine PR #92 checksum -> non-destructive upgrade reconciles 6..16 -> 6 and advances 7,8,9.
        with connect(env.owner) as c:
            with c.transaction():
                _name, real_sha = migrate.LEGACY_PR92_MIGRATIONS[11]
                c.execute(
                    "UPDATE aiagent_meta.schema_migrations SET checksum_sha256 = %s WHERE version = 11", (real_sha,)
                )

        applied = migrate.up(env.owner)
        assert applied == [7, 8, 9]
        with connect(env.owner) as c:
            migrate.check_compatible(c)
            epoch_row = c.execute(
                "SELECT source_epoch FROM agent.memory_scope_epochs WHERE tenant_id=%s AND task_id=%s",
                (seeded_tenant["tenantId"], task["taskId"]),
            ).fetchone()
            assert epoch_row is not None and epoch_row["source_epoch"] == 1
            versions = [
                r["version"] for r in c.execute("SELECT version FROM aiagent_meta.schema_migrations ORDER BY version")
            ]
            assert versions == list(range(1, migrate.APP_SCHEMA_VERSION + 1))
    finally:
        import os

        os.environ.pop("AIAGENT_ALLOW_DOWN", None)
        drop_database(env.name)
