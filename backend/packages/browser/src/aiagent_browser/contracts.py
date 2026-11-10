"""Browser-only provisional interfaces. Authoritative Run/Gateway/DB contracts live elsewhere."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit


class BrowserError(Exception):
    """Error code only: never embed URLs, HTML, tokens or Playwright errors."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class State(StrEnum):
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    CLOSED = "CLOSED"


_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True, slots=True)
class Authority:
    """Server-attested scope; never assemble from model, website or browser content."""

    tenant_id: str
    owner_user_id: str
    task_id: str
    run_id: str
    goal_revision: str
    lease_epoch: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "owner_user_id", "task_id", "run_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"invalid {name}")
        for name in ("goal_revision", "lease_epoch"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _DECIMAL.fullmatch(value) or int(value) >= 2**63:
                raise ValueError(f"invalid {name}")


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    state: State
    generation: int
    goal_revision: str
    lease_epoch: str


@dataclass(frozen=True, slots=True)
class Observation:
    session_id: str
    generation: int
    text: str
    text_truncated: bool


@dataclass(frozen=True, slots=True)
class FixtureResponse:
    status: int
    html: str = ""
    redirect_to: str | None = None


class AuthorityPort(Protocol):
    async def authorize(self, authority: Authority, operation: str, session_id: str | None) -> None: ...


class FencePort(Protocol):
    """Required authoritative CAS/lease owner. In-memory substitutes are tests ONLY."""

    async def register(self, session_id: str, authority: Authority) -> None: ...

    async def check(self, session_id: str, authority: Authority, mode: str = "AGENT") -> None: ...

    async def advance(self, session_id: str, old: Authority, new: Authority, mode: str) -> None: ...

    async def revoke(self, session_id: str, authority: Authority) -> None: ...


class FixtureGatePort(Protocol):
    """Both Gateway intent and Sentinel allow are required for EACH fixture hop."""

    async def permit(self, authority: Authority, url: str) -> None: ...


class FixturePort(Protocol):
    """No network access is granted to implementations by this interface."""

    async def fetch(self, url: str) -> FixtureResponse: ...


class RedactionPort(Protocol):
    """No observation may escape without a policy-owned redactor."""

    def redact(self, text: str) -> str: ...


class WorkerPort(Protocol):
    async def start(self, session_id: str) -> None: ...

    async def render(self, session_id: str, html: str) -> None: ...

    async def observe(self, session_id: str) -> str: ...

    async def close(self, session_id: str) -> None: ...


def validate_fixture_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Offline `fixture://` URLs ONLY; never a route to real hosts/private IPs."""
    if not isinstance(url, str) or len(url) > 2048 or "\\" in url or any(ord(c) < 32 for c in url):
        raise BrowserError("INVALID_FIXTURE_URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise BrowserError("INVALID_FIXTURE_URL") from exc
    if (
        parts.scheme != "fixture"
        or host is None
        or host not in allowed_hosts
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.fragment
        or not parts.netloc == host
    ):
        raise BrowserError("EGRESS_DENIED")
    return url


IdFactory = Callable[[], str]
