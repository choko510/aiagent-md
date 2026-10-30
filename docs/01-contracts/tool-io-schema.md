# Tool I/O Schema — Detailed Implementation Specification

> Owner CHAT_01 | Canonical docs/01-contracts/tool-io-schema.md | Draft / PROVISIONAL | Revision 0.1, research 2026-10-30 JST | Base main b45d4e989998663a00ef09673e69ae1f4191f00d | Dependencies core-domain PR #2, event-envelope 01-02, state-machines 01-03, error-taxonomy 01-06, CHAT_03 Tool Gateway/Registry and CHAT_09 security.

## 0. 採用案

**SchemaはLLMの提案とTool Gatewayの実行を分ける境界**。共通ToolDefinition、ToolInvocation、ToolResult、ToolProgress、Errorのwire契約を所有するが、Toolの発見・許可・副作用台帳・Executor・Policyの実装決定はCHAT_03が所有する。Model outputは未検証の提案にすぎず、schema validation、server-resolved tool snapshot、capability、予算、承認、Run leaseEpoch/GoalRevisionの再照合、DB Tool intentの確定を通るまで実行されない。大量のTool outputはBlob/Artifact Storeに格納し、モデルへは小さな安全なsummary+refだけ渡す。

## 1. 目的・範囲・非範囲

汎用Web調査ではSearch/HTTP/Browser/外部API/MCP/Python sidecarの戻り値を同じ形式で扱う必要がある。AmazonやX、ホテルに固有の型を共通へ混ぜない。対象はTool manifest schema、入力検証、Call/Result/Progress/errorの表現、codec/version、cancel/timeoutと不確定性に関わるwire意味論。非責務: registry選択/TTL/Tool実行、外部アダプタ内部、Secret注入、approval ledger（CHAT_03）、LLMループ（CHAT_02）、HTTP routes（CHAT_08）、Evidence一次評価（01-05）。SC-01..09および追加SC-10/11/12/13/15/16。

## 2. OSS横断調査と採否（実ソース・テストを静的閲覧、未実行）

| Reference/commit/license | 実コード・テスト | 採用する仕組み・制約 |
|---|---|---|
| MCP TypeScript SDK 39ae8e6192cf43273aaaf2de4169dfa9f7216e28; core-internal package.json=Apache-2.0, repository mixed legacy MIT / Apache-2.0 [S1] | packages/core-internal/src/types/spec.types.2025-11-25.ts: CallToolRequestParams, CallToolResult, JSONRPCMessage, ProgressToken, JSONRPC errors; test/e2e/scenarios/dynamic.test.ts、test/e2e/scenarios/errors.test.ts | ProtocolではCallToolResultが非構造化contentを許容、JSONRPCとtools/callはtransport規約。Providerのtool name/IDやprogress tokenは内部認可・idempotencyではない |
| PydanticAI 3809c1b861036197de4a06ae3ee38e27e5455100 MIT [S2] | pydantic_ai_slim/pydantic_ai/tools.py ToolDefinition / Tool.from_schema / prepare_tool_def; tests/test_tools.py の登録とschema validation; tests/test_exceptions.py ModelRetry/ToolFailed等 | JSON schema生成とvalidation/Retry feedbackを分離。ツールの内部実行エラーから権限を推測させない |
| OpenAI Agents SDK cd26bbbc52847845fde73b9952b51e62e2bcbdc0 MIT [S3] | src/agents/run_state.py にtool action/approval/JSON serialization、tests/test_run_state.py にtool_input roundtrip / schemaVersion guard | 実行途中のTool identityと承認状態を持続できる。SDK-native ToolCallIdだけで外部副作用を重複排除しない |

**案A**: Provider Tool objectをそのままGatewayへ渡す→ provider lock-in、secret露出、revision欠落で却下。**案B**: 全Toolを任意のJSON-anyとして扱う→ prompt injectionと型誤認で却下。**案C**: 厳格なversioned registry snapshot + gateway validated call + artifact-backed result→採用。**案D**: 動的スキーマをLLM生成して即有効化→権限エスカレーションのため禁止、Review/activate別段階。

## 3. 正本の責務図

