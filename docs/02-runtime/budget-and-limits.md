# Budget and Limits — Detailed Implementation Specification

> Owner CHAT_02 | Draft / [PROVISIONAL: CHAT_01/03/09/10] | Rev 0.1 | Research 2026-10-30 | Base main 3044c046b1caeea70c5ef6edfea7c57387c6c6da

## 0. 結論/採用設計
予算は表示用の加算カウンタではなく、LLM/Tool実行前に保守的な見積額を**原子的に予約**し、Provider usageを受信したら1 attemptのreceiptとして精算するAdmission制御。Task・Run・親子Taskの費用/時間/Token/steps/concurrencyをすべて有限値で保持。Billing未到着やtimeoutを0円として再配分しない。絶対額を経済的保証にしない（Provider後払い・二重モデル要求など）。

## 1. Scope/非Scope/Acceptance
Run/Task/parent budget envelope、reservation/settlement、preflight refusal、未知使用量の見積、最大step/時間/トークン・並列度・Tool総数、parent-child budget division。請求書の最終金額・Provider契約・Tool実行権限は他owner。Watch/Event新Runも同じadmissionを通す。
内部の上限・LifecycleだけをCHAT_02が定める。CHAT_01が共有State/Event/ID、CHAT_03がTool policyとSide-effect、CHAT_09が認可、CHAT_10が物理DDL、CHAT_11がE2E正本。該当SC: SC-02/05/07/08/09/10/12/13。

## 2. OSS実コードとテスト監査
| OSS source snapshot | Code and tests inspected | Adoption/limits |
|---|---|---|
| PydanticAI c2ac8cc MIT | pydantic_ai_slim/pydantic_ai/usage.py::RunUsage.incr, RequestUsage.incr, tests/test_usage_limits.py::test_request_token_limit/test_response_token_limit/test_stream_text_enforces_output_token_limit_mid_stream | token/time gateの参考、USD単位での分散予約は自前 |
| OpenAI Agents Python cd26bbb MIT | src/agents/usage.py::Usage.add,_mark_request_completed_without_usage, tests/test_max_turns.py::test_non_streamed_max_turns/test_streamed_max_turns; src/agents/run.py::AgentRunner._run_impl | missing usageを区別、max turnsは安全上限が必要。SDK max_turns=None許容例を本Harnessには導入しない |
| Mastra ec3ba11 Apache-2.0 | packages/core/src/agent/durable/__tests__/durable-agent-step-budget-recovery.test.ts (recover ceiling/default) | recovery後のStep計数をチェックポイントから再構築 |

採用: DB-backed conservative reservation and immutable usage receipts at each attempt。却下: 完了後のusage足し算だけ、maxTurns=unlimited、未知コスト0、子Runに親の全budgetを再付与。SDKのdemoや測定値をセルフホストHarnessの保証に流用しない。独自実装はライセンスを守り設計パターンのみ参照。

## 3. 依存関係/正本
~~~text
Task + Run authenticated principal (CHAT_01/09)
           -> Budget and Limits
           -> AgentLoop / ModelAdapter
           -> Tool Gateway (CHAT_03) -> side-effect ledger
           -> DB/Checkpoint/Usage/Outbox (CHAT_10)
           -> SSE/UI (CHAT_08)
~~~
Task.conversationId null可（Watch/Event）。Run.leaseEpochはsafe integer/DB bigintのproposal、Task.recordVersionは初期1、goalRevisionとRun Epoch/Tool attempt fenceは相互代用禁止。Task.activeRunIdとRun.owner/tenantが一致しなければfail closed。

