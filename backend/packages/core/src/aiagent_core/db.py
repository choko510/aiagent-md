"""PostgreSQL access helpers (psycopg 3). One pool per process role; transactions are short and never span I/O
to external services."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

Row = dict[str, Any]


def connect(url: str, *, autocommit: bool = True) -> psycopg.Connection[Row]:
    return psycopg.connect(url, autocommit=autocommit, row_factory=dict_row, prepare_threshold=None)


class Database:
    """Connection pool wrapper. `tx()` yields a connection inside a transaction; `tenant` scopes RLS."""

    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 8, name: str = "aiagent") -> None:
        self.url = url
        self._pool: ConnectionPool[psycopg.Connection[Row]] | None = None
        self._lock = threading.Lock()
        self.min_size, self.max_size, self.name = min_size, max_size, name

    @property
    def pool(self) -> ConnectionPool[psycopg.Connection[Row]]:
        with self._lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    self.url,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    name=self.name,
                    open=True,
                    kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None},
                    timeout=30.0,
                )
            return self._pool

    @contextmanager
    def tx(self, tenant: str | None = None) -> Iterator[psycopg.Connection[Row]]:
        with self.pool.connection() as conn, conn.transaction():
            if tenant is not None:
                conn.execute("SELECT set_config('aiagent.tenant_id', %s, true)", (tenant,))
            yield conn

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection[Row]]:
        with self.pool.connection() as conn:
            yield conn

    def one(self, sql: str, args: Any = None) -> Row | None:
        with self.conn() as c:
            return c.execute(sql, args).fetchone()

    def all(self, sql: str, args: Any = None) -> list[Row]:
        with self.conn() as c:
            return c.execute(sql, args).fetchall()

    def ping(self) -> bool:
        try:
            return self.one("SELECT 1 AS ok") is not None
        except psycopg.Error:
            return False

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
