# MCP Adapter — Verification Record

> Status: **HOST_VERIFIED for the local adapter slice; not PRODUCTION_VERIFIED**. Executed on Windows,
> CPython 3.13, branch `feat/mcp-adapter-foundation`, base `53ac14c5` (2026-11-26). No external MCP service,
> Docker, KVM, browser or production credential was used. A WSL2 PostgreSQL 14 instance was used only for
> the repository's existing migration suite; no MCP persistence/RLS schema exists. The 2026-11-27 review-fix
> pass adds regression coverage for P1-01..P1-09; the second-review Slice A2 pass adds invalidation-epoch/
> live-pin, policy-scope, output-contract, deadline, JSON-recursion and bounded-SSE coverage; the third-review
> pass adds historical-resurrection, policy-record-integrity, broker/call-slot deadline, structured-write and
> cancellation-cleanup coverage. Real Sentinel/PostgreSQL authority, durable MCP state and the Gateway
> integration path remain unexecuted (P1-12).

## 1. Commands and result

From `backend`:

```powershell
$env:PYTHONPATH='packages/mcp_adapter/src;packages/gateways/src;.'
.venv\Scripts\python.exe -m pytest tests/mcp_adapter -q --no-header -p no:cacheprovider
# 98 passed

.venv\Scripts\python.exe -m ruff format --check packages/mcp_adapter/src packages/gateways/src/aiagent_gateways/mcp_executor.py tests/mcp_adapter
# 29 files already formatted

.venv\Scripts\python.exe -m ruff check packages/mcp_adapter/src packages/gateways/src/aiagent_gateways/mcp_executor.py tests/mcp_adapter
# All checks passed

.venv\Scripts\python.exe -m mypy packages/mcp_adapter/src packages/gateways/src/aiagent_gateways/mcp_executor.py
# Success: no issues found in 17 source files

cd ..
uv sync --locked
# resolved, checked
```

The E2E tests in `tests/mcp_adapter/test_sdk_protocol.py` start a real MCP server (SDK 2.3.0,
`StreamableHTTPSessionManager` + `uvicorn`) on `127.0.0.1` and a real absolute-form forwarding proxy. They
verify initialize, two-page `tools/list`, one bound `tools/call`, purpose-scoped control permits,
`Proxy-Authorization` injection and stale credential refusal. The issuer is still a fake: no real
Sentinel/PostgreSQL authority was exercised. This is loopback-only and uses the `AIAGENT_PROFILE=test`
escape hatch.

## 2. Test inventory

| File | Tests | What is executed |
|---|---:|---|
| `test_schema_validation.py` | 8 | strict JSON, 2020-12 only, external refs, depth/property/regex caps, argument/output validation, description sanitization |
| `test_registry.py` | 16 | CAS publish, stale generation, monotonic high-watermark after TTL, TTL, immutable/current-only run pin, invalidation epoch/current removal, stale-scan publish denial, live descriptor change denial, removed/reintroduced descriptor cannot revive a pin, endpoint change denial, expired-pin tombstone, revoke, explicit re-enable, schema/risk/policy mismatch denial |
| `test_discovery.py` | 10 | multi-page opaque cursors, cursor loop, duplicate names, tool cap, malformed schema, list_changed invalidation, concurrent refresh, provider/tenant/owner/policy-revision scope |
| `test_normalization.py` | 8 | text caps/control chars, binary hashing, resource links, `isError`, empty text, output schema required/mismatch, structured-content cap |
| `test_egress.py` | 10 | absolute-form request, permit header, origin allowlist, denial proof, duplicate security header, truncated/oversized response, oversized request, bounded SSE JSON, timeout classification |
| `test_executor.py` | 21 | pinned ref/argument digest and input schema, mutation after prepare, endpoint/policy/credential/disable revision changes, current-send/open fence, list_changed pin invalidation, read failure vs write UNKNOWN, text-only/empty/whitespace/link-only/empty-object/truncated/isError writes, permit requirement, no auto-retry, no trusted receipt |
| `test_sdk_protocol.py` | 5 | real initialize/list/call, credential header, stale credential refusal, admitted permit override, SDK session-loss classification |
| `test_session_manager.py` | 9 | feature flag, disabled/revoked provider, missing credential, credential handoff, credential-broker deadline, post-semaphore open fence, expired overall deadline, call-slot deadline, call-slot concurrency |
| `test_policy.py` | 5 | provider/tenant/owner-scoped policy record, record name/revision/digest integrity, named approver, allowlist/policy intersection |
| `test_errors.py` | 6 | trusted pre-send denial vs post-send 401/403/429/5xx/redirect uncertainty, retryability, `-32601` call uncertainty, cancellation cleanup |
| **Total** | **98** | |

