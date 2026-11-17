# Free AI Lab (development-only)

This directory is a **development-time experiment harness**, not a product feature.

It sends three fixed synthetic fixtures to at most four fixed external AI endpoints so
that the implementation agent can note whether an unauthenticated service currently
answers with the expected schema. Nothing here is registered in the aiagent-md Model
Gateway, Agent Runtime, Sentinel, Web UI, public API, Docker image, release path or CI
gate that ships the product. Production model selection, routing, fallback, tool
execution, billing, contracts, database and deployment are untouched.

## What it can and cannot do

- `list` — print the frozen catalog (id, model, protocol, host, eligibility, evidence
  class, live availability, reason). Live availability is `unverified` until a probe
  was actually run in that environment; nothing is advertised as "available" or
  "confirmed free".
- `dry-run` — print each fixture id, its exact request-body byte count and destination.
  It reports `network_sent=0` and is **not** a budget or token estimate.
- `probe` — send **one** fixed fixture to **one** provider, once per invocation, with a
  local minimum interval and a local daily cap. The two acknowledgement flags are
  mandatory.

There is no flag for `--url`, `--base-url`, `--model`, `--prompt`, `--file`, `--repo`,
`--all`, `--repeat`, `--concurrency`, `--api-key`, `--token`, `--proxy`,
`--execute-generated-code` or `--fallback-to-paid`, and argparse rejects unknown flags.
Model prompts, repository sources, files, stdin, environment proxies and credentials
cannot enter a request.

## Commands (run from the repository root; probes in WSL/Linux)

```bash
python -m devtools.free_ai list
python -m devtools.free_ai dry-run
python -m devtools.free_ai probe \
  --provider kilo --fixture jp_hello \
  --ack-synthetic-only --ack-external-request
python -m devtools.free_ai report
```

`probe` accepts `--fixture jp_hello`, `--fixture arithmetic_2plus2` and the six
synthetic development tasks `--fixture dev_t01` … `dev_t06` for the two official chat
services (Kilo and OVHcloud). `--fixture refund_noul` is only valid for `jev_legacy`,
and the `dev_*` tasks are never allowed on the legacy services. Legacy services
additionally require `--allow-legacy`; they are still skipped with `UNVERIFIED_TERMS`
while their anonymous terms are unconfirmed (see below).

### Synthetic development tasks (STEP 2)

| Fixture | Task | Expected identifier | Allowed identifiers |
|---|---|---|---|
| `dev_t01` | array boundary defect | `INDEX_OUT_OF_RANGE` | `INDEX_OUT_OF_RANGE`, `TYPE_ERROR`, `NO_BUG` |
| `dev_t02` | SQL injection | `SQL_INJECTION` | `SQL_INJECTION`, `MEMORY_LEAK`, `NO_BUG` |
| `dev_t03` | unknown side-effect outcome | `DO_NOT_RETRY_AUTOMATICALLY` | `RETRY_IMMEDIATELY`, `DO_NOT_RETRY_AUTOMATICALLY` |
| `dev_t04` | secret logging | `SECRET_LOGGING` | `SECRET_LOGGING`, `SQL_INJECTION`, `NO_BUG` |
| `dev_t05` | stale `leaseEpoch` completion | `REJECT_STALE` | `COMMIT`, `REJECT_STALE` |
| `dev_t06` | boundary condition | `BOUNDARY_BUG` | `BOUNDARY_BUG`, `TYPE_ERROR`, `NO_BUG` |

Each task is a fixed constant in `fixtures.py` built from synthetic code or a synthetic
situation; nothing is read from files, stdin, environment variables or the repository.
The prompt asks for exactly one identifier. Scoring is deterministic Python:

- exact identifier after trimming surrounding whitespace → `PASS`
- an allowed identifier that is not the expected one → `CONTENT_MISMATCH`
- any other text (prose, different casing, partial match, empty answer) →
  `FORMAT_MISMATCH`; an answer is never extracted from an explanation
