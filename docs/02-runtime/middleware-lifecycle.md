# Middleware Lifecycle — Detailed Implementation Specification

> Owner CHAT_02 | Draft / [PROVISIONAL: CHAT_01/03/09/10] | Rev 0.1 | Research 2026-10-30 | Base main 3044c046b1caeea70c5ef6edfea7c57387c6c6da

## 0. 結論/採用設計
MiddlewareはPolicyそのものではなく観測・構造検証・コンテキスト整理・決定的後処理のプラグイン。固定のHook順序、タイムアウト、権限縮小、例外隔離、retry中の冪等実行境界を仕様化。危険なTool/LLMは必ずMiddlewareとは独立した決定的Gateを通る。Hookにuntrusted HTMLを渡して権限を拡大させない。

## 1. Scope/非Scope/Acceptance
before/after Run/Model/Tool/Verifier/ContextのHookとwrap、観測、Hook失敗時の分類、restart safe hooks。承認・SSRＦ・Budget・Executor権限は他OwnerでありMiddlewareで置換禁止。
内部の上限・LifecycleだけをCHAT_02が定める。CHAT_01が共有State/Event/ID、CHAT_03がTool policyとSide-effect、CHAT_09が認可、CHAT_10が物理DDL、CHAT_11がE2E正本。該当SC: SC-05/07/08/09/16。

## 2. OSS実コードとテスト監査
| OSS | Inspected real functions/tests | Lessons |
|---|---|---|
| OpenAI Agents Python cd26bbb MIT | src/agents/lifecycle.py::RunHooksBase.on_llm_start/on_llm_end/on_tool_start/on_tool_end; tests/test_agent_hooks.py::test_non_streamed_agent_hooks/test_streamed_agent_hooks | Hook引数と呼出順の検証。任意Hookをsecurity gateとはしない |
| OpenAI Agents Python cd26bbb MIT | src/agents/guardrail.py::InputGuardrail.run; tests/test_guardrails.py::test_parallel_guardrail_may_not_prevent_tool_execution | 同時実行guardrailがTool前に必ずブロックするとは限らない点が重要 |
| PydanticAI c2ac8cc MIT | tests/test_capability_hooks.py::test_run_hook_order/test_wrap_run_can_recover_from_error; pydantic_ai_slim/pydantic_ai/_agent_graph.py::_HandlerState | 回復可能なHookと出力変更の危険を認識 |
| DeepAgents 3efa098 MIT | middleware/summarization.py::_DeepAgentsSummarizationMiddleware and test_summarization_middleware.py | Context transformのHook分離の参考 |
採用: Typed staged hooks + mandatory non-bypassable gates + bounded sandbox。却下: 非同期Guardrailの結果を待たずTool開始、Hookの任意Executor invocation、Hook失敗を黙って成功扱い。SDKのdemoや測定値をセルフホストHarnessの保証に流用しない。独自実装はライセンスを守り設計パターンのみ参照。

## 3. 依存関係/正本
~~~text
Task + Run authenticated principal (CHAT_01/09)
           -> Middleware Lifecycle
           -> AgentLoop / ModelAdapter
           -> Tool Gateway (CHAT_03) -> side-effect ledger
           -> DB/Checkpoint/Usage/Outbox (CHAT_10)
           -> SSE/UI (CHAT_08)
~~~
Task.conversationId null可（Watch/Event）。Run.leaseEpochはsafe integer/DB bigintのproposal、Task.recordVersionは初期1、goalRevisionとRun Epoch/Tool attempt fenceは相互代用禁止。Task.activeRunIdとRun.owner/tenantが一致しなければfail closed。

