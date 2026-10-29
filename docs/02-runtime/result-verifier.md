# Result Verifier — Detailed Implementation Specification
> CHAT_02 | Status Draft / provisional | Research 2026-10-30 | Revision 0.1 | main b45d4e989998663a00ef09673e69ae1f4191f00d

## 0. 結論・採用設計
LLMが『完了』と出力しても、User Goal、Tool台帳、出典の取得日時、商品・部屋・人数・価格条件を個別に検証し、PASS/NEED_MORE_EVIDENCE/PARTIAL/UNCERTAIN/REJECTEDで確定する。hard constraintは決定的Ruleで評価し、二次LLMの意見やスコアを保証に使わない。

## 1. 解決範囲と非目標
Final evidence manifestを評価する、期限切れ観測と同一性不整合を検出する、必要な追加Toolをbudget内で選ぶかPartial終了を判断。Result Verifierは実行承認・予約・金銭移動しない。Evidence schemaはCHAT_01正本。
User-specified conditions are higher-priority than LLM suggestions. Existing Framework is prior art rather than implementation dependency. Task/Goal domain type CHAT_01; approval/Tool Gateway CHAT_03; DB DDL CHAT_10; security CHAT_09; Evidence and State CHAT_01/02 verifier boundary. Cross-cutting acceptance SC-01/02/03/04/05/07/08/11/12。

## 2. OSS一次実装/テスト
| Upstream / pinned | Verified code/test | Caution |
|---|---|---|
| PydanticAI c2ac8cc MIT | _output.py::OutputValidator.validate, run_output_validate_hooks, tests/test_agent_output_schemas.py | schema passはWeb事実の真偽保証にならない |
| OpenAI Agents Python cd26bbb MIT | src/agents/guardrail.py::OutputGuardrail.run/InputGuardrail.run, tests/test_guardrails.py::test_blocking_guardrail_prevents_agent_execution | 事後guardrailとTool実行前policyは別 |
| Mastra ec3ba11 Apache-2.0 declared | goal/scorer.ts::createGoalScorer and goal/scorer.test.ts (structured tri-state scoring) | LLM judgeのスコアは決定的受入判定でない |
| LangGraph ffc3336 MIT | pregel/_loop.py::tick, tests/test_pregel.py::test_checkpoint_errors | checkpoint状態とEvidence成功の意味は別 |
採用: Criterion-specific deterministic verifier with uncertainty and evidence lineage。却下: LLM自称Success/第二モデルの賛成のみで確定、stale fetchedAtをlive quoteとして使用。ライセンス、SDKのPython/TypeScript差、途中停止と外部副作用を評価して独立実装する。参照ライブラリの能力を本Harnessで実測したとは言わない。

## 3. 依存性/正本/Authority
~~~text
Trusted User Goal -> immutable GoalRevision (CHAT_01/10)
    -> Planner or Verifier (CHAT_02)
    -> bounded AgentLoop -> Tool Gateway (CHAT_03)
    -> Observation/Evidence (CHAT_01/04/05)
    -> final verifier judgement -> atomic Task result / Event / Outbox
~~~
Goalの入力をプロンプトから保存する場合もAuth/tenant/ownerをサーバー側に固定。Task.conversationId=nullを受け付け、TaskとRunは独立ID。GoalRevisionとrecordVersion、leaseEpochを混同しない。

## 4. Requirements
| REQ-ID | Level | Verifiable expectation | Scenario |
|---|---|---|---|
| RVF-1 | MUST | Goal successCriteriaごとにEvidence参照と評価理由を残す | SC-01/02/03/04/05/07/08/11/12 |
| RVF-2 | MUST | Hard constraint失敗はPASSにならない | SC-01/02/03/04/05/07/08/11/12 |
| RVF-3 | MUST | 実測/推測/条件付き価格を別のconfidenceで扱う | SC-01/02/03/04/05/07/08/11/12 |
| RVF-4 | MUST | 値とcurrency/timezone/guest/room/variantの同一性を検証 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-5 | MUST | Evidenceにはsource URL/observedAt/fetchedAt/validUntil/原典hashが必要 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-6 | MUST | 未確認の最新空室/買取値をConfirmed表示しない | SC-01/02/03/04/05/07/08/11/12 |
| RVF-7 | MUST | 不明結果や不可逆副作用UNKNOWNがあるとSUCCESS不可 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-8 | MUST | 追加Tool選択は既存budget/approval/Gatewayを越えない | SC-01/02/03/04/05/07/08/11/12 |
| RVF-9 | MUST | Verifier自身のretryは有限、無情報GainはPartial | SC-01/02/03/04/05/07/08/11/12 |
| RVF-10 | MUST | 全評価をGoalRevisionとRunEpochに固定してCASでterminalを保存 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-90 | SHOULD | 同一fixtureで精度・費用・レイテンシーを再測定し改善可能 | SC-09 |
| RVF-91 | EXPERIMENT | 意思決定/Verifyに二次モデルを追加するのは効果が実測された場合のみ | SC-09 |

