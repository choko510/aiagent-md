# MCP Adapter — Implementation Note (03-05 implementation)

> Status: **IMPLEMENTED_PARTIAL / CODE_READY (vertical slice)**. This document records what is in the
> repository, what was executed, and which integration gates remain. It does **not** promote the canonical
> Draft `docs/03-tools/mcp-adapter.md` to Locked and does not claim production verification.
>
> Base: `main` at `53ac14c5376509cef0293ed236bd417a6c8d2f6e` (2026-11-26). Branch:
> `feat/mcp-adapter-foundation`. Dependency: PyPI `mcp 2.3.0` (MIT, requires Python >=3.10; this workspace
> runs CPython 3.13).

## 1. What was built

The package `backend/packages/mcp_adapter` (`aiagent-mcp-adapter`) implements the bounded MCP
client/catalog half of the canonical adapter:

| Area | Modules | Behaviour |
|---|---|---|
| Admin configuration | `config.py` | HTTPS-only endpoint validation, loopback only in `AIAGENT_PROFILE=test`, hard ceilings, `MCP_ADAPTER_ENABLED` feature flag |
| Canonical identity | `canonical.py`, `contracts.py` | RFC 8785-style canonical JSON, SHA-256 digests, immutable `ToolRef` / `ToolEntry` / `RegistrySnapshot` |
| Strict schema | `schema_validation.py` | strict UTF-8/JSON, JSON Schema 2020-12 only, no external/dynamic refs, depth/property/regex bounds, output validation |
| Discovery | `discovery.py` | opaque-cursor pagination, page/tool/byte/time limits, duplicate/cursor-loop detection, `list_changed` invalidation, one atomic publish |
| Egress | `egress.py` | absolute-form HTTP through the existing forward proxy, `Proxy-Authorization: Aiagent tool_gateway:{permit}`, no redirect following in the adapter, HMAC denial proof check |
| SDK transport | `sdk_transport.py` | pinned `mcp 2.3.0` streamable HTTP client, explicit `ListToolsRequest` / `CallToolRequest`, bounded timeouts, session-loss and transport-error classification |
| Normalization | `normalization.py` | strips control characters, caps text, hashes binary content instead of inlining it, keeps resource links inert, validates `structuredContent` against the pinned output schema |
| Snapshot store | `registry_snapshot.py` | compare-and-swap publish, monotonic generation high-watermark, TTL, current-snapshot-only run pin, tombstoned pins, revoke, strict `ToolRef` resolution (provider, schema digest, risk class, policy revision, credential version, endpoint fingerprint) |
| Credentials | `credentials.py` | broker Protocol; production default refuses; static broker is explicitly test-only |
| Sessions | `session_manager.py` | fail-closed feature flag, provider enable/revoke, credential presence check, bounded sessions and admitted-call slots, one SDK session per operation |
| Telemetry | `telemetry.py` | bounded, secret-free event sink |

The Gateway-side adapter is `backend/packages/gateways/src/aiagent_gateways/mcp_executor.py`:
`McpToolExecutor` re-checks the pinned `ToolRef`, copies and hashes the exact arguments, validates them
against the pinned input schema, checks the current endpoint/policy/credential revision, and runs a
last-moment fence immediately before the single `tools/call` POST. Read failures return `ExecutorFailed`;
an `isError`, unsupported, truncated, empty or otherwise incomplete write response becomes
`ExecutorUnknown` so the existing side-effect ledger records UNKNOWN and never auto-retries.

## 2. Trust flow

```text
Admin config (endpoint, tenant/owner, credential binding ref)
  -> provider enable + feature flag + Credential Broker
  -> initialize / capability negotiation (tools required)
  -> bounded tools/list (all pages)
  -> strict schema + admin policy/risk assignment
  -> atomic Registry snapshot (generation, TTL)
  -> model sees only safe alias/description/schema (never credentials)
  -> Tool Gateway admission / approval / budget / intent / ledger
  -> McpToolExecutor resolves the pinned snapshot entry
  -> purpose-scoped permits for initialize/list/stream/close; the admitted permit only for the one call
  -> pre-send fence (deadline, revocation, pin, endpoint/policy/credential revision)
  -> MCP tools/call (single attempt)
  -> normalized result or FAILED/UNKNOWN (no provider-supplied receipt is trusted)
```

The MCP server's `serverInfo.name`, descriptions, annotations, `_meta`, resources and prompts are treated as
untrusted data. `annotations.readOnlyHint` never grants authority; risk class and approval are admin-assigned.

## 3. Implemented security properties

