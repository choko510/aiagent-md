# MCP Adapter — Verification Record

> Status: **HOST_VERIFIED for the local adapter slice; not PRODUCTION_VERIFIED**. Executed on Windows,
> CPython 3.13, branch `feat/mcp-adapter-foundation`, base `53ac14c5` (2026-11-26). No external MCP service,
> PostgreSQL, Docker, KVM, browser or production credential was used.

## 1. Commands and result

From `backend`:

```powershell
$env:PYTHONPATH='packages/mcp_adapter/src;packages/gateways/src;.'
.venv\Scripts\python.exe -m pytest tests/mcp_adapter -q
# 55 passed

.venv\Scripts\python.exe -m ruff check packages/mcp_adapter/src tests/mcp_adapter
# All checks passed

.venv\Scripts\python.exe -m mypy packages/mcp_adapter/src packages/gateways/src/aiagent_gateways/mcp_executor.py
# Success: no issues found
```

The E2E tests in `tests/mcp_adapter/test_sdk_protocol.py` start a real MCP server (SDK 2.3.0,
`StreamableHTTPSessionManager` + `uvicorn`) on `127.0.0.1` and a real absolute-form forwarding proxy. They
verify initialize, two-page `tools/list`, `tools/call`, `Proxy-Authorization` permit injection and stale
credential refusal. This is loopback-only and uses the `AIAGENT_PROFILE=test` escape hatch.

## 2. Test inventory

| File | Tests | What is executed |
|---|---:|---|
| `test_schema_validation.py` | 8 | strict JSON, 2020-12 only, external refs, depth/property/regex caps, argument/output validation, description sanitization |
| `test_registry.py` | 8 | CAS publish, stale generation, TTL, immutable run pin, revoke, explicit re-enable, schema/risk/policy mismatch denial |
| `test_discovery.py` | 8 | multi-page opaque cursors, cursor loop, duplicate names, tool cap, malformed schema, list_changed invalidation, concurrent refresh |
| `test_normalization.py` | 6 | text caps/control chars, binary hashing, resource links, `isError`, output schema mismatch, structured-content cap |
| `test_egress.py` | 7 | absolute-form request, permit header, origin allowlist, denial proof, truncated/oversized response, timeout classification |
| `test_executor.py` | 8 | pinned ref/argument digest, scope mismatch, read vs write uncertainty, permit requirement, no auto-retry, receipt mapping |
| `test_sdk_protocol.py` | 5 | real initialize/list/call, credential header, stale credential refusal, admitted permit override, SDK session-loss classification |
| `test_session_manager.py` | 5 | feature flag, disabled/revoked provider, missing credential, credential handoff, call-slot concurrency |
| **Total** | **55** | |

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
| MCP-T07 list_changed vs active run | HOST_VERIFIED (unit) | `test_discovery` invalidation + `test_registry` pinned resolution; real SSE not exercised |
| MCP-T08 lost notification / TTL stale | HOST_VERIFIED (unit) | `test_registry` TTL/pin |
| MCP-T09 list_changed mid-list | HOST_VERIFIED (unit) | `test_discovery` generation invalidation |
| MCP-T10 provider disabled/revoked mid-call | HOST_VERIFIED (unit) | `test_session_manager`; in-flight remote reconciliation not tested |
| MCP-T11 404 read → bounded reconnect | CODE_ONLY | `_map_sdk_error` classifies session loss; no real 404 reconnect test |
| MCP-T12 404 write → UNKNOWN, no replay | CODE_ONLY | executor maps `HTTP_SESSION_LOST` uncertain; no real 404 E2E because the SDK cancels the pending request on this path |
| MCP-T13 isError after write | HOST_VERIFIED (unit) | `test_normalization` |
| MCP-T14 structuredContent 0/false/null | HOST_VERIFIED (unit) | `test_normalization` |
| MCP-T15 output schema mismatch | HOST_VERIFIED (unit) | `test_normalization` |
| MCP-T16 redirect/DNS rebinding/link-local | CODE_ONLY | endpoint/egress origin checks unit-tested; live DNS/redirect not exercised |
| MCP-T17 approval in `_meta` | CODE_ONLY | no authority is read from `_meta`; no dedicated test |
| MCP-T18 abort/timeout before/after dispatch | HOST_VERIFIED (unit) | `test_egress`/`test_executor` uncertainty; process-kill test absent |
| MCP-T19 worker kill between list/publish | BLOCKED | in-memory store; no PostgreSQL/worker |
| MCP-T20 kill after remote success | BLOCKED | no real remote server/ledger |
| MCP-T21 actor B receives A's ref | HOST_VERIFIED (unit) | `test_executor`/`test_registry` scope mismatch |
| MCP-T22 event/recipe/attachment creates tool | UNEXECUTED | no event ingestion path in this slice |
| MCP-T23 fake SSE never terminates | UNEXECUTED | no streaming SSE proxy fixture |
| MCP-T24 2 tenants × 100 tools × 4 workers | UNEXECUTED | no load/benchmark environment |
| MCP-T25 unsupported revision during upgrade | CODE_ONLY | protocol revision check exists; no rolling-upgrade fixture |

## 4. Not verified (must not be reported as Verified)

- PostgreSQL persistence, CAS/RLS and migration `0029` (blocked on PR #122; no PostgreSQL available here).
- Gateway registration/model-surface filtering and end-to-end `ToolGateway.submit → McpToolExecutor`.
- Real remote HTTPS MCP provider, OAuth refresh/revocation, redirects, DNS rebinding. The current egress
  path has no TLS termination/CONNECT; a trusted TLS-terminating connector is a prerequisite.
- Real `tools/list_changed` SSE delivery and notification-loss recovery.
- Kubernetes/KVM isolation, compose deployment, load/cost/latency benchmarks.
- Web UI, API router, Marketplace candidate flow, provider health projection.
- OAuth token rotation, credential broker production implementation, secret-manager integration.
