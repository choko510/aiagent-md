"""Browser-only provisional interfaces. Authoritative Run/Gateway/DB contracts live elsewhere."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol
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
    worker_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "owner_user_id", "task_id", "run_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"invalid {name}")
        for name in ("goal_revision", "lease_epoch"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _DECIMAL.fullmatch(value) or int(value) >= 2**63:
                raise ValueError(f"invalid {name}")
        if self.worker_ref is not None and (
            not isinstance(self.worker_ref, str) or not self.worker_ref.strip() or len(self.worker_ref) > 128
        ):
            raise ValueError("invalid worker_ref")


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


LocatorKind = Literal["role", "label", "test_id", "css"]


@dataclass(frozen=True, slots=True)
class BrowserTarget:
    """Strictly typed DOM/AX locator; never a raw JS expression or CDP handle."""

    kind: LocatorKind
    selector: str
    role_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("role", "label", "test_id", "css"):
            raise BrowserError("INVALID_TARGET")
        if (
            not isinstance(self.selector, str)
            or not self.selector.strip()
            or len(self.selector) > 512
            or any(c in self.selector for c in ("\r", "\n", "\0"))
        ):
            raise BrowserError("INVALID_TARGET")
        if self.role_name is not None and (
            not isinstance(self.role_name, str)
            or len(self.role_name) > 256
            or any(c in self.role_name for c in ("\r", "\n", "\0"))
        ):
            raise BrowserError("INVALID_TARGET")


@dataclass(frozen=True, slots=True)
class FormInput:
    """Input field value bound to an explicit target; repr hides raw field values."""

    target: BrowserTarget
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, BrowserTarget):
            raise BrowserError("INVALID_TARGET")
        if not isinstance(self.value, str) or len(self.value) > 4096:
            raise BrowserError("INVALID_TARGET")


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    session_id: str
    generation: int
    action: Literal["click", "submit"]
    status: Literal["EXECUTED", "NEEDS_LOGIN", "CAPTCHA_CHALLENGE"]
    before_observation: Observation
    after_observation: Observation


class ApprovalPort(Protocol):
    """Explicit owner approval gate for side-effectful browser actions (submit/post/purchase)."""

    async def approve(self, authority: Authority, session_id: str, target: BrowserTarget) -> None: ...


class AuthTransitionPort(Protocol):
    """Verified credential/login transition broker (never exposes secrets to observations)."""

    async def permit_transition(self, authority: Authority, session_id: str, url: str) -> None: ...


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

    async def render(self, session_id: str, html: str) -> str | None: ...

    async def observe(self, session_id: str) -> str: ...

    async def click(self, session_id: str, target: BrowserTarget, *, timeout_ms: int = 5000) -> str: ...

    async def submit(
        self,
        session_id: str,
        target: BrowserTarget,
        inputs: tuple[FormInput, ...] = (),
        *,
        timeout_ms: int = 5000,
    ) -> str: ...

    async def close(self, session_id: str) -> None: ...


def validate_fixture_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Offline `fixture://` URLs ONLY; never a route to real hosts/private IPs."""
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or "\\" in url
        or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in url)
    ):
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


def validate_browser_url(
    url: str,
    allowed_fixture_hosts: frozenset[str],
    *,
    allowed_web_hosts: frozenset[str] = frozenset(),
    allow_https: bool = False,
    allow_http: bool = False,
) -> str:
    """Validate either an offline `fixture://` URL or an explicitly permitted `https://`/`http://` URL."""
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or "\\" in url
        or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in url)
    ):
        raise BrowserError("INVALID_FIXTURE_URL")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BrowserError("INVALID_FIXTURE_URL") from exc
    scheme = parts.scheme.lower()
    if scheme == "fixture":
        return validate_fixture_url(url, allowed_fixture_hosts)
    if scheme == "https":
        if not allow_https:
            raise BrowserError("EGRESS_DENIED")
    elif scheme == "http":
        if not allow_http:
            raise BrowserError("EGRESS_DENIED")
    else:
        raise BrowserError("EGRESS_DENIED")
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None or not host.isascii():
        raise BrowserError("EGRESS_DENIED")
    if allowed_web_hosts and "*" not in allowed_web_hosts and host not in allowed_web_hosts:
        raise BrowserError("EGRESS_DENIED")
    if not allowed_web_hosts:
        raise BrowserError("EGRESS_DENIED")
    return url


IdFactory = Callable[[], str]
