"""Fixed synthetic fixtures for the dev lab.

Every payload sent to a provider is assembled here from source constants. There is no
function that accepts free text, a file path, stdin, a repository or a user-supplied
prompt, so arbitrary data (source code, customer data, secrets) cannot reach a provider.

The ``dev_*`` fixtures are synthetic, self-contained development tasks that ask a model
for one identifier from a fixed list. The ``review_*`` fixtures ask for a strict JSON
defect list about similarly fixed synthetic code. They are evaluation aids, not product
features. The ground-truth answer key lives in ``review_key.py`` and is never imported
here, so it cannot be serialized into a prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .catalog import ProviderSpec, WireKind

FixtureId = Literal[
    "jp_hello",
    "arithmetic_2plus2",
    "refund_noul",
    "dev_t01",
    "dev_t02",
    "dev_t03",
    "dev_t04",
    "dev_t05",
    "dev_t06",
    "review_r01",
    "review_r02",
    "review_r03",
    "review_r04",
    "review_r05",
    "review_r06",
    "review_r07",
    "review_r08",
]

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

# --- DEV-T01..T06: synthetic classification tasks (chat providers only) -----------------
#
# Every task is a fixed constant. The model must reply with exactly one identifier from
# the allowed list; no free-form explanation is scored, and the identifiers are never
# extracted from prose.

DEV_T01_CODE = "def last_item(items):\n    return items[len(items)]"
DEV_T01_PROMPT = (
    "The following synthetic Python function contains exactly one defect:\n\n"
    f"{DEV_T01_CODE}\n\n"
    "Which defect class applies? Reply with exactly one identifier from the allowed list "
    "and nothing else.\n"
    "Allowed answers: INDEX_OUT_OF_RANGE, TYPE_ERROR, NO_BUG"
)
DEV_T01_ALLOWED: tuple[str, ...] = ("INDEX_OUT_OF_RANGE", "TYPE_ERROR", "NO_BUG")
DEV_T01_EXPECTED = "INDEX_OUT_OF_RANGE"

DEV_T02_CODE = 'query = "SELECT * FROM users WHERE name = \'" + name + "\'"'
DEV_T02_PROMPT = (
    "The following synthetic Python snippet builds a database query:\n\n"
    f"{DEV_T02_CODE}\n\n"
    "Which defect class applies? Reply with exactly one identifier from the allowed list "
    "and nothing else.\n"
    "Allowed answers: SQL_INJECTION, MEMORY_LEAK, NO_BUG"
)
DEV_T02_ALLOWED: tuple[str, ...] = ("SQL_INJECTION", "MEMORY_LEAK", "NO_BUG")
DEV_T02_EXPECTED = "SQL_INJECTION"

DEV_T03_PROMPT = (
    "A synthetic worker sent a side-effecting POST to an external API. The connection "
    "dropped before any response was received, so it is unknown whether the server "
    "processed the request.\n\n"
    "What is the safe next action? Reply with exactly one identifier from the allowed "
    "list and nothing else.\n"
    "Allowed answers: RETRY_IMMEDIATELY, DO_NOT_RETRY_AUTOMATICALLY"
)
DEV_T03_ALLOWED: tuple[str, ...] = ("RETRY_IMMEDIATELY", "DO_NOT_RETRY_AUTOMATICALLY")
DEV_T03_EXPECTED = "DO_NOT_RETRY_AUTOMATICALLY"

DEV_T04_CODE = 'headers = {"Authorization": "Bearer synthetic-example"}\nprint(headers)'
DEV_T04_PROMPT = (
    "The following synthetic Python snippet runs in a service that writes stdout to a "
    "shared log:\n\n"
    f"{DEV_T04_CODE}\n\n"
    "Which defect class applies? Reply with exactly one identifier from the allowed list "
    "and nothing else.\n"
    "Allowed answers: SECRET_LOGGING, SQL_INJECTION, NO_BUG"
)
DEV_T04_ALLOWED: tuple[str, ...] = ("SECRET_LOGGING", "SQL_INJECTION", "NO_BUG")
DEV_T04_EXPECTED = "SECRET_LOGGING"

DEV_T05_PROMPT = (
    "The current leaseEpoch of a Run is 5. A worker that just finished sent a completion "
    "notification carrying leaseEpoch 4.\n\n"
    "Should the coordinator commit the reported result? Reply with exactly one identifier "
    "from the allowed list and nothing else.\n"
    "Allowed answers: COMMIT, REJECT_STALE"
)
DEV_T05_ALLOWED: tuple[str, ...] = ("COMMIT", "REJECT_STALE")
DEV_T05_EXPECTED = "REJECT_STALE"

DEV_T06_CODE = "def allowed(age):\n    return age > 18"
DEV_T06_PROMPT = (
    "Specification: only people aged 18 or older are allowed.\n\n"
    f"{DEV_T06_CODE}\n\n"
    "Which defect class applies? Reply with exactly one identifier from the allowed list "
    "and nothing else.\n"
    "Allowed answers: BOUNDARY_BUG, TYPE_ERROR, NO_BUG"
)
DEV_T06_ALLOWED: tuple[str, ...] = ("BOUNDARY_BUG", "TYPE_ERROR", "NO_BUG")
DEV_T06_EXPECTED = "BOUNDARY_BUG"

# --- REVIEW_R01..R08: synthetic code-review tasks (chat providers only) ----------------
#
# The code is fixed, synthetic and numbered. The model must answer with a strict JSON
# object and nothing else. Gold lives in ``review_key.py`` only.


def _numbered_code(code: str) -> str:
    return "\n".join(f"{number}: {line}" for number, line in enumerate(code.splitlines(), 1))


def _review_prompt(code: str, specification: str | None = None) -> str:
    numbered = _numbered_code(code)
    spec_block = "" if specification is None else f"Specification: {specification}\n\n"
    return (
        "Review the following synthetic Python code. Each line is prefixed with its line "
        "number.\n\n"
        f"{spec_block}{numbered}\n\n"
        "Report every real defect. Reply with ONLY a JSON object, no prose and no markdown "
        "fences, in exactly this shape:\n"
        '{"findings":[{"line":<line number>,"rule_id":"<rule>","severity":"<severity>"}]}\n\n'
        "Allowed rule_id values: INDEX_OUT_OF_RANGE, SECRET_LOGGING, SQL_INJECTION, "
        "BOUNDARY_BUG, STALE_LEASE, UNSAFE_RETRY\n"
        "Allowed severities: P1, P2, P3\n"
        'Use an empty list when the code has no defect: {"findings":[]}\n'
        "Report at most eight findings, each line must exist in the numbered code, and do "
        "not use any other keys."
    )


REVIEW_R01_CODE = """def take(items, index):
    return items[len(items)]