## 5. 変更できるinternal contracts（共通型ではない）
~~~ts
type Verdict="PASS"|"NEED_MORE_EVIDENCE"|"PARTIAL"|"UNCERTAIN"|"REJECTED";
interface CriterionEvaluation {
 criterionId:string;status:Verdict;goalRevision:number;
 evidenceIds:string[];reasonCode:string;observedAt:string|null;
 confidence:"CONFIRMED"|"CONDITIONAL"|"ESTIMATED"|"UNKNOWN";
}
interface ResultVerification {
 taskId:string;runId:string;goalRevision:number;
 evaluations:CriterionEvaluation[];verdict:Verdict;
 additionalToolProposalIds:string[];verifiedAt:string;schemaVersion:1;
}
interface VerifierPort { verify(goalRef:string,resultRef:string,
 ledgerRef:string,epoch:number):Promise<ResultVerification>; }
~~~
Validation: immutable canonical user request and structured constraints store schemaVersion、source/user confirmation timestamp、IANA timezone、currency、decimal price。UUIDv7 generated on trusted server, no untrusted source ID can override tenant/owner/Goal. Bound strings/arrays/nesting. No client-controlled policy or all-true guard. New ports are proposals [PROVISIONAL: CHAT_01/10].

## 6. 状態とシーケンス
~~~text
Model candidate final -> load immutable Goal/current revision + Tool receipts
 -> check provenance, freshness, numeric equivalence, hard constraints
 -> every criterion PASS? -> PASS
 -> recoverable evidence gap + budget? -> NEED_MORE_EVIDENCE -> AgentLoop
 -> unknown side-effect? -> UNCERTAIN
 -> permanent unsupported/limit? -> PARTIAL/REJECTED
 -> CAS commit terminal + outbox, only then UI success
~~~

## 7. Algorithm
~~~ts
function verifyFinal(goal,manifest,ledger,budget) {
 if(ledger.hasUnknownEffect) return verdict("UNCERTAIN");
 const verdicts=goal.successCriteria.map(c=>
   evaluateRuleAndEvidence(c,manifest,goal.hardConstraints));
 if(verdicts.some(x=>x.status==="REJECTED")) return verdict("REJECTED");
 if(verdicts.every(x=>x.status==="PASS")) return verdict("PASS");
 const gaps=verdicts.filter(x=>x.status!=="PASS");
 if(budget.hasRoom && gaps.some(g=>safeNewEvidencePossible(g)))
   return proposedGatewayReadsOnly(gaps);
 return verdict("PARTIAL");
}
~~~
Invariants: Every model claim is checked against latest effective goalRevision; stale observation is not current. External Tool effects only through Gateway. No DB lock held across network. Deterministic checks are preferred to a second LLM; provider output is always untrusted until verified.

## 8. Failure/retry/race matrix
| Fault / subtle mismatch | Decision |
|---|---|
| Similar model different storage | reject entity match and profit |
| Coupon expires before purchase | conditional, not confirmed profit |
| Hotel price for 2 not 3 people | fail hard constraint |
| Site says available but last fetched yesterday | stale, require fresh evidence |
| X repost without official verification | conditional/unknown |
| Currency float imprecision | decimal exact arithmetic |
| Provider outage | partial with unavailable source |
| Old goalRevision result after Steer | reject stale verdict |
| Approval waiting/unknown effect | partial/uncertain; no success |
If Provider times out, stop within finite Budget and give partial/unknown; never fabricate "confirmed" fact. If Cancel/Steer committed before a new Tool dispatch, gateway refuses it. Old model callback cannot change Task terminal result after leaseEpoch loss.

## 9. Security, privacy
All Web/SNS/HTML/Tool output are untrusted observations, not instructions. System/Goal/Policy cannot be overwritten by fetched text. LLM does not receive credentials/Cookies or internal network access. Tool approvals bind actor/tenant/Run/GoalRevision/tool/args hash and expire. SSRF/egress and role checks at Gateway. Redact secrets/PII from prompt preview, event and log. Retain Evidence provenance/observedAt/confidence but no raw login data.

