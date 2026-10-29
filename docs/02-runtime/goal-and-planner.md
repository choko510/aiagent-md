# Goal and Planner — Detailed Implementation Specification
> CHAT_02 | Status Draft / provisional | Research 2026-10-30 | Revision 0.1 | main b45d4e989998663a00ef09673e69ae1f4191f00d

## 0. 結論・採用設計
Goalはユーザーの検証可能な目的とhard constraintsをimmutable revisionsとして保持する。Plannerは候補Actionを作れるが、費用/期限/価格/人数/日付/権限の制約を緩和できない。Steerは旧Planを破棄するのではなく、どのStepが旧revisionで実施され、どのEvidenceが再利用可能かを記録して再計画する。成功にはResult Verifierの独立判定が必須。

## 1. 解決範囲と非目標
任意Web調査のgoal理解と構造化、動的計画、並列化の適格判定、progress/achievabilityとSteer条件差分。Tool PolicyとEvidence validatorは持たない。PlanはLLMへの指示メモであり、強いユーザー条件の代用にならない。
User-specified conditions are higher-priority than LLM suggestions. Existing Framework is prior art rather than implementation dependency. Task/Goal domain type CHAT_01; approval/Tool Gateway CHAT_03; DB DDL CHAT_10; security CHAT_09; Evidence and State CHAT_01/02 verifier boundary. Cross-cutting acceptance SC-01/03/04/05/07/10/12。

## 2. OSS一次実装/テスト
| Upstream and fixed SHA | Actual files/classes/tests inspected | Lessons |
|---|---|---|
| Mastra ec3ba11 / Apache-2.0 | packages/core/src/agent/goal/objective.ts::readObjective/writeObjective/resolveEffectiveGoalSettings, goal/scorer.ts::createGoalScorer, goal/scorer.test.ts, durable/__tests__/durable-agent-goal.test.ts | objectiveと進捗judgementは独立にできるがscorer単体で真実保証なし |
| LangGraph ffc3336 / MIT | pregel/_loop.py::tick/_put_checkpoint, tests/test_pregel.py::test_pending_writes_resume | dynamic planner stateはcheckpoint後に確定 |
| DeepAgents 3efa098 / MIT (license meta must be revalidated for direct code use) | middleware/subagents.py::_build_task_tool/TaskToolSchema and tests/unit_tests/test_todo_middleware.py | plannerの書換と単一メッセージからの複数write競合を制限 |
| PydanticAI c2ac8cc / MIT | _output.py::OutputValidator.validate, tests/test_agent_output_schemas.py | structured output validationが別途必要 |
採用: Goal snapshot + deterministic hard-constraint validator + bounded replanning。却下: LLM自由文Planのみでhard constraintを上書き、変更前の空室/価格を再確認無しで採用。ライセンス、SDKのPython/TypeScript差、途中停止と外部副作用を評価して独立実装する。参照ライブラリの能力を本Harnessで実測したとは言わない。

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
| GPL-1 | MUST | Goal保存時にimmutable GoalRevisionとschemaVersion/authorTimeを作成 | SC-01/03/04/05/07/10/12 |
| GPL-2 | MUST | Hard constraintsとsoft preferencesを区別して保存・更新する | SC-01/03/04/05/07/10/12 |
| GPL-3 | MUST | Goal Revisionを変えるのは認可済みUser Steerのみ | SC-01/03/04/05/07/10/12 |
| GPL-4 | MUST | Plan各Stepは目的/入力/依存/Evidence gap/Tool候補/費用見積を持つ | SC-01/03/04/05/07/10/12 |
| GPL-5 | MUST | Plan refreshは旧Stepと新GoalRevisionを照合し古いApprovalを失効 | SC-01/03/04/05/07/10/12 |
| GPL-6 | MUST | ユーザー条件を勝手に緩和しない（候補0件ならPartial） | SC-01/03/04/05/07/10/12 |
| GPL-7 | MUST | 最大steps/深さ/parallel read quotaの有限Admissionを守る | SC-01/03/04/05/07/10/12 |
| GPL-8 | MUST | 結果をVerifierなしにSUCCESSへ確定しない | SC-01/03/04/05/07/10/12 |
| GPL-9 | MUST | Hard fact/calculationは決定的processor、LLM自由文は説明だけ | SC-01/03/04/05/07/10/12 |
| GPL-10 | MUST | Price/date/person/timezone/currencyを計画中も不変の正規値で保持 | SC-01/03/04/05/07/10/12 |
| GPL-90 | SHOULD | 同一fixtureで精度・費用・レイテンシーを再測定し改善可能 | SC-09 |
| GPL-91 | EXPERIMENT | 意思決定/Verifyに二次モデルを追加するのは効果が実測された場合のみ | SC-09 |