~~~text
LLM untrusted proposal
  -> normalize provider-specific call ID (diagnostics only)
  -> ToolDefinition snapshot lookup (CHAT_03) / registry allowlist
  -> JSON Schema + strict validation (CHAT_01)
  -> CHAT_03 policy / budget / approval / durable intent
  -> leaseEpoch, current GoalRevision, owner (CHAT_02/09/10) fresh check
  -> approved executor, optional Python sidecar
  -> sanitize output + artifact persistence
  -> ToolResult persisted via Gateway side-effect ledger
  -> Event 01-02 / Evidence 01-05 / normalized model feedback
~~~

The model cannot alter ToolDefinition.securityClass, capability or code by changing description/arguments. A provider call ID is not a business ID. Persistent server-issued logicalCallId is UUIDv7, stable across replay of that same captured proposal; replay does not generate a second ID.

## 4. Requirements

| ID | Level | Acceptance | SC |
|---|---|---|---|
| TIO-001 | MUST | ToolDefinition versioned immutable per invocation | 05,07 |
| TIO-002 | MUST | Args checked against approved JSON Schema and size/depth caps | 07,08 |
| TIO-003 | MUST | ToolCall/Result link exact Run/epoch/goalRevision/tenant/owner | 07,08 |
| TIO-004 | MUST | Error and uncertain external effects distinct from empty/zero result | 01,02,07 |
| TIO-005 | MUST | Sensitive credentials never appear in tool schema, prompt, event, progress or result | 08 |
| TIO-006 | MUST | Tool output over budget persisted in Artifact; model receives bounded summary/ref | 05,09,15 |
| TIO-007 | MUST | Non-idempotent tool never automatically retried after unclear ACK | 07 |
| TIO-008 | MUST | Approval-bound args digest verified after resume/Steer/lease change | 07 |
| TIO-009 | MUST | Unknown tool/schema major/version or stale registry snapshot fail closed | 08 |
| TIO-010 | MUST | Progress does not imply complete result; only persisted Result terminal | 07 |
| TIO-011 | MUST | SDK/MCP provider call IDs do not override internal call identity | 07,11 |
| TIO-012 | MUST | Timeouts/cancel race preserve uncertainty and side-effect ledger | 07 |
| TIO-013 | MUST | Result source/clock/units/currency passed to evidence normalization or marked unknown | 01,04,15 |
| TIO-014 | MUST | Tool descriptor cannot grant host arbitrary shell/ssh/privileged Docker/LAN access | 08 |
| TIO-015 | SHOULD | Changes to tool input schema detectable before in-flight call | 07 |
| TIO-016 | SHOULD | Batch calls checked individually with finite global budget/concurrency | 09 |
| TIO-017 | LATER | Streaming structured tool result chunks as independently verifiable artifact ranges | 15 |

## 5. Types, JSON validation, versioning

~~~ts
type SchemaRef = { schemaId: string; major: number; minor: number; sha256: string };
type ToolRefV1 = {
  registryId: string; toolId: string; toolRevision: string; // revision as decimal string
  name: string; provider: string; adapterId: string; manifestSha256: string;
};
type EffectClass = "read_only"|"idempotent_write"|"irreversible_or_unknown";
type ToolDefinitionV1 = {
  version: "1.0"; ref: ToolRefV1; title: string; description: string;
  inputSchema: JsonSchema2020_12; outputSchema: JsonSchema2020_12|null;
  inputSchemaRef: SchemaRef; outputSchemaRef: SchemaRef|null;
  // policy-owned registry-only fields (never supplied by model):
  effectClass: EffectClass; capabilityRef: string; timeoutMs: number;
  maxInputBytes: number; maxOutputBytes: number; maxProgressBytes: number;
};
type ToolInvocationV1 = {
  version: "1.0"; logicalCallId: string; tenantId: TenantId; ownerUserId: UserId;
  taskId: TaskId; runId: RunId; inputGoalRevision: string; effectiveGoalRevision: string;
  leaseEpoch: string; stepOrdinal: number; proposalOrdinal: number;
  toolRef: ToolRefV1; argsSha256: string; argsRef: ArtifactId;
  idempotencyKey: string; providerCallId: string|null;
  createdAt: UtcInstant; deadlineAt: UtcInstant;
};
type ToolProgressV1 = {
  version: "1.0"; logicalCallId: string; runId: RunId;
  sequence: string; progressKind: "started"|"count"|"message"|"heartbeat";
  percent: number|null; safeText: string|null; emittedAt: UtcInstant;
};
type ToolResultV1 = {
  version: "1.0"; logicalCallId: string; toolRef: ToolRefV1;
  outcome: "succeeded"|"failed"|"cancelled"|"indeterminate";
  resultSchemaRef: SchemaRef|null; resultSha256: string|null;
  inlineValue: unknown|null; artifactRef: ArtifactId|null;
  error: ErrorEnvelopeV1|null; startedAt: UtcInstant|null; finishedAt: UtcInstant|null;
  provenanceRefs: EvidenceId[]; externalEffectReceiptRef: string|null;
};
~~~

