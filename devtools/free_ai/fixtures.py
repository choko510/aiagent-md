"""Fixed synthetic fixtures for the dev lab.

Every payload sent to a provider is assembled here from source constants. There is no
function that accepts free text, a file path, stdin, a repository or a user-supplied
prompt, so arbitrary data (source code, customer data, secrets) cannot reach a provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .catalog import ProviderSpec, WireKind

FixtureId = Literal["jp_hello", "arithmetic_2plus2", "refund_noul"]

#: Synthetic chat prompt 1. The expected answer is a single Japanese word.
JP_HELLO_PROMPT = "日本語で『こんにちは』と一語だけ返してください。"
JP_HELLO_EXPECTED = "こんにちは"

#: Synthetic chat prompt 2. Deterministic arithmetic with an unambiguous answer.
ARITHMETIC_PROMPT = "Compute 2+2. Reply with only the digit 4."
ARITHMETIC_EXPECTED = "4"

#: Synthetic System One state and question (Jev wire format only).
REFUND_STATE = "Synthetic sample: The user wrote 'I request a refund.'"
REFUND_INSTRUCTIONS = "Does this text explicitly ask for a refund?"
REFUND_PASS_THRESHOLD = 0.5


class FixtureError(ValueError):
    """The requested fixture does not exist or does not match the provider's wire kind."""


@dataclass(frozen=True, slots=True)
class Fixture:
    """One immutable synthetic fixture definition."""

    fixture_id: FixtureId
    wire_kind: WireKind
    prompt: str | None
    expected_text: str | None
    noul_threshold: float | None
    description: str


_FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        fixture_id="jp_hello",
        wire_kind="chat_completions",
        prompt=JP_HELLO_PROMPT,
        expected_text=JP_HELLO_EXPECTED,
        noul_threshold=None,
        description="one-word Japanese greeting; exact match required",
    ),
    Fixture(
        fixture_id="arithmetic_2plus2",
        wire_kind="chat_completions",
        prompt=ARITHMETIC_PROMPT,
        expected_text=ARITHMETIC_EXPECTED,
        noul_threshold=None,
        description="2+2; exact single digit required",
    ),
    Fixture(
        fixture_id="refund_noul",
        wire_kind="system_one",
        prompt=None,
        expected_text=None,
        noul_threshold=REFUND_PASS_THRESHOLD,
        description="System One noul probability on a synthetic refund sample",
    ),
)

FIXTURES: Mapping[str, Fixture] = MappingProxyType({fixture.fixture_id: fixture for fixture in _FIXTURES})

ALL_FIXTURE_IDS: tuple[FixtureId, ...] = ("jp_hello", "arithmetic_2plus2", "refund_noul")


def get_fixture(fixture_id: str) -> Fixture | None:
    """Return the fixture for ``fixture_id`` or ``None`` when unknown."""
    return FIXTURES.get(fixture_id)


def require_fixture(fixture_id: str) -> Fixture:
    """Return the fixture or raise :class:`FixtureError`."""
    fixture = get_fixture(fixture_id)
    if fixture is None:
        raise FixtureError("unknown fixture id")
    return fixture


def allowed_fixtures(spec: ProviderSpec) -> tuple[FixtureId, ...]:
    """Return the fixtures that may be sent to ``spec`` (wire-kind restricted)."""
    if spec.wire_kind == "system_one":
        return ("refund_noul",)
    return ("jp_hello", "arithmetic_2plus2")


def build_payload(spec: ProviderSpec, fixture_id: str) -> dict[str, object]:
    """Assemble the fixed request body for ``spec`` and ``fixture_id``.

    The body is fully determined by source constants and the catalog; it has no
    parameter that could carry free text.
    """
    fixture = require_fixture(fixture_id)
    if fixture.fixture_id not in allowed_fixtures(spec):
        raise FixtureError("fixture is not allowed for this provider")
    if spec.wire_kind == "chat_completions":
        if fixture.prompt is None:
            raise FixtureError("chat fixture has no prompt")
        return {
            "model": spec.model_id,
            "messages": [{"role": "user", "content": fixture.prompt}],
            "stream": False,
        }
    if spec.wire_kind == "system_one":
        return {
            "model": spec.model_id,
            "state": REFUND_STATE,
            "questions": {"refund": {"type": "noul", "instructions": REFUND_INSTRUCTIONS}},
        }
    raise FixtureError("unsupported wire kind")


__all__ = [
    "ALL_FIXTURE_IDS",
    "ARITHMETIC_EXPECTED",
    "ARITHMETIC_PROMPT",
    "FIXTURES",
    "JP_HELLO_EXPECTED",
    "JP_HELLO_PROMPT",
    "REFUND_INSTRUCTIONS",
    "REFUND_PASS_THRESHOLD",
    "REFUND_STATE",
    "Fixture",
    "FixtureError",
    "FixtureId",
    "allowed_fixtures",
    "build_payload",
    "get_fixture",
    "require_fixture",
]