## 3. Canonical MCP-T coverage

Legend: **HOST_VERIFIED** = executed in this local slice; **CODE_ONLY** = code path exists but no direct
execution; **UNEXECUTED** = requires infrastructure not available here; **BLOCKED** = owned by another
change.

| Test ID | Status | Evidence / reason |
|---|---|---|
| MCP-T01 unapproved URL → no connection | HOST_VERIFIED (config + session) | `provider` validation, `SessionManager` disabled/revoked tests; no socket is opened |
| MCP-T02 no tools capability | CODE_ONLY | capability check in `sdk_transport.open`; no failing-server fixture |
| MCP-T03 three-page opaque cursor | HOST_VERIFIED | `test_discovery` pagination + real two-page E2E |
| MCP-T04 cursor loop / duplicate / malformed schema | HOST_VERIFIED | `test_discovery` |
| MCP-T05 malicious description/annotations | CODE_ONLY | sanitization and risk separation exist; no end-to-end malicious metadata test |
| MCP-T06 two providers same server name | HOST_VERIFIED (unit) | `test_registry` uses admin provider id, not `serverInfo.name` |
| MCP-T07 list_changed vs active run | HOST_VERIFIED (unit) | awaited pin invalidation removes the current surface, bumps the epoch, tombstones pins; executor fence + `test_registry` live-descriptor check; real SSE not exercised |
| MCP-T08 lost notification / TTL stale | HOST_VERIFIED (unit) | `test_registry` TTL/pin, monotonic generation, invalidation epoch, expired-pin tombstone |
| MCP-T09 list_changed mid-list | HOST_VERIFIED (unit) | `test_discovery` generation invalidation |
| MCP-T10 provider disabled/revoked mid-call | HOST_VERIFIED (unit) | `test_session_manager` + executor actual-send fence; real in-flight remote reconciliation not tested |
| MCP-T11 404 read → bounded reconnect | CODE_ONLY | `_map_sdk_error` classifies session loss; no real 404 reconnect test |
| MCP-T12 404 write → UNKNOWN, no replay | CODE_ONLY | executor maps `HTTP_SESSION_LOST` uncertain; no real 404 E2E because the SDK cancels the pending request on this path |
| MCP-T13 isError after write | HOST_VERIFIED (unit) | executor maps write `isError` to UNKNOWN; normalization tests cover the status |
| MCP-T14 structuredContent 0/false/null | HOST_VERIFIED (unit) | `test_normalization` |
| MCP-T15 output schema mismatch | HOST_VERIFIED (unit) | `test_normalization` |
| MCP-T16 redirect/DNS rebinding/link-local | CODE_ONLY | endpoint/egress origin checks unit-tested; live DNS/redirect not exercised |
| MCP-T17 approval in `_meta` | CODE_ONLY | no authority is read from `_meta`; no dedicated test |
| MCP-T18 abort/timeout before/after dispatch | HOST_VERIFIED (unit) | `test_egress`/`test_executor` uncertainty and pre-send fence; process-kill test absent |
| MCP-T19 worker kill between list/publish | BLOCKED | in-memory store; no PostgreSQL/worker |
| MCP-T20 kill after remote success | BLOCKED | no real remote server/ledger |
| MCP-T21 actor B receives A's ref | HOST_VERIFIED (unit) | `test_executor`/`test_registry` scope mismatch |
| MCP-T22 event/recipe/attachment creates tool | UNEXECUTED | no event ingestion path in this slice |
| MCP-T23 fake SSE never terminates | UNEXECUTED | no streaming SSE proxy fixture |
| MCP-T24 2 tenants × 100 tools × 4 workers | UNEXECUTED | no load/benchmark environment |
| MCP-T25 unsupported revision during upgrade | CODE_ONLY | protocol revision check exists; no rolling-upgrade fixture |

## 4. Review-finding disposition (first PR #124 review)