## 5. 変更できるinternal contracts（共通型ではない）
~~~ts
interface GoalSnapshot {
 taskId:string; revision:number; createdByUserId:string;
 sourceCommandId:string; goalTextRef:string;
 hardConstraints:ReadonlyArray<{key:string;operator:string;value:unknown;unit?:string}>;
 softPreferences:ReadonlyArray<{key:string;weight:number;value:unknown}>;
 successCriteria:ReadonlyArray<{criterionId:string;verifierId:string}>;
 timeZone:string; locale:string; schemaVersion:1;
}
interface PlanStep { localStepId:string;goalRevision:number;
 dependsOn:string[]; objective:string; evidenceNeeds:string[];
 proposedToolRefs:string[]; estimatedCostMicroUsd:string;
}
interface PlannerPort {
 plan(goal:GoalSnapshot,evidenceRefs:string[],budget:BudgetSnapshot):Promise<PlanStep[]>;
 replan(after:GoalSnapshot,prior:PlanStep[],controlVersion:number):Promise<PlanStep[]>;
}
~~~
Validation: immutable canonical user request and structured constraints store schemaVersion、source/user confirmation timestamp、IANA timezone、currency、decimal price。UUIDv7 generated on trusted server, no untrusted source ID can override tenant/owner/Goal. Bound strings/arrays/nesting. No client-controlled policy or all-true guard. New ports are proposals [PROVISIONAL: CHAT_01/10].

## 6. 状態とシーケンス
~~~text
User command -> AUTH -> goal normalized -> immutable revision1 -> PLAN
PLAN -> STEP READY -> Tool Gateway -> Evidence -> VERIFY -> DONE or REPLAN
STEER -> revision+1 (transaction) -> invalidate old unstarted Tool -> REPLAN
NO viable candidates -> PARTIAL/INSUFFICIENT_EVIDENCE (never silent relax)
~~~

## 7. Algorithm
~~~ts
async function replan(claim, requestedGoalRevision) {
 const goal=await goalStore.loadImmutable(claim.taskId,requestedGoalRevision);
 const current=await runStore.current(claim.runId);
 requireLatestEpochAndOwner(current,claim);
 const evidence=await evidenceStore.filterByProvenanceAndFreshness(goal);
 const proposed=await model.proposeBoundedPlan(goal,evidence);
 const steps=validateGraphAcyclicAndFinite(proposed);
 for(const s of steps) validateHardConstraintPreservation(s,goal);
 await checkpoint.casWrite(claim,steps,goal.revision);
 return steps;
}
~~~
Invariants: Every model claim is checked against latest effective goalRevision; stale observation is not current. External Tool effects only through Gateway. No DB lock held across network. Deterministic checks are preferred to a second LLM; provider output is always untrusted until verified.

## 8. Failure/retry/race matrix
| Failure | Response |
|---|---|
| ambiguous year/TZ/guest count | ask user or mark blocked; no invented values |
| invalid/contradicting constraints | user clarification or explicit impossibility |
| no eligible provider | partial with source limitations |
| two simultaneous Steer | recordVersion CAS, revision serial history |
| stale plan queued | goalRevision check before Tool |
| planner model timeout | bounded retry, previous valid evidence retained |
| budget exhausted | return partial, no new planning |
| planner hallucinated Tool | registry reject, replan bounded |
If Provider times out, stop within finite Budget and give partial/unknown; never fabricate "confirmed" fact. If Cancel/Steer committed before a new Tool dispatch, gateway refuses it. Old model callback cannot change Task terminal result after leaseEpoch loss.

## 9. Security, privacy
All Web/SNS/HTML/Tool output are untrusted observations, not instructions. System/Goal/Policy cannot be overwritten by fetched text. LLM does not receive credentials/Cookies or internal network access. Tool approvals bind actor/tenant/Run/GoalRevision/tool/args hash and expire. SSRF/egress and role checks at Gateway. Redact secrets/PII from prompt preview, event and log. Retain Evidence provenance/observedAt/confidence but no raw login data.

## 10. Trace and resource limits
plan generation latency, number of replans, invalid edges, unverified constraints, reused evidence with freshness, cost/eligible candidate, parallelism impact and constraint violation count.
Initial provisional finite gates: 24 model turns, max60 Tool proposals, 30-minute active Run, maximum4 concurrent independent read tools, one side-effect; token/cost caps via budget module. All are defaults pending benchmarking and user policy. Progress must not be 100% solely because model says done.

