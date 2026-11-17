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
```

`probe` also accepts `--fixture arithmetic_2plus2` for the two chat services, and
`--fixture refund_noul` for `jev_legacy` only. Legacy services additionally require
`--allow-legacy`; they are still skipped with `UNVERIFIED_TERMS` while their anonymous
terms are unconfirmed (see below).

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

Only the constants in `fixtures.py`: a one-word Japanese greeting, `2+2`, and a
synthetic refund sentence with a `noul` question. No private repository content, customer
data, cookies, tokens or `.env` values can be attached. `devtools/free_ai/.runs/` holds a
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
  for `Accept-Encoding: identity`; only `identity`, `gzip`, `deflate` and `br` responses
  are accepted, `Content-Length` is validated, and the decoded size is capped, so a
  compressed or mis-declared body cannot bypass the 131072-byte limit.

## Tests

The four test modules exercise FA-T01–FA-T36 without any network access
(`httpx.MockTransport` only). They run with the backend development environment:

```bash
python -m pytest -q devtools/free_ai/tests
python -m ruff check --config backend/pyproject.toml devtools/free_ai
python -m mypy --config-file backend/pyproject.toml --strict devtools/free_ai
```