**Shared types ownership:** ArtifactId/EvidenceId must import 01-05; ErrorEnvelopeV1 must import 01-06; none is separately redefined by CHAT_03. EffectClass policy is **[PROVISIONAL: CHAT_03]** and its member names require approval. Schema dialect JSON Schema draft 2020-12 is **proposal**, compatibility with MCP 2025-11-25/2026-07-28, provider tools and Python validators must be adapter-tested. A schema SHA is computed over canonicalized definition bytes with deterministic algorithm agreed by CHAT_03, not over arbitrary JSON.stringify ordering. Input schema MUST be a bounded plain JSON object; no arbitrary executable code, JavaScript schema evaluators, network dereference of external $ref, dynamic custom format callbacks or untrusted regex catastrophes.

**Field validation:** server mints logicalCallId/tenant/owner/task/run/epoch/goals from persisted state; model only supplies tool name+args. JSON byte caps proposed: max input 64KiB, inline output 16KiB, progress message 2KiB, nesting 16, array length 1000, string 8192 chars, max actions/run set by CHAT_02. All override limits finite and admin-reviewed. Reject NaN/Infinity/undefined/unsafe integer; money amounts use decimal string and explicit ISO4217 currency; dates UTC with IANA zone where needed. JSON unknown keys reject except declared additionalProperties to specific vetted schemas. Disallow arbitrary model-controlled file paths, raw cookie credentials and SSRF endpoints.

**Result outcome**: succeeded may contain empty list and is not failed; failed has error and no fabricated value; cancelled does not prove remote effect absence; indeterminate means side-effect outcome unknown regardless of HTTP cancellation. No consumer may treat indeterminate as zero/false or automatically retry irreversible_or_unknown. References must be tenant/owner scoped. Missing observedAt becomes unknown, not now().

## 6. Sequence / state

~~~text
Propose -> validate schema -> persist frozen intent + args hash
        -> policy budget/authz -> optional WAITING_APPROVAL (01-03 Run)
        -> renewed grant / current epoch and GoalRevision
        -> Gateway executes once-or-reconciles
        -> Artifact write + checksum confirm
        -> ToolResult + ledger + Event + Outbox TX
        -> optional Evidence extraction and model-safe summary
~~~

Tool lifecycle/ledger state itself belongs to CHAT_03, not this MD. Progress may be emitted before terminal commit but cannot cause success state or notification of confirmed fact. If registry revision changes while paused, Gateway must either execute an already approved immutable snapshot still authorized by current policy or reject and request explicit reapproval; it never silently substitute a new executable. In approval wait, tool args/artifact hash + scope is frozen, timeout expiry is checked at execution start.

## 7. Implementation pseudocode

~~~ts
async function prepareToolCall(ctx, rawProposal) {
  const reg = await registry.resolveVersionedSnapshot(rawProposal.toolName);
  assertNoModelControlledPolicyFields(rawProposal);
  const args = await strictValidateJsonSchema(reg.inputSchema, rawProposal.args);
  const ref = await artifacts.storeRedactedCanonicalArgs(args, ctx.tenantId);
  const call = buildServerIssuedCall(ctx, reg.ref, ref, sha256Canonical(args));
  return tx.atomic(async t => {
    await t.requireRunActiveLeaseGoalOwnerBudget(call);
    await t.insertUniqueToolIntent(call.logicalCallId, call);
    await t.appendEventOutbox(call); // no execution in transaction
    return call;
  });
}
async function settleToolResult(ctx, call, untrustedBytes, effectReceipt) {
  const content = sanitizeAndBound(untrustedBytes);
  const stored = await artifacts.writeAndVerify(content); // orphan GC if tx fails
  return tx.atomic(async t => {
    await t.requireCurrentRunFenceOrReconcileOnly(call);
    const outcome = await gatewayLedger.reconcileEffectResult(call,effectReceipt);
    const result = validateResultOrIndeterminate(call,stored,outcome);
    await t.recordResultWithUniqueCallId(result);
    await t.appendEventOutbox(result);
    return result;
  });
}
~~~