- **Feature flag is fail-closed.** `MCP_ADAPTER_ENABLED` must be `1`. `SessionManager` checks it on every
  session; a disabled provider, revoked provider, or missing credential never opens a socket.
- **Admin-owned destinations.** `ProviderConfig` is validated before use and is never derived from model
  input or MCP metadata. Plain HTTP and loopback are refused outside `AIAGENT_PROFILE=test`.
- **No direct SDK egress.** The httpx transport is replaced by `EgressHttpTransport`, which sends every
  request through `McpEgress` with a Sentinel-issued permit. The environment proxy is ignored and redirects
  are not followed by the adapter (a redirected request would need a new permit).
- **HTTPS deployment caveat.** The current forward-proxy contract is absolute-form HTTP and explicitly
  forbids `CONNECT` (canonical registry/marketplace dependency). The adapter does not terminate TLS and the
  local E2E fixture uses plain HTTP loopback. A production HTTPS MCP endpoint therefore requires a reviewed
  TLS-terminating egress connector that preserves the Sentinel permit and destination checks. That
  connector is **not** included here; do not point this adapter at a remote HTTPS provider until it exists.
- **No stdio.** Only `streamable_http` is accepted. There is no command, shell, Docker socket, SSH, or
  arbitrary local executable path in the provider model or call request.
- **No stale execution.** `SnapshotStore.resolve_pinned` checks generation, schema digest, policy revision,
  credential version, risk class and endpoint fingerprint against the run pin. A pin may only be created
  from the currently committed, unexpired snapshot; expired/revoked pins remain tombstoned and cannot be
  re-pinned. The executor re-checks the current provider config and pinned endpoint before opening a
  session and again at the actual-send fence. `tools/list_changed` is awaited and invalidates every pin
  for that provider, so an old ref cannot be dispatched after the notification.
- **No blind retry.** A 404/session loss or timeout after a write produces `ExecutorUnknown` for non-read
  tools. `query()` returns `None` because MCP has no generic receipt lookup; operators must reconcile.
- **No secret exposure.** Credentials exist only in `Credential` (repr suppressed) and are injected into the
  outbound HTTP header by the SDK transport. They are not placed in `ToolRef`, snapshots, telemetry, tool
  results, or model-visible metadata.
- **Metadata cannot grant approval.** MCP arguments are validated against the admin-approved schema, then
  admitted by the existing Gateway. An MCP `_meta` approval field is not read.
- **Bounded resources.** Pages, tools, listing bytes, schema bytes, regex length, argument bytes/depth/
  nodes/string/integer size, response bytes, text bytes, content blocks, sessions, concurrent admitted
  calls, timeouts and SSE event size all have hard ceilings.
- **Purpose-scoped permits.** The transport never reuses the admitted Gateway permit for initialization,
  notifications, list, GET stream or DELETE. Those requests receive a control permit with an explicit
  purpose; the admitted permit is accepted only for one exact `tools/call` whose tool name and canonical
  argument digest match the admitted binding.

## 4. Gateway integration status

`McpToolExecutor` is implemented and unit-tested, but it is **not yet registered into the production
Gateway execution path**. Two existing Gateway facts block a safe drop-in:

1. `ToolGateway.submit` resolves tools from the static `aiagent_domain.tools.TOOLS` registry and profile
   sets. MCP entries are tenant/owner-scoped and discovered at runtime; adding them to that global dict
   would expose one tenant's tool names to another tenant's model surface.
2. `ToolGateway._dispatch` calls `executor.destination_for_tool(tool, args, bound_form)` **before** it puts
   the admitted identity into `executor.context`. `McpToolExecutor` therefore cannot resolve the
   tenant-scoped entry/destination from the current callback. It deliberately raises
   `ExecutorFailed("mcp_destination_requires_admitted_identity")` instead of guessing an origin.

The integration layer must do one of the following under a separate reviewed change:

- resolve the tenant-scoped alias and admitted identity before destination resolution (and pass a bound
  request), then register a per-tenant dynamic tool source that is filtered before model exposure; or
- change Gateway ordering so `executor.context` is populated before `destination_for_tool`, and add a
  tenant-scoped dynamic registry rather than mutating global `TOOLS`.

Until that change lands, calling `McpToolExecutor.destination_for_tool` is **intentionally fail-closed**.
Do not work around this by calling the adapter directly from the model/runtime or by bypassing Sentinel.

## 5. Persistence status

`SnapshotStore` is an in-memory, process-local reference implementation with CAS/TTL/pin/revoke semantics.
It is adequate for the tested vertical slice but is **not** the production source of truth.

