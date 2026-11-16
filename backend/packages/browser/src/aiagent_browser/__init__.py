"""STEP 4 browser: fixture-only, fail-closed, no agent-facing Playwright handles."""

from .contracts import (
    ActionOutcome,
    Authority,
    BrowserError,
    BrowserTarget,
    FixtureResponse,
    FormInput,
    Observation,
    SessionView,
    State,
)
from .service import BrowserSessionService
from .worker import PlaywrightBrowserWorker

__all__ = [
    "ActionOutcome",
    "Authority",
    "BrowserError",
    "BrowserSessionService",
    "BrowserTarget",
    "FixtureResponse",
    "FormInput",
    "Observation",
    "PlaywrightBrowserWorker",
    "SessionView",
    "State",
]
