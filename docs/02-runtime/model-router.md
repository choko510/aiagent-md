# Model Router — Detailed Implementation Specification

> CHAT_02 | Status: Draft / [PROVISIONAL: CHAT_01/03/09/10] | Revision: 0.1 | Source inspection 2026-10-30 | main 3044c046b1caeea70c5ef6edfea7c57387c6c6da

## 0. 結論と採用案
ModelRouterはProvider/Model capabilityを起動時またはTTL付きrefreshで取得し、必要なTool schema/vision/streaming/context window/最大出力/価格を満たす候補のみ採用する。モデル呼出し時に選定と価格snapshotを固定、response途中のfailoverは確定Toolや副作用を発火しない。実行中のモデル変更は境界に限定し、suspended continuationが特定Providerに結び付く場合はその制約を守る。

## 1. Scope、Non-scope、受入Scenario
Model availability/capability/profileの解決、normalization、コストに基づくrouting、fallback、応答のstreamingと非streaming互換、provider error taxonomy、途中選択変更。Tool結果や権限、機密の判定はGateway/CHAT_09で保持。 外部HTMLをモデルへ丸投げするのではなく、事実・証拠と信頼指示を分離する。SC SC-05/07/08/09/15。このプロジェクトはセルフホストWeb調査用の汎用Harnessであり、コーディング/EC特化製品ではない。

## 2. 実OSS実装・テスト調査と比較
| OSS/Version | Real source and tests inspected | Implication |
|---|---|---|
| PydanticAI c2ac8cc MIT | pydantic_ai_slim/pydantic_ai/models/fallback.py::FallbackModel.request/request_stream/_request_chain, models/_abstract.py::AbstractModel.context_window; tests/test_capability_model_fallback.py::test_resumed_continuation_goes_to_the_pinned_model/test_streaming_open_failure_falls_back | fallbackとcontinuationのモデル固定を採用。価格・tool semantic portabilityは独自Adapter検証 |
| OpenAI Agents Python cd26bbb MIT, pyproject 0.24.0 | src/agents/models/multi_provider.py::MultiProvider.get_model/_get_prefix_and_model_name, src/agents/models/interface.py (read by path discovery; full source not inspected) | prefix/provider routing is informative, cross-provider tool IDs are not universal |
| Mastra ec3ba11 Apache-2.0 | packages/core/src/agent/durable/__tests__/durable-agent-model-retry-fallback.test.ts (directory verified), durable-agent-model-list-settings-recovery.test.ts | recovery + fallback需要を示す。個別テストの全文未読なのでDocs-onlyの参考として記す |
採用: Adapter registry / validated capability profile + bounded compatible failover + immutable per-attempt provider selection。却下: 全Model互換と仮定した単純文字列prefix routing、stream途中の未完Tool提案実行、価格不明の0円計上。公式のユースケース説明だけを根拠に採用しない。実コードとテストはGitHub上で読んだが当Harnessで実行していない。各OSSの挙動はpinしたコミットに限定。

## 3. 依存と正本/Authority
~~~text
User / API (CHAT_08/09) -> Task/Run + GoalRevision (CHAT_01)
        -> Model Router (CHAT_02)
        -> AgentLoop (CHAT_02) -> ToolGateway (CHAT_03)
        -> Browser/HTTP/Adapters (CHAT_04/05)
        -> Evidence/Artifact (CHAT_01/10) -> ResultVerifier
~~~
Public ID UUIDv7、Task.conversationId nullable、Run.leaseEpoch safe integer/DB bigint、Task.recordVersion initial1 (proposals, unLocked)。Task/Run/Goal revision and Tool attempt fence have separate meanings; never use provider Call ID as approval permission. Tenant/Workspace/User ownership is server-resolved, not model-specified.