## 4. 要求定義
| REQ | Strength | Observable contract | SC |
|---|---|---|---|
| MWL-001 | MUST | Hookはregistered manifestでID/version/priority/trust/capabilityを宣言 | SC-05/07/08/09/16 |
| MWL-002 | MUST | 固定順でbeforeRun→beforeModel→afterModel→beforeTool→afterTool→beforeVerify→afterVerify→afterRunを適用 | SC-05/07/08/09/16 |
| MWL-003 | MUST | mandatory Tool Gateway/Budget/Auth validationをHookでskip/replace不可能 | SC-05/07/08/09/16 |
| MWL-004 | MUST | Hookが元のowner/goalRevision/tool approval scopeを変更できない | SC-05/07/08/09/16 |
| MWL-005 | MUST | Hook timeout/throwを型付きerrorへ分類してfail closed | SC-05/07/08/09/16 |
| MWL-006 | MUST | retry可能Hookとnon-idempotent Hookを区別しduplicated executionを回避 | SC-05/07/08/09/16 |
| MWL-007 | MUST | Tool中断時にも既成功receiptは保存しafterToolの通知二重化を防ぐ | SC-05/07/08/09/16 |
| MWL-008 | MUST | Run停止・再起動時Hook execution attempt IDを保存し再入時修復 | SC-05/07/08/09/16 |
| MWL-009 | MUST | Hookが秘密情報・raw cookieを見ることはできずtyped redacted snapshotのみ | SC-05/07/08/09/16 |
| MWL-010 | MUST | Plugin/Skillが勝手にCore runtime codeやcapabilityを変更できない | SC-05/07/08/09/16 |
| MWL-011 | MUST | 観測Hookはperformance capとRate limitでRunを飢餓させない | SC-05/07/08/09/16 |
| MWL-901 | SHOULD | 安全性を維持しつつ実測ベンチマークに基づき最適化 | SC-09 |
| MWL-902 | EXPERIMENT | 複数Model/Plugin実行へ段階的展開しregression無し | SC-09 |

## 5. Input/Output Internal Port案（共有Schemaではない）
~~~ts
type HookStage="BEFORE_RUN"|"BEFORE_MODEL"|"AFTER_MODEL"|"BEFORE_TOOL"
 |"AFTER_TOOL"|"BEFORE_VERIFY"|"AFTER_VERIFY"|"AFTER_RUN";
interface MiddlewareManifest { id:string;version:string;
 stages:HookStage[];priority:number;maxDurationMs:number;
 needsCapabilities:string[];retrySafety:"PURE"|"ONCE_WITH_LEDGER";
}
interface FrozenHookContext {
 taskId:string;runId:string;goalRevision:string;
 leaseEpoch:string;stepOrdinal:number;redactedInputRef:string;
}
interface HookPort { execute(stage:HookStage,ctx:FrozenHookContext,
 manifest:MiddlewareManifest):Promise<ValidatedHookPatch>; }
~~~
Validation: external limit values must be finite, positive, correct units, validated by server policy. Actor/owner/tenant/workspace derived only from authenticated server DB. Record of Schema/Policy version and goalRevision required. New type names are proposals and not existing OSS exports.

## 6. 状態遷移/Sequence
~~~text
Run claim -> mandatory Auth + Budget
  -> beforeRun -> beforeModel -> Model
  -> afterModel -> parse + frozen proposals
  -> beforeTool (may constrain/refuse ONLY)
  -> MUST Gateway [Schema/Policy/Approval/Budget/fence] -> Execute
  -> afterTool (ledger receipt exists)
  -> beforeVerify -> deterministic Verify -> afterVerify
  -> afterRun -> terminal event/outbox
Retry: each Hook has idempotent attempt state; recover committed outcomes
~~~