**Timeout**: transport timeout means delivery result unknown, never equals safe-to-retry unless registered tool effectClass, idempotency support and external receipt/lookup jointly prove safe. A reply from an expired/stale Worker cannot be allowed to produce a terminal Task result, but an external receipt may be retained in a quarantined ledger for reconciliation. Cancel after dispatch is best effort; force-close of connection alone does not undo remote action.

## 8. Fault matrix

| Failure | Detection and handling | Expected |
|---|---|---|
| unknown tool / wrong schema | resolve exact registry generation | no executor |
| JSON invalid / extra field | strict validator | tool not called |
| malicious tool description | registry trust boundary | no privilege escalation |
| duplicate model ToolCallId | independent server logicalCallId | no cross-Run collision |
| existing same call replay | intent lookup and args hash equality | same logical result |
| approval older GoalRevision | frozen revision comparison | no dispatch |
| lease expired before effect | fresh fence CAS | no new dispatch |
| external write ACK lost | indeterminate ledger | reconciliation, not retry |
| large output / invalid charset | bounded parser + artifact content hash | no LLM context overflow |
| artifact store down | result settlement blocked/retry safe persistence only | no fake success |
| unexpected output schema | fail or mark partial with provenance | no silent cast |
| sidecar crash mid-fetch | read-only bounded retry if policy says safe | explicit failure/unknown |
| unauthorized URL/SSRF redirect | egress guard | block and audit |
| progress after cancel | stale progress filtered, no commit | no terminal success |
| inconsistent result hash | quarantine | no evidence publication |

## 9. Security and privacy

Tool descriptors are untrusted until registry-admin approved/signed. MCP tool list changes are not new capabilities; service principal may not inherit model-suggested roles. Never log full args containing PII, secret or authorization token. Tool Gateway injects credential handles under least privilege outside LLM context and strips inbound/outbound secrets; redaction applies to traces and error cause. Tool execution runs under no host Shell/SSH/Docker socket, no privileged container and restricted egress/SSRF DNS/redirect checks. HITL approval is server-held, tied to frozen arguments and authenticated principal (CHAT_03/09). Tool outcome text is untrusted content and cannot become an instruction.

## 10. Cost/observability/limits

Metrics by toolRef revision: schema_rejected_total, prepare_latency, approval_wait, dispatch_count, result_bytes, artifact_ref_ratio, indeterminate_total, blocked_ssrf, stale_lease_rejected, cost_reserved/settled, sidecar_rss. Benchmark fixed mocks: 1000 calls, 1/4/16 workers, 16KiB/1MiB output, 429/500/timeout, cancellation, 1k unknown schema; compare p95, CPU, RSS, retained bytes and effect duplicate count. No speed numbers asserted.

## 11. DB/migration [PROVISIONAL: CHAT_10]

Minimum physical uniqueness tenant+logicalCallId; immutable manifest revision/hash on intent, argsRef/checksum, Run/Task/owner/epoch/goal revision, result/outcome + source, ledger receipt, correlated Event/outbox in one DB transaction. Initial migration adds version columns and handles historical untyped output as quarantined, not automatically accepted. ToolAttempt fence is separate from Run leaseEpoch. Durable output artifact can be committed before database row, requiring orphan GC by TTL; DB cannot atomically commit external object storage.

## 12. UI/API [PROVISIONAL: CHAT_08]

Tool progress is provisional and distinct from ToolResult committed event. UI renders safe summary of arguments/effect before approval, never raw auth tokens; shows indeterminate as requires checking. API exposes logicalCallId, approvalRef and safe output artifact preview only through owner-authenticated endpoint. ToolSchema validation errors include field path but not secret raw content.

## 13. Implementation PR decomposition

TIO-PR1 shared codec/schema registry/type tests; TIO-PR2 Gateway intent/owner/fence/approval integration; TIO-PR3 Artifact projection and result persistence; TIO-PR4 fault/benchmark/security suite. No runtime implementation in this repo.

## 14. Designed tests (NOT executed)

