"""Shared fixtures: a fresh PostgreSQL database per test session, migrated by the real runner, with one LOGIN role
per process (the same least-privilege grants as production). AIAGENT_TEST_PG_URL = superuser DSN of a disposable
cluster, e.g. postgresql://postgres:pw@127.0.0.1:55432/postgres."""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import pytest

os.environ.setdefault("AIAGENT_PROFILE", "test")
sys.path.insert(0, str(Path(__file__).parent))

from aiagent_core import admin, migrate  # noqa: E402
from aiagent_core.db import Database, connect  # noqa: E402

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from stack import Stack

PG = os.environ.get("AIAGENT_TEST_PG_URL")
ROLE_PW = "test-" + secrets.token_hex(8)
SESSION = secrets.token_hex(3)


def _with_db(url: str, dbname: str, user: str | None = None, password: str | None = None) -> str:
    p = urlsplit(url)
    netloc = p.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((p.scheme, netloc, "/" + dbname, p.query, p.fragment))


@dataclass
class PgEnv:
    owner: str
    dsn: dict[str, str]
    name: str

    def db(self, role: str, **kw: object) -> Database:
        return Database(self.dsn[role], **kw)  # type: ignore[arg-type]


def make_database(prefix: str = "aiagent_t") -> PgEnv:
    assert PG
    name = f"{prefix}_{secrets.token_hex(4)}"
    with connect(PG) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    owner = _with_db(PG, name)
    migrate.up(owner)
    # login roles are cluster-wide: a per-session prefix keeps concurrent test sessions from clobbering each other
    logins = admin.create_logins(owner, {k: ROLE_PW for k in admin.GROUPS}, prefix=f"aiagent_t{SESSION}_")
    dsn = {k: _with_db(PG, name, v, ROLE_PW) for k, v in logins.items()}
    return PgEnv(owner, dsn, name)


def drop_database(name: str) -> None:
    with connect(PG) as c:  # type: ignore[arg-type]
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def pg() -> Iterator[PgEnv]:
    if not PG:
        pytest.skip("AIAGENT_TEST_PG_URL not set")
    env = make_database()
    yield env
    if os.environ.get("AIAGENT_TEST_KEEP_DB") != "1":
        drop_database(env.name)


@pytest.fixture(scope="session")
def dbs(pg: PgEnv) -> Iterator[dict[str, Database]]:
    out = {k: pg.db(k, max_size=16) for k in pg.dsn}
    out["owner"] = Database(pg.owner, max_size=8)
    yield out
    for d in out.values():
        d.close()


@pytest.fixture()
def tenant(pg: PgEnv) -> dict[str, str]:
    return admin.provision_tenant(pg.owner)


# ------------------------------------------------------------------ multi-process stack + API client (E2E suites)
@pytest.fixture(scope="session")
def stack(pg: PgEnv, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    from stack import Stack

    s = Stack(pg.dsn, tmp_path_factory.mktemp("stack"), owner_dsn=pg.owner).start_all()
    yield s
    s.close()


@pytest.fixture()
def api(pg: PgEnv) -> Iterator[TestClient]:
    from aiagent_api.app import create_app
    from fastapi.testclient import TestClient

    db = Database(pg.dsn["api"], max_size=4)
    with TestClient(create_app(db)) as c:
        yield c
    db.close()