| Finding | Disposition | Evidence / remaining gap |
|---|---|---|
| P1-01 single-use permit reused | FIXED (adapter); real authority BLOCKED | E2E records control purposes and exactly one effect POST. Real Sentinel+PostgreSQL permit issuance is not executed. |
| P1-02 pinned input schema not validated | FIXED | executor clones/bounds arguments, verifies digest, compiles pinned schema and validates before session; tests cover type/mutation. |
| P1-03 isError/UNSUPPORTED/TRUNCATED CONFIRMED | FIXED (executor) | writes with these outcomes raise `ExecutorUnknown`; empty writes UNKNOWN; provider receipt trust removed. Gateway ledger E2E remains absent. |
| P1-04 post-send errors pre-send | PARTIAL | 401/403/429/5xx/redirect/malformed response are classified uncertain; retryable is limited to connect failures. Real partial-write/process-kill fixtures absent. |
| P1-05 stale endpoint/credential/policy | FIXED (unit) | executor compares current provider endpoint fingerprint, policy revision and credential version; endpoint/policy/credential/disabled changes fail closed. |
| P1-06 list_changed old pin | FIXED (unit) | async callback awaited; all provider pins invalidated; fence rejects old pin. Real SSE absent. |
| P1-07 generation reuse | FIXED (unit, process-local) | monotonic high-watermark, current-only pin, expired tombstone. PostgreSQL durability absent. |
| P1-08 no actual-send fence | FIXED (unit) | `_before_effect_send` re-resolves pin, checks deadline/revocation/revisions immediately before the effect POST. Real queue/lease E2E absent. |
| P1-09 policy provider scoping | FIXED | `PolicySet.decide` rejects transplanted provider records; executor intersects provider allowlist. |
| P1-10 CPU/schema bounds | PARTIAL | no-`$ref` subset, depth/node/string/int caps, regex screening and output/block caps; combinatorial regex/CPU exhaustion is not formally bounded. |
| P1-11 raw wire/strict JSON | PARTIAL | strict JSON parser on egress response, structural caps and header validation; SDK still parses the JSON-RPC envelope, and real response fuzzing is absent. |
| P1-12 Gateway admitted context | OPEN / BLOCKED | executor is not wired into `ToolGateway.submit`; dynamic tenant-scoped model surface and destination resolution remain unimplemented. |

## 4.1 Second-review Slice A2 disposition

Second review verdict: **REQUEST_CHANGES / IMPLEMENTED_PARTIAL / NOT MERGE_READY**. This pass fixes the
locally actionable P1 items and adds failing-regression-first unit coverage; it does not claim the
durable/Gateway/runtime work below is complete.

| Finding | Disposition | Evidence / remaining gap |
|---|---|---|
| R2-P1-01 invalidation did not clear current/epoch | FIXED (unit, process-local) | `invalidate_pins` removes `_current`, bumps epoch, tombstones pins; stale scan publish is rejected; new Run must re-discover/re-pin. Epoch is not durable. |
| R2-P1-02 old pin executed changed/deleted tool | PARTIAL → R3-01 | `resolve_pinned` requires the live current snapshot, the same descriptor/schema/endpoint; any published surface change revokes all provider pins, including removed-then-reintroduced descriptors. Real SSE and durable history remain open. |
| R2-P1-03 policy scope/revision transplant | PARTIAL → R3-02 | `ToolPolicy` binds provider/tenant/owner, name, revision and a canonical record digest; discovery rejects transplanted/tampered/renamed/future records. The local digest is not cryptographic provenance; real authority store absent. |
| R2-P1-04 weak write confirmation | PARTIAL → R3-05 | output schema requires `structuredContent`; text-only, empty/whitespace, link/binary-only and empty-object writes become UNKNOWN. A non-empty structured object is machine evidence, not proof of the business outcome; no provider receipt contract. |
| R2-P1-05 `-32601`/cleanup/cancel classification | PARTIAL → R3-06 | `-32601` on a call is uncertain; cleanup no longer replaces a success; cancellation now runs transport cleanup before re-raising. Exception-group/effect-state and real remote cancel remain unexercised. |
| R2-P1-06 real authority/single-use consumption | OPEN / BLOCKED | no real Sentinel/PostgreSQL authority; fake issuer only. |
| R2-P1-07 semaphore wait outside deadline/config re-read | PARTIAL → R3-03 | the overall deadline now covers credential-broker resolution, the admitted-call slot and the session; live state is rechecked after waits. Proxy/authority send window and hard CPU bounds remain open. |
| R2-P1-08 JSON recursion/SSE/header/content-type | PARTIAL | `RecursionError`, bounded SSE event JSON, duplicate security headers and non-JSON/SSE response rejection added; no real streaming SSE, and SDK envelope parsing remains. |
| R2-P1-09 runtime record/capability/annotation isolation test | PARTIAL | policy tenant/owner record isolation test added; full runtime capability/annotation isolation test remains missing. |
| R2-P1-10 CPU/schema bounds | PARTIAL | bounded subset and caps remain; no formal combinatorial/CPU proof. |
| R2-P1-12 Gateway admitted context/dynamic surface | OPEN / BLOCKED | executor still not registered in `ToolGateway.submit`; no real Gateway ledger E2E. |