- missing/unsafe/other model id, tool calls and non-zero reported cost keep their
  existing `FORMAT_MISMATCH` / `MODEL_DRIFT` / `UNEXPECTED_TOOL_OUTPUT` /
  `UNEXPECTED_BILLING` behaviour

### `report`

`report` reads only the fixed `devtools/free_ai/.runs/probes.jsonl` (no path argument, no
directory scan, no network) and prints one JSON line aggregating per provider: attempts,
evaluated responses, `PASS`, `CONTENT_MISMATCH`, `FORMAT_MISMATCH`, `MODEL_DRIFT`, other
evaluated failures, pass rate over evaluated responses, `communication_failures`,
`local_preflight_skips`, median `elapsed_ms`, observed token sums, and how many records
had no reported cost. It never prints prompts, answers, reasoning or headers.

`communication_failures` counts only outcomes where a request was actually attempted
(`NETWORK_ERROR`, `TIMEOUT`, `REMOTE_ERROR`, `ACCESS_RESTRICTED`, HTTP 429, and a
response-stage `SAFETY_BLOCKED`). Results rejected before any send (`UNVERIFIED_TERMS`,
`LOCAL_INPUT_INVALID`, local quota `/` held lock, local legacy refusal) are counted as
`local_preflight_skips` and are never part of the quality denominator.

Records are validated for meaning as well as shape: `PASS` requires HTTP 200, no positive
cost and, for Kilo/OVH, the exact requested model (legacy aliases stay exempt);
`MODEL_DRIFT` requires a different observed model; `UNEXPECTED_BILLING` requires a
positive cost; status/reason pairs, provider/fixture pairs, catalog model ids and
HTTP/status pairings are checked against what the lab can actually produce.

The log is limited to 1 MiB and 5000 lines. A missing or empty file is an empty report;
malformed JSON, duplicate keys, `NaN`/`Infinity`, unexpected or missing fields, wrong
types, unknown or impossible values, semantic contradictions or limit violations print a
constant `INVALID_LOG` JSON line and exit `5`; no partial aggregate is printed as success.

Exit codes: `0` fixture contract satisfied (never a production guarantee), `2` safe
skip (missing acknowledgement, disabled legacy service, local quota), `3` provider
unavailable / 429 / 5xx / network / timeout, `4` HTTP 200 with a bad schema, content
mismatch, model drift, tool output or non-zero reported cost, `5` local safety
invariant (redirect, unexpected host, oversized body, held lock, corrupt quota).

Exactly one JSON line is printed per probe. It has the thirteen fields
`schema_version, provider_id, model_requested, model_observed, fixture_id,
observed_at_utc, http_status, status, reason_code, elapsed_ms, input_tokens,
output_tokens, cost_reported_usd`. Prompts, response bodies, reasoning, headers,
credentials and exception messages are never printed or logged.

## Data that leaves the machine

Only the constants in `fixtures.py`: a one-word Japanese greeting, `2+2`, a
synthetic refund sentence with a `noul` question, and the six synthetic development
tasks (fixed code snippets or a fixed situation plus their allowed identifiers). No
private repository content, customer data, cookies, tokens or `.env` values can be
attached. `devtools/free_ai/.runs/` holds a
local quota file and a metadata-only JSONL log; it is git-ignored and is never created by
`list` or `dry-run`. Set `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` if you like — they are
ignored (`trust_env=False`), redirects are refused, and the TLS certificate is always
verified.

## Usage terms reviewed on 2026-11-17 (catalog checked on 2026-11-17)

| Service | Endpoint / model | Evidence | Notes |
|---|---|---|---|
| Kilo | `https://api.kilo.ai/api/gateway/chat/completions`, `nvidia/nemotron-3.5-super-vl:free` | official docs describe anonymous use and a per-IP hourly limit | model availability, trial data terms and `:free` pricing can change; never send personal data |
| OVHcloud | `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions`, `gpt-oss-120b` | official docs describe anonymous per-minute limits | this tool uses a stricter 65 s interval than the documented limit |
| OpenCode Zen (`jev_legacy`) | `https://opencode.ai/zen/v1/systemone`, `jev-1.13-free` | user-observed legacy endpoint | current Console lists the model but gates use behind an API key; anonymous use is **not** confirmed, so the tool skips it |
| Pollinations (`pollinations_legacy`) | `https://text.pollinations.ai/openai`, `openai-fast` | user-observed legacy endpoint | current `gen.pollinations.ai` requires an API key; the old anonymous alias may move or end, so the tool skips it |