## 10. Trace and resource limits
criterion coverage, incorrect-success count, source link coverage, observedAt gap, numeric-difference count, judge/manual review disagreement, additional Tool count, estimated and billed cost. Verification outcome reproducible from same immutable evidence fixture.
Initial provisional finite gates: 24 model turns, max60 Tool proposals, 30-minute active Run, maximum4 concurrent independent read tools, one side-effect; token/cost caps via budget module. All are defaults pending benchmarking and user policy. Progress must not be 100% solely because model says done.

## 11. DB/transaction/migration [PROVISIONAL: CHAT_10]
Append-only result_verification (proposal; CHAT_10 owns DDL) keyed runId/goalRevision/evidenceManifestHash/verifierVersion; terminal result CAS when Run leaseEpoch and Task current revision agree. Evidence artifact remains immutable even if stale; UI labels stale instead of overwrite.
Task recordVersion init1 vs current SQL revision default0, public UUIDv7 vs SQL gen_random_uuid(), nullable Task.conversationId vs SQL NOT NULL, Run.leaseEpoch/Task.activeRunId missing from SQL: cross-team blockers, not silently fixed by planner. At every commit use CAS by latest Run leaseEpoch + task recordVersion and store Event/Outbox atomically; retry only transactional writes, never foreign effects.

## 12. API/UI [PROVISIONAL: CHAT_08]
Show claim table with criterion, exact source URL, observation age, confirmed/conditional/estimated and reason for uncertainty. A 0-result hotel search is not silently broadened. Mobile/Telegram shorten presentation but preserve evidence IDs.
Server response must expose inspectedAt/observedAt and which constraints are verified vs assumed; live current price/availability must not be invented. User can revise Goal through authorized steer, with immutable history and version conflict 412/428.

## 13. Implementation PR order
RVF-1 deterministic constraint comparators + decimal/date/time validation; RVF-2 provenance/freshness Evidence adapter; RVF-3 terminal CAS + NEED_MORE_EVIDENCE loop; RVF-4 adversarial and business E2E fixtures.
Never expose unapproved actions before cross-team contracts pass tests.

## 14. Tests (all **Designed / NOT RUN**)
| TEST | Fixture→action→expected |
|---|---|
| RVF-T1 | each criterion PASS with valid evidence→terminal |
| RVF-T2 | hard constraint violated despite model positive text→REJECTED |
| RVF-T3 | conditional coupon→not confirmed profit |
| RVF-T4 | wrong storage/color/JAN→not same entity |
| RVF-T5 | hotel 2-person quote for 3-person goal→not PASS |
| RVF-T6 | stale availability timestamp→NEED_MORE_EVIDENCE |
| RVF-T7 | UNKNOWN booking effect→UNCERTAIN |
| RVF-T8 | budget exhausted while missing source→PARTIAL |
| RVF-T9 | two verifier retries same evidence→finite stop |
| RVF-T10 | Steer while verdict pending→old verdict CAS rejects |
| RVF-T99 | deterministic fixtures x100 → measure false-success, p95 cost/latency; baseline recorded, no claimed numbers |

## 15. Traceability
| MUST REQ | TEST-ID | SC |
|---|---|---|
| RVF-1 | RVF-T1 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-2 | RVF-T2 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-3 | RVF-T3 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-4 | RVF-T4 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-5 | RVF-T5 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-6 | RVF-T6 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-7 | RVF-T7 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-8 | RVF-T8 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-9 | RVF-T9 | SC-01/02/03/04/05/07/08/11/12 |
| RVF-10 | RVF-T10 | SC-01/02/03/04/05/07/08/11/12 |

## 16. Pending decisions and Cross-team proposals
CHAT_01 Evidence/Artifact/Result Error schemas; CHAT_10 verifier receipt/terminal CAS; CHAT_03 tool gateway reads for missing Evidence and UNKNOWN ledger; CHAT_05 product/hotel providers; CHAT_12 entity resolver; CHAT_08/UI explainable uncertain; CHAT_11 adversarial/source fixtures.
All unresolved cross-owner fields are [PROVISIONAL]; do not mark Locked before accepted owner changes and actual test pass.

## 17. Sources (observed 2026-10-30)
- [S1] https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/_output.py ; https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_agent_output_schemas.py
- [S2] https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/guardrail.py ; https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_guardrails.py
- [S3] https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/goal/scorer.ts ; https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/goal/scorer.test.ts
- [S4] https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py
Code verified = actual source inspected; test definition verified = test file read but not run. MIT for PydanticAI/OpenAI Agents Python/LangGraph/Deep Agents as recorded in inspected project metadata; Mastra @mastra/core declares Apache-2.0. No implementation/tests executed in this spec repository.