## 7. Algorithm/pseudocode
~~~ts
async function applyStage(stage,ctx,inputs) {
 const list=manifestStore.eligibleFor(stage,ctx).sort(byStablePriority);
 let value=inputs;
 for(const hook of list) {
   await assertRunEpochAndAuthCurrent(ctx);
   const prior=await hookLedger.find(ctx.runId,stage,hook.id,ctx.stepOrdinal);
   if(prior?.status==="COMPLETED") { value=mergeValidatedPatch(value,prior.patch);continue; }
   if(prior?.status==="UNKNOWN" && hook.retrySafety!=="PURE")
     return suspend("HOOK_UNCERTAIN");
   const patch=await runBoundedHookWithRedactedContext(hook,ctx,value);
   value=validateOnlyRestrictivePatch(value,patch); // cannot elevate policy
   await hookLedger.commitIdempotently(ctx,hook,patch);
 }
 return value;
}
~~~
Each persistence step runs a short CAS/fenced transaction (never holds row locks across network). Old Worker cannot authorize new Tool. External effects cannot be exactly-once guaranteed by in-process middleware or budget alone.

## 8. Failure/timeout/cancel/race
| Failure | Outcome |
|---|---|
| Security hook missing | mandatory Gateway still enforces; deny when required |
| beforeTool tries to enable purchase | rejected patch, executor0 |
| afterTool duplicate callback | receipt already committed, Hook deduped |
| Hook timeout | abort Hook, typed partial/fail-closed |
| Slow observation Hook | finite timeout, no unbounded queue |
| Crash after external notification Hook | ONCE_WITH_LEDGER UNKNOWN and reconcile |
| Hook changed goalRevision | validation rejects |
| Hook target secret ref outside scope | Owner check rejects |
| Mismatch Hook schema after restart | migration or deny, no unsafe replay |
Cancel/Steerの先後はTool dispatch claimとDB controlVersionで線形化、既開始のside-effectはGatewayで問い合わせする。Unknown receiptを勝手にretryせずWAITING_RECONCILIATION。プロセス再起動しても同じBudget/Hook receiptを繰り返し書かない。

## 9. Security/Privacy
Host shell/SSH/Docker socket/privileged container、任意MCPコード実行、未承認購入/予約/投稿、credential prompt・ログ記録を禁じる。External content is untrusted data; cannot raise authority or change budget. Policy/Secret/effect checks are mandatory and not overridable by middleware. SSRF/egress performed by Gateway/Browser Worker. Lack of authorization=default deny, no new charge/Tool start.

## 10. Trace/Performance
Hook execution p50/p95, failure modes, retry counts, redacted patch sizes, bypass attempts, total added token/cost, correlation runId+stage+attempt, duplicate execution replays. Bound perHook execution by finite maxDuration and memory limits.
暫定値: 1Run最大24 LLM turns、60 Tool proposals、30min、read parallel4/effect parallel1、USD2 default ceiling (user bounded overrides)。いずれも設計仮置きで実測SLOではない。cache tokensとinput/output、provider price snapshot、unknown billed amountsは混同せず別記録。Bench条件固定でp50/p95と証拠品質も測る。

## 11. Persistence/Migration [PROVISIONAL: CHAT_10]
Hook registrations versioned/immutable; run snapshot stores hook manifest digest, execution ledger unique (runId,stepOrdinal,stage,hookId,attempt), patch ref and immutable redacted audit. Stateful effects must use Tool Gateway; no separate direct external network side effect from Hook. Migration upgrades should not change hook behavior midrun without revalidation.
Shared DDL不一致: UUIDv7とgen_random_uuid、nullable Task.conversationId、recordVersion初期値1対DB revision0、Task.goalRevision/activeRunId、Run.leaseEpoch保存の解消がP0。新規table/columnsの最終名称を本書で勝手に確定しない。expand/contractと旧snapshot schema adapter/停机migrationを設計。

## 12. REST/UI/Telegram [PROVISIONAL: CHAT_08]
Show safe Hook lifecycle status and reason codes; no full prompt/log secrets. Operators can disable future Hook loads but in-flight replan/restart honors policy. Untrusted website cannot install/enroll Middleware.
Event outboxのcommitまでは完了扱いせず、SSEはreplay cursorを使う。Secret/Raw arguments公開禁止。Budget exceedやHook failureではPartial/Uncertainを明示して新Tool発火を止める。