def log_request(headers):
    token = headers["Authorization"]
    print("token=" + token)"""
REVIEW_R01_PROMPT = _review_prompt(
    REVIEW_R01_CODE,
    "stdout is shipped to a shared log, and incoming headers contain a secret Authorization token.",
)

REVIEW_R02_CODE = """def find_user(conn, name):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return conn.execute(query)

def allowed(age):
    return age > 18"""
REVIEW_R02_PROMPT = _review_prompt(REVIEW_R02_CODE, "Only people aged 18 or older are allowed.")

REVIEW_R03_CODE = """def complete(run, worker_epoch):
    run.commit(worker_epoch)
    return "OK"
def retry_unknown(send):
    try:
        return send()
    except TimeoutError:
        return send()"""
REVIEW_R03_PROMPT = _review_prompt(
    REVIEW_R03_CODE,
    "A completion carries worker_epoch while run.lease_epoch is the current lease. "
    "send performs an external side effect; when the connection times out, no response "
    "is received.",
)

REVIEW_R04_CODE = """def last_item(items):
    if not items:
        return None
    return items[-1]

def log_status(status):
    print("status=" + status)"""
REVIEW_R04_PROMPT = _review_prompt(REVIEW_R04_CODE)

REVIEW_R05_CODE = """import sqlite3

def find_user(conn, name):
    query = "SELECT * FROM users WHERE name = ?"
    return conn.execute(query, (name,)).fetchone()

