from __future__ import annotations

import time

import pytest

from aiagent_development.code_intelligence.deadline import require_deadline
from aiagent_development.code_intelligence.models import ReadError


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True, -1.0])
def test_deadline_rejects_nonfinite_or_expired_values(invalid: object) -> None:
    with pytest.raises(ReadError, match="deadline"):
        require_deadline(invalid)  # type: ignore[arg-type]


def test_deadline_rejects_unbounded_future() -> None:
    with pytest.raises(ReadError, match="deadline"):
        require_deadline(time.monotonic() + 7200)