PostgreSQL persistence is **BLOCKED_ON_PR_122**:

- `origin/main` currently owns schema version 28; PR #122 owns migration `0029_autonomous_development`.
- This change must not add `0029`/`0030` to `backend/migrations` or edit shared migration numbering.
  The root `uv.lock` was regenerated only to add the new workspace package; resolve any conflict with
  PR #122 by regenerating, not by hand-editing dependency entries.
- The package-local DDL and tenant/owner RLS policies are therefore not present in this PR. No PostgreSQL
  or RLS test was executed. Do not claim RLS verified.

A follow-up must add package-local SQL and a numbered migration **after** PR #122 lands, with real
PostgreSQL tests for tenant/owner isolation, CAS publish, TTL expiry and pinned-run resolution.

## 6. Failure semantics

| Condition | Adapter result | Ledger effect for non-read |
|---|---|---|
| Feature flag/admin disabled, provider revoked | no connection; `ADAPTER_DISABLED` / `POLICY_DENIED` | no dispatch |
| Unsupported protocol revision, missing tools capability | fail closed before listing | no dispatch |
| Schema invalid / cursor loop / duplicate / page limit | quarantine or abort; no partial publish | no dispatch |
| Sentinel denies destination | `EGRESS_DENIED` | FAILED (no external send) |
| Connect/TLS/HTTP timeout before send | `CONNECT_FAILED` / `CONNECT_TIMEOUT` | FAILED |
| Timeout or reset after request bytes may have been sent | `TRANSPORT_TIMEOUT` / `TRANSPORT_RESET`, `effect_uncertain=true` | UNKNOWN |
| HTTP 404/session lost after call | `HTTP_SESSION_LOST`, `effect_uncertain=true` | UNKNOWN |
| Server returns `isError:true` on a read | `ExecutorFailed`; the failure is visible, not DONE | n/a |
| Server returns `isError:true`, empty, unsupported or truncated content on a write | `ExecutorUnknown`; no CONFIRMED settlement | UNKNOWN |
| Output schema mismatch | `OUTPUT_SCHEMA_MISMATCH`; no false business success | UNKNOWN when a write may have happened |
| Cancellation by the caller | propagate cancellation; do not convert a remote cancel into success | UNKNOWN if the write may have been sent |

## 7. How to run the slice

From `backend` on Windows (PowerShell):

```powershell
$env:PYTHONPATH='packages/mcp_adapter/src;packages/gateways/src;.'
.venv\Scripts\python.exe -m pytest tests/mcp_adapter -q
.venv\Scripts\python.exe -m ruff check packages/mcp_adapter/src tests/mcp_adapter
.venv\Scripts\python.exe -m mypy packages/mcp_adapter/src packages/gateways/src/aiagent_gateways/mcp_executor.py
```

The loopback SDK tests start a real `uvicorn` MCP server and a real absolute-form forward proxy on
`127.0.0.1`, then exercise initialize, paginated list, call, credential injection and stale-credential
refusal through the adapter transport. They do not contact an external service.

## 8. Test evidence

`75` adapter tests pass locally (Python 3.13, Windows). Detailed mapping is in
`docs/11-qa/mcp-adapter-verification.md`. This is local execution evidence only; it is not a production
verification or a remote-provider certification.

## 9. Known limitations / follow-ups

- Gateway registration and tenant-scoped dynamic tool surface (blocked as described in §4).
- PostgreSQL snapshot/policy persistence, RLS and migration (blocked on PR #122).
- Real `tools/list_changed` SSE delivery through a streaming proxy is not yet exercised; the current
  notification path is unit-tested with a callback. `EgressHttpTransport` buffers a complete response, so
  it cannot hold a long-lived GET SSE stream; a streaming egress connector is required for real
  server-initiated notifications.
- The generation high-watermark and pin tombstones are process-local. A restart loses them until the
  PostgreSQL-backed store is added; do not treat the in-memory store as a durable anti-replay boundary.
- The Gateway-to-executor admitted-context/destination port and the tenant-scoped dynamic model surface
  remain unimplemented; the executor is not registered in the production Gateway path.
- No real remote MCP/OAuth provider was contacted. The current egress path is absolute-form HTTP without
  TLS termination; a production HTTPS connector is a prerequisite. OAuth refresh/revocation and
  provider-specific reconciliation are not verified.
- No Web UI, Marketplace candidate flow, API router, or provider-health projection is included.
- No Kubernetes/KVM isolation test, load test, or cost benchmark.
- MCP resources/prompts/sampling/elicitation are intentionally not exposed.