The catalog check is a snapshot, not a guarantee. `:free`, `cost: 0` or "anonymous" are
not promises that future requests are free; this tool never switches to a paid model,
never asks for a paid credential, never retries automatically, never falls back to
another provider, and never evades authentication, rate limits, CAPTCHA or usage terms.
A `cost` reported by a provider is recorded as observed; `cost_reported_usd: null` means
"not reported", which is different from a verified zero charge.

Single live observations from the implementation environment on 2026-11-17, kept only
as point-in-time evidence: `kilo`/`jp_hello` returned HTTP 200 with the requested model
and no reported cost; `ovh`/`arithmetic_2plus2` timed out after about 16 s with an
unknown outcome and no retry. Neither legacy service was contacted. These are single
runs, not availability or pricing guarantees.

## Reading the results

`PASS` only means the fixed synthetic fixture matched under this contract for this
service, model and moment. It does not establish tool calling, long-context, coding or
production readiness. `model_observed` is whatever the provider reported; model drift is
rejected for the two official services and recorded as a fact for the legacy aliases.
`UNKNOWN_OUTCOME` on network failures means a request may have reached the provider, so
the local slot is consumed and no retry is attempted.

Implementation note: the fixed reason-code set has no billing code, so
`UNEXPECTED_BILLING` is reported with `reason_code=REMOTE_FAILURE`; the status is the
authoritative signal. Local quota skips use `status=RATE_LIMITED` with
`reason_code=RATE_LIMIT` and exit `2`, which is distinct from a provider 429 (exit `3`).

## Hardening invariants

- `transport.post_json(spec, fixture_id)` takes no body. The payload is rebuilt from the
  frozen catalog and `fixtures.py` constants immediately before serialization, so a
  caller cannot send a free-form prompt, a file or a different model. The legacy
  services are refused at this egress boundary as well as in the CLI
  (`SAFETY_BLOCKED` / `LEGACY_DISABLED`, no network).
- Kilo and OVHcloud must report the exact requested model id. A missing model, a
  non-string model, or a model id that is not a short printable token (≤128 chars) is
  never scored as `PASS`; a different reported model is `MODEL_DRIFT`. Unsafe model ids
  are dropped, so no provider-controlled text or control character can reach stdout or
  `probes.jsonl`.
- The quota file is parsed with duplicate-key and `NaN`/`Infinity` rejection, finite and
  bounded timestamps, and a bounded day counter. Any malformed value means zero requests
  and exit `5`; it is never repaired silently.
- Reported `cost` and System One `noul` values that cannot be represented as a finite
  float (for example a 309-digit integer) are a `FORMAT_MISMATCH`, not an exception, and
  no conversion error text is stored.
- Response bodies are capped before a chunk is appended to the buffer. The request asks
  for `Accept-Encoding: identity`; only an absent or `identity` content encoding is
  accepted. `gzip`, `deflate`, `br` and unknown encodings are `FORMAT_MISMATCH` and are
  refused before a decoder starts, so a compression bomb never expands. `Content-Length`
  is validated and the unencoded size is capped at 131072 bytes.
- JSON that is nested too deeply aborts the parser with `RecursionError`; that is mapped
  to `FORMAT_MISMATCH` / `INVALID_JSON` (exit 4) instead of escaping as a crash.

## Tests

The four test modules exercise FA-T01–FA-T36 without any network access
(`httpx.MockTransport` only). They run with the backend development environment:

```bash
python -m pytest -q devtools/free_ai/tests
python -m ruff check --config backend/pyproject.toml devtools/free_ai
python -m mypy --config-file backend/pyproject.toml --strict devtools/free_ai
```
