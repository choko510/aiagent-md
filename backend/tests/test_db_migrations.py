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
    (d / f"{migrate.APP_SCHEMA_VERSION + 1:04d}_broken.up.sql").write_text(
        "CREATE TABLE agent.half_done(x int);\nSELECT * FROM no_such_table;\n"
    )
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate.up(pg.owner, d)
    with connect(pg.owner) as c:
        assert c.execute("SELECT to_regclass('agent.half_done') AS t").fetchone()["t"] is None
        assert (
            c.execute("SELECT max(version) AS v FROM aiagent_meta.schema_migrations").fetchone()["v"]
            == migrate.APP_SCHEMA_VERSION
        )
        assert (
            c.execute("SELECT schema_version FROM agent.schema_control").fetchone()["schema_version"]
            == migrate.APP_SCHEMA_VERSION
        )


def test_upgrade_from_previous_version_and_unknown_future_refused(tmp_path: Path) -> None:
    env = make_database("aiagent_up")
    try:
        # simulate an old deployment at version 3, then upgrade
        migrate_dir = migrate.default_dir()
        import os

        os.environ["AIAGENT_ALLOW_DOWN"] = "1"
        assert migrate.down(env.owner, 3) == list(range(migrate.APP_SCHEMA_VERSION, 3, -1))
        assert migrate.up(env.owner) == list(range(4, migrate.APP_SCHEMA_VERSION + 1))
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
        assert sorted(len(r) for r in results) == [0, 0, 0, migrate.APP_SCHEMA_VERSION]
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
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    from aiagent_core.commands import Scope, submit_task
    from aiagent_core.db import Database
    from aiagent_domain import uuid7
    from conftest import PG, ROLE_PW, SESSION, _with_db

    name = f"aiagent_real92_{secrets.token_hex(4)}"
    with connect(PG) as c:  # type: ignore[arg-type]
        c.execute(f'CREATE DATABASE "{name}"')
    owner = _with_db(PG, name)  # type: ignore[arg-type]
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "legacy_pr92"
    try:
        # 1. Build a genuine PR #92 PostgreSQL 16 database from 0001..0005 + real 0006..0016 SQLs.
        assert migrate.up(owner, target=5) == [1, 2, 3, 4, 5]
        legacy_files = sorted(fixture_dir.glob("*.up.sql"))
        assert len(legacy_files) == 11
        with connect(owner) as c:
            for sql_file in legacy_files:
                ver = int(sql_file.name[:4])
                mig_name = sql_file.stem.removesuffix(".up")[5:]
                raw_bytes = sql_file.read_bytes()
                sha = hashlib.sha256(raw_bytes).hexdigest()
                with c.transaction():
                    c.execute(raw_bytes.decode("utf-8"))
                    c.execute(
                        "INSERT INTO aiagent_meta.schema_migrations(version, checksum_sha256, name,"
                        " app_min_reader, app_min_writer) VALUES (%s,%s,%s,5,5)",
                        (ver, sha, mig_name),
                    )
                    c.execute("UPDATE agent.schema_control SET schema_version=%s WHERE singleton", (ver,))

        logins = admin.create_logins(owner, {k: ROLE_PW for k in admin.GROUPS}, prefix=f"aiagent_t{SESSION}_")
        dsn = {k: _with_db(PG, name, v, ROLE_PW) for k, v in logins.items()}
        seeded_tenant = admin.provision_tenant(owner)
        api_db = Database(dsn["api"], max_size=2)
        try:
            task = submit_task(
                api_db,
                Scope(seeded_tenant["tenantId"], seeded_tenant["userId"], seeded_tenant["workspaceId"]),
                "authentic-legacy-92-task",
            )
        finally:
            api_db.close()

        # Seed real rows in legacy PR #92 Context/Memory/Restore/Consent tables before upgrade.
        art_raw_id, art_proj_id, mem_id = uuid7(), uuid7(), uuid7()
        now = datetime.now(UTC)
        with connect(owner) as c:
            with c.transaction():
                c.execute(
                    "INSERT INTO agent.context_artifacts(tenant_id, id, task_id, run_id, owner_user_id, kind,"
                    " content_sha256, byte_length, nonce, ciphertext, key_id, expires_at)"
                    " VALUES (%s,%s,%s,%s,%s,'raw',%s,5,%s,%s,'k1',%s),"
                    "        (%s,%s,%s,%s,%s,'raw',%s,5,%s,%s,'k1',%s)",
                    (
                        seeded_tenant["tenantId"],
                        art_raw_id,
                        task["taskId"],
                        task["runId"],
                        seeded_tenant["userId"],
                        "a" * 64,
                        b"\x01" * 12,
                        b"cipher-raw",
                        now + timedelta(hours=1),
                        seeded_tenant["tenantId"],
                        art_proj_id,
                        task["taskId"],
                        task["runId"],
                        seeded_tenant["userId"],
                        "b" * 64,
                        b"\x02" * 12,
                        b"cipher-sum",
                        now + timedelta(hours=1),
                    ),
                )
                c.execute(
                    "INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id, source_epoch)"
                    " VALUES (%s, %s, %s, 1)",
                    (seeded_tenant["tenantId"], seeded_tenant["userId"], task["taskId"]),
                )
                c.execute(
                    "INSERT INTO agent.memory_records(tenant_id, owner_user_id, task_id, id, revision,"
                    " source_artifact_id, projection_artifact_id, recipient, consent_basis, observed_at, expires_at)"
                    " VALUES (%s,%s,%s,%s,1,%s,%s,'anthropic','user_explicit',%s,%s)",
                    (
                        seeded_tenant["tenantId"],
                        seeded_tenant["userId"],
                        task["taskId"],
                        mem_id,
                        art_raw_id,
                        art_proj_id,
                        now,
                        now + timedelta(days=7),
                    ),
                )
                c.execute(
                    "INSERT INTO agent.memory_consents(tenant_id, owner_user_id, recipient, consent_basis,"
                    " granted_at, record_version) VALUES (%s,%s,'anthropic','user_explicit',now(),1)",
                    (seeded_tenant["tenantId"], seeded_tenant["userId"]),
                )
                c.execute(
                    "UPDATE agent.memory_tombstone_coverage SET covered_revocation_generation=3,"
                    " chain_head=repeat('c',64) WHERE singleton"
                )

        # 2. Tampered legacy checksum still fails closed.
        with connect(owner) as c:
            with c.transaction():
                c.execute(
                    "UPDATE aiagent_meta.schema_migrations SET checksum_sha256 = repeat('0', 64) WHERE version = 11"
                )
        with pytest.raises(migrate.MigrationError, match="checksum mismatch for legacy migration"):
            migrate.up(owner)

        # 3. Genuine legacy DB upgrades non-destructively to latest APP_SCHEMA_VERSION.
        with connect(owner) as c:
            with c.transaction():
                _name, real_sha = migrate.LEGACY_PR92_MIGRATIONS[11]
                c.execute(
                    "UPDATE aiagent_meta.schema_migrations SET checksum_sha256 = %s WHERE version = 11", (real_sha,)
                )

        applied = migrate.up(owner)
        assert applied == list(range(7, migrate.APP_SCHEMA_VERSION + 1))
        with connect(owner) as c:
            migrate.check_compatible(c)
            # Legacy rows preserved intact
            assert (
                c.execute(
                    "SELECT count(*) AS n FROM agent.context_artifacts WHERE tenant_id=%s AND task_id=%s",
                    (seeded_tenant["tenantId"], task["taskId"]),
                ).fetchone()["n"]
                == 2
            )
            assert (
                c.execute(
                    "SELECT source_epoch FROM agent.memory_scope_epochs WHERE tenant_id=%s AND task_id=%s",
                    (seeded_tenant["tenantId"], task["taskId"]),
                ).fetchone()["source_epoch"]
                == 2
            )
            assert (
                c.execute(
                    "SELECT recipient FROM agent.memory_records WHERE tenant_id=%s AND id=%s",
                    (seeded_tenant["tenantId"], mem_id),
                ).fetchone()["recipient"]
                == "anthropic"
            )
            assert (
                c.execute(
                    "SELECT covered_revocation_generation FROM agent.memory_tombstone_coverage WHERE singleton"
                ).fetchone()["covered_revocation_generation"]
                == 3
            )
            # Upgraded objects, columns, and functions all exist cleanly
            assert c.execute("SELECT to_regclass('agent.browser_sessions') AS t").fetchone()["t"] is not None
            assert c.execute("SELECT to_regclass('agent.dev_cycles') AS t").fetchone()["t"] is not None
            assert c.execute("SELECT to_regclass('agent.web_sessions') AS t").fetchone()["t"] is not None
            assert c.execute("SELECT to_regclass('agent.notification_bindings') AS t").fetchone()["t"] is not None
            assert c.execute("SELECT to_regclass('agent.notification_intents') AS t").fetchone()["t"] is not None
            assert c.execute("SELECT to_regclass('agent.notification_attempts') AS t").fetchone()["t"] is not None
            assert (
                c.execute(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_schema='agent' AND table_name='runs' AND column_name='max_tool_calls'"
                ).fetchone()
                is not None
            )
            assert (
                c.execute(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_schema='agent' AND table_name='automation_watches' AND column_name='local_hour'"
                ).fetchone()
                is not None
            )
            assert (
                c.execute(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                    " WHERE n.nspname='agent' AND p.proname='browser_session_check'"
                ).fetchone()
                is not None
            )
            assert (
                c.execute(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                    " WHERE n.nspname='agent' AND p.proname='create_web_session'"
                ).fetchone()
                is not None
            )
            versions = [
                r["version"] for r in c.execute("SELECT version FROM aiagent_meta.schema_migrations ORDER BY version")
            ]
            assert versions == list(range(1, migrate.APP_SCHEMA_VERSION + 1))

        # 4. Post-upgrade RLS works cleanly for least-privilege roles and blocks cross-tenant.
        with connect(dsn["runtime"]) as rc:
            with rc.transaction():
                rc.execute("SELECT set_config('aiagent.tenant_id', %s, true)", (seeded_tenant["tenantId"],))
                assert rc.execute("SELECT count(*) AS n FROM agent.dev_cycles").fetchone()["n"] == 0
        other = admin.provision_tenant(owner)
        with connect(dsn["api"]) as ac:
            with ac.transaction():
                ac.execute("SELECT set_config('aiagent.tenant_id', %s, true)", (other["tenantId"],))
                assert ac.execute("SELECT count(*) AS n FROM agent.tasks").fetchone()["n"] == 0

        # 5. Post-upgrade restore bump still succeeds cleanly on migrated DB.
        assert admin.restore_bump(owner) == 2
        with connect(owner) as c:
            migrate.check_compatible(c)
    finally:
        drop_database(name)