## 4. 要求定義
| REQ-ID | Level | Measurable requirement | SC |
|---|---|---|---|
| MRT-001 | MUST | Provider capability metadataにobservedAt/source/versionと検証stateを保存 | SC-05/07/08/09/15 |
| MRT-002 | MUST | Tool calling/JSON/stream/vision/context windowを必要条件として検証 | SC-05/07/08/09/15 |
| MRT-003 | MUST | 不明なcapabilityは対応と推測せずfail closedまたは明示降格 | SC-05/07/08/09/15 |
| MRT-004 | MUST | 価格snapshotとtoken units/currencyをattemptに固定して計測 | SC-05/07/08/09/15 |
| MRT-005 | MUST | fallbackはRequest precommitの範囲で有限候補に制限 | SC-05/07/08/09/15 |
| MRT-006 | MUST | tool proposal永続化後の同step model swapは自動行わない | SC-05/07/08/09/15 |
| MRT-007 | MUST | suspended continuationのpinned provider情報を尊重 | SC-05/07/08/09/15 |
| MRT-008 | MUST | Provider output/tool call schema差異をadapterがcanonical proposalへ正規化 | SC-05/07/08/09/15 |
| MRT-009 | MUST | Provider 429/5xx/timeoutとinvalid authを分類しretry budgetを分ける | SC-05/07/08/09/15 |
| MRT-010 | MUST | stream tokenはtentative、Tool dispatchはfull response検証とcommit後 | SC-05/07/08/09/15 |
| MRT-011 | MUST | Model切替時もGoalRevision、権限・Evidence timestampを維持 | SC-05/07/08/09/15 |
| MRT-012 | MUST | 実使用量が不明ならestimatedを保持し0円として継続しない | SC-05/07/08/09/15 |
| MRT-901 | SHOULD | 同fixtureで性能・費用・メモリ/トークンを実測後に調整可能 | SC-09 |
| MRT-902 | EXPERIMENT | 個別Provider/Adapter最適化はcompatibility試験後のみ有効化 | SC-09 |

## 5. 型/validation/API proposal（共有型ではない）
~~~ts
interface ModelCaps {
 providerId:string;modelId:string;revision:string;
 observedAt:string;expiresAt:string;verified:boolean;
 tools:boolean;structuredOutput:boolean;vision:boolean;streaming:boolean;
 contextWindow:number;maxOutputTokens:number;
 tokenizerId?:string;priceSnapshotRef?:string;
}
interface ModelAttempt {
 attemptId:string;runId:string;stepOrdinal:number;
 providerId:string;modelId:string;profileHash:string;
 goalRevision:number;usageReservationRef:string;status:"RESERVED"|"STREAMING"|"COMMITTED"|"UNRESOLVED";
}
interface ModelRouter {
 select(required:RequiredCapabilities,budget:BudgetSnapshot):Promise<ModelAttempt>;
 invoke(chosen:ModelAttempt,request:ModelRequest):Promise<NormalizedModelResult>;
}
~~~
すべてrun、tenant、goalRevision、schemaVersionに束縛。private HTML/credentialは外部テキスト参照として扱い、Modelへ秘密そのものを送らない。有限最大件数、サイズ、深度を検査。共通Event/DDLの正本はCHAT_01/10。

## 6. Sequence / State
~~~text
Step requirements -> capability cache validate -> candidate allowlist & price/budget
 -> select model (record attempt, reserve estimated cost)
 -> stream tentative -> full reply normalize/validate -> atomic commit
  ├─ recoverable failure precommit -> bounded retry/fallback another compatible model
  ├─ auth/security unsupported -> fail closed
  └─ suspended provider continuation -> pinned original model or explicit restart
~~~
Checkpointはモデル呼出し結果が永続化された後だけ次Stepへ進める。Pause/Steer/Cancelの安全境界、古い結果、承認/Tool台帳を再読込。新しいRun claimが別Workerに移れば古いCallbackはコミットできない。

