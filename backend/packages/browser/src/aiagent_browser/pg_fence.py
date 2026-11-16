"""PostgreSQL-backed FencePort bound to agent.browser_sessions SQL SECURITY DEFINER functions."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .contracts import Authority, BrowserError


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BrowserError("INVALID_AUTHORITY") from exc


def _is_fence_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    msg = str(exc)
    return "STALE_LEASE" in msg or "CheckViolation" in name


class PostgresFencePort:
    """Authoritative browser session lease/scope fence backed by PostgreSQL."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def _run_tx(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            with self._db.tx() as conn:
                conn.execute(sql, params)
        except Exception as exc:
            if _is_fence_error(exc):
                raise BrowserError("STALE_LEASE") from exc
            raise BrowserError("FENCE_UNAVAILABLE") from exc

    async def register(self, session_id: str, authority: Authority) -> None:
        params = (
            _parse_uuid(authority.tenant_id),
            _parse_uuid(session_id),
            _parse_uuid(authority.owner_user_id),
            _parse_uuid(authority.task_id),
            _parse_uuid(authority.run_id),
            int(authority.goal_revision),
            int(authority.lease_epoch),
            authority.worker_ref,
        )
        await asyncio.to_thread(
            self._run_tx,
            "SELECT agent.browser_session_register(%s,%s,%s,%s,%s,%s,%s,%s)",
            params,
        )

    async def check(self, session_id: str, authority: Authority, mode: str = "AGENT") -> None:
        if mode not in ("AGENT", "HUMAN"):
            raise BrowserError("STALE_LEASE")
        params = (
            _parse_uuid(authority.tenant_id),
            _parse_uuid(session_id),
            _parse_uuid(authority.owner_user_id),
            _parse_uuid(authority.task_id),
            _parse_uuid(authority.run_id),
            int(authority.goal_revision),
            int(authority.lease_epoch),
            mode,
            authority.worker_ref,
        )
        await asyncio.to_thread(
            self._run_tx,
            "SELECT agent.browser_session_check(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            params,
        )

    async def advance(self, session_id: str, old: Authority, new: Authority, mode: str) -> None:
        if mode not in ("AGENT", "HUMAN"):
            raise BrowserError("STALE_LEASE")
        params = (
            _parse_uuid(old.tenant_id),
            _parse_uuid(session_id),
            _parse_uuid(old.owner_user_id),
            _parse_uuid(old.task_id),
            _parse_uuid(old.run_id),
            int(old.goal_revision),
            int(old.lease_epoch),
            _parse_uuid(new.run_id),
            int(new.goal_revision),
            int(new.lease_epoch),
            mode,
            new.worker_ref,
        )
        await asyncio.to_thread(
            self._run_tx,
            "SELECT agent.browser_session_advance(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            params,
        )

    async def revoke(self, session_id: str, authority: Authority) -> None:
        params = (
            _parse_uuid(authority.tenant_id),
            _parse_uuid(session_id),
            _parse_uuid(authority.owner_user_id),
            _parse_uuid(authority.task_id),
            _parse_uuid(authority.run_id),
            int(authority.goal_revision),
            int(authority.lease_epoch),
            authority.worker_ref,
        )
        await asyncio.to_thread(
            self._run_tx,
            "SELECT agent.browser_session_revoke(%s,%s,%s,%s,%s,%s,%s,%s)",
            params,
        )
