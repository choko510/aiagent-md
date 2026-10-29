# MCP Adapter — Detailed Implementation Specification (03-05)

> CHAT_03 / canonical `docs/03-tools/mcp-adapter.md` / Draft [PROVISIONAL] / rev 0.2 / inspected 2026-10-30 JST / main `b45d4e989998663a00ef09673e69ae1f4191f00d`.
>
> Scope: externally provided MCP protocol tools to internal Registry/Gateway contract. **Not** generic unrestricted MCP deployment, not a new agent loop. Owners: tool policy/gateway CHAT_03, shared Tool IO CHAT_01, external connections CHAT_05 (provider integration), secrets/egress CHAT_09, DB CHAT_10, UI CHAT_08, QA CHAT_11.

## 0. 決定事項
MCPは**利用できる外部Tool APIのtransport/description protocolであり、それ自身が実行権限や安全分類を保証しない**。管理者が許可したMCP serverのみ接続。MCP `initialize`→capability negotiation→`tools/list`全ページ走査→strict validation→管理者allowlist・risk付与→Registry snapshotへのatomic publish。モデルには安全に整形したTool metadataだけを表示し、`tools/call`は**必ずTool Gateway**から実行する。

MCPの`annotations`、tool descriptions、server instructions、resources、prompts、JSON Schemaは**非信頼データ**。readOnlyHintで承認を省略せず、HTTPのredirectやOAuth callback経由でもLAN/metadataへアクセスさせない。MCP serverが任意コマンドを起動できる環境はPhase 1で採用せず、**LLMがstdio command/shell executableを指定する経路を禁止**。

Transport/sessionの性質：Transport再接続の成功は前の`tools/call`が失敗した証拠にならない。特に`404 session not found`などを受けた時、protocolに沿って新sessionへ再initializeできても、**過去のwrite callはBlind replay禁止**。Time-out/Cancellation/Progress通知は外部effectの取り消し完了ではない。

## 1. Goals, exclusions, use cases
**Goals**：静的Toolでは対応できない新しいデータソースや社内/クラウド公式APIをユーザーの明示許可で発見・呼出、dynamic `tools/list_changed`、信頼済みcredential binding、冷却/失効/復帰、output normalization、Session pinningを提供。
**Non-goals**：任意MCP server auto-install、外部server instructionsをsystem prompt化、ホストshell process、任意filesystem/SSH/Docker、無期限poll、TLS/SSRF保証の迂回、認証・CookieのLLM文脈露出、MCP protocol全機能（resources/prompts/completions/sampling/elicitation）の初期全面公開。

SC-05/06：Web researchやログイン済みscope、SC-07：crash/duplicate/sessions、SC-08：悪意あるMCP・egress、SC-09：latency/cost、SC-10：新候補ツール、SC-11：X eventからのread-only Tool利用、SC-13：Browser recipeがMCPを使ってもpolicy再検証、SC-15：抽出入力もUntrusted、SC-16：通知ツールの承認制御。

