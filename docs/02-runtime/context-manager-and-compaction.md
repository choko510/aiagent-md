# Context Manager and Compaction — Detailed Implementation Specification

> CHAT_02 | Status: Draft / [PROVISIONAL: CHAT_01/03/09/10] | Revision: 0.1 | Source inspection 2026-10-30 | main 3044c046b1caeea70c5ef6edfea7c57387c6c6da

## 0. 結論と採用案
Contextを immutable trusted policy、Task Goal constraints、現在Working facts、外部Evidence/Artifact参照、明示承認Memoryの層へ分ける。容量超過時に古い会話を単純に丸ごと切断せず、ユーザーの日時/人数/上限額/権限/未解決疑義を必ず固定領域に保持したまま、長いTool HTMLと媒体をArtifact Storeへ退避し引用を維持する。Compactionは新しい真実を生成せず、原文の検証可能なSummaryと省略箇所の参照を作る。

## 1. Scope、Non-scope、受入Scenario
LLMへのBounded Context生成、Tool response token制限、summary/evictionの決定、resume/revision後のContext再構築、長文artifactへのoffload。記憶の永続知識/SkillはCHAT_06、raw Artifact/Evidence schemaはCHAT_01、Browser/HTML取得はCHAT_04、料金上限は別Budget Spec。 外部HTMLをモデルへ丸投げするのではなく、事実・証拠と信頼指示を分離する。SC SC-01/03/04/05/06/07/08/09/10/16。このプロジェクトはセルフホストWeb調査用の汎用Harnessであり、コーディング/EC特化製品ではない。

## 2. 実OSS実装・テスト調査と比較
| OSS/Commit | Source+test inspected | What to borrow / limitation |
|---|---|---|
| DeepAgents 3efa098, MIT | libs/deepagents/deepagents/middleware/summarization.py::_DeepAgentsSummarizationMiddleware, compute_summarization_defaults, _rewrite_data_url_blocks; tests/unit_tests/middleware/test_summarization_middleware.py::test_offload_writes_to_backend and test_compaction_recovery.py::test_provider_overflow_recovers_once/test_no_cutoff_never_resends_unchanged_input | HTML/mediaをBackendへ退避し要約する構造。summarizerによる固有名詞/制約消失対策は自前必要 |
| PydanticAI c2ac8cc, MIT | pydantic_ai_slim/pydantic_ai/_agent_graph.py::GraphAgentState, tests/test_run_cancellation.py::test_tool_cancels_run_and_history_is_resumable | restart時の履歴修復、Token会計を参考。Python ctxは永続正本でない |
| Mastra ec3ba11, Apache-2.0 | packages/core/src/agent/durable/__tests__/durable-agent-request-context-memory-resume.test.ts (test definition existence inspected via directory) | request/context復帰の境界を参考。個別試験内容未精読、Code-only claimしない |
採用: Typed immutable constraint anchor + provenance-preserving Artifact offload + bounded summary。却下: last N messages以外を捨てる単純tail compaction、LLMにHTML全文とCookieを渡す方法、要約文をEvidenceの代用にする方式。公式のユースケース説明だけを根拠に採用しない。実コードとテストはGitHub上で読んだが当Harnessで実行していない。各OSSの挙動はpinしたコミットに限定。

## 3. 依存と正本/Authority
~~~text
User / API (CHAT_08/09) -> Task/Run + GoalRevision (CHAT_01)
        -> Context Manager and Compaction (CHAT_02)
        -> AgentLoop (CHAT_02) -> ToolGateway (CHAT_03)
        -> Browser/HTTP/Adapters (CHAT_04/05)
        -> Evidence/Artifact (CHAT_01/10) -> ResultVerifier
~~~
Public ID UUIDv7、Task.conversationId nullable、Run.leaseEpoch safe integer/DB bigint、Task.recordVersion initial1 (proposals, unLocked)。Task/Run/Goal revision and Tool attempt fence have separate meanings; never use provider Call ID as approval permission. Tenant/Workspace/User ownership is server-resolved, not model-specified.

## 4. 要求定義
| REQ-ID | Level | Measurable requirement | SC |
|---|---|---|---|
| CTX-001 | MUST | Policy/Goal hard constraintsはcompaction不能のtrusted anchorとして保持 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-002 | MUST | Task goalRevision・日時/人数/金額/通貨/locale/TZを検証した独立領域で保持 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-003 | MUST | 外部HTML/巨大Tool Outputは容量上限より大きければArtifact refへ退避 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-004 | MUST | Summaryには省略したsource refs、hash、observedAtと未解決の疑義を保持 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-005 | MUST | Summaryが新しい根拠・価格・権限を作ることは禁止 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-006 | MUST | Model input token予約とoutput reserveの両方に基づきfinite contextを選択 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-007 | MUST | Provider overflow後は有界な再compactionを試行し、不変な入力の無限再送を禁止 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-008 | MUST | Goal steer後に旧summaryをそのまま条件正本としない | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-009 | MUST | 履歴中Tool call/resultの対応を壊さずmodel transcript repairを行う | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-010 | MUST | 跨tenantのMemory/Artifact参照とsecret情報の混入を拒否 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-011 | MUST | Compaction後もRun復旧時同じworking stateを再構築できる | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-901 | SHOULD | 同fixtureで性能・費用・メモリ/トークンを実測後に調整可能 | SC-09 |
| CTX-902 | EXPERIMENT | 個別Provider/Adapter最適化はcompatibility試験後のみ有効化 | SC-09 |

