"""BrowserSessionService: scoped admission, local mutex, external fencing, zero real-site egress."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from .contracts import (
    ActionOutcome,
    ApprovalPort,
    Authority,
    AuthorityPort,
    AuthTransitionPort,
    BrowserError,
    BrowserTarget,
    FencePort,
    FixtureGatePort,
    FixturePort,
    FormInput,
    IdFactory,
    Observation,
    RedactionPort,
    SessionView,
    State,
    WorkerPort,
    validate_browser_url,
)


def _resolve_redirect(current: str, redirect_to: str) -> str:
    if not isinstance(redirect_to, str) or len(redirect_to) > 2048 or "\\" in redirect_to:
        raise BrowserError("INVALID_FIXTURE_REDIRECT")
    try:
        redir_parts = urlsplit(redirect_to)
    except ValueError as exc:
        raise BrowserError("INVALID_FIXTURE_REDIRECT") from exc
    if redir_parts.scheme:
        return redirect_to
    base = urlsplit(current)
    target_scheme = base.scheme.lower()
    if target_scheme == "fixture":
        joined = urlsplit(urljoin(f"http://{base.netloc}{base.path or '/'}", redirect_to))
        if joined.scheme != "http":
            raise BrowserError("EGRESS_DENIED")
        return urlunsplit(("fixture", joined.netloc, joined.path, joined.query, joined.fragment))
    if target_scheme in ("http", "https"):
        return urljoin(current, redirect_to)
    raise BrowserError("EGRESS_DENIED")


@dataclass(slots=True)
class _Entry:
    scope: tuple[str, str, str]
    state: State = State.STARTING
    generation: int = 1
    current_url: str | None = None
    last_challenge: str | None = None
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
        approval: ApprovalPort | None = None,
        auth_transition: AuthTransitionPort | None = None,
        max_sessions_per_task: int = 2,
        max_observation_chars: int = 4096,
        allowed_web_hosts: frozenset[str] = frozenset(),
        allow_https: bool = False,
        allow_http_web: bool = False,
    ) -> None:
        if (
            not allowed_fixture_hosts
            or not all(isinstance(h, str) and bool(h.strip()) and h == h.strip().lower() for h in allowed_fixture_hosts)
            or max_sessions_per_task < 1
            or not 1 <= max_observation_chars <= 8192
        ):
            raise ValueError("invalid browser configuration")
        self._worker = worker
        self._authority = authority
        self._fence = fence
        self._gate = fixture_gate
        self._fixture = fixture
        self._redactor = redactor
        self._new_id = new_id
        self._hosts = allowed_fixture_hosts
        self._web_hosts = frozenset(h.strip().lower() for h in allowed_web_hosts if h.strip())
        self._allow_https = allow_https
        self._allow_http_web = allow_http_web
        self._approval = approval
        self._auth_transition = auth_transition
        self._max_sessions_per_task = max_sessions_per_task
        self._max_chars = max_observation_chars
        self._entries: dict[str, _Entry] = {}
        self._create_lock = asyncio.Lock()

    def _validate_url(self, url: str) -> str:
        return validate_browser_url(
            url,
            self._hosts,
            allowed_web_hosts=self._web_hosts,
            allow_https=self._allow_https,
            allow_http=self._allow_http_web,
        )

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
            async with entry.lock:
                await self._fence.check(session_id, authority)
                await self._worker.start(session_id)
                await self._fence.check(session_id, authority)
                entry.state = State.ACTIVE
                return SessionView(session_id, State.ACTIVE, 1, authority.goal_revision, authority.lease_epoch)
        except BaseException:
            entry.state = State.CLOSED
            self._entries.pop(session_id, None)
            try:
                await self._worker.close(session_id)
            except Exception:
                pass
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

    async def _fetch_and_render_locked(
        self,
        session_id: str,
        authority: Authority,
        entry: _Entry,
        url: str,
        operation: str,
    ) -> Observation:
        current = self._validate_url(url)
        visited: set[str] = set()
        try:
            for _ in range(4):
                if current in visited:
                    raise BrowserError("REDIRECT_LOOP")
                visited.add(current)
                await self._check(session_id, authority, operation)
                # Cannot infer a real-network permit from a mock fixture permission.
                await self._gate.permit(authority, current)
                response = await self._fixture.fetch(current)
                if response.status in (301, 302, 303, 307, 308):
                    if response.redirect_to is None or not response.redirect_to.strip():
                        raise BrowserError("INVALID_FIXTURE_REDIRECT")
                    current = self._validate_url(_resolve_redirect(current, response.redirect_to))
                    continue
                is_web = current.startswith(("http://", "https://"))
                max_html_bytes = 524_288 if is_web else 65_536
                if (
                    response.status not in ((200, 401, 403) if is_web else (200,))
                    or len(response.html.encode("utf-8")) > max_html_bytes
                ):
                    raise BrowserError("FIXTURE_RESPONSE_DENIED")
                await self._check(session_id, authority, operation)
                if is_web and hasattr(self._worker, "render"):
                    try:
                        status_str = await self._worker.render(  # type: ignore[call-arg]
                            session_id, response.html, origin_url=current, status=response.status
                        )
                    except TypeError:
                        status_str = await self._worker.render(session_id, response.html)
                else:
                    status_str = await self._worker.render(session_id, response.html)
                await self._fence.check(session_id, authority)
                entry.current_url = current
                if is_web and status_str in ("NEEDS_LOGIN", "CAPTCHA_CHALLENGE"):
                    entry.state = State.NEEDS_LOGIN
                    entry.last_challenge = status_str
                else:
                    entry.last_challenge = None
                return await self._observe_locked(session_id, authority, entry)
            raise BrowserError("REDIRECT_LIMIT")
        except BrowserError as exc:
            if exc.code == "BROWSER_CRASHED":
                entry.state = State.RECOVERING
            raise

    async def navigate_fixture(self, session_id: str, authority: Authority, url: str) -> Observation:
        entry = await self._operation(session_id, authority, "navigate_fixture")
        async with entry.lock:
            await self._operation(session_id, authority, "navigate_fixture")
            return await self._fetch_and_render_locked(session_id, authority, entry, url, "navigate_fixture")

    async def navigate(self, session_id: str, authority: Authority, url: str) -> Observation:
        """Navigate either `fixture://` or explicitly allowed `https://`/`http://` URLs safely."""
        return await self.navigate_fixture(session_id, authority, url)

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
        if entry.state not in (State.ACTIVE, State.PAUSED, State.NEEDS_LOGIN):
            raise BrowserError("INVALID_STATE")
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
            if entry.state not in (State.ACTIVE, State.PAUSED, State.NEEDS_LOGIN):
                raise BrowserError("INVALID_STATE")
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

    async def click(
        self,
        session_id: str,
        authority: Authority,
        target: BrowserTarget,
        *,
        timeout_ms: int = 5000,
    ) -> ActionOutcome:
        if not isinstance(target, BrowserTarget) or not 1 <= timeout_ms <= 30_000:
            raise BrowserError("INVALID_TARGET")
        entry = await self._operation(session_id, authority, "click")
        async with entry.lock:
            await self._operation(session_id, authority, "click")
            if entry.current_url is not None:
                self._validate_url(entry.current_url)
                await self._gate.permit(authority, entry.current_url)
            before = await self._observe_locked(session_id, authority, entry)
            try:
                worker_status = await self._worker.click(session_id, target, timeout_ms=timeout_ms)
            except BrowserError as exc:
                if exc.code == "BROWSER_CRASHED":
                    entry.state = State.RECOVERING
                elif exc.code == "ACTION_TIMEOUT":
                    entry.state = State.PAUSED
                    raise BrowserError("ACTION_OUTCOME_UNKNOWN") from exc
                raise
            await self._fence.check(session_id, authority)
            after = await self._observe_locked(session_id, authority, entry)
            if worker_status == "NEEDS_LOGIN":
                entry.state = State.NEEDS_LOGIN
                return ActionOutcome(session_id, entry.generation, "click", "NEEDS_LOGIN", before, after)
            return ActionOutcome(session_id, entry.generation, "click", "EXECUTED", before, after)

    async def submit(
        self,
        session_id: str,
        authority: Authority,
        target: BrowserTarget,
        inputs: tuple[FormInput, ...] = (),
        *,
        timeout_ms: int = 5000,
    ) -> ActionOutcome:
        if (
            not isinstance(target, BrowserTarget)
            or not isinstance(inputs, tuple)
            or any(not isinstance(item, FormInput) for item in inputs)
            or not 1 <= timeout_ms <= 30_000
        ):
            raise BrowserError("INVALID_TARGET")
        entry = await self._operation(session_id, authority, "submit")
        async with entry.lock:
            await self._operation(session_id, authority, "submit")
            if self._approval is None:
                raise BrowserError("APPROVAL_REQUIRED")
            await self._approval.approve(authority, session_id, target)
            await self._operation(session_id, authority, "submit")
            if entry.current_url is not None:
                self._validate_url(entry.current_url)
                await self._gate.permit(authority, entry.current_url)
            before = await self._observe_locked(session_id, authority, entry)
            try:
                worker_status = await self._worker.submit(session_id, target, inputs, timeout_ms=timeout_ms)
            except BrowserError as exc:
                if exc.code == "BROWSER_CRASHED":
                    entry.state = State.RECOVERING
                elif exc.code == "ACTION_TIMEOUT":
                    entry.state = State.PAUSED
                    raise BrowserError("ACTION_OUTCOME_UNKNOWN") from exc
                raise
            await self._fence.check(session_id, authority)
            after = await self._observe_locked(session_id, authority, entry)
            if worker_status == "NEEDS_LOGIN":
                entry.state = State.NEEDS_LOGIN
                return ActionOutcome(session_id, entry.generation, "submit", "NEEDS_LOGIN", before, after)
            return ActionOutcome(session_id, entry.generation, "submit", "EXECUTED", before, after)

    async def authenticated_navigate(self, session_id: str, authority: Authority, url: str) -> Observation:
        entry = await self._check(session_id, authority, "authenticated_navigate")
        if entry.state not in (State.ACTIVE, State.NEEDS_LOGIN):
            raise BrowserError("INVALID_STATE")
        if self._auth_transition is None:
            raise BrowserError("AUTH_BROKER_REQUIRED")
        async with entry.lock:
            entry = await self._check(session_id, authority, "authenticated_navigate")
            if entry.state not in (State.ACTIVE, State.NEEDS_LOGIN):
                raise BrowserError("INVALID_STATE")
            target_url = self._validate_url(url)
            await self._auth_transition.permit_transition(authority, session_id, target_url)
            await self._fence.check(session_id, authority)
            obs = await self._fetch_and_render_locked(
                session_id, authority, entry, target_url, "authenticated_navigate"
            )
            entry.state = State.ACTIVE
            entry.last_challenge = None
            return obs

    def session_details(self, session_id: str, authority: Authority) -> tuple[State, int, str | None, str | None]:
        entry = self._entry(session_id, authority)
        return entry.state, entry.generation, entry.current_url, entry.last_challenge

    async def apply_owner_takeover_resume(
        self,
        session_id: str,
        authority: Authority,
        *,
        db_generation: int,
    ) -> None:
        entry = await self._check(session_id, authority, "takeover_resume")
        async with entry.lock:
            if db_generation > entry.generation and entry.state in (State.PAUSED, State.NEEDS_LOGIN, State.ACTIVE):
                entry.generation = db_generation
                entry.state = State.ACTIVE
                entry.last_challenge = None
