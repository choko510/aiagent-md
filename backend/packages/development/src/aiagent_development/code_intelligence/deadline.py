"""Finite monotonic deadlines for bounded Code Intelligence operations."""

from __future__ import annotations

import math
import time

from .models import ReadError


def require_deadline(deadline: float) -> None:
    now = time.monotonic()
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ReadError("invalid operation deadline")
    if not now < deadline <= now + 3600:
        raise ReadError("operation deadline exceeded or too distant")