## 4.2 Third-review (3b75a0a) disposition

Third review verdict: **REQUEST_CHANGES / IMPLEMENTED_PARTIAL / NOT MERGE_READY**. This pass adds
failing-regression-first tests for the locally closable A2 residuals. It does not claim the real authority,
durable state, Gateway integration or streaming work is complete.

| Finding | Disposition | Evidence / remaining gap |
|---|---|---|
| R3-01 historical resurrection / endpoint replacement / multi-provider pins | FIXED (unit, process-local) | publishing a different surface revokes every provider pin; `resolve_pinned` requires the current surface digest and endpoint to equal the pin; a deletion→re-appearance sequence, an endpoint-only change and one run pinning two providers are regression tests. Real SSE/restart durability open. |
| R3-02 policy record identity/provenance | PARTIAL | record name, scope, revision and canonical record digest are checked; renamed/tampered/future-revision records are rejected. A local digest is not cryptographic authority and cannot prove who approved it; Sentinel/PostgreSQL provenance remains R3-04. |
| R3-03 deadline vs call slot/broker/send | PARTIAL | credential resolution and the call-slot wait are bounded by the overall deadline; session deadline is recomputed after waits. The proxy/authority send window and hard CPU bound remain open. |
| R3-04 real authority/single-use consumption | OPEN / BLOCKED | fake issuer only; no real Sentinel/PostgreSQL single-use consumption. |
| R3-05 semantic write confirmation | PARTIAL | a write is CONFIRMED only with a non-empty structured result; text-only is UNKNOWN. No generic MCP business-receipt contract exists, so semantic effect verification remains server-specific/UNKNOWN. |
| R3-06 cancel/ExceptionGroup effect state | PARTIAL | cancellation cleanup is awaited before re-raising; cleanup failure cannot replace the primary error. Real remote cancel and full ExceptionGroup→effect-state mapping remain untested. |
| R3-07 CPU/RSS/streaming/envelope bounds | PARTIAL | strict JSON, duplicate security headers, bounded buffered SSE and response caps exist; no streaming SSE proxy, parser differential fuzz or hard CPU/RSS limit. |
| R3-08 Gateway/durable state | OPEN / BLOCKED | no Gateway registration, tenant-scoped model surface, PostgreSQL snapshot/policy store or ledger E2E. |

## 5. Not verified (must not be reported as Verified)

- MCP PostgreSQL persistence, CAS/RLS and a numbered migration (blocked on PR #122). The repository's
  existing migration suite was executed against local PostgreSQL 14 (12 passed), but no MCP schema exists
  and PostgreSQL 16 parity was not exercised. The generation high-watermark and pin tombstones are
  process-local.
- Gateway registration/model-surface filtering and end-to-end `ToolGateway.submit → McpToolExecutor`.
- Real remote HTTPS MCP provider, OAuth refresh/revocation, redirects, DNS rebinding. The adapter does
  not terminate TLS; a reviewed Sentinel/proxy TLS-terminating connector is an unexecuted prerequisite,
  not proof that remote HTTPS is architecturally impossible.
- Real `tools/list_changed` SSE delivery and notification-loss recovery.
- Kubernetes/KVM isolation, compose deployment, load/cost/latency benchmarks.
- Web UI, API router, Marketplace candidate flow, provider health projection.
- OAuth token rotation, credential broker production implementation, secret-manager integration.

## 6. Full backend suite context

The full backend suite was also run with the CI-equivalent ignores at this revision and a local WSL2
PostgreSQL 14 URL (`AIAGENT_TEST_PG_URL=postgresql://postgres:ci@127.0.0.1:5432/postgres`):
**557 passed, 106 skipped, 19 failed in 296.78 s**.

- The 12 `tests/test_db_migrations.py` tests passed, including the three that fail without a PG URL.
- 18 browser/UI integration failures are environmental: Playwright Chromium is not installed and
  `web/dist` was not built on this Windows host. CI installs Chromium and builds the Web UI.
- `tests/test_sandbox_execution_result.py::test_capture_signature_requires_signed_binding_and_exact_private_bytes`
  uses POSIX `os.O_DIRECTORY`, which does not exist on Windows.

These are not evidence of MCP adapter correctness, and they do not turn the PostgreSQL, browser or sandbox
gates into PASS. The adapter-specific 98-test result above is the executed evidence for this change. This
local run is not Linux/CI parity and is not PostgreSQL 16 parity.