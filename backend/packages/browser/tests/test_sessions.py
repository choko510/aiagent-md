"""Fixture-only unit / mock integration. Never uses production credentials or public network."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace

import pytest
from aiagent_browser import Authority, BrowserError, BrowserSessionService, FixtureResponse, State


class MockAuth:
    def __init__(self):
        self.denied = set()

    async def authorize(self, authority, operation, session_id):
        if operation in self.denied:
            raise BrowserError("AUTH_DENIED")


class MockFence:
    """Test fixture mimicking one atomic persisted row, NOT a production fence."""

    def __init__(self):
        self.values = {}
        self.lock = asyncio.Lock()

    async def register(self, session_id, authority):
        async with self.lock:
            assert session_id not in self.values
            self.values[session_id] = (authority, "AGENT")

    async def check(self, session_id, authority, mode="AGENT"):
        async with self.lock:
            if self.values.get(session_id) != (authority, mode):
                raise BrowserError("STALE_LEASE")

    async def advance(self, session_id, old, new, mode):
        async with self.lock:
            if self.values.get(session_id) != (old, "AGENT"):
                raise BrowserError("STALE_LEASE")
            if int(new.lease_epoch) <= int(old.lease_epoch):
                raise BrowserError("STALE_LEASE")
            self.values[session_id] = (new, mode)

    async def revoke(self, session_id, authority):
        async with self.lock:
            if self.values.get(session_id) != (authority, "AGENT"):
                raise BrowserError("STALE_LEASE")
            del self.values[session_id]


class MockGate:
    def __init__(self):
        self.permits = []
        self.reject = False

    async def permit(self, authority, url):
        self.permits.append(url)
        if self.reject:
            raise BrowserError("GATEWAY_DENIED")


class MockFixtures:
    def __init__(self):
        self.data = {
            "fixture://alpha.test/": FixtureResponse(200, "<h1>alpha</h1><input type='password' value='PRIVATE123'>"),
            "fixture://alpha.test/next": FixtureResponse(200, "<h1>next</h1>"),
            "fixture://beta.test/": FixtureResponse(200, "<h1>beta</h1>"),
        }

    async def fetch(self, url):
        return self.data.get(url, FixtureResponse(404))


class MockRedactor:
    def redact(self, text):
        return re.sub(r"TOKEN:[^\s]+", "[REDACTED]", text)


class MockWorker:
    def __init__(self):
        self.pages = {}
        self.start_count = 0
        self.running = None
        self.enter = None
        self.release = None
        self.crashed = False

    async def start(self, session_id):
        self.start_count += 1
        self.pages[session_id] = ""
        self.crashed = False

    async def render(self, session_id, html):
        if self.crashed:
            raise BrowserError("BROWSER_CRASHED")
        if self.enter is not None:
            self.running = session_id
            self.enter.set()
            await self.release.wait()
        self.pages[session_id] = html

    async def observe(self, session_id):
        if self.crashed:
            raise BrowserError("BROWSER_CRASHED")
        # Simplified browser visible text: input attributes must not become visible text.
        html = self.pages[session_id]
        return " ".join(re.findall(r">([^<>]+)<", html))

    async def close(self, session_id):
        self.pages.pop(session_id, None)


@pytest.fixture
def ctx():
    auth, fence, gate, fixtures, redactor, worker = (
        MockAuth(),
        MockFence(),
        MockGate(),
        MockFixtures(),
        MockRedactor(),
        MockWorker(),
    )
    ids = iter(
        [
            "019a55c4-00f0-7000-8000-000000000001",
            "019a55c4-00f0-7000-8000-000000000002",
            "019a55c4-00f0-7000-8000-000000000003",
        ]
    )
    service = BrowserSessionService(
        worker=worker,
        authority=auth,
        fence=fence,
        fixture_gate=gate,
        fixture=fixtures,
        redactor=redactor,
        new_id=lambda: next(ids),
        allowed_fixture_hosts=frozenset({"alpha.test", "beta.test"}),
    )
    p = Authority("t1", "o1", "task1", "run1", "1", "1")
    return service, p, auth, fence, gate, fixtures, worker


@pytest.mark.asyncio
async def test_create_navigate_observe_and_private_password(ctx):
    service, p, _, _, gate, _, worker = ctx
    session = await service.create(p)
    assert session.state is State.ACTIVE
    assert session.session_id not in worker.pages[session.session_id]
    obs = await service.navigate_fixture(session.session_id, p, "fixture://alpha.test/")
    assert obs.text == "alpha"
    assert "PRIVATE123" not in repr(obs)
    assert gate.permits == ["fixture://alpha.test/"]
    assert await service.observe(session.session_id, p) == obs


@pytest.mark.asyncio
async def test_cross_scope_task_tenant_owner_are_invisible(ctx):
    service, p, *_ = ctx
    sid = (await service.create(p)).session_id
    for name, value in (("tenant_id", "other"), ("owner_user_id", "other"), ("task_id", "other")):
        with pytest.raises(BrowserError, match="SESSION_NOT_FOUND"):
            await service.observe(sid, replace(p, **{name: value}))
    # Another Task owns another worker context, not the original Page.
    another = replace(p, task_id="task2")
    second = await service.create(another)
    assert sid != second.session_id
    await service.navigate_fixture(sid, p, "fixture://alpha.test/")
    assert (await service.observe(second.session_id, another)).text == ""


@pytest.mark.asyncio
async def test_stale_epoch_goal_and_concurrent_claim(ctx):
    service, p, _, fence, *_ = ctx
    sid = (await service.create(p)).session_id
    n = replace(p, lease_epoch="2")
    attempts = await asyncio.gather(
        fence.advance(sid, p, n, "AGENT"),
        fence.advance(sid, p, n, "AGENT"),
        return_exceptions=True,
    )
    assert len([x for x in attempts if x is None]) == 1
    assert len([x for x in attempts if isinstance(x, BrowserError)]) == 1
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        await service.observe(sid, p)
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        await service.observe(sid, replace(n, goal_revision="2"))
    assert (await service.observe(sid, n)).text == ""


@pytest.mark.asyncio
async def test_concurrent_page_operation_is_serial(ctx):
    service, p, _, _, _, _, worker = ctx
    sid = (await service.create(p)).session_id
    worker.enter, worker.release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(service.navigate_fixture(sid, p, "fixture://alpha.test/"))
    await worker.enter.wait()
    second = asyncio.create_task(service.observe(sid, p))
    await asyncio.sleep(0)
    assert not second.done()
    worker.release.set()
    assert (await first).text == "alpha"
    assert (await second).text == "alpha"


@pytest.mark.asyncio
async def test_cancel_fences_inflight_result(ctx):
    service, p, _, fence, _, _, worker = ctx
    sid = (await service.create(p)).session_id
    worker.enter, worker.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(service.navigate_fixture(sid, p, "fixture://alpha.test/"))
    await worker.enter.wait()
    cancel = asyncio.create_task(service.cancel(sid, p))
    await asyncio.sleep(0)
    assert sid not in fence.values  # revoked even while Page operation is pending
    worker.release.set()
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        await pending
    await cancel
    assert worker.pages == {}
    with pytest.raises(BrowserError):
        await service.observe(sid, p)


@pytest.mark.asyncio
async def test_takeover_rejects_old_agent(ctx):
    service, p, _, _, *_ = ctx
    sid = (await service.create(p)).session_id
    human = replace(p, lease_epoch="2")
    view = await service.takeover(sid, p, human)
    assert view.state is State.PAUSED
    for old in (p, human):
        with pytest.raises(BrowserError):
            await service.navigate_fixture(sid, old, "fixture://alpha.test/")


@pytest.mark.asyncio
async def test_fixture_redirect_hops_and_ssrf_fail_closed(ctx):
    service, p, _, _, gate, fixtures, worker = ctx
    sid = (await service.create(p)).session_id
    fixtures.data["fixture://alpha.test/redirect"] = FixtureResponse(302, redirect_to="fixture://beta.test/")
    assert (await service.navigate_fixture(sid, p, "fixture://alpha.test/redirect")).text == "beta"
    assert gate.permits[-2:] == ["fixture://alpha.test/redirect", "fixture://beta.test/"]
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/",
        "fixture://127.0.0.1/",
        "fixture://alpha.test:80/",
        "fixture://other.test/",
        "fixture://alpha.test@169.254.169.254/",
        "fixture://alpha.test/\\evil",
    ):
        with pytest.raises(BrowserError):
            await service.navigate_fixture(sid, p, url)
    fixtures.data["fixture://alpha.test/redirect"] = FixtureResponse(302, redirect_to="http://127.0.0.1/")
    with pytest.raises(BrowserError, match="EGRESS_DENIED"):
        await service.navigate_fixture(sid, p, "fixture://alpha.test/redirect")
    assert worker.pages[sid] == "<h1>beta</h1>"  # rejected redirect did not render


@pytest.mark.asyncio
async def test_gateway_denial_and_side_effects(ctx):
    service, p, _, _, gate, _, worker = ctx
    sid = (await service.create(p)).session_id
    gate.reject = True
    with pytest.raises(BrowserError, match="GATEWAY_DENIED"):
        await service.navigate_fixture(sid, p, "fixture://alpha.test/")
    assert worker.pages[sid] == ""
    for method in (service.click, service.submit, service.authenticated_navigate):
        with pytest.raises(BrowserError, match="NOT_INTEGRATED"):
            await method(sid, p, "fixture://alpha.test/")


@pytest.mark.asyncio
async def test_crash_recovery_requires_epoch_and_explicit_resume(ctx):
    service, p, _, _, _, _, worker = ctx
    sid = (await service.create(p)).session_id
    worker.crashed = True
    with pytest.raises(BrowserError, match="BROWSER_CRASHED"):
        await service.observe(sid, p)
    assert (await service.view(sid, p)).state is State.RECOVERING
    n = replace(p, lease_epoch="2")
    view = await service.recover(sid, p, n)
    assert view.state is State.NEEDS_LOGIN and view.generation == 2
    with pytest.raises(BrowserError):
        await service.navigate_fixture(sid, n, "fixture://alpha.test/")
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        await service.observe(sid, p)
    await service.resume_offline_fixture(sid, n)
    assert (await service.navigate_fixture(sid, n, "fixture://alpha.test/")).generation == 2


@pytest.mark.asyncio
async def test_quota_and_rejected_identity(ctx):
    service, p, _, _, _, _, worker = ctx
    await service.create(p)
    await service.create(p)
    with pytest.raises(BrowserError, match="SESSION_QUOTA"):
        await service.create(p)
    assert worker.start_count == 2
    with pytest.raises(ValueError):
        Authority("t", "o", "task", "run", "01", "1")
    with pytest.raises(ValueError):
        Authority("t", "o", "task", "run", "1", str(2**63))


@pytest.mark.asyncio
async def test_redacted_observation_never_returns_secret(ctx):
    service, p, _, _, _, fixtures, _ = ctx
    sid = (await service.create(p)).session_id
    fixtures.data["fixture://alpha.test/secret"] = FixtureResponse(200, "<p>TOKEN:supersecret</p>")
    obs = await service.navigate_fixture(sid, p, "fixture://alpha.test/secret")
    assert obs.text == "[REDACTED]"
    assert "supersecret" not in repr(obs)


@pytest.mark.asyncio
async def test_authz_failure_no_worker_dispatch(ctx):
    service, p, auth, _, _, _, worker = ctx
    auth.denied.add("create")
    with pytest.raises(BrowserError, match="AUTH_DENIED"):
        await service.create(p)
    assert worker.start_count == 0


@pytest.mark.asyncio
async def test_takeover_fences_inflight_result(ctx):
    service, p, _, fence, _, _, worker = ctx
    sid = (await service.create(p)).session_id
    worker.enter, worker.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(service.navigate_fixture(sid, p, "fixture://alpha.test/"))
    await worker.enter.wait()
    newer = replace(p, lease_epoch="2")
    takeover = asyncio.create_task(service.takeover(sid, p, newer))
    await asyncio.sleep(0)
    assert fence.values[sid] == (newer, "HUMAN")
    worker.release.set()
    with pytest.raises(BrowserError, match="STALE_LEASE"):
        await pending
    assert (await takeover).state is State.PAUSED


@pytest.mark.asyncio
async def test_duplicate_simultaneous_creation_atomic_quota(ctx):
    service, p, *_ = ctx
    result = await asyncio.gather(*(service.create(p) for _ in range(3)), return_exceptions=True)
    assert len([r for r in result if not isinstance(r, BaseException)]) == 2
    assert len([r for r in result if isinstance(r, BrowserError) and r.code == "SESSION_QUOTA"]) == 1


@pytest.mark.asyncio
async def test_secret_deny_policy_can_reject_observation(ctx):
    service, p, auth, *_ = ctx
    sid = (await service.create(p)).session_id
    auth.denied.add("observe")
    with pytest.raises(BrowserError, match="AUTH_DENIED"):
        await service.observe(sid, p)