## 2. OSS比較（実コード・テスト・Issue）
| OSS / immutable commit / License | Inspected source/test, 2026-10-30 | Adoption & limits |
|---|---|---|
| MCP TypeScript SDK `39ae8e6192cf43273aaaf2de4169dfa9f7216e28` MIT/Apache-2.0移行中 | [`packages/client/src/client/client.ts::Client.listTools`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L2556-L2584)（ページ集約/cache）、[`_setupListChangedHandlers`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L777-L789)（list_changed→refresh）、[`callTool`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L2331-L2493)（outputSchema検証）、[`streamableHttp.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/streamableHttp.ts) transport。 | **Code verified (read)**。SDK採用候補だがwire/era/versionごとadapterで隔離。 |
| MCP TS SDK tests, same commit | [`test/e2e/scenarios/dynamic.test.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/dynamic.test.ts#L25-L69) dynamic tool、[`pagination.test.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/pagination.test.ts#L46-L116) opaque cursors、[`test/e2e/scenarios/protocol.test.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/protocol.test.ts#L77-L129) Abort/cancellation、[`responseCache.test.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/test/client/responseCache.test.ts#L424-L445) cache。 | **Test inspected, not run**。Notification loss/cache stale/transport crashは追加アプリテストで保証。 |
| Mastra `a24447357742cc9d0fa072dc6f14357a7c497446` Apache-2.0 non-ee | [`mastracode/sdk/src/mcp/manager.ts::createMcpManager/getTools`](https://github.com/mastra-ai/mastra/blob/a24447357742cc9d0fa072dc6f14357a7c497446/mastracode/sdk/src/mcp/manager.ts#L438-L479)の`listToolsetsWithErrors`/server-by-server statuses、[manager test](https://github.com/mastra-ai/mastra/blob/a24447357742cc9d0fa072dc6f14357a7c497446/mastracode/sdk/src/mcp/__tests__/manager.test.ts#L42-L165)のstdio/HTTP/Auth確認。 | **Code/test read**。Provider単位のstatus/partial failureは採用。`serverName_toolName` flat nameだけを権限識別子にするのは却下。 |
| PydanticAI `3809c1b861036197de4a06ae3ee38e27e5455100` MIT | [`FilteredToolset`](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/toolsets/filtered.py#L14-L33) のcontextual tool filtering、[tests](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_toolsets.py#L2924-L2944) のAsync Filter。 | **Code/test read**。MCP exposed catalogのrun-scope filteringの参考。Pydantic型やApproval wrapperをTS側Policy正本にしない。 |
| MCP normative Tools spec | [MCP tools specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) / [transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) | **Docs source**。公開protocol revisionの概念を参照するがSDK `39ae8e`内の2026-07-28 era branchは追加/相違があるためversion negotiate後に固定。 |
| Issues | [SDK #2232](https://github.com/modelcontextprotocol/typescript-sdk/issues/2232): stateless HTTP `list_changed` notification loss、[#3056](https://github.com/modelcontextprotocol/typescript-sdk/issues/3056): 404 session expiry reconnect gap、[PR #2668](https://github.com/modelcontextprotocol/typescript-sdk/pull/2668): cancellation on initialize fix proposal | **Issue/PR report**, 2026-10-30閲覧。外部caseの現行修正有無は独立確認が必要。 |

### Alternatives
A. **Raw MCP Tools directly in LLM/provider with no Gateway**：最短だがPolicy/Schema/Approval/Secret/Result normalizationを迂回 → **却下**。
B. **Custom raw JSON-RPC implementation**：柔軟でもprotocol drift/streaming/SSE/mixed revisions/validation負担が大きい → **原則却下**、SDK不適合が確認されれば限定実装。
C. **Pinned MCP SDK library behind `McpTransportPort` + controlled Registry/Gateway**：library updatesに影響を封じ込めやすい → **採用候補**。新version/era compatibility検証前に本番導入版固定はしない。
D. **stdio MCP executable spawning by Agent**：host code execution不可の製品要件に反する → **禁止**。事前検証済み専用sidecarサービスのadmin起動は将来ADR/CHAT_09審査。

## 3. Dependencies and trust ownership
~~~text
Admin-configured Provider -> CHAT_09 endpoint/secret policy
           ↓                 (private-network deny / cert / DNS/redirect)
McpTransportPort (CHAT_03) -- handshake, negotiated capabilities, session
           ↓
McpCatalogImporter (CHAT_03) -- all pages, bounded, schema strict
           ↓
ToolRegistry (03-01) -- stable providerId/sourceName, digest, actor filtering
           ↓
Agent Loop (CHAT_02) -- opaque ToolRef proposal
           ↓
Gateway (03-02) -- validate/policy/approval/budget/intent + ledger
           ↓
McpToolExecutor (CHAT_03) -- callTool only with approved scoped binding
           ↓
ResultNormalizer/Artifact/Evidence (CHAT_01/10)
~~~

CHAT_01 owns shared Tool IO/Event/Error; CHAT_09 owns principal, capability, OAuth/secret handles and egress; CHAT_10 owns any DB; CHAT_08 owns REST UI/Telegram; CHAT_05 owns domain-specific provider adapters (e.g. X/Keepa). MCP server's self-reported serverInfo.name is **not trusted namespace identity**. Administrator-assigned `providerId` + immutable endpoint/credential binding + source tool name are unique.

Server connections:
- single actor/tenant per credential/session profile by default; no shared cross-user cookie/session/Authorization. Host service that multiplexes tenants MUST maintain authenticated per-request scope and be separately reviewed; Phase 1 default = isolated per scope.
- Registry discovery doesn't expose Server credentials, token source paths or raw URI query secrets. Tool arguments referencing BrowserSession must be validated by Browser Worker owner, not MCP process.
- Resource/prompt MCP channels are not Tool permissions. If later enabled, each gets data-only schema/provenance/policy, never instruction authority.

## 4. Requirements
| REQ | Level | Observable acceptance | SC |
|---|---|---|---|
| MCP-001 | MUST | MCP discovery/execute connects only admin-enabled vetted endpoints and authenticated tenant-specific credentials | 06,08 |
| MCP-002 | MUST | `initialize`+capability negotiation must precede Tool operations; unsupported/no tools -> no exposure | 05,08 |
| MCP-003 | MUST | `tools/list` pagination fully traversed with opaque cursor and finite limits; atomic publish on success only | 05,07 |
| MCP-004 | MUST | Source tool/schema/annotations validated, admin-assigned namespace and classification; bad tool quarantined | 08 |
| MCP-005 | MUST | Cache/list_changed/reconnect/TTL never allow stale tool revision to execute | 07,08 |
| MCP-006 | MUST | MCP `tools/call` only through Gateway after fresh auth/approval/lease/budget and side-effect ledger | 07,08 |
| MCP-007 | MUST | output Content/structuredContent and `isError` normalized without false success or secret leak | 05,07,08 |
| MCP-008 | MUST | transport reconnect/HTTP 404/session reset never blindly resend potentially side-effectful Tool request | 07 |
| MCP-009 | MUST | cancellations/timeouts/progress do not certify external effect absence; UNCERTAIN when appropriate | 07 |
| MCP-010 | MUST | HTTP transport SSRF/DNS rebinding/redirect/link-local checks and Secret isolation identical to Fetch | 08 |
| MCP-011 | MUST | Malicious server metadata cannot inject policy or elevate role; hidden private tools never shown | 08 |
| MCP-012 | MUST | Provider-level health/needs-auth/degraded/quarantine and explicit partial task visibility | 03,05,07 |
| MCP-013 | MUST | finite request/response/tool count/pages/timeout/rate/cost and no unbounded SSE connection | 09 |
| MCP-014 | MUST | unknown major schema/SDK era/client transport mismatch fail closed, feature flag rollout | 07,08 |
| MCP-015 | MUST | telemetry stores only safe refs/latency/tool revision/error; auth headers excluded | 08,09 |
| MCP-016 | MUST | Event/X/learned Browser/attachment Task MCP tools use identical authorization, no ambient server privileges | 11,13,15 |
| MCP-017 | SHOULD | bounded reconcileable health check/TTL refresh even if `list_changed` absent | 07 |
| MCP-018 | SHOULD | `outputSchema` strict validation support with fallback to explicit Unknown, not acceptance as arbitrary success | 05,08 |
| MCP-019 | EXPERIMENT | HTTP transport version and SSE reconnection strategy measured on real auth/provider fixtures | 09 |

## 5. Local TypeScript Ports / strict validation
~~~ts
type ProviderId=string;               // admin assigned stable, NOT serverInfo.name
type ScopedSecretRef=string;          // vaulted, never provided to LLM
type MCPProtocolRevision=string;      // negotiated and stored per session
type MCPToolName=string;              // preserve exact case sensitive source name
interface MCPServerConfig {
  providerId:ProviderId; tenantScopeRef:string;
  endpointRef:string; // admin approved URI, not LLM value
  credentialBindingRef:ScopedSecretRef;
  transport:"streamable_http"; // stdio disabled for initial phase
  enabled:boolean; allowedToolNames:readonly MCPToolName[];
  maxTools:number; maxBytes:number; maxRequestMs:number;
}
interface McpSession { providerId:ProviderId; tenantScopeRef:string;
  sessionRef:string; protocolRevision:MCPProtocolRevision;
  capabilities:Readonly<Record<string,unknown>>;
  connectedAt:string; sessionGeneration:string; credentialVersion:string; }
interface RawMcpTool { name:string; description?:string;
  inputSchema:unknown; outputSchema?:unknown; annotations?:unknown;
  _meta?:unknown; }
interface RegistryImportedTool { sourceName:string; toolId:string;
  inputSchema:Readonly<unknown>; outputSchema?:Readonly<unknown>;
  descriptorRevision:string; adminRiskRef:string; }
interface McpPort {
  connectApproved(config:MCPServerConfig, signal:AbortSignal):Promise<McpSession>;
  listPage(session:McpSession, cursor?:string):Promise<{tools:RawMcpTool[];nextCursor?:string}>;
  callTool(session:McpSession, name:MCPToolName,args:unknown,
           signal:AbortSignal):Promise<unknown>;
  disconnect(session:McpSession):Promise<void>;
}
~~~

Only local types. UUIDv7 identity and EntityIds belong CHAT_01. Protocol 2026-07-28 and older 2025-11-25 may differ; store actual negotiated version in `sessionGeneration`, refuse unsupported, and do **not** derive grant from version. Session Id is not browser session ID. `ToolRef` is model opaque; actual server Tool name recovered from trusted Registry entry after revision check. Replacing same name after `list_changed` yields new revision; never execute old `ToolRef` on changed schema.

Validation:
- `name`: UTF-8/Unicode length bound, safe canonical identity mapping to alias, no name collision override; duplicate source names within same provider => reject list.
- JSON Schema: declared dialect or known supported subset, no arbitrary external `$ref`, bounded depth/bytes/keys/regex compile. Strip unauthorized extensions only for display if meaning unchanged; else quarantine. `annotations.readOnlyHint`, destructive/idempotent/openWorld metadata are *hints* and never risk policy.
- `tools/list` pages: max 64 cursor hops, 128 Tools/provider, total page payload 1MiB, request timeout 5s, cursor retained as opaque string; limits are initial suggested values and subject to CHAT_11 benchmarks.
- Transport: approved HTTPS origin host/SNI/certificate, DNS resolution and every redirect revalidated by egress proxy; forbid link-local/loopback/metadata/private ranges unless narrow explicit trusted service policy via CHAT_09. Block suspicious OAuth redirect to local servers by default. No HTTP arbitrary header from Tool description.
- Schema result: `content` array and `structuredContent` are data; `isError:true` means Tool-reported error, **not** necessarily operation effect absent; schema invalid → explicit `OUTPUT_SCHEMA_MISMATCH`, preserve raw restricted Artifact if permitted.

## 6. Session lifecycle and sequence
~~~mermaid
stateDiagram-v2
  [*] --> DISABLED
  DISABLED --> CONNECTING: admin enable
  CONNECTING --> NEGOTIATING: HTTPS/TLS/auth success
  NEGOTIATING --> LISTING: negotiated tools capability
  LISTING --> READY: full bounded list + publish
  CONNECTING --> NEEDS_AUTH: 401/token unavailable
  CONNECTING --> UNAVAILABLE: timeout/dns/network
  NEGOTIATING --> QUARANTINED: unsupported rev/schema/hostile
  READY --> REFRESHING: list_changed/ttl/manual
  REFRESHING --> READY: validated generation CAS
  READY --> DEGRADED: session 404/transport reset
  DEGRADED --> CONNECTING: bounded new session
  READY --> DISABLED: admin disable / revoke
  DEGRADED --> DISABLED: admin revoke
~~~

~~~mermaid
sequenceDiagram
  participant A as Admin
  participant S as MCP Adapter
  participant R as Registry
  participant G as Gateway
  participant M as MCP Server
  A->>S: enable provider (trusted config+secret handle)
  S->>M: TLS, initialize, capability negotiation
  M-->>S: negotiated revision + tools capability
  loop bounded cursor until end
    S->>M: tools/list (opaque cursor)
    M-->>S: list items,nextCursor
  end
  S->>S: verify schemas, admin allowlist, risk
  S->>R: atomic registry generation publish
  R-->>G: ToolRef+revision binding
  G->>G: authorize/budget/approval/ledger/fence
  G->>S: invoke approved source name + scoped args
  S->>M: tools/call
  M-->>S: result | protocol error | disconnect
  S-->>G: normalized outcome or UNCERTAIN
~~~

Request timeouts are separate for handshake, listing and per-call; all finite. Disconnect after invocation does not prove server cancellation. For `notifications/tools/list_changed`, update happens asynchronously in Registry control plane; **old ToolRefs fail execution** on revision mismatch or revoked capability. Notification may be lost, so TTL/refresh is required. A degraded Provider cannot be treated as "up to date" merely because cache exists.

## 7. Algorithms / pseudocode
~~~ts
async function refreshProvider(providerId:string, expectedEpoch:string) {
  const cfg=await adminProviderStore.load(providerId);
  if (!cfg.enabled) return disable(providerId);
  await egress.validateEndpointAndCredentialScope(cfg);
  const lease=await registryLease.acquire(providerId); // fencing token, bounded
  try {
    const session=await transport.connectApproved(cfg,deadlineSignal(5000));
    if (!session.capabilities.tools) return registry.quarantine(providerId,"NO_TOOLS_CAPABILITY");
    let cursor: string|undefined=undefined, page=0, bytes=0;
    const seen=new Set<string>(), all:RawMcpTool[]=[];
    while(true) {
      if (++page > 64) throw new UnsupportedPagination();
      const p=await transport.listPage(session,cursor);
      bytes+=byteLengthUtf8(p);
      if (bytes>1024*1024 || all.length+p.tools.length>128) throw LimitExceeded();
      all.push(...p.tools);
      if (!p.nextCursor) break;
      if(seen.has(p.nextCursor)) throw CursorLoop();
      seen.add(p.nextCursor);
      cursor=p.nextCursor; // opaque; never decode or parse
    }
    const validated=validateAllAndSelectAdminAllowed(all,cfg.allowedToolNames);
    await registry.publishWithFencing(providerId,expectedEpoch,lease,validated,
      session.protocolRevision);
    return {state:"READY",tools:validated.length};
  }catch(e) {
    await registry.markDegradedOrQuarantined(providerId,redact(e));
    return {state:"DEGRADED",retryable:isMetadataRetryable(e)};
  }finally{ await lease.release(); }
}
async function executeMcpTool(boundGatewayAttempt) {
  // Called only by Gateway after durable mayHaveSent and approval/lease checks.
  const entry=await registry.resolveCurrent(boundGatewayAttempt.toolRef);
  assertSameOwnerPolicyRevisionAndAllowedTool(boundGatewayAttempt,entry);
  const scopedSession=await sessionManager.getApprovedSession(entry.providerId,
      boundGatewayAttempt.tenantScopeRef);
  const args=await validateJSONAgainstPinnedInputSchema(entry,boundGatewayAttempt.argsRef);
  try {
    const raw=await transport.callTool(scopedSession,entry.sourceName,args,
      boundGatewayAttempt.abortSignal);
    return normalizeMcpResult(raw,entry.outputSchema);
  }catch(err) {
    // Any failure after dispatch may be a remote side effect.
    return classifyForLedger(err,boundGatewayAttempt.effectClass); // UNCERTAIN on writes
  }
}
async function handleSession404(providerId) {
  await sessionStore.markDegradedAndInvalidate(providerId);
  await refreshProvider(providerId,await policyEpoch.now());
  // IMPORTANT: DO NOT replay old tools/call here.
  await ledger.reconcileMayHaveSent(providerId);
}
~~~

**Session 404 caveat**：SDK issue #3056 reports a missing re-initialization in a particular version. Protocol’s new-session handshake and app-level retry semantics are distinct. Even when adapter does reinitialize, it must **not** automatically resend nonidempotent Tool call. If an SDK auto retries internally, wrapper must disable/intercept it for external-effect tools or deem SDK unsuitable.

**Notification race**：`list_changed` during `tools/list` can make assembled pages inconsistent; initialize a generation token and if notification observed before full publication force another bounded full refresh, not mixed collection. On repeated changes, rate-limit and fail closed rather than loop forever. `list_changed` alone does not prove current catalog changed, only invalidates cache. Remote risk metadata doesn't control privilege.

## 8. Failure matrix
| Failure / detection | Recovery | External effect / user status |
|---|---|---|
| no tools capability / invalid `initialize` | QUARANTINED/UNAVAILABLE | Tool not shown |
| 401/expired OAuth | NEEDS_AUTH; owner re-auth via CHAT_09 UI | no credential in prompt/log |
| DNS rebinding / private redirect | egress denies before connect and on each hop | security audit; no connection |
| paginated cursor loop / large schema | abort listing; quarantine tool/provider | no partial new generation |
| `list_changed` lost on stateless transport | periodic bounded full refresh | stale catalog not treated as fresh |
| `list_changed` during refresh | invalidate generation and rescan, finite attempts | no mixed generation |
| stale executor/sourceName schema change | Gateway refuses old ToolRef | explicit rediscovery |
| 404 stale `Mcp-Session-Id` | drop session+reinitialize, no Tool replay | in-flight write UNCERTAIN |
| HTTP 500 after write | Ledger may-have-sent | UNCERTAIN; status inquiry |
| cancellation/progress lost | local abort, no guarantee remote stopped | UNCERTAIN where effect possible |
| server returned `isError:true` | distinguish Tool business error from transport | may remain UNCERTAIN |
| `structuredContent` null/0/false | validate against outputSchema, not "missing" | valid scalar if schema permits |
| tool response > max bytes | bounded raw Artifact/explicit truncate | PARTIAL not success without proof |
| duplicate Tool names across providers | admin stable namespaced identity | no ambiguity |
| multiple tenants share server reported ID | tenant-specific credential/session store | cross-tenant result forbidden |
| restart mid-list or run | discard uncommitted list, reopen bounded; ledger reconcile call | no blind retry |
| credential rotation after discovery | invalidate old session+ToolRef, require fresh auth | no stale scope reuse |
| stdio executable request from model | forbidden | no host process launched |

## 9. Security, audit, privacy
Adapter runs in egress-controlled Worker with no host shell, Docker socket, private filesystem read/write, SSH, arbitrary container privileges. TLS and redirect check handled by CHAT_09 even for backend-initiated handshake/status check. SSRF: allowlist by admin, block private/link-local/metadata and DNS rebinding; no user URL or MCP server prompts can change endpoint origin. Credentials via vaulted bindings injected directly into transport, never model args. OAuth refresh token rotation/expiry scoped to actual tenant/user; reuse prohibited.

ServerTool descriptions are **data only**; strip or escape malicious "system instruction" for model display and enforce length limit. Any remote Resource/Prompt exposure requires separate ADR; otherwise ignore. MCP tool calls with an inputSchema that allows arbitrary URLs or command text are **not automatically safe**; Gateway must inspect normalized operation target/egress/risk classification. Tool results may include secret-bearing links; redact before model/telemetry.

Audit (no secret): `providerId, tenantRefHash, sessionGeneration, protocolRevision, toolNameHash, descriptorRevision, invocationIdHash, requestIdHash, outcomeClass, errorKind, duration, bytes, cursorPages, capturedAt`. Task provenance/observation time available via CHAT_01 Event/Evidence. Never log Authorization/Cookie/client secret/callback code in raw MCP frames.

## 10. Bounds & observability
Proposed ceilings (to be benchmarked): at most 128 tools/provider, 64 list pages, 1MiB total listing bytes, 64KiB per schema, 5s handshake and list per provider, 10s default call (bounded by Task/provider), 3 reconnect attempts with exponential backoff, max 2 concurrent calls/provider/session, max 16KiB text delivered to model per call with Artifact ref for larger content, session TTL at most 30min without activity and periodic health refresh. Client stream/SSE must have idle deadline, reconnect budget, no infinite await. User operations may legitimately require longer bounded limits subject to explicit provider policy.

Metric dimensions: `mcp_connect_ms`, `mcp_list_pages`, `mcp_list_changed_total`, `mcp_invalid_schema_total`, `mcp_output_schema_mismatch_total`, `mcp_reconnect_total`, `mcp_session_404_total`, `mcp_tool_transport_error_total`, `mcp_stale_ref_denied_total`, `mcp_provider_active_sessions`, `mcp_tool_bytes`. Provider/tenant IDs as protected traces not unbounded metric tags.

Benchmark setup: same mocked MCP server/version/transport, 1/10/100tools, 2 tenants, 1/4 workers, inject 401/404/cursor loop/list_changed/response huge; measure p50/p95, CPU/RSS, per-call tokens/bytes, denied calls, cost and false-positive success. Do not assume SDK benchmarks transfer to Harness.

## 11. Persistence/Transactions/Migration [PROVISIONAL: CHAT_10]
Logical stores: `mcp_provider` admin config+tenant binding/endpointRef, `mcp_session` owner+credential version+transport/protocol/session generation+status (no tokens), `mcp_discovery_revision` tool descriptor/schema digest, `registry_generation` pinned snapshot/expiry, `mcp_health` state and errors. All durable writes use tenant-qualified lookup and CAS. No raw credentials, cookies or signed URLs in Postgres logs/events. `list_changed` refresh publishes only once per complete validated generation; invalid scan cannot overwrite the good generation. Runtime restart invalidates expired sessions and searches, but in-flight calls recovered **only through side-effect ledger**.

**Contract P0**：CHAT_10 workspace_id must map authenticated tenantId/ownerUserId, not assume equality; DB `gen_random_uuid()` default v4 must not create public CHAT_01 UUIDv7, `revision 0` must map correctly to `recordVersion 1`; `Run.leaseEpoch` and Task.activeRunId/goalRevision need durable fence for execute. MCP `sessionGeneration` is **not** Run leaseEpoch or BrowserSession lease. Task may lack Conversation for scheduled/X trigger. Proposal references owner, migration, integration tests below.

## 12. UI/REST proposals [PROVISIONAL: CHAT_08]
Admin: provider list with approved endpoint (redacted), OAuth needs-auth, enable/disable, tool inventory, schema/permission change warning, last list successful time, credentials revoked, test-only connection status. User: list permitted tool names only, no disallowed schema existence leaks. `MCP_SESSION_EXPIRED` communicates reconnection without auto replay; `UNCERTAIN_EFFECT` displays user-visible followup. API endpoint URLs proposed `GET /providers/{id}`, `POST /providers/{id}/refresh` admin-only, `GET /tool-catalog` actor-filtered. Public REST version owned by CHAT_08.

## 13. Implementation PR phases
M1 MCP SDK adapter transport/auth handshake/egress under feature flag, integration with fake stateless/stateful servers. M2 bounded list, schema normalization, atomic Registry generation and notification invalidation. M3 Gateway executor only (ToolRef pinned, outputSchema/artifact, no auto write retry), effect ledger integration. M4 session rotation/404/reinit, policy revoke and crash/restart tests. M5 ops dashboard and benchmark/security, docs for supported protocol revisions and disabled stdio mode. Any SDK upgrade must run conformance and high-risk replay tests before deployment.

## 14. Tests (designed, not executed)
| TEST | Setup→Action | Expected |
|---|---|---|
| MCP-T01 | Unapproved MCP URL in model prompt→connect | 0 connection, audit deny |
| MCP-T02 | Server without tools capability→list | no tools exposed |
| MCP-T03 | Three pages with arbitrary opaque cursors→list | all pages traversed exactly, one publish |
| MCP-T04 | Cursor loop / duplicate toolName / malformed schema | bounded abort/quarantine, no partial publish |
| MCP-T05 | Malicious descriptions/MCP annotations with fake auth scope→import | no policy alteration, safe excerpts |
| MCP-T06 | Two admin scopes same reported serverInfo.name | distinct namespace/credentials, no data mixing |
| MCP-T07 | `list_changed`→active Run old ToolRef execute | revision mismatch denied |
| MCP-T08 | Lost notification→TTL refresh | stale entry detected, no stale execution |
| MCP-T09 | list_changed concurrently mid-list→publish | no mixed list generation |
| MCP-T10 | Remote provider disabled / OAuth revoked mid-call | no new calls, in-flight reconciled |
| MCP-T11 | MCP 404 session lost during read → reconnect | fresh initialize, no unbounded retry |
| MCP-T12 | MCP 404 session lost after write send → reconnect | UNCERTAIN; no automatic replay |
| MCP-T13 | Tool returns isError:true after potential write | not incorrectly marked externally failed |
| MCP-T14 | Tool structuredContent=0/false/null with matching schema | valid data where permitted |
| MCP-T15 | output schema compile error/unknown dialect | no false business success |
| MCP-T16 | malicious redirect/DNS rebinding IPv6 localhost | denied with safe audit |
| MCP-T17 | Inject approval field into MCP `_meta` | Gateway ignores; needs real approval |
| MCP-T18 | Abort/progress frames/timeouts before and after dispatch | safe local stop, remote uncertainty preserved |
| MCP-T19 | Worker kill between list/cache and publish | last committed generation valid |
| MCP-T20 | Worker kill after remote success before outcome commit | Ledger UNCERTAIN, status inquiry |
| MCP-T21 | Authenticated Actor B receives Actor A's ref | cross-tenant denied |
| MCP-T22 | X event/Recipe/Attachment content creates dynamic tool | no unauthorized Tool registry modification |
| MCP-T23 | Fake SSE request never terminates | bounded idle/overall deadline |
| MCP-T24 | stress 2 tenants x100 tools x 4 workers | p50/p95, bytes, CPU, errors measured; no isolation breach |
| MCP-T25 | Unsupported negotiated version while rolling upgrade | fail closed/reconnect with compatible adapter only |

## 15. Traceability
| REQ | Tests | SC | Acceptance |
|---|---|---|---|
| MCP-001 | T01,T06,T21 | 06,08 | unapproved connections 0 |
| MCP-002 | T02,T25 | 05,08 | negotiation verified |
| MCP-003 | T03,T04 | 05,07 | all valid pages or no publish |
| MCP-004 | T04,T05,T06 | 08 | untrusted metadata cannot grant |
| MCP-005 | T07,T08,T09 | 07,08 | stale ref executions 0 |
| MCP-006 | T10,T17,T20 | 07,08 | all tools through Gateway |
| MCP-007 | T13,T14,T15 | 05,07,08 | structured output checked |
| MCP-008 | T11,T12 | 07 | remote write replay 0 |
| MCP-009 | T18,T20 | 07 | cancel != confirmed undo |
| MCP-010 | T01,T16 | 08 | deny SSRF and private |
| MCP-011 | T05,T17,T22 | 08 | policy unchanged by metadata |
| MCP-012 | T02,T10,T11 | 03,05,07 | health visible / partial |
| MCP-013 | T04,T23,T24 | 09 | finite resources |
| MCP-014 | T15,T25 | 07,08 | unknown version blocked |
| MCP-015 | T17,T21,T24 | 08,09 | log secret/PII 0 |
| MCP-016 | T17,T22 | 11,13,15 | no alternate privilege route |

## 16. Cross-team proposal and remaining uncertainty
| ID | Owner priority | Requested decision / impact / test |
|---|---|---|
| MCP-X01 | CHAT_01 P0 | Tool IO schema version, opaque ToolRef vs UUIDv7 Entity IDs, transport error vs business isError vs UNCERTAIN; MCP-T12/T14 |
| MCP-X02 | CHAT_09 P0 | admin endpoint allowlist TLS/OAuth tenant secret scope/redirect/DNS/fresh revocation; MCP-T01/T16/T21 |
| MCP-X03 | CHAT_10 P0 | Registry/provider/session revision storage & transactional publish, tenant-workspace mapping, UUIDv7/recordVersion/leaseEpoch; MCP-T09/T19 |
| MCP-X04 | CHAT_03 (03-01/02/04) P0 | schema fingerprint/ToolRef stale, no direct adapter execute, side-effect uncertainty and status inquiry; MCP-T07/T20 |
| MCP-X05 | CHAT_05 P1 | domain provider MCP-vs-native policy, OAuth version/rate/terms, SDK transport endpoint validation; MCP-T10 |
| MCP-X06 | CHAT_08 P1 | Provider admin auth status, reauth and error projections; MCP-T10 |
| MCP-X07 | CHAT_11 P1 | MCP Protocol conformance matrix incl HTTP 404/notifications/drop, cost/benchmark; MCP-T25 |
| MCP-X08 | CHAT_02 P1 | Run cancel/pause/fencing and safe numeric lease codec with MCP session reconnect; MCP-T18 |
| MCP-X09 | CHAT_13/14/15 P1 | X trigger, saved recipe, file embedded tool metadata are data; MCP-T22 |
| MCP-X10 | CHAT_00 P2 | Any use of trusted isolated stdio MCP as admin opt-in requires ADR, never host command by Agent |

## 17. Evidence ledger
- [S1] MCP SDK [`Client.listTools`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L2556-L2584), [`callTool`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L2331-L2493) and [StreamableHTTP](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/streamableHttp.ts), commit `39ae8e6`, **Code verified by reading** 2026-10-30, LICENSE mixed MIT/Apache-2.0 transition.
- [S2] MCP SDK [dynamic](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/dynamic.test.ts), [pagination](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/pagination.test.ts), [protocol cancellation](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/protocol.test.ts) tests — **test inspected, not executed**.
- [S3] Mastra [MCP manager](https://github.com/mastra-ai/mastra/blob/a24447357742cc9d0fa072dc6f14357a7c497446/mastracode/sdk/src/mcp/manager.ts#L438-L479), [manager tests](https://github.com/mastra-ai/mastra/blob/a24447357742cc9d0fa072dc6f14357a7c497446/mastracode/sdk/src/mcp/__tests__/manager.test.ts), `a244473`, **Code/test inspected**, Apache-2.0 for non-ee paths (ee separate).
- [S4] PydanticAI [FilteredToolset](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/toolsets/filtered.py), `3809c1b`, MIT, **Code verified**.
- [S5] [SDK issue #2232](https://github.com/modelcontextprotocol/typescript-sdk/issues/2232), [#3056](https://github.com/modelcontextprotocol/typescript-sdk/issues/3056), [SDK PR #2668](https://github.com/modelcontextprotocol/typescript-sdk/pull/2668), **Issue/PR reports**, 2026-10-30, limitations not independently reproduced.
- [S6] [MCP Tools normative docs](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [Project brief](../00-foundation/project-brief.md), [SC-01..16](../00-foundation/acceptance-scenarios.md). Our transport/TTL/security caps are **design hypothesis**, no runtime tests performed.
