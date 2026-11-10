"""STEP 4 browser: fixture-only, fail-closed, no agent-facing Playwright handles."""

from .contracts import Authority, BrowserError, FixtureResponse, Observation, SessionView, State
from .service import BrowserSessionService
from .worker import PlaywrightBrowserWorker

__all__ = [
    "Authority",
    "BrowserError",
    "BrowserSessionService",
    "FixtureResponse",
    "Observation",
    "PlaywrightBrowserWorker",
    "SessionView",
    "State",
]
