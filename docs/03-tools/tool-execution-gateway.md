# Tool Execution Gateway — 実装仕様 (03-02)

> Owner CHAT_03 / canonical path `docs/03-tools/tool-execution-gateway.md` / **Draft [PROVISIONAL]** / Rev 0.2 / 調査 2026-10-30 JST / main基線 `b45d4e989998663a00ef09673e69ae1f4191f00d` / 依存: CHAT_01 Tool IO・State・Error、CHAT_02 loop/lease/budget、CHAT_03 03-01/03-03/03-04/03-05、CHAT_09 auth/egress/secrets、CHAT_10 DB、CHAT_11 fixtures。
>
> 本書は**Tool Gatewayの意味論の正本**。共有型/公開Event/物理DDLの正本は持たない。MCP/Browser/Connectorの実行はそれぞれのAdapter ownerが担当。仕様とテスト設計であり実装/実測ではない。

## 0. 決定
中心的な不変条件：**LLMのTool proposalは外部操作ではなく不信入力**。全Toolを `freeze proposal→strict validate→read-only policy preflight→conservative budget reservation→approval check→durable intent+outbox commit→dispatch fence→executor invocation→normalize+persist outcome→settle usage` に通す。時間切れ・Worker kill・ACK欠落・承認待ちのいずれにも「外部に副作用を送った可能性」を過小評価しない。

- `prepare` はrun/step単位で**全件**呼出後にdispatch可否を決める（CHAT_02 Draftのbarrierを採用）。`WAITING_APPROVAL`が1件でもあれば既定では同じstepの全Toolを未開始にする。既存Resultの複製は実行ではなく履歴参照。
- `execute` は承認、policy、remaining budget、tenant/owner、Run`leaseEpoch`、Task `goalRevision`、Tool `descriptorRevision`、side-effect ledgerを**開始直前**に再検査。Discovery refはauth tokenではない。
- `reconcile` はLedger結果の正当な確定/再照会のみ。**外部副作用の実行後にDBへ確定保存できなかったら UNCERTAIN**（外部に問い合わせ/人間判断なしで再実行しない）。
- 回答の`isError`・HTTPエラー・Transport failure・Validation errorは異なる。外部操作失敗との混同禁止。Object Store原本＋size-bounded model-facing projection＋出典/観測時刻を分離。
- Tool Executorは秘密参照や対象アクセス権を自己発行できない。実際の网络アクセスはCHAT_09 egressに従う。host shell/SSH/Docker socket/privileged APIは禁止。

## 1. Scope・依存・SC
対象：LLM提案、定期Task、Event発火Task、Learning replay、バッチ実行からの**すべての**Tool開始の唯一の入口。Generic Web Search、HTTP Fetch、Browser Worker、MCP、公式API、deterministic compute、notification adapterに適用。買い物/ホテルは調査のみ自動可能、購入・予約確定・投稿・送金・許可外送信はapproval必須。

非対象：モデルルーティング（CHAT_02）、UIコマンド受付（CHAT_08）、Prompt content生成、外部APIのendpoint定義（CHAT_05）、BrowserSessionのPage直接操作（CHAT_04）、Watchの重複通知（CHAT_07）、物理schema（CHAT_10）。

SC-01/03/04/05：evidence preserving read-only tools、SC-02/11：watch/event triggered taskにも同一policy、SC-06/13：browser lease/recipeに特権なし、SC-07：crash/retry/steer、SC-08：悪意あるMCP/HTML/secret、SC-09：時間・費用、SC-10：unknown entity analysis、SC-12：diff、SC-14：trend、SC-15：file ingestion、SC-16：通知・feedback read-only。最終受入結果は各ownerと共同判定。

