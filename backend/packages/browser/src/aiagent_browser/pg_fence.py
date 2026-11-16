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

    async def record_state(
        self,
        session_id: str,
        authority: Authority,
        *,
        mode: str = "AGENT",
        state: str,
        current_url: str | None = None,
        takeover_reason: str | None = None,
        obs_text: str | None = None,
        obs_truncated: bool = False,
        update_storage: bool = False,
        storage_nonce: bytes | None = None,
        storage_ciphertext: bytes | None = None,
        storage_key_id: str | None = None,
        storage_sha256: str | None = None,
    ) -> None:
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
            state,
            current_url,
            takeover_reason,
            obs_text,
            obs_truncated,
            update_storage,
            storage_nonce,
            storage_ciphertext,
            storage_key_id,
            storage_sha256,
        )
        await asyncio.to_thread(
            self._run_tx,
            "SELECT agent.browser_session_record_state(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            params,
        )

    async def read_status(self, session_id: str, authority: Authority, *, mode: str = "AGENT") -> dict[str, Any]:
        def q() -> dict[str, Any]:
            try:
                with self._db.tx() as conn:
                    row = conn.execute(
                        "SELECT state, mode, generation, current_url, takeover_reason"
                        " FROM agent.browser_session_read_status(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            _parse_uuid(authority.tenant_id),
                            _parse_uuid(session_id),
                            _parse_uuid(authority.owner_user_id),
                            _parse_uuid(authority.task_id),
                            _parse_uuid(authority.run_id),
                            int(authority.goal_revision),
                            int(authority.lease_epoch),
                            mode,
                            authority.worker_ref,
                        ),
                    ).fetchone()
                    if row is None:
                        raise BrowserError("STALE_LEASE")
                    return dict(row)
            except BrowserError:
                raise
            except Exception as exc:
                if _is_fence_error(exc):
                    raise BrowserError("STALE_LEASE") from exc
                raise BrowserError("FENCE_UNAVAILABLE") from exc

        return await asyncio.to_thread(q)

    async def fetch_storage_state(
        self, session_id: str, authority: Authority, *, mode: str = "AGENT"
    ) -> dict[str, Any] | None:
        def q() -> dict[str, Any] | None:
            try:
                with self._db.tx() as conn:
                    row = conn.execute(
                        "SELECT storage_state_nonce, storage_state_ciphertext, storage_state_key_id, storage_state_sha256"
                        " FROM agent.browser_session_fetch_storage_state(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            _parse_uuid(authority.tenant_id),
                            _parse_uuid(session_id),
                            _parse_uuid(authority.owner_user_id),
                            _parse_uuid(authority.task_id),
                            _parse_uuid(authority.run_id),
                            int(authority.goal_revision),
                            int(authority.lease_epoch),
                            mode,
                            authority.worker_ref,
                        ),
                    ).fetchone()
                    if row is None or row["storage_state_ciphertext"] is None:
                        return None
                    return {
                        "nonce": bytes(row["storage_state_nonce"]),
                        "ciphertext": bytes(row["storage_state_ciphertext"]),
                        "key_id": str(row["storage_state_key_id"]),
                        "sha256": str(row["storage_state_sha256"]),
                    }
            except Exception as exc:
                if _is_fence_error(exc):
                    raise BrowserError("STALE_LEASE") from exc
                raise BrowserError("FENCE_UNAVAILABLE") from exc

        return await asyncio.to_thread(q)
