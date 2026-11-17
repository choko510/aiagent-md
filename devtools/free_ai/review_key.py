"""Independent answer key for the STEP 3 synthetic code-review tasks.

This module is deliberately separate from ``fixtures.py``. Request payloads are built
only from fixture constants, so this key can never be serialized into a prompt. It is
imported solely by deterministic scoring, score-log validation and the offline
``review-report`` aggregation.

The key uses the line numbers of the numbered code shown in each fixture prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

ReviewKind = Literal[
    "INDEX_OUT_OF_RANGE",
    "SQL_INJECTION",
    "SECRET_LOGGING",
    "BOUNDARY_BUG",
    "RACE_CONDITION",
    "MISSING_VALIDATION",
]
ReviewSeverity = Literal["low", "medium", "high"]

#: The strict parser never accepts more findings than this per task.
MAX_REVIEW_FINDINGS: Final = 20
#: Upper bound for a reported line number; also keeps the parser independent of a fixture.
MAX_REVIEW_LINE: Final = 200

REVIEW_KINDS: Final = frozenset(get_args(ReviewKind))
REVIEW_SEVERITIES: Final = frozenset(get_args(ReviewSeverity))


@dataclass(frozen=True, slots=True)
class Defect:
    """One ground-truth defect at a numbered line in the synthetic code."""

    line: int
    kind: ReviewKind
    severity: ReviewSeverity


#: Ground truth per review fixture. Empty tuples are deliberately clean code.
REVIEW_KEY: Mapping[str, tuple[Defect, ...]] = MappingProxyType(
    {
        "review_01": (Defect(line=2, kind="INDEX_OUT_OF_RANGE", severity="high"),),
        "review_02": (
            Defect(line=2, kind="SQL_INJECTION", severity="high"),
            Defect(line=3, kind="SECRET_LOGGING", severity="high"),
        ),
        "review_03": (),
        "review_04": (Defect(line=2, kind="BOUNDARY_BUG", severity="medium"),),
        "review_05": (),
        "review_06": (
            Defect(line=2, kind="RACE_CONDITION", severity="high"),
            Defect(line=6, kind="MISSING_VALIDATION", severity="low"),
        ),
        "review_07": (
            Defect(line=2, kind="MISSING_VALIDATION", severity="medium"),
            Defect(line=4, kind="SQL_INJECTION", severity="high"),
            Defect(line=7, kind="SECRET_LOGGING", severity="high"),
        ),
        "review_08": (),
    }
)

REVIEW_FIXTURE_IDS: Final = tuple(REVIEW_KEY)


def truth_count(fixture_id: str) -> int:
    """Return the number of ground-truth defects, rejecting unknown tasks."""
    return len(REVIEW_KEY[fixture_id])
