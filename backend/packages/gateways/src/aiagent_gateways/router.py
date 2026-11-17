"""Model Router, capability/consent evaluation, circuit breaker, and cost estimation for ModelGateway."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Route:
    provider: str
    provider_model: str
    weight: int = 1


@dataclass(frozen=True)
class RouteRates:
    """Optional micro-USD per 1M tokens for metered usage reporting (never fabricated when unconfigured)."""

    input_micro_usd_per_1m: int
    output_micro_usd_per_1m: int

    def estimate_micro_usd(self, input_tokens: int | None, output_tokens: int | None) -> int | None:
        if input_tokens is None or output_tokens is None:
            return None
        return (input_tokens * self.input_micro_usd_per_1m + output_tokens * self.output_micro_usd_per_1m) // 1_000_000


class RouteBreaker:
    """In-memory per-(provider, model) circuit breaker to skip flapping routes before fallback exhaustion."""

    def __init__(self, *, threshold: int = 3, cooldown_s: float = 30.0) -> None:
        self.threshold = max(1, threshold)
        self.cooldown_s = max(0.1, cooldown_s)
        self._failures: dict[tuple[str, str], int] = {}
        self._open_until: dict[tuple[str, str], float] = {}

    @staticmethod
    def _key(route: Route) -> tuple[str, str]:
        return (route.provider, route.provider_model)

    def is_open(self, route: Route) -> bool:
        key = self._key(route)
        until = self._open_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            # Cooldown expired: allow half-open probe.
            self._open_until.pop(key, None)
            self._failures[key] = max(0, self.threshold - 1)
            return False
        return True

    def record_success(self, route: Route) -> None:
        key = self._key(route)
        self._failures.pop(key, None)
        self._open_until.pop(key, None)

    def record_failure(self, route: Route) -> None:
        key = self._key(route)
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= self.threshold:
            self._open_until[key] = time.monotonic() + self.cooldown_s


def select_ordered_routes(usable_routes: list[Route], breaker: RouteBreaker) -> list[Route]:
    """Order routes with closed circuit breakers before open ones, weighted descending within each tier."""
    closed = sorted(
        (r for r in usable_routes if not breaker.is_open(r)),
        key=lambda r: -max(1, r.weight),
    )
    if closed:
        return closed
    open_routes = sorted(usable_routes, key=lambda r: -max(1, r.weight))
    return open_routes[:1]


def route_rejection_reason(
    route: Route,
    providers: dict[str, Any],
    consent: set[tuple[str, str]],
    *,
    tenant_id: str,
    requires_tools: bool,
    context_chars: int,
) -> str | None:
    """Verify provider presence, tool capability, context size, and recipient consent for a candidate route."""
    provider = providers.get(route.provider)
    if provider is None:
        return "MODEL_POLICY"
    cap = provider.capability(route.provider_model)
    if requires_tools and not cap.tools:
        return "CAPABILITY"
    if context_chars > cap.max_context_chars:
        return "CONTEXT_TOO_LARGE"
    if cap.external_recipient and (tenant_id, cap.provider) not in consent and ("*", cap.provider) not in consent:
        return "RECIPIENT_CONSENT"
    return None