## 11. DB/transaction/migration [PROVISIONAL: CHAT_10]
task_goal_revisions immutable keyed (tenant,task,revision), Task.currentGoalRevision CAS and activeRunId, Step goalRevision checkpoint. PLAN hash + Tool intent must reference same revision. Steer/Outbox/Event atomic; no plan mutation after commit.
Task recordVersion init1 vs current SQL revision default0, public UUIDv7 vs SQL gen_random_uuid(), nullable Task.conversationId vs SQL NOT NULL, Run.leaseEpoch/Task.activeRunId missing from SQL: cross-team blockers, not silently fixed by planner. At every commit use CAS by latest Run leaseEpoch + task recordVersion and store Event/Outbox atomically; retry only transactional writes, never foreign effects.

## 12. API/UI [PROVISIONAL: CHAT_08]
Show hard/soft constraints separately, appliedGoalRevision vs requestedGoalRevision, progress per criterion with evidence and human review; never show inferred hotel room count as confirmed.
Server response must expose inspectedAt/observedAt and which constraints are verified vs assumed; live current price/availability must not be invented. User can revise Goal through authorized steer, with immutable history and version conflict 412/428.

## 13. Implementation PR order
GPL-1 goal schema/validators/immutable history; GPL-2 deterministic constraints & plan graph; GPL-3 steer/replan/admission; GPL-4 verifier+UI integration and E2E.
Never expose unapproved actions before cross-team contracts pass tests.

## 14. Tests (all **Designed / NOT RUN**)
| TEST | Fixture→action→expected |
|---|---|
| GPL-T1 | Goal create→revision1 immutable |
| GPL-T2 | Hard/soft normalized distinct |
| GPL-T3 | unauthorized model Steer→revision unchanged |
| GPL-T4 | Plan DAG cycles/unknown Tool rejected |
| GPL-T5 | Steer vs old approval→effect0 |
| GPL-T6 | hotel 3 people total15k parking no matches→no silent relax |
| GPL-T7 | 1000 plan steps requested→finite limit |
| GPL-T8 | LLM final unsupported→no SUCCESS |
| GPL-T9 | profit calculation with coupon constraints→deterministic mismatch caught |
| GPL-T10 | year/TZ/guest constraint persists after compaction |
| GPL-T99 | deterministic fixtures x100 → measure false-success, p95 cost/latency; baseline recorded, no claimed numbers |

## 15. Traceability
| MUST REQ | TEST-ID | SC |
|---|---|---|
| GPL-1 | GPL-T1 | SC-01/03/04/05/07/10/12 |
| GPL-2 | GPL-T2 | SC-01/03/04/05/07/10/12 |
| GPL-3 | GPL-T3 | SC-01/03/04/05/07/10/12 |
| GPL-4 | GPL-T4 | SC-01/03/04/05/07/10/12 |
| GPL-5 | GPL-T5 | SC-01/03/04/05/07/10/12 |
| GPL-6 | GPL-T6 | SC-01/03/04/05/07/10/12 |
| GPL-7 | GPL-T7 | SC-01/03/04/05/07/10/12 |
| GPL-8 | GPL-T8 | SC-01/03/04/05/07/10/12 |
| GPL-9 | GPL-T9 | SC-01/03/04/05/07/10/12 |
| GPL-10 | GPL-T10 | SC-01/03/04/05/07/10/12 |

## 16. Pending decisions and Cross-team proposals
P0 CHAT_01 GoalRevision type and immutable goal ownership; CHAT_10 goal history + Task current revision; CHAT_03 approval revision binding; CHAT_08 Steer view and 412; CHAT_11 SC-01/04/10/12 fixtures. No ability to alter Tool policy.
All unresolved cross-owner fields are [PROVISIONAL]; do not mark Locked before accepted owner changes and actual test pass.

## 17. Sources (observed 2026-10-30)
- [S1] https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/goal/objective.ts
- [S2] https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/goal/scorer.ts ; https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/goal/scorer.test.ts
- [S3] https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-goal.test.ts
- [S4] https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/subagents.py ; https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/test_todo_middleware.py
- [S5] https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/_output.py
Code verified = actual source inspected; test definition verified = test file read but not run. MIT for PydanticAI/OpenAI Agents Python/LangGraph/Deep Agents as recorded in inspected project metadata; Mastra @mastra/core declares Apache-2.0. No implementation/tests executed in this spec repository.