| TEST | Fixture/action | Expected |
|---|---|---|
| TIO-T01 | malformed ToolDefinition/schemaVersion/unknown major | reject |
| TIO-T02 | invalid args/extra fields/depth/huge size | executor 0 |
| TIO-T03 | 2 owners same provider callId | logical call/permissions isolated |
| TIO-T04 | tool/Run epoch mismatch | new effect 0 |
| TIO-T05 | malicious HTML/MCP metadata claims admin | policy unchanged |
| TIO-T06 | 1MiB valid output | Artifact, inline summary <=16KiB |
| TIO-T07 | irreversible tool timeout after outbound send | indeterminate, 0 blind retry |
| TIO-T08 | older approval after Steer / expired token | executor 0 |
| TIO-T09 | registry revision changed after proposal | invalid/reapprove, no changed binary |
| TIO-T10 | progress completed but Result absent | no completed Task |
| TIO-T11 | provider call_id collision after replay | internal ID unchanged |
| TIO-T12 | cancel/timeout races with ACK loss | uncertain state preserved |
| TIO-T13 | output prices lack currency/time | unknown, no inferred profit |
| TIO-T14 | tool asks to read host secrets or arbitrary Shell | denied |
| TIO-T15 | batch 1000 ToolCalls, budget exceeds limit | bounded accepted subset, rest denied |
| TIO-T16 | fault before/after Artifact persistence and DB commit | no phantom success, orphans GC |
| TIO-T17 | adversarial SSRF redirect/rebind | blocked prefetch + on redirect |
| TIO-T18 | 1000 mocked calls/varied payload sizes | p95/RSS/DB pressure recorded |

## 15. Traceability

| REQ | TEST | SC |
|---|---|---|
| TIO-001 | T01,T09 | 05/07 |
| TIO-002 | T02 | 07/08 |
| TIO-003 | T03,T04 | 07/08 |
| TIO-004 | T07,T12,T13 | 01/02/07 |
| TIO-005 | T05,T14,T17 | 08 |
| TIO-006 | T06,T16 | 09/15 |
| TIO-007 | T07 | 07 |
| TIO-008 | T08 | 07 |
| TIO-009 | T01,T09 | 08 |
| TIO-010 | T10 | 07 |
| TIO-011 | T11 | 07/11 |
| TIO-012 | T12 | 07 |
| TIO-013 | T13 | 01/04/15 |
| TIO-014 | T14,T17 | 08 |
| TIO-015 | T09 | 07 |
| TIO-016 | T15,T18 | 09 |

## 16. Pending proposals

- TIO-P01 P0 CHAT_03: EffectClass names, registry signed snapshot and frozen approval+ledger authority, idempotent retries, logicalCallId uniqueness. T03/T07/T08.
- TIO-P02 P0 CHAT_09: credential handle, tenant-owner worker authority, policy revocation, egress/SSRF. T04/T05/T14/T17.
- TIO-P03 P0 CHAT_10: manifest/intent/result/outbox schema, durable Artifact transaction gap. T07/T16.
- TIO-P04 P1 CHAT_02: max active calls, safe-boundary Steer/Cancel, budget reservation. T08/T12/T15.
- TIO-P05 P1 CHAT_04/05/15: source/structured multimodal return, caller input validation.
- TIO-P06 P1 CHAT_01 self: import ErrorEnvelopeV1 from 01-06 and Artifact IDs from 01-05; no duplicated final definitions.
- TIO-P07 P1 CHAT_08: progress vs committed status and approval UI.
Do not mark Locked until Tool Gateway and schema dialect are agreed.

## 17. Sources (inspected 2026-10-30)

[S1] Code verified: https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/src/types/spec.types.2025-11-25.ts and https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/dynamic.test.ts and https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/errors.test.ts. Protocol revision/version must be pinned by adapter; license per package not independently confirmed.  
[S2] Code verified: https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/tools.py and https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_tools.py; MIT LICENSE.  
[S3] Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_state.py and https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_run_state.py; MIT LICENSE.  
[S4] Hypothesis: canonical Tool IO types / finite caps / gate mapping, not native OSS API and not benchmarked.

**Review gates:** Tool Gateway policy, Schema dialect and owner/lease DB contract unresolved. Tests designed but not run, Draft only.

**MCP license evidence (Docs verified, pinned 39ae8e6, 2026-10-30):** https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/LICENSE (MIT→Apache-2.0 transition); https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/package.json (license: Apache-2.0). This project copies no upstream source code.