## 4. 要求定義
| REQ | Strength | Observable contract | SC |
|---|---|---|---|
| BDG-001 | MUST | Task/Runに有限cost/time/step/token/tool count/concurrency上限を必須とする | SC-02/05/07/08/09/10/12/13 |
| BDG-002 | MUST | LLM開始前に上限の保守的予約をatomic CASで確保 | SC-02/05/07/08/09/10/12/13 |
| BDG-003 | MUST | Tool開始前にToolコストとrate/concurrencyをreserveしGatewayへ伝える | SC-02/05/07/08/09/10/12/13 |
| BDG-004 | MUST | reservation+settlementはattemptIdごとに高々一回の確定記録 | SC-02/05/07/08/09/10/12/13 |
| BDG-005 | MUST | 外部請求量不明ならUNKNOWNのままreserveを解除しない | SC-02/05/07/08/09/10/12/13 |
| BDG-006 | MUST | Streaming途中でもoutput tokens/時間 ceilingで新しい操作を停止 | SC-02/05/07/08/09/10/12/13 |
| BDG-007 | MUST | 親子Taskは親の利用可能予算より大きいreservationを持てない | SC-02/05/07/08/09/10/12/13 |
| BDG-008 | MUST | Budget上限で止まる場合はPartialと費用/未確定usageを記録 | SC-02/05/07/08/09/10/12/13 |
| BDG-009 | MUST | Model fallbackやHTTP retryごとに追加費用を正しく予約 | SC-02/05/07/08/09/10/12/13 |
| BDG-010 | MUST | 異通貨/USD conversionはobservedAt/rate sourceとDecimal系で記録 | SC-02/05/07/08/09/10/12/13 |
| BDG-011 | MUST | 費用0や無料ToolをLLM自己申告で無検証許可しない | SC-02/05/07/08/09/10/12/13 |
| BDG-012 | MUST | Worker crash後にusage snapshotをrecoverして上限をリセットしない | SC-02/05/07/08/09/10/12/13 |
| BDG-901 | SHOULD | 安全性を維持しつつ実測ベンチマークに基づき最適化 | SC-09 |
| BDG-902 | EXPERIMENT | 複数Model/Plugin実行へ段階的展開しregression無し | SC-09 |

## 5. Input/Output Internal Port案（共有Schemaではない）
~~~ts
type MicroUsd = string; // unsigned decimal integer, no JS float math
interface BudgetEnvelope {
 taskId:string;runId:string;goalRevision:string;
 costLimitMicroUsd:MicroUsd; tokensMax:number;
 stepsMax:number;toolsMax:number;wallDeadlineUtc:string;
 readConcurrencyMax:number;effectConcurrencyMax:1;
}
interface Reservation { id:string;attemptId:string;scopeId:string;
 maxCostMicroUsd:MicroUsd;expiresAt:string;state:"HELD"|"SETTLED"|"UNKNOWN";
 priceSnapshotRef:string; }
interface BudgetPort {
 authorizeAndReserve(scope:BudgetEnvelope, attemptId:string,
 estimate:Estimate):Promise<Reservation>;
 settleExactlyOnce(id:string,observedUsage:UsageReceipt):Promise<void>;
 holdUnknown(id:string):Promise<void>;
}
~~~
Validation: external limit values must be finite, positive, correct units, validated by server policy. Actor/owner/tenant/workspace derived only from authenticated server DB. Record of Schema/Policy version and goalRevision required. New type names are proposals and not existing OSS exports.

## 6. 状態遷移/Sequence
~~~text
Run admission -> load budget + spent + held
 -> atomic reserve for Model/Tool attempt -> execute bounded
 -> usage available? -> settle receipt with CAS, release unused reserved
 -> usage unknown? -> retain HELD/UNKNOWN with conservative cap
 -> reached ceiling? -> stop further steps with PARTIAL
Crash -> reload held+settled, never reset counters
Parent -> reserve allocation BEFORE child admission
~~~