def allowed(age):
    return age >= 18"""
REVIEW_R05_PROMPT = _review_prompt(REVIEW_R05_CODE)

REVIEW_R06_CODE = """def complete(run, worker_epoch):
    if worker_epoch != run.lease_epoch:
        return "STALE"
    run.committed = True
    return "OK"
def debug_headers(headers):
    token = headers["Authorization"]
    print(token)"""
REVIEW_R06_PROMPT = _review_prompt(
    REVIEW_R06_CODE,
    "stdout is shipped to a shared log, and incoming headers contain a secret Authorization token.",
)

REVIEW_R07_CODE = """def send_with_retry(send):
    try:
        return send()
    except TimeoutError:
        return send()"""
REVIEW_R07_PROMPT = _review_prompt(
    REVIEW_R07_CODE,
    "send performs an external side effect; a timeout leaves the outcome unknown.",
)

REVIEW_R08_CODE = """def complete(run, worker_epoch):
    if worker_epoch != run.lease_epoch:
        return "STALE"
    run.committed = True
    return "OK"

def send_once(send):
    try:
        return send()
    except TimeoutError:
        return "UNKNOWN_OUTCOME"
"""
REVIEW_R08_PROMPT = _review_prompt(REVIEW_R08_CODE)

#: Identifiers for the synthetic code-review tasks, in task order.
REVIEW_FIXTURE_IDS: tuple[FixtureId, ...] = (
    "review_r01",
    "review_r02",
    "review_r03",
    "review_r04",
    "review_r05",
    "review_r06",
    "review_r07",
    "review_r08",
)

#: Identifiers for the synthetic development tasks, in task order.
DEV_FIXTURE_IDS: tuple[FixtureId, ...] = (
    "dev_t01",
    "dev_t02",
    "dev_t03",
    "dev_t04",
    "dev_t05",
    "dev_t06",
)


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
    expected_answer: str | None
    allowed_answers: tuple[str, ...]
    description: str
    review_task: bool = False
    review_line_count: int = 0


_FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        fixture_id="jp_hello",
        wire_kind="chat_completions",
        prompt=JP_HELLO_PROMPT,
        expected_text=JP_HELLO_EXPECTED,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="one-word Japanese greeting; exact match required",
    ),
    Fixture(
        fixture_id="arithmetic_2plus2",
        wire_kind="chat_completions",
        prompt=ARITHMETIC_PROMPT,
        expected_text=ARITHMETIC_EXPECTED,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="2+2; exact single digit required",
    ),
    Fixture(
        fixture_id="refund_noul",
        wire_kind="system_one",
        prompt=None,
        expected_text=None,
        noul_threshold=REFUND_PASS_THRESHOLD,
        expected_answer=None,
        allowed_answers=(),
        description="System One noul probability on a synthetic refund sample",
    ),
    Fixture(
        fixture_id="dev_t01",
        wire_kind="chat_completions",
        prompt=DEV_T01_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=DEV_T01_EXPECTED,
        allowed_answers=DEV_T01_ALLOWED,
        description="synthetic array boundary defect classification",
    ),
    Fixture(
        fixture_id="dev_t02",
        wire_kind="chat_completions",
        prompt=DEV_T02_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=DEV_T02_EXPECTED,
        allowed_answers=DEV_T02_ALLOWED,
        description="synthetic SQL injection classification",
    ),
    Fixture(
        fixture_id="dev_t03",
        wire_kind="chat_completions",
        prompt=DEV_T03_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=DEV_T03_EXPECTED,
        allowed_answers=DEV_T03_ALLOWED,
        description="unknown side-effect outcome: no automatic retry",
    ),
    Fixture(
        fixture_id="dev_t04",
        wire_kind="chat_completions",
        prompt=DEV_T04_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=DEV_T04_EXPECTED,
        allowed_answers=DEV_T04_ALLOWED,
        description="synthetic secret logging classification",
    ),
    Fixture(
        fixture_id="dev_t05",
        wire_kind="chat_completions",
        prompt=DEV_T05_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=DEV_T05_EXPECTED,
        allowed_answers=DEV_T05_ALLOWED,
        description="stale leaseEpoch completion must be rejected",
    ),
    Fixture(
        fixture_id="dev_t06",
        wire_kind="chat_completions",
        prompt=DEV_T06_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=DEV_T06_EXPECTED,
        allowed_answers=DEV_T06_ALLOWED,
        description="synthetic boundary condition classification",
    ),
    Fixture(
        fixture_id="review_r01",
        wire_kind="chat_completions",
        prompt=REVIEW_R01_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: array boundary plus secret logging",
        review_task=True,
        review_line_count=5,
    ),
    Fixture(
        fixture_id="review_r02",
        wire_kind="chat_completions",
        prompt=REVIEW_R02_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: SQL injection plus boundary condition",
        review_task=True,
        review_line_count=6,
    ),
    Fixture(
        fixture_id="review_r03",
        wire_kind="chat_completions",
        prompt=REVIEW_R03_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: stale lease plus unsafe retry",
        review_task=True,
        review_line_count=8,
    ),
    Fixture(
        fixture_id="review_r04",
        wire_kind="chat_completions",
        prompt=REVIEW_R04_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: clean code",
        review_task=True,
        review_line_count=7,
    ),
    Fixture(
        fixture_id="review_r05",
        wire_kind="chat_completions",
        prompt=REVIEW_R05_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: clean parameterized query and boundary",
        review_task=True,
        review_line_count=8,
    ),
    Fixture(
        fixture_id="review_r06",
        wire_kind="chat_completions",
        prompt=REVIEW_R06_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: secret logging",
        review_task=True,
        review_line_count=8,
    ),
    Fixture(
        fixture_id="review_r07",
        wire_kind="chat_completions",
        prompt=REVIEW_R07_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: unsafe retry after unknown outcome",
        review_task=True,
        review_line_count=5,
    ),
    Fixture(
        fixture_id="review_r08",
        wire_kind="chat_completions",
        prompt=REVIEW_R08_PROMPT,
        expected_text=None,
        noul_threshold=None,
        expected_answer=None,
        allowed_answers=(),
        description="code review: clean lease check and no automatic retry",
        review_task=True,
        review_line_count=11,
    ),
)

FIXTURES: Mapping[str, Fixture] = MappingProxyType({fixture.fixture_id: fixture for fixture in _FIXTURES})

ALL_FIXTURE_IDS: tuple[FixtureId, ...] = (
    "jp_hello",
    "arithmetic_2plus2",
    "refund_noul",
    *DEV_FIXTURE_IDS,
    *REVIEW_FIXTURE_IDS,
)


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
    """Return the fixtures that may be sent to ``spec``.

    The synthetic development tasks are only allowed for the non-legacy chat providers
    (Kilo and OVHcloud); the System One service only accepts its own ``noul`` fixture.
    """
    if spec.wire_kind == "system_one":
        return ("refund_noul",)
    if spec.is_legacy:
        return ("jp_hello", "arithmetic_2plus2")
    return ("jp_hello", "arithmetic_2plus2", *DEV_FIXTURE_IDS, *REVIEW_FIXTURE_IDS)


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
    "DEV_FIXTURE_IDS",
    "DEV_T01_ALLOWED",
    "DEV_T01_CODE",
    "DEV_T01_EXPECTED",
    "DEV_T01_PROMPT",
    "DEV_T02_ALLOWED",
    "DEV_T02_CODE",
    "DEV_T02_EXPECTED",
    "DEV_T02_PROMPT",
    "DEV_T03_ALLOWED",
    "DEV_T03_EXPECTED",
    "DEV_T03_PROMPT",
    "DEV_T04_ALLOWED",
    "DEV_T04_CODE",
    "DEV_T04_EXPECTED",
    "DEV_T04_PROMPT",
    "DEV_T05_ALLOWED",
    "DEV_T05_EXPECTED",
    "DEV_T05_PROMPT",
    "DEV_T06_ALLOWED",
    "DEV_T06_CODE",
    "DEV_T06_EXPECTED",
    "DEV_T06_PROMPT",
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