## 5. 型/validation/API proposal（共有型ではない）
~~~ts
type TrustTier="POLICY"|"USER_GOAL"|"WORKING"|"OBSERVATION"|"MEMORY";
interface ContextItem {
 ref:string; trustTier:TrustTier; tokenEstimate:number;
 goalRevision:number; observedAt?:string; rawArtifactRef?:string;
 sourceHash?:string; redactionClass:string;
}
interface ContextPlan {
 runId:string;stepOrdinal:number;goalRevision:number;schemaVersion:1;
 selectedRefs:string[];summaryArtifactRef?:string;
 droppedRefs:string[];reservedOutputTokens:number;estimatedInputTokens:number;
}
interface ContextPort {
 build(runId:string,stepOrdinal:number,modelProfile:ModelCaps):Promise<ContextPlan>;
 compact(plan:ContextPlan):Promise<ContextPlan>;
}
~~~
すべてrun、tenant、goalRevision、schemaVersionに束縛。private HTML/credentialは外部テキスト参照として扱い、Modelへ秘密そのものを送らない。有限最大件数、サイズ、深度を検査。共通Event/DDLの正本はCHAT_01/10。

## 6. Sequence / State
~~~text
Goal+Policy anchors -> collect bounded provenance refs -> estimate tokens
 -> fits? build ModelRequest
 -> overflow: offload raw content and summarize non-anchor working context
 -> recheck hard constraints & hash -> persist compaction version/ref
 -> Model request -> optional provider overflow -> one reduced attempt or PARTIAL
Steer -> reconstruct from new immutable GoalRevision; memory doesn't override goal
~~~
Checkpointはモデル呼出し結果が永続化された後だけ次Stepへ進める。Pause/Steer/Cancelの安全境界、古い結果、承認/Tool台帳を再読込。新しいRun claimが別Workerに移れば古いCallbackはコミットできない。

## 7. 実装疑似コード
~~~ts
async function buildModelContext(run, caps) {
 const anchor=await verifiedImmutablePolicyAndGoal(run);
 const candidates=await gatherAuthorizedContextItems(run);
 const maxInput=finiteCap(caps.window - reservedOutputTokens - systemOverhead);
 if(estimateTokens(anchor)>maxInput) return suspend("CONTEXT_TOO_LARGE");
 const selected=selectByProvenanceRecencyAndRelevance(candidates,maxInput-anchor.tokens);
 const offloaded=await artifactStore.offloadLargeExternalBodies(selected);
 const summary=await summarizeBoundedNonAnchor(offloaded);
 assertNoNewClaimsOrLostHardConstraints(anchor,summary);
 await checkpoint.persistContextPlan(run,summary.refs,summary.hash);
 return assembleAnchoredRequest(anchor,summary);
}
~~~
モデル入力前の決定的Policy/Budget Gateが最終権限である。外部APIとDB transactionをまたぐexactly-onceを謳わない。直前のRun leaseEpochとGoalRevisionをチェックし、不可逆作用はToolGatewayで承認を再検査。

## 8. 障害/競合/再開Matrix
| Failure/race | Action |
|---|---|
| Provider context overflow | bounded compaction once with smaller safe budget; no repeated unchanged request |
| Large PDF/HTML | store raw artifact with hash; provide excerpt/locator |
| Summary loses 3 guests/parking | compare against immutable anchors, reject/rewrite |
| Artifact store failure | fail closed; no phantom source |
| Memory from other tenant | deny before model exposure |
| Steer during summary work | old revision result CAS rejection |
| Interleaved tool call/result after truncation | transcript repair or recover from original event log |
| Compaction retry over cost budget | partial with evidence retained |
Unknown resultはsuccessful responseではない。重複Queue/Worker restart/期限切れと手動Steerが同時発生しても、結果の正本をDBから再構築する。モデル入力と返却したEvidenceの境界でCredentialをマスク。

## 9. セキュリティ、Privacyとtrust
Host shell/SSH/Docker socket/任意権限昇格禁止。Untrusted Webページ・SNS・PDF・MCP応答の命令はSystem/Task/Policyを変えられない。Auth principalに結びつけないモデル生成URIやredirectはSSRF/egressでfail closed。BrowserSessionのPageはCHAT_04所有。Trace/SSE/ArtifactにCookie/API key/passwordを残さない。認可失効時は新規LLM/Tool dispatchを停止。