## 7. Algorithm/pseudocode
~~~ts
async function chargeAttempt(run,estimate,action) {
 const reservation=await budgetDb.tx(async tx=>{
   const b=await tx.lockBudgetAndAncestors(run);
   const conservative=upperBoundCost(estimate,b.priceSnapshot);
   if(!finiteFits(b.spent,b.held,conservative,b.cap)) throw BudgetExceeded();
   return tx.insertUniqueReservation(run.attemptId,conservative);
 });
 try {
   const result=await action();
   await budgetDb.settleIdempotent(reservation.id,result.usage);
   return result;
 } catch(e) {
   await budgetDb.recordUnknownOrFailedUsage(reservation.id,e);
   throw e;
 }
}
~~~
Each persistence step runs a short CAS/fenced transaction (never holds row locks across network). Old Worker cannot authorize new Tool. External effects cannot be exactly-once guaranteed by in-process middleware or budget alone.

## 8. Failure/timeout/cancel/race
| Event | Required behavior |
|---|---|
| Provider token usage missing | reservation stays UNKNOWN, never 0 |
| fallback after 503 | new attemptId/reservation; original maybe billed |
| parallel child reservations | lock parent allocation; sum <= cap |
| after model request before usage commit crash | recover held cap and provider lookup |
| simultaneous step budget race | one wins CAS, loser does not start |
| output stream exceeds token cap | cancel stream before next Tool; record estimated usage |
| clock deadline vs tool running | no NEW tool, in-flight effects reconciled |
| exchange rate missing/stale | no unsupported USD estimate, refuse or conservative cap |
| operator tries no limit | config validation rejects finite requirement |
Cancel/Steerの先後はTool dispatch claimとDB controlVersionで線形化、既開始のside-effectはGatewayで問い合わせする。Unknown receiptを勝手にretryせずWAITING_RECONCILIATION。プロセス再起動しても同じBudget/Hook receiptを繰り返し書かない。

## 9. Security/Privacy
Host shell/SSH/Docker socket/privileged container、任意MCPコード実行、未承認購入/予約/投稿、credential prompt・ログ記録を禁じる。External content is untrusted data; cannot raise authority or change budget. Policy/Secret/effect checks are mandatory and not overridable by middleware. SSRF/egress performed by Gateway/Browser Worker. Lack of authorization=default deny, no new charge/Tool start.

## 10. Trace/Performance
per model/Tool provider input/output/cached tokens, reserved/settled/unknown microUSD, estimated vs billed delta, fallback duplication, budget denial causes, active child allocations, p95 admission overhead and cost/task. Never quote marketing savings.
暫定値: 1Run最大24 LLM turns、60 Tool proposals、30min、read parallel4/effect parallel1、USD2 default ceiling (user bounded overrides)。いずれも設計仮置きで実測SLOではない。cache tokensとinput/output、provider price snapshot、unknown billed amountsは混同せず別記録。Bench条件固定でp50/p95と証拠品質も測る。

## 11. Persistence/Migration [PROVISIONAL: CHAT_10]
BudgetEnvelope versioned by task/Run with parent budget link, reservations unique (scope,attemptId), cost usage receipts eventId unique; atomic parent+child reserve. Distinguish reserved/settled/actual and estimated, data sources and currency. Transactions hold DB locks only while reserve/settle, external request outside.
Shared DDL不一致: UUIDv7とgen_random_uuid、nullable Task.conversationId、recordVersion初期値1対DB revision0、Task.goalRevision/activeRunId、Run.leaseEpoch保存の解消がP0。新規table/columnsの最終名称を本書で勝手に確定しない。expand/contractと旧snapshot schema adapter/停机migrationを設計。

## 12. REST/UI/Telegram [PROVISIONAL: CHAT_08]
Display hard cap, spent/held/unknown, estimated USD and time; when limit reached, show preserved evidence and Partial. Admin config changes do not silently authorize a previously denied irreversible action.
Event outboxのcommitまでは完了扱いせず、SSEはreplay cursorを使う。Secret/Raw arguments公開禁止。Budget exceedやHook failureではPartial/Uncertainを明示して新Tool発火を止める。

## 13. 実装PR分割
BDG-A Decimal/bounded config and fake token metering; BDG-B atomic reserve/settle and parent allocations; BDG-C fallback/unknown usage; BDG-D mid-stream cancel + UI; BDG-E fault injection and benchmarking.

