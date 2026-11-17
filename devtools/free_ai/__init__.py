"""Development-only Free AI Lab: fixed synthetic probes against unauthenticated services.

This package is *not* a product feature. It never registers a provider, a route or a
fallback in the product Model Gateway. It sends three fixed synthetic fixtures to at
most four fixed HTTPS endpoints, one request per invocation, and stores only a small
metadata record. See ``README.md`` in this directory for the exact contract.

This module only defines the shared closed value sets used by the submodules; it
performs no I/O and imports no sibling module, so it cannot create an import cycle.
"""

from __future__ import annotations

from typing import Literal

#: Closed set of terminal statuses a probe can report. Never invent new values.
ProbeStatus = Literal[
    "PASS",
    "CONTENT_MISMATCH",
    "FORMAT_MISMATCH",
    "MODEL_DRIFT",
    "UNEXPECTED_BILLING",
    "UNEXPECTED_TOOL_OUTPUT",
    "ACCESS_RESTRICTED",
    "RATE_LIMITED",
    "REMOTE_ERROR",
    "NETWORK_ERROR",
    "TIMEOUT",
    "UNVERIFIED_TERMS",
    "LOCAL_INPUT_INVALID",
    "SAFETY_BLOCKED",
]

#: Closed set of machine-readable reason identifiers. Provider-supplied strings are
#: never used here.
ReasonCode = Literal[
    "OK",
    "COST_UNKNOWN",
    "UNKNOWN_OUTCOME",
    "AUTH_REQUIRED",
    "LEGACY_DISABLED",
    "RATE_LIMIT",
    "REMOTE_FAILURE",
    "INVALID_JSON",
    "INVALID_SCHEMA",
    "UNEXPECTED_HOST",
    "UNEXPECTED_REDIRECT",
    "BODY_TOO_LARGE",
    "MODEL_CHANGED",
    "OUTPUT_MISMATCH",
    "PROHIBITED_TOOL",
]

__all__ = ["ProbeStatus", "ReasonCode"]
