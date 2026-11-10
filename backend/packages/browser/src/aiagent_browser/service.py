"""BrowserSessionService: scoped admission, local mutex, external fencing, zero real-site egress."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from .contracts import (
    Authority,
    AuthorityPort,
    BrowserError,
    FencePort,
    FixtureGatePort,
    FixturePort,
    IdFactory,
    Observation,
    RedactionPort,
    SessionView,
    State,
    WorkerPort,
    validate_fixture_url,
)


@dataclass(slots=True)
class _Entry:
    scope: tuple[str, str, str]
    state: State = State.STARTING
    generation: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserSessionService:
    """All ports mandatory. Never interpret model/page-provided fields as authorization."""

    def __init__(
        self,
        *,
        worker: WorkerPort,
        authority: AuthorityPort,
        fence: FencePort,
        fixture_gate: FixtureGatePort,
        fixture: FixturePort,
        redactor: RedactionPort,
        new_id: IdFactory,
        allowed_fixture_hosts: frozenset[str],
        max_sessions_per_task: int = 2,
        max_observation_chars: int = 4096,
    ) -> None:
        if not allowed_fixture_hosts or max_sessions_per_task < 1 or not 1 <= max_observation_chars <= 8192:
            raise ValueError("invalid browser configuration")
        self._worker = worker
        self._authority = authority
        self._fence = fence
        self._gate = fixture_gate
        self._fixture = fixture
        self._redactor = redactor
        self._new_id = new_id
        self._hosts = allowed_fixture_hosts
        self._max_sessions_per_task = max_sessions_per_task
        self._max_chars = max_observation_chars
        self._entries: dict[str, _Entry] = {}
        self._create_lock = asyncio.Lock()

    def _entry(self, session_id: str, authority: Authority) -> _Entry:
        entry = self._entries.get(session_id)
        if entry is None or entry.scope != (authority.tenant_id, authority.owner_user_id, authority.task_id):
            raise BrowserError("SESSION_NOT_FOUND")
        return entry

    async def _check(self, session_id: str, authority: Authority, operation: str) -> _Entry:
        entry = self._entry(session_id, authority)
        await self._authority.authorize(authority, operation, session_id)
        await self._fence.check(session_id, authority)
        return entry

    async def create(self, authority: Authority) -> SessionView:
        await self._authority.authorize(authority, "create", None)
        async with self._create_lock:
            active = sum(
                x.scope == (authority.tenant_id, authority.owner_user_id, authority.task_id)
                and x.state is not State.CLOSED
                for x in self._entries.values()
            )
            if active >= self._max_sessions_per_task:
                raise BrowserError("SESSION_QUOTA")
            session_id = self._new_id()
            try:
                valid = uuid.UUID(session_id)
            except (ValueError, AttributeError, TypeError) as exc:
                raise BrowserError("INVALID_SESSION_ID") from exc
            if valid.version != 7 or str(valid) != session_id or session_id in self._entries:
                raise BrowserError("INVALID_SESSION_ID")
            entry = _Entry(scope=(authority.tenant_id, authority.owner_user_id, authority.task_id))
            await self._fence.register(session_id, authority)
            self._entries[session_id] = entry
            try:
                await self._fence.check(session_id, authority)
                await self._worker.start(session_id)
                await self._fence.check(session_id, authority)
                entry.state = State.ACTIVE
                return SessionView(session_id, State.ACTIVE, 1, authority.goal_revision, authority.lease_epoch)
            except Exception:
                entry.state = State.CLOSED
                await self._worker.close(session_id)
                try:
                    await self._fence.revoke(session_id, authority)
                except BrowserError:
                    pass
                raise

    async def view(self, session_id: str, authority: Authority) -> SessionView:
        entry = await self._check(session_id, authority, "observe")
        return SessionView(session_id, entry.state, entry.generation, authority.goal_revision, authority.lease_epoch)

    async def _operation(self, session_id: str, authority: Authority, name: str) -> _Entry:
        entry = await self._check(session_id, authority, name)
        if entry.state is not State.ACTIVE:
            raise BrowserError("SESSION_NOT_ACTIVE")
        return entry

    async def navigate_fixture(self, session_id: str, authority: Authority, url: str) -> Observation:
        entry = await self._operation(session_id, authority, "navigate_fixture")
        async with entry.lock:
            await self._operation(session_id, authority, "navigate_fixture")
            current = validate_fixture_url(url, self._hosts)
            try:
                for _ in range(4):
                    await self._check(session_id, authority, "navigate_fixture")
                    # Cannot infer a real-network permit from a mock fixture permission.
                    await self._gate.permit(authority, current)
                    response = await self._fixture.fetch(current)
                    if response.status in (301, 302, 303, 307, 308):
                        if response.redirect_to is None:
                            raise BrowserError("INVALID_FIXTURE_REDIRECT")
                        current = validate_fixture_url(response.redirect_to, self._hosts)
                        continue
                    if response.status != 200 or len(response.html.encode("utf-8")) > 65536:
                        raise BrowserError("FIXTURE_RESPONSE_DENIED")
                    await self._check(session_id, authority, "navigate_fixture")
                    await self._worker.render(session_id, response.html)
                    await self._fence.check(session_id, authority)
                    return await self._observe_locked(session_id, authority, entry)
                raise BrowserError("REDIRECT_LIMIT")
            except BrowserError as exc:
                if exc.code == "BROWSER_CRASHED":
                    entry.state = State.RECOVERING
                raise

    async def _observe_locked(self, session_id: str, authority: Authority, entry: _Entry) -> Observation:
        await self._check(session_id, authority, "observe")
        try:
            raw_text = await self._worker.observe(session_id)
        except BrowserError as exc:
            if exc.code == "BROWSER_CRASHED":
                entry.state = State.RECOVERING
            raise
        await self._fence.check(session_id, authority)
        # Redaction comes from policy owner; never log or store raw rendered text.
        redacted = self._redactor.redact(raw_text)
        if not isinstance(redacted, str):
            raise BrowserError("REDACTION_DENIED")
        truncated = len(redacted) > self._max_chars
        return Observation(session_id, entry.generation, redacted[: self._max_chars], truncated)

    async def observe(self, session_id: str, authority: Authority) -> Observation:
        entry = await self._operation(session_id, authority, "observe")
        async with entry.lock:
            await self._operation(session_id, authority, "observe")
            return await self._observe_locked(session_id, authority, entry)

    async def takeover(self, session_id: str, current: Authority, human: Authority) -> SessionView:
        entry = self._entry(session_id, current)
        await self._authority.authorize(current, "takeover", session_id)
        if (current.tenant_id, current.owner_user_id, current.task_id) != (
            human.tenant_id,
            human.owner_user_id,
            human.task_id,
        ):
            raise BrowserError("AUTHORITY_MISMATCH")
        await self._authority.authorize(human, "human_takeover", session_id)
        # Fence FIRST to invalidate a pending operation, then wait for local Page mutex.
        await self._fence.advance(session_id, current, human, mode="HUMAN")
        async with entry.lock:
            entry.state = State.PAUSED
            return SessionView(session_id, entry.state, entry.generation, human.goal_revision, human.lease_epoch)

    async def mark_crashed(self, session_id: str, authority: Authority) -> None:
        entry = await self._check(session_id, authority, "recover")
        async with entry.lock:
            await self._check(session_id, authority, "recover")
            entry.state = State.RECOVERING
            await self._worker.close(session_id)

    async def recover(self, session_id: str, current: Authority, renewed: Authority) -> SessionView:
        entry = self._entry(session_id, current)
        await self._authority.authorize(current, "recover", session_id)
        async with entry.lock:
            if entry.state is not State.RECOVERING:
                raise BrowserError("RECOVERY_NOT_REQUIRED")
            await self._fence.check(session_id, current)
            if (current.tenant_id, current.owner_user_id, current.task_id) != (
                renewed.tenant_id,
                renewed.owner_user_id,
                renewed.task_id,
            ):
                raise BrowserError("AUTHORITY_MISMATCH")
            await self._authority.authorize(renewed, "recover_renewed", session_id)
            await self._fence.advance(session_id, current, renewed, mode="AGENT")
            await self._worker.close(session_id)
            try:
                await self._worker.start(session_id)
            except BrowserError:
                entry.state = State.RECOVERING
                raise
            await self._fence.check(session_id, renewed)
            entry.generation += 1
            entry.state = State.NEEDS_LOGIN  # Never assume persisted cookies or authenticated state.
            return SessionView(session_id, entry.state, entry.generation, renewed.goal_revision, renewed.lease_epoch)

    async def resume_offline_fixture(self, session_id: str, authority: Authority) -> None:
        entry = await self._check(session_id, authority, "resume_offline_fixture")
        async with entry.lock:
            await self._check(session_id, authority, "resume_offline_fixture")
            if entry.state is not State.NEEDS_LOGIN:
                raise BrowserError("SESSION_NOT_RECOVERED")
            entry.state = State.ACTIVE  # No real login; offline fixtures ONLY.

    async def cancel(self, session_id: str, authority: Authority) -> None:
        entry = await self._check(session_id, authority, "cancel")
        # Revoke first: in-flight observations will fail post-action fence.
        await self._fence.revoke(session_id, authority)
        async with entry.lock:
            entry.state = State.CLOSED
            await self._worker.close(session_id)

    async def close(self, session_id: str, authority: Authority) -> None:
        await self.cancel(session_id, authority)

    async def click(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserError("NOT_INTEGRATED")

    async def submit(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserError("NOT_INTEGRATED")

    async def authenticated_navigate(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserError("NOT_INTEGRATED")