## 14. Tests (all designed / NOT RUN)
| TEST ID | Setup/action/expected |
|---|---|
| BDG-T01 | missing or unlimited Budget rejected before LLM |
| BDG-T02 | concurrent model reserve near cap→one request starts |
| BDG-T03 | Tool cost gate denied→executor0 |
| BDG-T04 | same attempt settled twice→cost counted once |
| BDG-T05 | usage absent on network timeout→UNKNOWN retained |
| BDG-T06 | stream output crosses ceiling→no further Tool |
| BDG-T07 | two children competing budget→sum<=parent |
| BDG-T08 | cost cap reached→partial preserving evidence |
| BDG-T09 | fallback 503 charges two attempts conservatively |
| BDG-T10 | currency rate missing→no false-zero estimate |
| BDG-T11 | Tool claims zero cost but policy cost positive→policy applied |
| BDG-T12 | worker restart held reservations→not double released |
| BDG-T99 | 100 mock RunsでCPU/RSS/tokens/USD/latency/accuracy計測、再現fixture付きで初めて閾値決定 |

## 15. Acceptance criteria / Traceability
| MUST | TEST | SC |
|---|---|---|
| BDG-001 | BDG-T01 | SC-02/05/07/08/09/10/12/13 |
| BDG-002 | BDG-T02 | SC-02/05/07/08/09/10/12/13 |
| BDG-003 | BDG-T03 | SC-02/05/07/08/09/10/12/13 |
| BDG-004 | BDG-T04 | SC-02/05/07/08/09/10/12/13 |
| BDG-005 | BDG-T05 | SC-02/05/07/08/09/10/12/13 |
| BDG-006 | BDG-T06 | SC-02/05/07/08/09/10/12/13 |
| BDG-007 | BDG-T07 | SC-02/05/07/08/09/10/12/13 |
| BDG-008 | BDG-T08 | SC-02/05/07/08/09/10/12/13 |
| BDG-009 | BDG-T09 | SC-02/05/07/08/09/10/12/13 |
| BDG-010 | BDG-T10 | SC-02/05/07/08/09/10/12/13 |
| BDG-011 | BDG-T11 | SC-02/05/07/08/09/10/12/13 |
| BDG-012 | BDG-T12 | SC-02/05/07/08/09/10/12/13 |

## 16. Unresolved and Cross-team proposals
P0 CHAT_10 cost reservation tx/unique usage, CHAT_01 money and state types, CHAT_03 Tool Provider budget/fence and cost profile, CHAT_09 tenant quotas, CHAT_07 Watch recurrence budget policy, CHAT_08 usage UI, CHAT_11 injected unknown billing and fallback verification. Initial USD2 / Run defaults are not Locked.
Contract ownersの合意と実試験を経るまではDraft。実コード未実装・未テストであり、Verified/Lockedは主張しない。

## 17. Sources (inspected 2026-10-30)
- [S1] Code verified https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/usage.py
- [S2] Test verified https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_usage_limits.py
- [S3] Code verified https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/usage.py ; https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run.py
- [S4] Test verified https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_max_turns.py
- [S5] Test verified https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-step-budget-recovery.test.ts
Evidence: Code verified = precise source function inspection; Test definition verified = tests read not run. License MIT for OpenAI Agents Python/PydanticAI; Mastra @mastra/core Apache-2.0 declared. External issues cited only as reports if any.


### Round 4 cross-owner int64 reconciliation (2026-10-30)
Version/Epoch fields above follow CHAT_01's canonical signed-int64 **decimal string** wire format, validated and compared with internal BigInt and persisted as PostgreSQL BIGINT; JSON number conversion is prohibited. Other scalar number fields (bounded counts, percentages, step ordinals) remain number unless separately versioned. Regression test proposed: `9007199254740992` and `9223372036854775806` round-trip exactly, reject unsafe Number and stale authority; not executed against Harness code.