## 7. 実装疑似コード
~~~ts
async function routeModelStep(run,required) {
 const profiles=await registry.getTrustedCurrentProfiles(required);
 const eligible=profiles.filter(p=>matchesAllRequiredCapabilities(p,required) &&
   priceKnownOrConservativeCap(p) && policyAllows(p));
 const selection=stableRankWithinBudget(eligible,run.preferences);
 for(const model of selection.slice(0,finiteFallbackCount)) {
   const receipt=await usage.reserve(run,model.priceSnapshotRef);
   try {
     const full=await adapter(model).generateAndNormalize(run.input,receipt);
     await checkpoint.commitFullResponseAndUsage(run,full,receipt);
     return full;
   } catch(e) {
     await usage.recordFailureOrUnknown(receipt,e);
     if(!isSafePrecommitRetry(e) || run.hasCommittedProposal) throw e;
   }
 }
 return partial("NO_COMPATIBLE_MODEL");
}
~~~
モデル入力前の決定的Policy/Budget Gateが最終権限である。外部APIとDB transactionをまたぐexactly-onceを謳わない。直前のRun leaseEpochとGoalRevisionをチェックし、不可逆作用はToolGatewayで承認を再検査。

## 8. 障害/競合/再開Matrix
| Case | Action |
|---|---|
| Profile stale or missing | refresh bounded, no unsupported guess |
| Provider 429/503 | finite backoff; eligible alternate if no committed Tool proposal |
| Provider auth error | abort provider, notify operator without credential leakage |
| Stream dies mid-tool JSON | tentative drop, no Tool execution |
| JSON type incompatible | adapter validation fail, no Tool |
| Provider change after approval | cannot revive stale approval |
| Pinned continuation unsupported | preserve suspended state, request restart/replan |
| Unknown billing usage | keep reserve, reconcile or conservative cap |
| Old lease callback | CAS reject |
Unknown resultはsuccessful responseではない。重複Queue/Worker restart/期限切れと手動Steerが同時発生しても、結果の正本をDBから再構築する。モデル入力と返却したEvidenceの境界でCredentialをマスク。

## 9. セキュリティ、Privacyとtrust
Host shell/SSH/Docker socket/任意権限昇格禁止。Untrusted Webページ・SNS・PDF・MCP応答の命令はSystem/Task/Policyを変えられない。Auth principalに結びつけないモデル生成URIやredirectはSSRF/egressでfail closed。BrowserSessionのPageはCHAT_04所有。Trace/SSE/ArtifactにCookie/API key/passwordを残さない。認可失効時は新規LLM/Tool dispatchを停止。

## 10. Budget/Trace/benchmark
selection reason, capability cache age, fallback count, p50/p95 model latency, tokens and cost per provider, context overflow, normalization rejection, upstream 429, tentative output drops, billed vs estimated gap.
初期有限案: Run24 model turns、最大60 Tool calls、active時間30分、read並列4、effect並列1、Model最大token/costは別spec。未計測usage/不明料金を0扱いしない。計測条件、各モデル版、input size、cache hitを記録してp50/p95 cost / failureを計測する（未測定）。

## 11. Persistence/migration [PROVISIONAL: CHAT_10]
Model profiles stored as versioned snapshots; ModelAttempt row has provider/model/price/profile hash, status/usage and goalRevision. Resume resolves original adapter/model when continuation pinned. Actual payment records from provider may arrive late; usage reservation not automatically refunded without evidence. Tool intent only after complete normalized reply is persisted.
Cas records recordVersion=1で開始する提案。UUIDv7を満たさないDB default gen_random_uuid()は改修必要。Task conversation null/activeRunId/goalRevision、Run leaseEpoch/owner/expiryも現在PR #10では整合未完。Schema versionとsnapshot migrationはexpand/contract手順とfallbackの安全停止を用意。Event/Outbox/usage receiptはatomic commit。

## 12. UI/API [PROVISIONAL: CHAT_08]
Task details show selected model, factual token/cost receipts, fallback reason and whether estimates are uncertain. User-specified model may be declined if missing required tool/vision capability; never fake support.
SSEのtentative deltaは完了Evidence/Tool効果を意味しない。ETag/If-MatchはrecordVersionを使用。Task/Watch notificationの出典/observedAtと変更元GoalRevisionを提供し、失敗を0価格や空室ありと偽らない。