## 10. Budget/Trace/benchmark
input tokens by trust tier; summary compression ratio and fidelity failures; hard-constraint preservation errors; artifact offload bytes, context overflow count, compaction cost, retrieval relevance and p95 latency, PII leak count.
初期有限案: Run24 model turns、最大60 Tool calls、active時間30分、read並列4、effect並列1、Model最大token/costは別spec。未計測usage/不明料金を0扱いしない。計測条件、各モデル版、input size、cache hitを記録してp50/p95 cost / failureを計測する（未測定）。

## 11. Persistence/migration [PROVISIONAL: CHAT_10]
Raw Tool bytes/summary/ContextPlan include immutable artifact hashes/GoalRevision/versions and are referenced by durable checkpoint; raw and summary ACL tenant scoped. Summary write+checkpoint pointer commit after artifact durability, not within external HTTP. Retention and encryption decisions CHAT_09/10. Old snapshots support rehydration with schema adapters.
Cas records recordVersion=1で開始する提案。UUIDv7を満たさないDB default gen_random_uuid()は改修必要。Task conversation null/activeRunId/goalRevision、Run leaseEpoch/owner/expiryも現在PR #10では整合未完。Schema versionとsnapshot migrationはexpand/contract手順とfallbackの安全停止を用意。Event/Outbox/usage receiptはatomic commit。

## 12. UI/API [PROVISIONAL: CHAT_08]
Show concise retrieval sources and what is older; compaction is internal and never implies new search. Human Handoff and credential artifacts excluded from LLM. Warning when evidence too large and precision uncertain.
SSEのtentative deltaは完了Evidence/Tool効果を意味しない。ETag/If-MatchはrecordVersionを使用。Task/Watch notificationの出典/observedAtと変更元GoalRevisionを提供し、失敗を0価格や空室ありと偽らない。

## 13. 実装PR分割
CTX-A context tiers+hard anchor tests; CTX-B Artifact offload/readback; CTX-C deterministic token/select+summary; CTX-D recover/steer/transcript repair; CTX-E adversarial/fault/bench.
外部副作用のCI/契約レビュー未通過で本番実行しない。

## 14. Unit/Contract/Integration/Fault/Security/Benchmark tests
**すべて設計のみ（NOT RUN）**。FakeModel、MockGateway、Artifact fixture、Killpointで実装後に実施。実サービスへの予約・投稿・購入は実行しない。
| TEST-ID | Setup/action/expected |
|---|---|
| CTX-T01 | compaction retains exact user 3 guests/price/TZ |
| CTX-T02 | hard constraints never summarized away |
| CTX-T03 | 1MB HTML becomes artifact and bounded excerpt |
| CTX-T04 | summary preserves hash/citation/time |
| CTX-T05 | synthetic price added by summarizer→reject |
| CTX-T06 | input+output reservation never exceeds model window estimate |
| CTX-T07 | provider overflow repeated→bounded retries and partial |
| CTX-T08 | steer during summarize→old summary not committed |
| CTX-T09 | truncated assistant Tool call→repair or reject |
| CTX-T10 | cross tenant Memory artifact→zero leaked bytes |
| CTX-T11 | worker crash during offload→snapshot points only to durable artifact |
| CTX-T99 | 100 fixture workloads → cost/latency/memory/precision/false-success recorded and threshold decided after measurement |

## 15. Traceability
| MUST REQ | TEST | SC |
|---|---|---|
| CTX-001 | CTX-T01 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-002 | CTX-T02 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-003 | CTX-T03 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-004 | CTX-T04 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-005 | CTX-T05 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-006 | CTX-T06 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-007 | CTX-T07 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-008 | CTX-T08 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-009 | CTX-T09 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-010 | CTX-T10 | SC-01/03/04/05/06/07/08/09/10/16 |
| CTX-011 | CTX-T11 | SC-01/03/04/05/06/07/08/09/10/16 |

## 16. Pending decisions / Cross-team proposals
P0 CHAT_01 Context/Event/Evidence contracts, CHAT_10 Artifact version/hash/checkpoint pointer, CHAT_09 redaction & ACL, CHAT_06 Memory trust tiers, CHAT_02 model/budget token capability, CHAT_11 fidelity/overflow fixtures. Pending context window/tokenizer version validation.
別Ownerの未Locked仕様には[PROVISIONAL]を付け、静かに再定義しない。Contract Testとmigrationが承認されるまでDraft維持。

## 17. Sources and Evidence Grades (2026-10-30)
- [S1] Code verified: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/summarization.py
- [S2] Test definitions verified: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_summarization_middleware.py ; https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_compaction_recovery.py
- [S3] PydanticAI Code verified: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/_agent_graph.py ; tests/test_run_cancellation.py (same commit)
- [S4] Mastra test directory verified, contents not audited: https://github.com/mastra-ai/mastra/tree/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__
Code verified = GitHub source functions inspected. Test definition verified = read test source but not executed. Issue report = not reproduced. Licenses (inspected): DeepAgents MIT, PydanticAI MIT, OpenAI Agents Python MIT, Mastra @mastra/core package Apache-2.0 declaration. Independent implementation only.
