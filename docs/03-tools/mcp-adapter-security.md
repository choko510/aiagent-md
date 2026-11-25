# MCP Adapter — Security Note

> Draft implementation note. Status: **IMPLEMENTED_PARTIAL**. Companion to
> `docs/03-tools/mcp-adapter.md` (canonical Draft) and `mcp-adapter-implementation.md`.

## 1. Security invariants applied

1. **MCP metadata is data, not authority.** `serverInfo.name`, tool descriptions, annotations, `_meta`,
   resources, prompts and JSON Schema never change tenant scope, risk class, approval policy or credentials.
   Risk and approval are assigned by the admin policy layer (`policy.py`) and enforced again by the Gateway.
2. **Admin-owned endpoint and credential binding.** A model cannot name a URL, header, token, command or
   stdio executable. `ProviderConfig` accepts only admin configuration; `parse_endpoint` requires HTTPS and
   rejects loopback/link-local/private literals unless the test-only flag is set.
3. **All egress through Sentinel.** The MCP SDK does not get a raw network transport. `EgressHttpTransport`
   sends an absolute-form request through `ForwardProxyEgress` with the Sentinel permit. The environment
   proxy is ignored; the adapter never opens a direct socket to the MCP origin. The existing forward-proxy
   contract forbids `CONNECT` and this adapter does not terminate TLS, so a remote HTTPS provider needs a
   reviewed TLS-terminating egress connector. Until then, this is a loopback/HTTP test slice only.
   The admitted Gateway permit is handed into the transport and spent for that call; the transport does not
   mint a fresh provider-scoped permit (`test_sdk_protocol` / `test_executor`).
   Request bodies are bounded by `AdapterLimits.max_request_bytes` before egress; oversized calls fail
   before any byte is sent.
4. **All calls through the Tool Gateway.** `McpToolExecutor` accepts only an already-admitted `effect_key`,
   a pinned `ToolRef`, a permit, and arguments whose canonical digest matches the admitted request. It
   cannot be invoked safely by the runtime directly.
5. **No self-approval.** `CallBinding` carries the Gateway-issued permit and intent; MCP tool arguments are
   schema-validated but any embedded approval/role field is inert.
6. **No stdio / host execution.** Only `transport="streamable_http"` exists. No subprocess, shell, Docker
   socket, SSH, arbitrary file path or package auto-install is reachable from this package.
7. **No secret to the model.** Credentials are resolved inside the adapter process/worker, held in
   `Credential` with `repr=False`, and injected only into the outbound header. Telemetry records safe refs
   and codes, never header values, tokens, cookies or raw URLs with secrets.

## 2. Threat model and controls

| Threat | Control | Evidence / gap |
|---|---|---|
| Model supplies a malicious MCP URL | Provider endpoint is admin config; no endpoint field in call arguments | config tests / code review |
| SSRF to loopback, link-local, metadata, private ranges | `parse_endpoint` and `parse_target` reject non-public addresses; loopback only under `AIAGENT_PROFILE=test` | unit tests; DNS rebinding at the proxy is delegated to existing egress |
| Redirect to another host or plaintext | adapter does not follow redirects; every request needs a fresh permit and allowlisted origin | unit tests; real redirect chain not exercised |
| HTTPS downgrade / no TLS to remote origin | existing egress forbids CONNECT; adapter needs a TLS-terminating trusted connector | **BLOCKED**; loopback HTTP only |
| Cross-tenant tool or credential reuse | snapshot/entry/provider checks tenant, owner and provider; run pin checks generation | unit tests; no PostgreSQL RLS test yet |
| Stale ToolRef after `list_changed` / TTL | pinned resolution rejects changed schema, risk, policy revision, credential version; list_changed invalidates pending discovery | unit tests; real SSE notification not exercised |
| Malicious schema / regex / deep JSON | strict 2020-12 subset, no external `$ref`, depth/property/node/regex bounds, byte caps | unit tests |
| Prompt injection in description/server instructions | descriptions sanitized and length-capped; instructions only digested, never promoted to system prompt | code review / unit tests |
| Oversized or malformed result | response, text, block and SSE byte caps; binary hashed; invalid structured output rejected | unit tests |
| Secret leak through error/log | safe error details; telemetry bounded; credential repr suppressed | code review |
| Blind retry after uncertain write | executor maps uncertainty to `ExecutorUnknown`; Gateway records UNKNOWN; no auto-retry | executor tests |
| Caller cancellation interpreted as remote cancel | cancellation propagates; adapter never reports success or "not sent" from a local cancel | code review; integration gap for real remote cancel |
| Tool description injects approval | `_meta`/annotations ignored for authority; approval only from Gateway | no test currently asserts an injected `_meta` field is ignored end-to-end; code review |
| Host shell / stdio | no stdio transport or command field | code review |
| Feature enabled accidentally | `MCP_ADAPTER_ENABLED` defaults off; provider `enabled` also required | session manager tests |

## 3. Credential handling

`CredentialBroker` is a Protocol. Production must provide a broker backed by the existing Credential Broker
trust boundary. `StaticCredentialBroker` is test-only and must not be wired in production.
`RefusingCredentialBroker` is the safe default.

The adapter checks the credential version against the admin configuration and refuses a stale version. It
does not write credentials to snapshots, tool refs, events, telemetry, result payloads or model context. The
current slice does not implement OAuth refresh/rotation; a revoked upstream token can only fail closed until
the broker supplies a new version.

## 4. Authorization boundaries that remain outside this package

- Approval, budget, run/lease/fence, goal checks, intent idempotency, side-effect ledger and final admission
  stay in `ToolGateway`. The adapter cannot approve, budget or fence a call.
- Sentinel owns the destination decision and permit. The adapter checks the origin again as defense in
  depth, but the proxy/Sentinel remains the enforcement point.
- The model surface must be built from an admin-approved, tenant-filtered catalog. The current Gateway uses
  a global static `TOOLS` dict, so exposing runtime MCP entries without a tenant-scoped resolver would leak
  names across tenants. That is why registration is not enabled in this change.

## 5. Production enablement gates

Do not set `MCP_ADAPTER_ENABLED=1` in production until all of the following are true:

1. Tenant-scoped dynamic tool registration and model-surface filtering are implemented and reviewed.
2. Gateway ordering/bound-request integration makes `destination_for_tool` deterministic.
3. PostgreSQL snapshot/policy persistence with tenant/owner RLS is migrated after PR #122 and tested
   against a real PostgreSQL instance.
4. A real remote provider (TLS, auth, revocation, 404/session-loss, SSE) is exercised in staging.
5. `tools/list_changed` and notification-loss paths are verified through a streaming SSE proxy.
6. Security review covers prompt injection, tool-name collision, redirect/DNS rebinding and log redaction.
7. A kill/restart test proves that an UNKNOWN write is not replayed automatically.