## 13. 実装PR分割
MRT-A capability/price registry + validators; MRT-B canonical provider adapters; MRT-C retry/fallback budgets; MRT-D streamed partial and pinned continuation; MRT-E provider compatibility/benchmark.
外部副作用のCI/契約レビュー未通過で本番実行しない。

## 14. Unit/Contract/Integration/Fault/Security/Benchmark tests
**すべて設計のみ（NOT RUN）**。FakeModel、MockGateway、Artifact fixture、Killpointで実装後に実施。実サービスへの予約・投稿・購入は実行しない。
| TEST-ID | Setup/action/expected |
|---|---|
| MRT-T01 | expired model profile→refresh/no unsafe selection |
| MRT-T02 | vision requirement vs text-only provider→reject |
| MRT-T03 | capability unknown→not selected |
| MRT-T04 | price snapshot immutable per attempt |
| MRT-T05 | 503 precommit fallback limited candidates |
| MRT-T06 | after frozen proposal no silent model swap |
| MRT-T07 | pinned suspended continuation preserves model |
| MRT-T08 | unknown tool call schema→normalization reject |
| MRT-T09 | 401 auth error→no retry exposing key |
| MRT-T10 | stream broken before full response→Tool0 |
| MRT-T11 | Steer during model call→old result fenced |
| MRT-T12 | usage missing→unknown reserve retained |
| MRT-T99 | 100 fixture workloads → cost/latency/memory/precision/false-success recorded and threshold decided after measurement |

## 15. Traceability
| MUST REQ | TEST | SC |
|---|---|---|
| MRT-001 | MRT-T01 | SC-05/07/08/09/15 |
| MRT-002 | MRT-T02 | SC-05/07/08/09/15 |
| MRT-003 | MRT-T03 | SC-05/07/08/09/15 |
| MRT-004 | MRT-T04 | SC-05/07/08/09/15 |
| MRT-005 | MRT-T05 | SC-05/07/08/09/15 |
| MRT-006 | MRT-T06 | SC-05/07/08/09/15 |
| MRT-007 | MRT-T07 | SC-05/07/08/09/15 |
| MRT-008 | MRT-T08 | SC-05/07/08/09/15 |
| MRT-009 | MRT-T09 | SC-05/07/08/09/15 |
| MRT-010 | MRT-T10 | SC-05/07/08/09/15 |
| MRT-011 | MRT-T11 | SC-05/07/08/09/15 |
| MRT-012 | MRT-T12 | SC-05/07/08/09/15 |

## 16. Pending decisions / Cross-team proposals
CHAT_01 ModelEvent/UsageReceipt schema, CHAT_03 Tool schema & provider capability, CHAT_09 key store and allowed providers, CHAT_10 price snapshots/usage+attempt DB, CHAT_08 user model selection/telemetry, CHAT_11 compatibility fixtures; default model/provider and currency conversion rates are not fixed here.
別Ownerの未Locked仕様には[PROVISIONAL]を付け、静かに再定義しない。Contract Testとmigrationが承認されるまでDraft維持。

## 17. Sources and Evidence Grades (2026-10-30)
- [S1] Code verified: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/models/fallback.py ; https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/models/_abstract.py
- [S2] Test definitions verified: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_capability_model_fallback.py
- [S3] Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/models/multi_provider.py
- [S4] Mastra directory-level evidence, tests not read: https://github.com/mastra-ai/mastra/tree/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__
Code verified = GitHub source functions inspected. Test definition verified = read test source but not executed. Issue report = not reproduced. Licenses (inspected): DeepAgents MIT, PydanticAI MIT, OpenAI Agents Python MIT, Mastra @mastra/core package Apache-2.0 declaration. Independent implementation only.
