"""Independent gold key for the STEP 3 synthetic code-review tasks.

This module is deliberately separate from ``fixtures.py``. Request payloads are built
only from fixture constants, so this key can never be serialized into a prompt. It is
imported solely by deterministic scoring, score-log validation and the offline
``review-report`` aggregation.

Line numbers refer to the numbered code shown in each fixture prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

#: The six rule identifiers from the STEP 3 contract. No other value is accepted.
RuleId = Literal[
    "INDEX_OUT_OF_RANGE",
    "SECRET_LOGGING",
    "SQL_INJECTION",
    "BOUNDARY_BUG",
    "STALE_LEASE",
    "UNSAFE_RETRY",
]
Severity = Literal["P1", "P2", "P3"]

#: Exactly the eight findings the answer must be able to report, never more.
MAX_REVIEW_FINDINGS: Final = 8

RULE_IDS: Final = frozenset(get_args(RuleId))
SEVERITIES: Final = frozenset(get_args(Severity))


@dataclass(frozen=True, slots=True)
class Defect:
    """One gold defect at a numbered line in the synthetic code."""

    line: int
    rule_id: RuleId
    severity: Severity


#: Gold per review fixture. Empty tuples are deliberately clean code.
REVIEW_KEY: Mapping[str, tuple[Defect, ...]] = MappingProxyType(
    {
        "review_r01": (
            Defect(line=2, rule_id="INDEX_OUT_OF_RANGE", severity="P2"),
            Defect(line=5, rule_id="SECRET_LOGGING", severity="P1"),
        ),
        "review_r02": (
            Defect(line=2, rule_id="SQL_INJECTION", severity="P1"),
            Defect(line=6, rule_id="BOUNDARY_BUG", severity="P2"),
        ),
        "review_r03": (
            Defect(line=2, rule_id="STALE_LEASE", severity="P1"),
            Defect(line=8, rule_id="UNSAFE_RETRY", severity="P1"),
        ),
        "review_r04": (),
        "review_r05": (),
        "review_r06": (Defect(line=8, rule_id="SECRET_LOGGING", severity="P1"),),
        "review_r07": (Defect(line=5, rule_id="UNSAFE_RETRY", severity="P1"),),
        "review_r08": (),
    }
)

#: Number of numbered code lines per fixture; a reported line must exist in that range.
REVIEW_LINE_COUNTS: Mapping[str, int] = MappingProxyType(
    {
        "review_r01": 5,
        "review_r02": 6,
        "review_r03": 8,
        "review_r04": 7,
        "review_r05": 8,
        "review_r06": 8,
        "review_r07": 5,
        "review_r08": 11,
    }
)

REVIEW_FIXTURE_IDS: Final = tuple(REVIEW_KEY)


def truth_count(fixture_id: str) -> int:
    """Return the number of gold defects, rejecting unknown tasks."""
    return len(REVIEW_KEY[fixture_id])