## 13. 実装PR分割
MWL-A manifest/ordering/fake hooks; MWL-B readonly immutable context & patch validation; MWL-C Tool Gateway mandatory gates; MWL-D restart-safe ledger and failure isolations; MWL-E observability/security/fault tests.

## 14. Tests (all designed / NOT RUN)
| TEST ID | Setup/action/expected |
|---|---|
| MWL-T01 | unregistered Hook denied |
| MWL-T02 | before/after stage executes in strict order |
| MWL-T03 | beforeTool tries skip Gateway→executor0 |
| MWL-T04 | Hook changes owner/approval→rejected |
| MWL-T05 | throw/timeout fail closed |
| MWL-T06 | replay pure Hook deterministic and bounded |
| MWL-T07 | duplicate afterTool→single ledger action |
| MWL-T08 | crash between afterTool/commit→safe recover |
| MWL-T09 | Plugin asks cookie access→redacted no data |
| MWL-T10 | new Skill attempts runtime self-edit→deny |
| MWL-T11 | slow hook limited time, no starvation |
| MWL-T99 | 100 mock RunsでCPU/RSS/tokens/USD/latency/accuracy計測、再現fixture付きで初めて閾値決定 |

## 15. Acceptance criteria / Traceability
| MUST | TEST | SC |
|---|---|---|
| MWL-001 | MWL-T01 | SC-05/07/08/09/16 |
| MWL-002 | MWL-T02 | SC-05/07/08/09/16 |
| MWL-003 | MWL-T03 | SC-05/07/08/09/16 |
| MWL-004 | MWL-T04 | SC-05/07/08/09/16 |
| MWL-005 | MWL-T05 | SC-05/07/08/09/16 |
| MWL-006 | MWL-T06 | SC-05/07/08/09/16 |
| MWL-007 | MWL-T07 | SC-05/07/08/09/16 |
| MWL-008 | MWL-T08 | SC-05/07/08/09/16 |
| MWL-009 | MWL-T09 | SC-05/07/08/09/16 |
| MWL-010 | MWL-T10 | SC-05/07/08/09/16 |
| MWL-011 | MWL-T11 | SC-05/07/08/09/16 |

## 16. Unresolved and Cross-team proposals
P0 CHAT_01 Event/Error/versioning, CHAT_03 mandatory Tool Gateway policy, CHAT_09 sandbox/hook privileges/secret redaction, CHAT_10 idempotent Hook ledger, CHAT_06 Skill enablement approval, CHAT_08 operator controls, CHAT_11 adversarial Hook tests. Hook plugin trust policy and allowed external logging need ADR.
Contract ownersの合意と実試験を経るまではDraft。実コード未実装・未テストであり、Verified/Lockedは主張しない。

## 17. Sources (inspected 2026-10-30)
- [S1] Code verified https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/lifecycle.py ; https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/guardrail.py
- [S2] Test definition verified https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_agent_hooks.py ; https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_guardrails.py
- [S3] Test verified https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_capability_hooks.py
- [S4] Code verified https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/_agent_graph.py
- [S5] Code verified https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/summarization.py
Evidence: Code verified = precise source function inspection; Test definition verified = tests read not run. License MIT for OpenAI Agents Python/PydanticAI; Mastra @mastra/core Apache-2.0 declared. External issues cited only as reports if any.


### Round 4 cross-owner int64 reconciliation (2026-10-30)
Version/Epoch fields above follow CHAT_01's canonical signed-int64 **decimal string** wire format, validated and compared with internal BigInt and persisted as PostgreSQL BIGINT; JSON number conversion is prohibited. Other scalar number fields (bounded counts, percentages, step ordinals) remain number unless separately versioned. Regression test proposed: `9007199254740992` and `9223372036854775806` round-trip exactly, reject unsafe Number and stale authority; not executed against Harness code.