## 2. 既存OSS：ソース・テスト・Issue比較
| project/pin | 読んだ具体的実装・試験 | Evidence と採否 |
|---|---|---|
| PydanticAI `3809c1b861036197de4a06ae3ee38e27e5455100` (MIT) | [`_tool_execution.py:process_tool_calls`](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L321-L355) は`graceful/exhaustive/early`とsequential barrierを分ける。`_validate_function_calls`[#L673](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L673-L722)を確認。[test](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_run_cancellation.py)は取消/再開の回帰。 | **Code/test inspected、未実行**。実行順と引数検証を参考。Python runtimeのhistory保存だけでside-effect exactly-onceになると推定しない。 |
| OpenAI Agents Python SDK `cd26bbbc52847845fde73b9952b51e62e2bcbdc0` (MIT) | [`run_internal/approvals.py`](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_internal/approvals.py#L24-L65) のpending approval placeholder/error output、[`tests/test_tool_approval_call_id_reuse.py`](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py#L333-L435) のcall identity/replayケース。 | **Code/test inspected**。tool_call_idのみを唯一鍵とするのは却下しlogical intent+args/revision+ownerでbind。 |
| Mastra `ec3ba11dad428caeb3b4afae77d1669462e8c129` (Apache-2.0 / ee除外) | [`agent/thread-stream-runtime.ts`](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/thread-stream-runtime.ts) は`run-lease-fence`呼び出しとlate handoffの競合対策、[`durable/workflows/steps/tool-call.ts`](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/workflows/steps/tool-call.ts)を調査。 | **Code inspected**。分散leaseとTool開始時のfence原理を採用。プロセス内フラグだけの堅牢性は保証しない。 |
| MCP SDK TS `39ae8e6192cf43273aaaf2de4169dfa9f7216e28` (MIT/Apache-2.0混在) | [`Client.callTool`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L2331-L2493) のtransport errorと`structuredContent` output schema検証、[`protocol.test.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/protocol.test.ts)。 | **Code/test inspected**。MCP contentの構造化検証は参考、Tool side effectが成功したかはTransport timeoutから断定しない。 |
| Issues [PydanticAI #10926](https://github.com/pydantic/pydantic-ai/issues/10926), [Mastra #27485](https://github.com/mastra-ai/mastra/issues/27485), [LangGraph #9397](https://github.com/langchain-ai/langgraph/issues/9397) | 承認済みtool再開が外部副作用を再実行、二重approveのresume競合、crashと並行step replayのreported reproductions。 | **Issue report（未独立再現）**。SDK上の成功をDB/外部効果のexactly-once証明に使わない。 |

参考APIのfeature availabilityとメジャー版は本番依存導入時に再検証する。ここではコードコピーしない。

### 採用 vs 却下
- A **モデル直結function tool + handler内で必要に応じてチェック**：簡単だがToolごとのpolicy抜け、複数Runのcross-user誤用、effectのlost ACKが危険。**却下**。
- B **in-memory promise/mapによる呼出重複抑制**：同一process内で速いがcrash/再claim/別workerで破綻。**補助のみ採用**。
- C **DB intent/approval/ledger + per-invocation fence + idempotent executor bridge**：待機やcrashでも説明可能。外部exactly-onceは保証できないがUncertainを表現できる。**採用**。
- D **全処理を一DBトランザクション内で外部呼出**：長時間DBロック・外部/DBのatomicityは得られない。**却下**。

## 3. 所有権と型境界
- CHAT_01 Draft [PR #2](https://github.com/choko510/aiagent-md/pull/2)：`tenantId`、`ownerUserId`、`Task.conversationId nullable`、UUIDv7 Entity ID、`recordVersion`(1開始)、`goalRevision`、`leaseEpoch` の論理正本。CHAT_02 Draft [PR #1](https://github.com/choko510/aiagent-md/pull/1)：`prepare/execute/reconcile` Portと`logicalCallId`、step barrier。**いずれも未Locked**。
- CHAT_03 Tool Registryは`(provider,namespace,sourceName,revision)`を確定的解決。Approval policyとTool ledgerは03-03/03-04、本Gatewayは状態遷移を統率する。Tool AdapterにPolicy decision権を与えない。
- CHAT_09 Draft [PR #9](https://github.com/choko510/aiagent-md/pull/9)はprincipalをTask→Run→owner DBから復元する。Web/Telegram/Watch/Xイベントのclient-supplied `tenantId/actorId`は信頼しない。
- CHAT_10 Draft [PR #10](https://github.com/choko510/aiagent-md/pull/10)は`tool_intents/approval_records/tool_attempts`物理DDL案。ただし`workspace_id↔tenantId`、UUIDv7 vs `gen_random_uuid()`、`revision DEFAULT 0` vs `recordVersion 1`、Run `leaseEpoch`/Task `activeRunId`等は未整合。**本書は変換が決まった前提で実行できると主張しない**。
- Event envelope/Artifact storeはCHAT_01/10、UI/RESTはCHAT_08（[PR #8](https://github.com/choko510/aiagent-md/pull/8)）、BrowserタブはCHAT_04。Tool Gatewayから外部site直接Page操作は禁止。

## 4. 要求仕様
| REQ-ID | level | MUST / observable result | SC |
|---|---|---|---|
| GW-001 | MUST | 単一路でcanonical callをfreeze/validateし、未知Tool・Schema・argsをExecutorへ渡さない | 05,08 |
| GW-002 | MUST | 認証済Run/Task/tenant/owner/goalRevision/leaseEpochをDB正本から照合、client claimでは開始不可 | 07,08,11 |
| GW-003 | MUST | policy fail-closed、risk classificationはadmin正本、外部annotationsはhintのみ | 04,08 |
| GW-004 | MUST | 残予算をatomic reserveし失敗なら開始0、settle/refundとcrash回復を追跡 | 07,09 |
| GW-005 | MUST | 1 stepの全提案をprepareしてから、承認待ちなら同stepのTool実行0 | 04,07 |
| GW-006 | MUST | 外部呼出より前にimmutable intent+ledger+outboxをDB commit | 07 |
| GW-007 | MUST | 承認のidentity/hash/revision/expiry/policyEpochを実行開始前に再検証しsingle consume | 04,07,08 |
| GW-008 | MUST | Tool dispatch開始直前にRun claim/fence/cancel/steerを再検証し古いWorkerの新規送信0 | 07 |
| GW-009 | MUST | external result、transport ACK、DB commitを別扱いしlost ACKをUNCERTAINへ | 07 |
| GW-010 | MUST | 非冪等/不明効果は期限切れ後に無条件retryしない。status inquiry/reconcileが優先 | 07,11 |
| GW-011 | MUST | output schemaと上限を検証、原本Artifactとmodel向けexcerptを分離し秘密除去 | 03,05,08,15 |
| GW-012 | MUST | timeout/cancel/worker crashでin-flight操作のeffect absenceを断定しない | 07 |
| GW-013 | MUST | Invalid/Denied/NeedsApproval/Uncertain/Partialを区別し明示したログ・Eventに変換 | 04,07,08 |
| GW-014 | MUST | GatewayとAdapterでno shell/SSH/Docker socket、SSRF/redirect/IP/secret境界を守る | 06,08 |
| GW-015 | MUST | Tool attempt・budget使用・policy・evidence timestampを監査し、外部未確認を成功にしない | 01,03,09 |
| GW-016 | MUST | event/recipe/batch経由でも同じgateway、approvalを迂回しない | 10,11,13,16 |
| GW-017 | SHOULD | 明白なread-only独立呼出は安全に並列化（budgetとDB capacity内）、external writeは排他 | 05,09 |
| GW-018 | SHOULD | metadata cache/Brokerを効率化してもセキュリティ判定とledgerを省略しない | 09 |
| GW-019 | EXPERIMENT | provider batch APIの費用低減は同一Task fixtureで測定後採用 | 09 |

## 5. Local interface / strict validation ([PROVISIONAL: CHAT_01])
次の型は**CHAT_03内部のport例**。公開JSON ContractやDB列はCHAT_01/10の決定を待つ。Entity公開IDはUUIDv7、`opaque` ToolRef/ArtifactRefはUUIDと異なる。BigIntはJSON numberに出さずdecimal-string codecを検討、CH01の`LeaseEpoch:number`を安全整数以上に拡張するかCHAT_01/02で確認する。

~~~ts
type EntityId = string;       // validated canonical UUIDv7 by CHAT_01
type OpaqueRef = string;      // server-resolved, no authority embedded
type MonotonicFence = string; // canonical non-negative decimal [PROVISIONAL: CHAT_01/02]
type ToolRisk = "read"|"sensitive"|"write"|"irreversible"; // 03-03 local
type PrepareDisposition = "READY"|"WAITING_APPROVAL"|"DENIED"|"INVALID"|"OVER_BUDGET"|"STALE";
interface TrustedRunScope {
  tenantId: EntityId; taskId: EntityId; runId: EntityId;
  ownerUserId: EntityId; goalRevision: number; stepOrdinal: number;
  leaseEpoch: MonotonicFence; policyEpoch: string;
}  // constructed from DB by server, never model-supplied
interface ToolProposal {
  toolRef: OpaqueRef; logicalCallId: string; proposalOrdinal: number;
  argsArtifactRef: OpaqueRef; canonicalArgsSha256: string;
  providerCallId?: string; // diagnostic only; not identity
}
interface PreparedTool { disposition: PrepareDisposition; intentId: EntityId;
  toolRevision: string; risk: ToolRisk; approvalId?: EntityId;
  expiresAt: string; expectedRunFence: MonotonicFence;
  requestDigest: string; }  // opaque receipt, loaded from DB on execute
type ResultKind = "SUCCEEDED"|"FAILED_CONFIRMED"|"UNCERTAIN"|"DENIED"|"NEEDS_APPROVAL"|"CANCELLED"|"PARTIAL";
interface GatewayOutcome { intentId: EntityId; result: ResultKind;
  evidenceRefs: readonly OpaqueRef[]; resultArtifactRef?: OpaqueRef;
  observedAt?: string; executedAt?: string; errorCode?: string; }
interface GatewayPorts {
  prepareRunStep(scope: TrustedRunScope, proposals: readonly ToolProposal[]): Promise<PreparedTool[]>;
  executePrepared(scope: TrustedRunScope, prepared: readonly PreparedTool[]): Promise<GatewayOutcome[]>;
  reconcile(scope: TrustedRunScope, intentId: EntityId): Promise<GatewayOutcome>;
}
~~~

**canonical arguments**: decoding from Artifact Store must enforce validated JSON Schema (supported dialect and no remote refs), UTF-8 byte limit, nesting, unknown properties and integer safe-range; use stable canonical representation (RFC 8785/JCS candidate) and compute SHA-256 over the **exact validated semantics**. `schemaRevision` + `toolId` + `tenantId` + `runId` + `stepOrdinal` + `proposalOrdinal` + `goalRevision` + `argsSha256` + `risk` are part of the identity fingerprint. Do not hash/display secret values: secret injections occur after validation in a trusted adapter and are referenced by handles; if changing credentials changes target identity, separate credentialBinding version enters fingerprint.

**Call identity**：`logicalCallId` は `(runId, stepOrdinal, proposalOrdinal)`のserver stable tupleから決定。Provider-generated call idは一意/安定と仮定しない。同じ logicalCallId に異なるtool/args/targetが来たら `409 INTENT_MISMATCH`、同じものなら保存済みreceipt返却（再実行ではない）。承認/外部副作用をstep間で使い回さない。

**Validator limits (initial policy defaults, unmeasured)**：args max 64KiB (raw), JSON depth 16, 128 object keys, tool result max 2MiB inline raw then Artifact streaming, model result 8KiB excerpt, max 8 proposals/step, max 4 concurrent independent read executors/run, max 1 write executor/target, gateway start deadline 10s; provider-specific deadline from approved config, never infinite. Limits may tighten; exceed = explicit fail-close, not schema truncation.

## 6. sequence・state lifecycle
~~~mermaid
sequenceDiagram
  participant M as AgentLoop
  participant G as Gateway
  participant D as Postgres
  participant P as Policy/Auth/RunMgr
  participant E as Approved Executor
  participant A as Artifact Store
  M->>G: prepareRunStep(frozen proposals)
  G->>P: resolve owner/fence/schema, authorize, budget reserve
  G->>D: TX store intents, approvals, reservations, outbox
  D-->>G: durable receipt(s)
  G-->>M: READY or WAITING_APPROVAL/DENIED
  Note over M,G: WAITING_APPROVAL -> suspend; nothing execute
  M->>G: executePrepared(receipts)
  G->>D: TX lock intent+approval+current fence, claim attempt
  G->>P: fresh policy/cancel/goal/budget check
  G->>D: CAS dispatch barrier + IN_FLIGHT
  G->>E: invoke with scoped secret binding and abort signal
  E-->>G: result or timeout/unknown
  G->>A: put bounded raw output and evidence
  G->>D: TX settle ledger/outbox+cost
  G-->>M: terminal result or UNCERTAIN for reconciliation
~~~

**Logical phases (not CHAT_01 public RunState):**
`PROPOSED → VALIDATING → POLICY_CHECKED → BUDGET_RESERVED → INTENT_COMMITTED → WAITING_APPROVAL | READY → DISPATCH_CLAIMED → MAY_HAVE_SENT → CONFIRMED_SUCCESS | CONFIRMED_FAILURE | UNCERTAIN`。Reject before external send: `INVALID/DENIED/OVER_BUDGET/CANCELLED`。After MAY_HAVE_SENT no automatic return to READY for non-idempotent or unclassified effect. Status transition invariants are in 03-04; approval state in 03-03.

**Linearization boundary**：`Gateway.start`のcommit前にDBに`intentId, toolRevision, goalRevision, policyEpoch, leaseEpoch, argsHash, requestedRisk, attemptId`を記録。外部callをdispatchするprocessがfenceを失ったら新規送信禁止。DB commit直後から実際にsocket writeされるまでの間にもcrashは起こるため、記録上`MAY_HAVE_SENT`は保守的。stale intentのreconcileはread-only status問い合わせ/監査以外を行わない。

## 7. 実装疑似コード（DB contractはCHAT_10への提案）
~~~ts
async function prepareRunStep(runRef: string, proposalRefs: readonly string[]) {
  const ctx = await runManager.loadAndFenceFromDb(runRef); // principal rebuilt from DB
  assertStepCaps(ctx, proposalRefs); // same Run/step/generation, bounded count
  const freezes = await Promise.all(proposalRefs.map(id => artifact.readFrozen(id)));
  const drafts = [];
  for (const [ordinal, frozen] of freezes.entries()) {
    const resolved = await registry.resolveForGateway(frozen.toolRef, ctx);
    const args = strictValidate(resolved.schema, frozen.arguments);
    const digest = sha256(canonicalize(args, resolved.revision, ctx, ordinal));
    const policy = await policyService.decideNow(ctx, resolved, args);
    const budgetQuote = await metering.estimateUpperBound(resolved, args, ctx);
    drafts.push({resolved, argsRef:frozen.argsRef, digest, policy, budgetQuote});
  }
  // One transaction for proposal group. No executor invoked here.
  return db.transaction(async tx => {
    await tx.assertRunTaskGoalAndEpoch(ctx); // locks/version CAS
    const receipts = [];
    for (const d of drafts) {
      const old = await tx.intentByLogicalCall(ctx, d.digest);
      if (old) { receipts.push(old.sameFingerprintOrConflict(d)); continue; }
      const reserve = await tx.budgetReserve(ctx, d.budgetQuote); // all-or-none reservation
      const intent = await tx.insertFrozenIntent(ctx, d, reserve);
      await tx.insertApprovalIfRequired(intent, d.policy); // WAITING_APPROVAL
      await tx.insertOutboxEvent("TOOL_INTENT_PREPARED", intent);
      receipts.push(intent.receipt);
    }
    return receipts;
  });
}
async function executePrepared(runRef: string, receipts: readonly string[]) {
  const ctx = await runManager.loadAndFenceFromDb(runRef);
  const intents = await db.loadFrozenReceiptsScoped(ctx, receipts);
  if (intents.some(x => x.waitingApproval)) return blockWholeStep("WAITING_APPROVAL");
  const results = [];
  for (const intent of arrangeSafeExecutionOrder(intents)) {
    const previous = await ledger.load(intent.id);
    if (previous.confirmed) { results.push(previous.receipt); continue; }
    if (previous.mayHaveSent) { results.push(await reconcile(intent)); continue; }
    const claim = await db.transaction(async tx => {
      await tx.assertActiveRunFenceGoalCancel(ctx);   // current Task.activeRunId!
      await tx.assertSchemaRevisionAndCurrentPolicy(intent);
      await tx.assertBudgetReservationLive(intent);
      await tx.consumeApprovalAtomicallyIfNeeded(intent);
      return await tx.claimDispatchCAS(intent, ctx.leaseEpoch);
    });
    if (!claim.ok) { results.push(denyOrReload(claim)); continue; }
    try {
      const output = await approvedExecutor.invoke(claim.scopedBinding, claim.argsRef,
        claim.deadline, claim.abortSignal);
      const normalized = await normalizeAndPersist(output, claim); // output schema+artifact+evidence
      results.push(await ledger.commitConfirmed(claim, normalized)); // CAS, outbox, cost
    } catch (err) {
      results.push(await ledger.classifyAfterDispatch(claim, err));
    }
  }
  return results;
}
async function reconcile(intent) {
  // Query only if adapter exposes an independently authenticated status API.
  // "did not find the transaction" is NOT proof of no external effect unless provider contract guarantees it.
  return ledger.reconcileWithoutBlindReplay(intent);
}
~~~

**Critical correction for double-check race**：policy判定はdb transaction外のpreflightだけで終わらない。Call-time判定を行った後の失効との線形化をCHAT_09 policy version／revocation epochによってDB claim CASと接続する。Policy storeが非DBの場合にstrict atomic snapshotが保証できなければ有効期限を短縮しても原子性は保証しないため、Gatewayは`FAIL_CLOSED_POLICY_UNAVAILABLE`に留め、P0判定事項とする。

## 8. 障害マトリクス
| Injection/condition | Detection | Safe handling / user status | Retry |
|---|---|---|---|
| unknown/stale ToolRef, Schema invalid | strict Registry resolve | INVALID/STALE、再発見。Executor 0 | 新proposalのみ |
| denied/capability revoked | fresh policy epoch | DENIED + redacted audit | 自動なし |
| budget unavailable/overspend | atomic quote/reserve | OVER_BUDGET、全step実行0 | 増額時のみ |
| approval pending/rejected/expired | frozen approval row | NEEDS_APPROVAL or DENIED、他Toolも待機 | 同じargs再承認のみ |
| Worker lease stolen | Task activeRun & Run leaseEpoch CAS | stale worker cannot dispatch; leave reconciler | no stale replay |
| DB crash before intent commit | no durable intent | no external call permitted; model step can reprepare | yes, same logicalCallId |
| DB crash just after dispatch claim | MAY_HAVE_SENT ambiguous | UNCERTAIN、status inquiry/manual reconciliation | no blind retry |
| adapter HTTP 2xx but output save fails | external result may exist | UNCERTAIN, artifact repair if safely reconstructable | only status inquiry |
| MCP `isError:true` | tool-level response | FAILED_CONFIRMED only if provider certifies effect absent; else UNCERTAIN | policy + ledger |
| timeout/AbortSignal | transport exception | can stop local computation, cannot guarantee remote effect undone | safe read only |
| response > limit | bounded parser / streaming | store raw evidence if possible, model projection truncation; explicit PARTIAL | no reexecute effect |
| cancellation after send | run control changed | no further starts; in-flight settles/reconciles | none |
| event replay / X posted duplicate | stable logical identity | reuse existing intent/Task linkage, no additional external effect | none |
| region/clock skew / approval expiry | DB UTC timestamp | Expired fails closed using DB wall clock; stale client clock ignored | reapproval |
| race: two workers same logical call | unique + CAS/fence | one physical dispatch claim, losers observe receipt/uncertainty | no duplicate |

Gateway must **not** equate `request returned 200` with confirmed business effect if response schema doesn't certify it; HTTP read vs POST side effects require adapter-provided evidence classification. False positive success in SC-01/04 forbidden.

## 9. Security
Authority flows only server→Gateway. Untrusted tool name, args, raw HTML, X posts, PDF macros, learned Browser recipes, MCP annotations cannot alter `tenantId`, `ownerUserId`, `policyEpoch`, `approval`, `risk`, `credentialBinding`。Provenance is never instruction. SSRF/redirect/IPv6 private/rebinding filtering + secret relay scoped to connector destination is CHAT_09. Gateway never logs raw args / provider response / access token. Risk-sensitive approvals hide secrets while showing verified target, domain, amount, purchase/booking indication.

Grant evaluation includes **subject (tenant/owner/actor), action (tool identity+revision+risk), object (target URL/account/resource), context (run goalRevision, leaseEpoch, policyEpoch, time, resource ACL)**。Network policy and human approval remain independent checks; possession of approve receipt cannot create new capability. Host shell and raw arbitrary script imports forbidden.

## 10. Budget & operations
Per-call fixed upper bound must be estimated before dispatch. Token/USD budgets are owned by CHAT_02; budget reservation and settlement persisted by CHAT_10. Unknown downstream price = reject/quarantine or conservative configured cap, never implicit infinite. Soft UI estimate may change; hard dispatch cap enforced by limiter. Cached same intent does not reserve/pay twice; actual usage settles at most once by usage idempotency key. Failed/aborted unknown charge is recorded as uncertain estimate, not $0.

Metrics: `tool_prepare_ms`, `tool_dispatch_ms`, `gateway_budget_reserved`, `gateway_intent_dedup`, `gateway_uncertain_effects`, `gateway_policy_denies`, `tool_output_schema_failures`, `stale_lease_denials`（bounded cardinality）. Trace dimensions: tenant-safe traceId, hashed intentId, runId, goalRevision, toolRevision, leaseEpoch, decisionReason, bytes, observedAt, confidence, source URL ref. Secret/PII redaction before logging. Logs are not an alternative ledger.

Benchmark: same model, frozen task fixture, mock providers with defined latency/429/disconnect and 10+ repeats, measure p50/p95, tokens, provider call count, duplicate side effects, recovered UNCERTAIN, cost. Proposed safety acceptance: **unauthorized starts=0, stale-fence starts=0, fixture double writes=0**; production exactly-once not promised.

## 11. Persistence & migration proposals ([PROVISIONAL: CHAT_10])
Logical aggregates: `tool_intent`, `approval_record`, `attempt`, `external_effect_ledger`, `budget_reservation`, `result_artifact`, `outbox_event`。Current PR #10's `agent.tool_intents/approval_records/tool_attempts` is **a Draft and insufficient for final semantics** without actor/owner scope, goalRevision, toolRevision, policyEpoch, claim+send marker, reconciliation status, immutable exact target hash, budget reservation and uniqueness/stability checks.

Required transaction boundaries: (T1) intent insert+budget reserve+approval request+outbox, (T2) current run fence/control+policy+approval consume+dispatch claim state, (T3) output/usage/ledger terminal+outbox; no external I/O in transactions. A DB failure after T2 is *UNCERTAIN*, not replayable READY; compensating transaction cannot undo external action. DDL and physical CAS indexes belong CHAT_10. Older `tool_intents` rows with absent fingerprint/fence must migrate to blocked+needs reconciliation, not auto dispatch.

**Contract reconciliation P0**: `workspace_id` ≠ automatically `tenantId`; `gen_random_uuid()` likely v4 ≠ CHAT_01 UUIDv7; `revision=0` might be DB-private but not `recordVersion=1`; `leaseEpoch` (Run claim) and `goalRevision` require durable CAS columns & numeric codecs. No Task Conversation requirement on a schedule/event Task. Proposed `ToolIntent.id` is UUIDv7, `toolRef` opaque. Need common migration & CI across CH01/02/08/09/10.

## 12. UI / API proposals ([PROVISIONAL: CHAT_08])
`GET /tasks/:id/tool-intents` → readonly redacted list of intent/approval/uncertain/status; `GET /tool-intents/:id` → confirmed evidence links, observedAt, artifactRef, estimated+actual cost; approval API uses `POST /approvals/:id/decision` from CHAT_08 Draft and must not treat HTTP 202 as executed. User-visible `Queued/Waiting approval/Running/Partial/Uncertain/Succeeded` are projections of committed statuses only. Event stream derives from transactional outbox, not in-memory callback. Telegram cannot bypass identity/fresh-auth challenge.

## 13. Implementation PR plan
1. **G1** Validator + frozen canonical args + stable logical identity, test contract with CHAT_01; unknown tool fails closed.
2. **G2** TrustedRunScope/POLICY/Budget ports + preflight + atomic reservation + approval wait barrier.
3. **G3** Durable Intent/Dispatch claim with lease-fence+goal-revision+policy epoch CAS (CHAT_10), crash/restart integration.
4. **G4** Adapter executor + normalize/artifact/output schemas + timeout/cancellation + side-effect ledger reconciliation.
5. **G5** UI/Event observability, security/benchmark stress tests; only after cross-team contracts merge.

## 14. Tests (proposed, 0 executed)
| TEST-ID | setup → action | expected; level |
|---|---|---|
| GW-T01 | Fake model invents unknown tool and extra args → prepare | 0 executor starts; INVALID, Unit |
| GW-T02 | Other tenant submits borrowed run/ref → prepare/execute | 0, Security |
| GW-T03 | Web page injects new policy/role → prepare | unchanged authoritative policy, Security |
| GW-T04 | quote exceeds hard budget → prepare | no intent in READY, reserve not leaked, Unit |
| GW-T05 | read+irreversible booking in same step → prepare | WAITING_APPROVAL; all external starts 0, Integration |
| GW-T06 | kill just before T1 commit → resume | same logical intent, external call 0 until commit, Fault |
| GW-T07 | approve valid then mutate args/goalRevision/schema → execute | denied, consumed authorization 0, Security |
| GW-T08 | expire/revoke approval → execute | 0 external calls, Contract |
| GW-T09 | two workers same run after lease takeover → start | stale epoch cannot start, Conflict |
| GW-T10 | write sent, remote succeeded, kill before T3 → resume | UNCERTAIN/status inquiry, no blind replay, Fault |
| GW-T11 | HTTP 200 but provider business response uncertain → normalize | no false success, Contract |
| GW-T12 | adapter timeout and lost ACK → resume | UNCERTAIN where effect possible, Fault |
| GW-T13 | huge HTML/PDF and malicious tool response → normalize | bounded excerpt; immutable raw artifact; no prompt authority, Security |
| GW-T14 | cancel before/after T2 → executor | before=0 start, after=eventual settle or uncertain, Integration |
| GW-T15 | X duplicate event and Browser Recipe replay → execute | dedup + full policy, SC-11/13 E2E |
| GW-T16 | provider 429/retry after send for non-idempotent | only status inquiry; no second write, Fault |
| GW-T17 | API body for another tenant, invalid workspace map | deny; no default tenant, Security |
| GW-T18 | 16.1KB args/output schemas, unknown dialect, forged JSON values | strict validation; no silent schema coercion, Fuzz |
| GW-T19 | two identical prepare calls with same logical ID but differing args | 409 mismatch for changed args; same receipt for identical, Race |
| GW-T20 | benchmark 100 fixtures across 1/4 parallel reads, cost caps | finite token/time/USD; p95 and duplicate effects measured, Benchmark |

Test execution is **not** claimed; all use fake providers and dummy approvals; never real purchases/posts.

## 15. REQ→TEST→SC traceability
| REQ | TEST(s) | SC | acceptance |
|---|---|---|---|
| GW-001 | 01,18,19 | 05,08 | invalid calls executed 0 |
| GW-002 | 02,09,17 | 07,08,11 | principal/fence mismatch 0 starts |
| GW-003 | 03,08,17 | 04,08 | privilege escalation 0 |
| GW-004 | 04,20 | 07,09 | cost hard cap respected |
| GW-005 | 05,07 | 04,07 | all same-step starts 0 before approve |
| GW-006 | 06,10 | 07 | T1 committed prior to dispatch |
| GW-007 | 07,08,19 | 04,07,08 | replay/changed target denied |
| GW-008 | 09,14 | 07 | stale owner 0 starts |
| GW-009 | 10,11,12 | 07 | unknown effect never success |
| GW-010 | 10,12,16 | 07,11 | nonidempotent duplicate 0 |
| GW-011 | 13,18 | 03,05,08,15 | bounded/validated model output |
| GW-012 | 10,12,14 | 07 | abort not equated to undo |
| GW-013 | 05,08,11 | 04,07,08 | externally visible reason distinction |
| GW-014 | 02,03,13,17 | 06,08 | no host or SSRF escape |
| GW-015 | 11,13,20 | 01,03,09 | provenance, usage, audit persisted |
| GW-016 | 15,17 | 10,11,13,16 | no alternate invocation paths |

## 16. 未決・Cross-team proposal
| ID | Priority / owner | Current discrepancy & requested contract / exit test |
|---|---|---|
| GW-X01 | P0 CHAT_01 | public UUIDv7 EntityId vs opaque ToolRef; logicalCallId/argsHash/step ID/frozen schemaVersion; GW-T01/T19 |
| GW-X02 | P0 CHAT_01/02/10 | Run `leaseEpoch` number vs bigint wire/DB, Task.activeRunId and goalRevision fence; lease takeover; GW-T09/T14 |
| GW-X03 | P0 CHAT_09/10 | tenant↔workspace binding, policyEpoch revocation linearization and admin-defined risk; GW-T02/T03/T17 |
| GW-X04 | P0 CHAT_10 | tool intent T1/T2/T3 DB schema & outbox, separate ledger, budget reservation; GW-T06/T10 |
| GW-X05 | P0 CHAT_03 (03-03/04) | Approval token single use & uncertain effect ledger; never trust adapter declaration; GW-T07/T10 |
| GW-X06 | P1 CHAT_08 | response projections, 202 decision semantics and uncertainty UX; GW-T05/T11 |
| GW-X07 | P1 CHAT_04/05 | Browser session owner and provider operation status API semantics; GW-T10/T16 |
| GW-X08 | P1 CHAT_11 | cross-module kill fixture; cap performance measurement; GW-T06/T09/T20 |
| GW-X09 | P1 CHAT_13/14/15 | Event/Recipe/file ingestion must use gateway; GW-T15/T18 |
| GW-X10 | P1 CHAT_01/08/10 | optional Task.conversationId for event/scheduler task; migration and API projection; GW-T17 |

## 17. Evidence ledger & verification limits
- [S1] [PydanticAI source](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L321-L355) / [test](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_run_cancellation.py): 2026-10-30 **Code verified (read), tests not executed**; MIT [license](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/LICENSE).
- [S2] [OpenAI Agents approvals](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_internal/approvals.py) and [approval replay tests](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py), 2026-10-30 **Code verified (read), not run**, MIT (repo LICENSE).
- [S3] [Mastra thread runtime](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/thread-stream-runtime.ts), 2026-10-30 **Code verified**, Apache-2.0 general non-ee.
- [S4] [MCP SDK callTool](https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/client/src/client/client.ts#L2331-L2493), 2026-10-30 **Code verified**, mixed MIT/Apache-2.0 transition.
- [S5] [PydanticAI issue 10926](https://github.com/pydantic/pydantic-ai/issues/10926), [Mastra issue 27485](https://github.com/mastra-ai/mastra/issues/27485), [LangGraph issue 9397](https://github.com/langchain-ai/langgraph/issues/9397), 2026-10-30 **Issue report**; not separately reproduced.
- [S6] [Project requirements](../00-foundation/project-brief.md), [Architecture Rules](../00-foundation/architecture-rules.md), [SC-01..16](../00-foundation/acceptance-scenarios.md), latest main `b45d4e9`. Our validation/caps and storage design = **Hypothesis**, not SDK contract.
