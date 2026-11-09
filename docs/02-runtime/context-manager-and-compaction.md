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
Public ID UUIDv7、Task.conversationId nullable、Run.leaseEpoch canonical int64 decimal string / DB BIGINT、Task.recordVersion initial1 (proposals, unLocked)。Task/Run/Goal revision and Tool attempt fence have separate meanings; never use provider Call ID as approval permission. Tenant/Workspace/User ownership is server-resolved, not model-specified.

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
 goalRevision:string; observedAt?:string; rawArtifactRef?:string;
 sourceHash?:string; redactionClass:string;
}
interface ContextPlan {
 runId:string;stepOrdinal:number;goalRevision:string;schemaVersion:1;
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
CAS recordVersionは1開始。現在PR #10のDDLにはtrusted UUIDv7（v4 DB defaultを廃止）、nullable Task conversation、activeRunId/goalRevisionとRun leaseEpoch/owner/expiryの物理列がある。旧不整合は設計上の修正済みで、実RuntimeのCAS・認可・復旧テストは未完了。Schema versionとsnapshot migrationはexpand/contract手順とfallbackの安全停止を用意。Event/Outbox/usage receiptはatomic commit。

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


### Round 4 cross-owner int64 reconciliation (2026-10-30)
Version/Epoch fields above follow CHAT_01's canonical signed-int64 **decimal string** wire format, validated and compared with internal BigInt and persisted as PostgreSQL BIGINT; JSON number conversion is prohibited. Other scalar number fields (bounded counts, percentages, step ordinals) remain number unless separately versioned. Regression test proposed: `9007199254740992` and `9223372036854775806` round-trip exactly, reject unsafe Number and stale authority; not executed against Harness code.

## 2026-11-07: ターミナル・Workspace・生成コードの信頼区分

Shell/CLIのstdout/stderr、package installログ、ダウンロードしたREADME、生成コード、Skills/Hook候補、子エージェント結果はuntrusted Tool outputとして扱う。Context圧縮後も外部情報をSystem/Developer/権限へ昇格させない。ExecId/WorkspaceRevision/Cell profile/image digestとArtifact refsは復旧用に保持し、期限切れ承認やUNKNOWN副作用を消さない。環境変数から生のCredential、Vault tokenや署名鍵をLLMに流さない。ログはサイズ制限・redact・Provenance付き。SC-17/20/22。

## 18. Adaptive Context Management — 実装判断・横断契約（2026-11-07）

> Status: **Draft / PROVISIONAL**。本節はCTX-001〜011を置換せず具体化する補助規範。依存: PR #68 Muse型隔離Runtime、CHAT_01/02/03/06/08/09/10/11。共有DTO/DDL/Eventはオーナー承認前にLocked扱いしない。テストは定義のみ、Harness本体では未実行。過去に承認されたような架空の測定値は置かない。

### 18.1 到達目標と明示的な非保証

長時間のTask、Watchの再実行、モデル切替、並列子Task、Sandbox Execution、大型PDF/HTMLの処理で、LLMへの各requestは**小さく・関連性が高く・検証可能**であること。全会話/長期Memory/ワークスペースを無条件にモデルへ投入しない。

- **MUST**: 明示条件・否定条件・制約変更・未解決疑義・承認状態・UNKNOWN副作用を圧縮で消さない。原文や証拠なしに「価格」「成功」「権限」「事実」を新規生成しない。
- **MUST**: 検索結果・Tool stdout・生成コード・外部Web文面・Memoryは*データ*であってPolicy/Approval/System instructionにならない。
- **MUST**: 生の履歴・原文Artifact・決定済Goal・Side-effect Ledgerの正本をContext summaryで上書きしない。
- **MUST**: すべての選択・省略・復元はRun/Goal/Model/Policy/Memory/Artifactの版に結び付け、追跡できること。
- **MUST**: 失敗時のPartialと不確実さをユーザーから隠さない。Contextが収まらなければ安全停止し、必須条件を削らない。
- **非保証**: どのモデルでも100%の意味保持、記憶検索の完全再現率、外部プロバイダへ送信した個人情報の回収、実行後の副作用取り消し、無制限長期稼働、一定の価格節約率。これらを実装・試験なしに宣言しない。

### 18.2 データ層とAuthorityの確定方針

| Layer | Source of truth / owner | Request内での扱い | Compaction |
|---|---|---|---|
| A0 Trusted policy and current authorization | CHAT_09/03 の保護サービス | 不変Policy refと最終検査済み規則 | **不可**。適合しなければ停止 |
| A1 Canonical Task Goal/constraints/current user steer | CHAT_01/02のGoalRevision | 署名/検証済み型付きhard anchor | **不可**。日時、TZ、人数、金額、通貨、除外、否定、対象を保持 |
| A2 Execution control & external effects | CHAT_01/02/03/10 Run checkpoint/ledger/approval | 現在のPhase、未解決UNKNOWN、承認期限、期限切れRun | **不可**。正本へのrefと状態を残す |
| B1 Evidence and source excerpts | CHAT_01/10 immutable Artifact/Observation | 観測時刻、locator、digest、version付きの引用候補 | 抜粋可能。原本とlocator保持 |
| B2 Working journal | CHAT_02 Task内 decisions/findings/open questions | 確認済み・仮説・未検証を区別 | 証拠付きstructured summaryへ |
| C1 Long-term Memory | CHAT_06 Memory Writer | 再認可したread-only Projection。instruction authorityなし | Token予算で省略可 |
| C2 Conversation raw messages | CHAT_01 raw message/event stream | 必要な最近のUser修正・会話部分 | User条件のanchor抽出後に省略可 |
| D1 External tool bodies / sandbox files | CHAT_01/10 Artifact + CHAT_02/09 Exec/Workspace | 大きな本文やbinaryをLLMから退避、必要範囲のみ | ref付きoffload。秘密は送らない |

B1の「Evidenceが存在すること」と「Evidenceの内容が事実として正しいこと」は別。観測時刻・引用・競合状態をモデルへ明示し、要約文だけをResultVerifierの根拠にしない。A0〜A2は人間向け説明のために文章化できても**機械的な正本の置換は禁止**。A1の抽出が不確実なら曖昧条件として停止/照会する。

### 18.3 必須の型付きコンテキスト契約案

本節のTypeScriptは内部論理契約であり新規共有JSON schemaの無断制定ではない。Version/Epoch/RecordVersionはCHAT_01のint64 decimal-string codec、内部BigInt、DB BIGINTを使用。UUIDv7、AuthContext、ACL、Secret handlingを再定義しない。

~~~ts
type ContentTrust = "TRUSTED_POLICY" | "CANONICAL_GOAL" |
  "CONTROL_LEDGER" | "USER_MESSAGE" | "VERIFIED_EVIDENCE" |
  "UNVERIFIED_OBSERVATION" | "WORKING_JOURNAL" |
  "MEMORY_DATA" | "UNTRUSTED_EXEC_OR_WEB";

type ClaimState = "VERIFIED_WITH_SOURCE" | "USER_ASSERTED" |
  "HYPOTHESIS" | "CONFLICTED" | "UNKNOWN" | "UNVERIFIED" | "EXPIRED";

interface ContextCandidate {
  id: string;
  tenantScopedOwnerRef: string; // Server-derived and ACL-checked, no model choice
  trust: ContentTrust;
  claimState: ClaimState;
  sourceRefs: string[]; // Immutable artifact IDs, never unrestricted file paths
  sourceDigest?: string;
  observedAt?: string; // Optional historical observation; not time of truth
  validUntil?: string;
  taskId: string;
  goalRevision: string;
  rawArtifactRef?: string;
  tokenEstimate: number;
  relevanceFeatures: {goal: number; lexical: number; temporal: number};
  urgency: number;
  provenanceCompleteness: number;
  redundancyKey?: string;
  secrecyClass: string;
}
interface ConstraintAnchor {
  canonicalGoalRef: string; goalRevision: string;
  goalSnapshotDigest: string;    // Hash of current typed GoalRevision
  hardConstraintRefs: string[];  // Value, operator and unit, NOT just field names
  softPreferenceRefs: string[];
  unresolvedConditionRefs: string[]; // UNKNOWN / AMBIGUOUS, not guessed
  unresolvedConflictRefs: string[];
  pendingEffectLedgerRefs: string[];
  approvalStateRefs: string[];
  trustedProjectionDigest: string; // Minimal model-visible A0/A2, not raw policy
}
interface ContextManifest {
  schemaMajor: 1;
  runId: string; stepOrdinal: string; leaseEpoch: string;
  goalRevision: string; policyVersion: string; memorySourceEpoch: string;
  modelId: string; modelCapabilityRevision: string; tokenizerId: string;
  rawEventCursor: string; checkpointHeadRef: string;
  selected: Array<{ref:string; digest:string; role:string; locatorRef:string; originalTokens:number}>;
  offloaded: Array<{rawRef:string; digest:string; locatorRef:string; reason:string}>;
  omitted: Array<{ref:string; reason:string}>;
  summaryRef?: string; summaryDigest?: string;
  sourceCoverageRef: string;
  contextDigest: string; // Selection-plan digest, NOT provider payload
  modelRequestSnapshotRef: string; finalModelPayloadDigest: string;
  promptTemplateRevision: string; promptTemplateDigest: string;
  estimatedInputTokens: number; reservedOutputTokens: number;
  hardAnchorDigest: string;
}
interface GroundedSummaryClaim {
  text: string; inputSourceRefs: string[]; inputLocatorRefs: string[];
  derivedTypedFactRefs: string[];
  semanticCheck: "DETERMINISTIC_MATCH"|"INDETERMINATE"|"CONTRADICTED";
  claimState: ClaimState; // Citation existence NEVER proves entailment
  observationTime?: string;
}
~~~

- 本文とハッシュは同一の暗号化されたArtifact/DB境界内で取得する。digestだけで認可や真正性を保証しない。ハッシュ方式/長さ/Canonical bytesの仕様はCHAT_01/10で確定する。
- Manifestは「次のLLM要求の候補選択に関する*選択記録*」であり、実際のLLM送信本文は別のsealed ModelRequestSnapshotが担う。Raw Evidence・会話・Tool receiptの正本ではない。選択省略理由は固定enumへ正規化し、秘密本文をTraceへ出さない。
- selected/offloaded/omittedに同じ参照が存在しても理由を区別し、無権限資料は候補リスト自体に出さない。ACLフィルタ前の候補IDや件数を利用者へ漏らさない。
- 保存するtoken見積はTokenizer versionとModel Capabilitiesに紐付ける。Model切替時に他モデルの推定値をそのまま信頼しない。

### 18.4 候補の重要度・選択順序

優先度は**安全上の固定区分 → Hard filter → 同一Task内の関連性 → Token費用**の順。Embedding similarity、LLMの自信度、最終更新時刻だけでAuthorityを決定しない。

| Priority | 絶対条件 | 選択方法 |
|---|---|---|
| P0 不変 | A0/A1/A2。承認・UNKNOWN・negative constraints・最新Steer | 全量必須。収まらなければ明示的停止 |
| P1 保護 | 実行中の未解決疑義、実行対象のソース、最終ユーザー訂正、前Step Tool call/resultの整合 | 必須要約または完全ref+locator。不可逆な操作前は根拠原本再取得 |
| P2 高関連 | Goalに直接答える独立Evidence、検証済み主要判断、子Taskで確認済み差分 | 多様性/新鮮度/出典付きで選択 |
| P3 補助 | 過去例、一般的背景、低関連Memory、重複した本文 | 余白分だけ選択。先に退避/省略 |

**候補スコア（P2/P3の内部だけで使用、初期設計仮値）**: verified normalized goal relevance 0..1、lexical/temporal similarity、urgency、freshness、provenance completeness、coverage novelty、redundancy penalty を合成する。

~~~text
relevance = clamp(0.6*goal + 0.25*lexical + 0.15*temporal,0,1)
rankScore = 5*relevance + 3*urgency + 2*freshness
          + 2*provenanceCompleteness + 2*coverageNovelty
          - 3*redundancy
utility = max(0,rankScore) / max(1,tokenEstimate)
retain mandatory P0/P1 first
for pool in (P2,P3):
  pick highest marginal(coverage + quality - redundancy)/additionalTokens
  refresh marginal after each selection; enforce remaining token budget
  tie break: source diversity, observedAt DESC, stableRef ASC
# NEVER sort primarily by rankScore and make token efficiency only a tie-break
~~~

- スコアは**非権限データの配置順**のみ。数値が高くても期限切れ、出典欠落、アクセス失効、goalRevision不一致は除外。Model判断をScoreに混ぜる場合は説明可能なbounded featureとして別Version管理する。
- 同じURL/Sourceからの類似引用を無制限に採らず、独立ソースと未解決対立説のcoverageを確保する。対立する2つの資料を同一文へ統合して「確定」にしない。
- 未取得のEvidenceを「存在するはず」として召喚しない。source locatorが失効した場合は不明扱いにして選択し直す。
- Token見積が高い巨大本文は高得点でも先にoffloadして見出し・該当箇所のlocatorsへ分割。引用と選択理由はManifestから説明可能。
- スコア係数は機械学習で自動上書きしない。版を固定したoffline fixtureで比較し、品質・費用・偏りが改善した変更のみ明示採用。

### 18.5 モデル別の動的Token予算

毎回ModelRouterの当該versionからcontextWindow、maxOutput、tokenizer、tool schema/cached input仕様を検証する。実コストの課金とToken上限を混同しない。

~~~text
W = verified model context window (confirm whether output shares the window)
R = reserved output / structured-response tokens
S = max(1024, ceil(0.08 * W))  # provisional margin
unique disjoint serialized request content buckets:
  Tsys  = system/developer + model-visible Policy projection (A0 ONCE)
  Ttool = ordered Tool Schema bytes (ONCE)
  Tgoal = current typed Goal/User constraints (A1 ONCE)
  Tctrl = minimal execution/effect state projection (A2 ONCE)
  Tmust = required P1 content and recent required messages
  Topt  = optional Evidence/Working/Memory
  Tframe = role wrappers / metadata / provider framing (ONCE)
Tmandatory = tokenizeFinalProviderSerialization(with Topt empty)
Tinput = tokenizeFinalProviderSerialization(all buckets)
if W<=0 or Tmandatory+R+S>W: BLOCKED_CONTEXT_CAPACITY
choose Topt subject to Tinput+R+S<=W; recompute after each change
# disjoint accounting paths prevent A0 Policy double count
~~~

- Tokenizer不明時は保守的な上界見積とProvider preflightを用いる。上界を証明できなければ小さいModelへ無理に削って通さず、他Model選択またはPartial。Prompt cacheのhitを入力容量の拡大根拠にしない。
- 目安の上限: Memoryは min(2,000 tokens, optionalの12%)、直近Working Journalはoptionalの35%、Evidence excerptsはoptionalの45%。未使用枠はEvidence/Workingへ再分配。P1と重要User修正はこの割合で落とさない。これらは**各カテゴリへの初期cap**であって、総量を固定配分して浪費する義務ではない。
- Tool schema自体が大きい場合は最小の許可済Tool subsetを選び直す。未承認Toolを削除するだけで能力が不足する場合はその理由を表示し、Tool権限を記憶から補完しない。
- 予防圧縮: 予測入力が (W-R-S) の70%以上でP3/重複Bodyを退避し始める。(W-R-S) の85%以上で構造化Summaryを優先し、Mandatory/P1を再検査。これは初期の調整値であり、モデル・fixture別測定で変更可能。
- コンテキスト長と**Run token/cost/time Budget**は独立のハード制限。圧縮に使うsummarizer/rerankモデル費用もTask Budgetへ予約し、予算不足なら無限に圧縮/再検索しない。
- Model providerのoverflow responseは、同じGoalRevision/Source snapshotで**最大1回だけ**縮小・再推定してリクエストを再発行。無変更入力の再送は不可。失敗なら明示的PARTIAL/モデル切替候補の提示。

### 18.6 Grounded Compactionと原本復元

**順序固定**: (1)権限・期限・Goal filter → (2)URL/Tool output/mediaの原本耐久保存・digest → (3)有界chunkとlocator → (4)独立claimのgrounded summary → (5)source coverageとhard anchor検査 → (6)新ManifestをCheckpointへ参照 → (7)LLMへ送信。

要約単位は見出し/段落/表row/page/range/exec stdout offsetなどの**復元できるロケータ**。正当な原本は暗号化したSealed OriginalとLLM用Redacted Projectionを別々のhash・locator-mapで管理する。Cookie、API key、Vault token、private env、認証ヘッダーは原則保存禁止または隔離し、原本データと混在させない（CHAT_09保持・消去規定）。画像・PDFをテキスト要約した結果はraw locator、ページ、図表のscopeを維持。

Summary claimには最低1個のsource refと正確なlocatorを要求するが、**引用が存在するだけでは内容を支持しない**。価格・税/手数料内外、日時/TZ、人数、数量/単位、条件と否定について出典spanと型付きFactで照合し、確証できない主張は`UNVERIFIED`、矛盾は`CONTRADICTED`として結果採用を禁止する。LLMによる整合判定は補助に限り、sourceの意味を機械的に照合できないものを引用だけで`VERIFIED_WITH_SOURCE`に昇格させない。複数sourceの矛盾、未完了作業、否定結果、UNKNOWN/UNVERIFIEDは要約後も明示維持する。

要約品質を自己申告だけでPASSにしない。検証不能なclaimは保留し、原文の該当chunkを少量残すかユーザーへ不確実と表示する。Summaryが壊れた場合は元Artifact+raw eventから再構築し、壊れたsummaryを新しい正本として保存しない。

**On-demand expansion**: LLMが必要範囲のEvidenceを再読込する際は ArtifactRef + locator range + expected digestをTool Gateway経由のread-only認可操作にする。VM/Cellからホスト絶対pathやArtifact Store Volumeを直接読む方式にしない。最大byte/tokens/timeを再適用し、過去の非信頼内容を高優先度指示へ変換しない。欠損・変更・古さは明示エラーであり「元の記録を見た」ことにしない。

### 18.7 再検索・Rehydrationの正確なトリガー

| Trigger | 必須動作 | 禁止事項 |
|---|---|---|
| 新Steer/GoalRevision、User訂正 | anchor再生成、関連候補再選択、旧Summaryをstaleに | 旧Goalを現行として再利用 |
| Pause/Resume/Worker crash、lease再Claim | DB checkpoint HEAD/Manifest/ledger/ACLで検証後再構築 | in-process promptキャッシュを正本化 |
| Model fallback/Tokenizer change | Token見積・Tool schema・output reserveを再計算 | 元モデルの上限を無条件コピー |
| Memory modify/delete/revoke | 次Model dispatch前にepoch/active revisionを再検証、失効投影除外 | tombstone済みMemoryの復活 |
| Source revoked/Artifact hash mismatch | 参照隔離/再確認、結果に明示的Uncertain | 旧URLから無断で現在値推定 |
| Watchが新しいOccurrenceを開始 | 独立Goal snapshot＋観測日時で新しいfinite Run | 長いLLM conversationを継続して最新値だと主張 |
| 長時間経過・Task入力切替 | Source-specific freshness policyで再確認対象を選択 | すべての資料に共通「10分で無効」を強制 |
| 子Taskの結果/失敗通知 | manifestのGoalRevision・Source refs・confidenceを親で再評価 | 子の「成功」メッセージだけで完了 |
| Sandbox Exec/Workspace generation変更 | ExecId・Cell image digest・workspace snapshotの出典を再検証 | 以前のファイルと権限を新Runへ暗黙継承 |

時間ベースは観測の**鮮度と目的の適合性**を判定し、価格/空室/運航状況と説明用の不変文書を同じTTLにしない。Providerの利用許可・料金・レート制限・SSRF/Egress/Sentinel審査に合格した場合だけ再取得し、アクセス不能を現在値0や非存在へ変換しない。

### 18.8 Durable Task JournalとCheckpointに残す最小情報

長時間Taskは「LLMが覚えていること」に依存しない。Protected Serviceが Run ごとに append-only journal を記録し、必要な範囲のみcontextへ投影する。

- Goal/active constraints・変更履歴・明示的な否定条件
- 決定した方針と理由、採用/却下候補、未解決の問い、保留中の外部作用
- 検証済み事実/仮説/不一致の別、Evidence refs/observedAt/validity
- Task内の進捗、まだ実行していない操作、Tool Intent/receipt ID（本文秘密は除外）
- Run checkpoint HEAD・leaseEpoch・Manifest digest・sourceEpoch・policyVersion
- 最近のUser操作/承認/取消のref、親子Task lineageとChild Result status

Journalは生会話やProvider responseを重複全文コピーしない。見かけ上のSummary更新成功だけでRun cursorを進めず、Artifact durability → manifest persist → HEAD CAS → model dispatchの順に行う（外部HTTPをDBトランザクション内で呼ばない）。既存のCPR-T系を参照し、最終CASを失ったWorkerの結果はcommit不可。

Task再開の決定的手順: fresh claim → canonical Task/Goal/Auth/Policy/Memory epoch read → HEAD+artifact+digest check → ledgerのUNKNOWN解決要否 → context rebuild → pre-dispatch再検証 → model。旧CheckpointのwriterEpochと新Run leaseEpochは一致する必要がないが、古いWorkerの新規writeは不可。

### 18.9 親子エージェントの情報伝達とConflict統合

子へのHandoffは親のチャット履歴全量でなく、許可済みGoal snapshot、最小Constraint anchors、出典参照、予算、対象Tool capability、保留疑義のみ。Child private working journal、Browser Cookie、Secrets、可変Workspace全体は親子で転送・共有しない。PR #68のCell独立性も適用。

~~~ts
interface ChildContextHandoff {
  parentTaskId:string; parentGoalRevision:string;
  childTaskId:string; childGoalRevision:string;
  purpose:string; requiredConstraintRefs:string[];
  evidenceRefs:Array<{ref:string; digest:string; observedAt?:string}>;
  unansweredQuestionRefs:string[]; budgetReservationRef:string;
  authorizedToolRefs:string[]; handoffDigest:string;
}
interface ChildResultBundle {
  childTaskId:string; childGoalRevision:string;
  status:"DONE"|"PARTIAL"|"UNCERTAIN"|"FAILED";
  verifiedClaimRefs:string[]; disputedClaimRefs:string[];
  evidenceManifestRef:string; uncompletedWorkRefs:string[];
  externalUnknownEffectRefs:string[]; outputDigest:string;
}
~~~

Parentは兄弟間で重複した主張をcanonical entity/Source digestで統合し、**独立Source数・証拠の時点・否定結果・対立**を保持。子のverdictを親のGoal successと直結しない。親Steer後は変更条件と不整合な子結果をSTALEとして隔離し、必要なら新Goalに沿う**新しい**有限子Taskを作る。子の実行費用/時間/Tool権限は親の配分を超えず、重複Task作成/commitを禁止する。結果はModel出力だけでなくResultVerifierと証拠参照で評価する。

### 18.10 Memory/会話検索との協調

- Memory正本は構造化したPostgreSQL record。User→Project→Taskのscope、consent、source status、expiry、revocation epochをDBで再検証。検索Index/Embeddingはキャッシュであり認可ではない。
- まずmetadata+keyword上位候補を取得し、ベクトル再ランクはoptional。停止時はkeyword-onlyを使用し、degradedを計測・表示。候補0件を「過去の条件が存在しない」と断定しない。
- 過去の「好み」と現在のGoal制約が不一致なら**現在の明示User条件が優先**。変更されたBudgetや日付を過去の要約で巻き戻さない。
- Long-term Memory、Userの明示訂正、Working Summary、Skill、Source Evidenceは別資産。Taskの進捗を永続プロフィールとして記録しない。
- 自動Memory書込みは候補化と同意/レビューを経由し、同じRunのsummaryが生成した推測を循環的に再読込して「検証済み」へ昇格させない。
- 個人情報・Credentials・機密の自動記憶禁止。削除済み投影の次dispatchでの可視性は0を保証する設計とし、**すでにProviderへ送信した情報は撤回できない**ことを明記する。

### 18.11 Muse Runtime Cell・Sentinelとの接続

Runtime Cellが生成したshell/python/nodeスクリプト、依存導入ログ、stdout/stderr、browser DOM、MCP resultはすべてUNTRUSTED_EXEC_OR_WEB。必要な出典/ExecutionId/WorkspaceGeneration/Cell image digest/Tool attempt IDを付けてArtifactへ保存し、命令でなく資料として再入力する。

- LLMがTool outputを引用してもHost/Protected Services/Secret/Browser CDPの権限は増えない。
- コンテキスト圧縮やMemory検索を通じてPolicyの優先順位を書き換えない。Prompt Injectionに由来する悪意ある手順はsource provenanceを保持しつつ命令として非採用。
- 以前の成功した一時Script/Skillを再実行するときも**現Run**のGateway、Cell Admission、Sentinel、Approval、Lease、Budgetを再審査する。Context再利用はAuthorization再利用でない。
- Contextが外部Host filesystemへの参照/秘密を含んでもCellにmount/権限を追加しない。見られない資料はアクセス不可と明示。
- 外部効果UNKNOWN、ユーザー承認待ち、送信済み処理の未確認を圧縮中に「成功」「未実行」に置き換えない。

### 18.12 UI/可観測性/ユーザー操作案（CHAT_08へ提案）

Task詳細に「使用Context」「維持した必須条件」「圧縮/退避」「参照した資料」「古い/不確実な記憶」「サブエージェント由来」「再検索理由」「モデル切替による再構築」「Token/cost」を分ける。LLMへ送信した秘密/内部Policy本文/他tenant資料をユーザー表示APIでそのまま公開しない。

操作候補: (1) 対象Taskの明示条件を修正するSteer、(2) Source引用の参照/確認、(3) Memory候補の承認・訂正・Undo・削除、(4) Context再構築の手動要求（新しい外部検索は別の許可ゲート）、(5) 期限切れ情報の更新要求、(6) 非常停止。Summaryの編集でGoal/Approval/Tool Receiptを直接書き換えられない。

各操作はAuthenticated Principal＋If-Match/expectedVersion、idempotency、Auditを経由。UIはprojection token比率、staleness、overflow、degraded、source coverageをread-only表示可能とするが、未実測精度を百分率の正答率として表示しない。

### 18.13 エラー・障害・Recovery matrix

| Case | Deterministic outcome |
|---|---|
| A0/A1/A2が収まらない | BLOCKED_CONTEXT_CAPACITY。条件を削らずModel実行0 |
| Retriever DB/Vector失敗 | DBなら許可済み現在Inputだけでdegraded/stop、Vectorのみならkeyword fallback |
| 原本Artifact取得不能・digest不一致 | 対象を隔離、引用無効、Uncertain。架空のEvidenceを返さない |
| Summaryが根拠なしclaimを生成 | reject、再要約の有限回またはbounded original chunkへ戻る |
| Compactionの途中にSteer/Cancel/Memory delete | stale Goal/epoch manifest commit禁止。現在版で再構築 |
| Provider overflow 2回目 | これ以上同一request再送せずPARTIAL/モデル切替候補 |
| Model切替でTokenizer/Tool schema不一致 | capabilities再検証、再plan。未対応Toolを偽装しない |
| 子Task失敗/権限失効 | 既存の検証済みEvidenceだけ親へ返し、未完了を明示 |
| Sandbox Crash | Durable refsとledgerから回復。UNKNOWN effectは再送禁止 |
| Budget不足/Timeout | 新規LLM/retrieval/toolを停止。保存済みEvidenceでPartial |
| ACL/retention期限切れ | 対象の検索/モデル注入拒否。索引の古いヒットで許可しない |
| Web/API Source時刻不明 | 現在値として採用せず「時点不明」を明示 |
| 高優先度UI操作とWorker競合 | newest recordVersion/goalRevision/leaseEpochでCASし旧Writer拒否 |
| Long-term MemoryとGoalが衝突 | Goal優先・Memory suppress・理由をmanifest記録 |
| Multi-tenant横断検索/ArtifactRef | unauthorizedとして応答0。漏洩内容と件数は公開しない |

### 18.14 永続化/API/移行の依存（新しい正本をここで勝手に作らない）

**CHAT_01**へ: ContextManifestRef、Goal anchor ref、message/event cursor、Artifact locator/observation timestamp、idempotent Child Result source mapping、共通error/event識別子。

**CHAT_10**へ: Manifest/Journal/Projectionのappend-only version と HEAD CAS、scope/RLS、Artifact durability、hash/serializer version、sourceEpoch、replay/outbox、retention/backup tombstone high watermark。Manifest/summaryを原本としないDB FK。既存schemaと互換な expand→backfill→read-switch→contract を後から設計。

**CHAT_02**へ: ContextPort.build/compact/rehydrate、typed hard-anchor extractor、tokenizer capability adapter、ModelRouter invalidation、Budget reservations、Worker lease fencing、Child handoff semantics。

**CHAT_06**へ: projection consent/read freshness、keyword/semantic検索、MemoryWriter CAS+Undo、sourceEpoch revocation barrierとconfirmed deletion UI。

**CHAT_03/09**へ: read-by-locator Gateway、Redaction/ACL/PII、Sandbox/Cell provenance、Sentinelの外部通信最終権限、Tool Ledger UNKNOWN保存。

**CHAT_08**へ: read-only context manifest views、source inspection、freshness and degraded indicators、command/steer surfaces。

**CHAT_11**へ: oracle用のgolden source/anchor/claim dataset、原本照合用fixture、fault/tenant/AI hallucination/overflow tests。新しい共有DTOはOwner ADRとmigrationを通すまでPROVISIONAL。

### 18.15 実装優先度・Release gate

| Phase | Owner/依存 | 到達条件（コード実装後に判定） |
|---|---|---|
| ACM-0 Contract lock | 01/02/03/06/09/10/11 | ContextManifest・Goal anchors・memory epoch・Artifact locator・errorsの型とACL/versions合意 |
| ACM-1 Safe admission | 02/09/10 | 必須条件、tokenizer上限、hard filters、Model input構築、匿名Traceを動作 |
| ACM-2 Artifact offload/grounded summaries | 01/02/03/10 | Raw→locator→summaryの引用/完全性検証、oversizeとoverflowが安全停止 |
| ACM-3 Durable journal/rehydration | 01/02/10 | Killpoint/Goal steer/Model swapで復元、old workerとUNKNOWN副作用を排除 |
| ACM-4 Memory and subagent handoff | 02/06/09/10 | revoke、scope、parent-child merge、重複/対立をテスト |
| ACM-5 UI/observability + Quality benchmark | 08/11 | source/compaction/uncertain表示、精度・費用・回復率をfixtureで計測 |

**Hard gates (合格条件、未測定)**:
1. Hard anchorの変更/欠落 0、Goal旧版を現行へ注入 0、Scope/secret/permission leakage 0、失効Memory再注入 0。
2. Source無しの価格・日時・承認・成功状態生成 0、Evidence locator取り違え 0、UNKNOWN外部効果の自動再実行 0。
3. Provider overflowで同一request無限再送 0、Budget無制限 0、old lease commit 0、並列Child権限拡大 0。
4. Event/Artifact/Manifestの復旧結果が根拠に整合。Raw/summaryの誤同一視 0。すべてのfailed/pending/degraded結果をユーザーに可視化。

**Quality gates（測定してからしきい値採用）**: evidence recall@budget、source precision、grounded claim coverage、contradiction preservation、context token/task、cost USD/task、compaction recovery p95、retrieval p95、source age distribution、child merge duplication、latency p50/p95。Golden fixtureとBaseline (no compaction/safe tail/offload) を同一モデル/Tool/costで比較し、**実測前に架空の目標改善率を決めない**。採用後の退行はRelease blockerにする。

**Minimum fixture catalog (ACM-T01..T20、すべて設計で未実行)**:

| Test | Input / fault | Expected |
|---|---|---|
| ACM-T01 | 8名/2泊/総額上限/和暦TZを含む長会話 | hard anchorsがbyte/typed equivalentで残る |
| ACM-T02 | 古い2名のMemory、新Goalは8名 | 8名のGoalを優先、旧記憶は候補抑制 |
| ACM-T03 | 10MB HTML+PDFのページ/表 | 原本 offload、出典locatorで再読込 |
| ACM-T04 | Summaryで架空の料金と承認を挿入 | grounded validation reject、偽の完了0 |
| ACM-T05 | 相反するSource A/Bと否定Evidence | 対立を両方保持、勝手に片側を確定しない |
| ACM-T06 | 誤ったToken estimate、Provider overflow | 有界再構築1回、再度超過でPartial |
| ACM-T07 | contextWindow 8k→128k→4kのModel切替 | tokenizer/schema再評価、4k不可なら安全停止 |
| ACM-T08 | Compaction処理中にSteer/Cancel | stale manifest HEAD CAS 失敗、旧Tool0 |
| ACM-T09 | Memory delete vs in-flight selection | 次モデルdispatch時の削除済みprojection 0 |
| ACM-T10 | 他tenant Evidence/embedding hitを混入 | データ本文/件数/識別子漏洩0 |
| ACM-T11 | 子Agent×4、重複URLと矛盾結論 | dedupe + timestamp + disputed refs保全 |
| ACM-T12 | 親のGoal変更後に旧子が完了 | stale隔離、親成功を捏造しない |
| ACM-T13 | Crash後に同Run/new leaseで再開 | Journal/manifest/ledgerから復旧、old writer0 |
| ACM-T14 | Sandbox stdoutがSYSTEM相当の命令と秘密を含む | データとして隔離、Credential/Policy/Tool grant不変 |
| ACM-T15 | Workspace fileが再開時に別digest | 旧参照失効、勝手に旧File読んだ扱いにしない |
| ACM-T16 | Tool callだけ残ったtruncated transcript | repairまたは停止。call/result不整合0 |
| ACM-T17 | 期限切れの価格・ホテル空室 | stale明示、現在値/availableを捏造しない |
| ACM-T18 | Artifact durabilityとCheckpoint HEADの間にkill | HEADは耐久済みrefのみ、破損時はcorrupt |
| ACM-T19 | Budget不足＋retriever故障＋User再開 | 無限Retry/課金0、Evidence付きPartial |
| ACM-T20 | 100+Step相当を複数finite Run/Watchで継続 | 既存Goal/ユーザー訂正/出典/Cost/状態が整合 |

### 18.16 具体的な動作例（仕様の検証用）

**例A: 長時間宿泊調査**。User「関西で8人、2泊、駐車場あり、総額が安い」。候補50ページ分をArtifactへ保存し、Goal anchor「8人・2泊・駐車場・総額・日付未指定」を保持。抽出表の料金には出典/観測時刻と人数条件を付ける。Userが「サウナ付きも追加」とSteerしたらGoalRevisionを増やし、旧Summary内の絞り込み条件を正本として使わず再ランキング。予約実行は別途Approvalを要求し、宿泊可能性を未確認のまま確定と表示しない。

**例B: 商品価格の定期監視**。前回の価格45,000円はHistorical Observationであり長期Memoryの現在値ではない。Watchの新Occurrenceで新Runを作成して許可済Providerを再照会。Webが失敗したら「0円」ではなく「確認不可」。圧縮した結果からSNS投稿の指示を高優先度命令として採用しない。

**例C: サブエージェント並列調査とSandbox**。子Aが比較表、子Bが公式条件を収集、子CがCode/CLIで表を変換。各子は独立Task/Cell/Workspaceと分配Budget、stdoutはuntrusted evidence。親は重複/不一致を照合し、raw Citationなしの「成功」はResultVerifierで失敗。親がCancelしたら以後のTool/egress grantを止めるが、外部効果UNKNOWNはLedgerで個別照会する。

### 18.17 この判断の競合・見送り

- **採用**: deterministic constraints + artifact offload + structured journal + selective on-demand rehydration + keyword first/optional vector + post-retrieval ACL + typed manifest + bounded adaptive budget。
- **見送り**: Prompt全文をVector DBへ永久保存、全MemoryをSystem messageに注入、LLMに自身の権限やSecretを記憶させる、古い結果の自動真実化、モデルの主観スコアのみで安全判断、長時間の無限Single Run、子Agentのprivate state共有、ユーザーの同意なしの永続Preference追加。
- **Experimental**: 小型LLMの階層要約、学習型reranker、source affinity cache、階層ごとのL2/L3 compression。採用には匿名fixture上でHard gates完全維持かつQualityとCostの再現可能な改善を必須とする。実機測定前に仕様の必須経路へ追加しない。
- **Owner未合意の境界**: context manifestとcommon event schema、Retrieval APIとACL、Artifact locator/retention、quality thresholds、Source TTL、Model tokenizer capabilities、UI最終API/計測単位。これらをPRマージだけでLOCKEDと称さない。

### 18.18 P0: Semantic Fact entailment と Typed Goal Anchor（未Locked）

**検証すべきものは出典の存在ではなく、出典の特定範囲が主張の各属性を支持するかである。** 参照・Digest・Locatorのみの検査はデータ同一性を示すが、意味的支持を示さない。数値・日付・料金・人数・単位・税/手数料内外・肯定/否定・条件/例外の要約を`SourceBackedFact`に正規化する。自動的なVerifierが対応できない自由文は`UNVERIFIED`に落とし、決定的な照合なしに`VERIFIED_WITH_SOURCE`にしない。

~~~ts
type ValueKind = "MONEY"|"DECIMAL"|"DATE_TIME"|"LOCAL_DATE"|"BOOL"|"ENUM"|"TEXT";
type FactSupport = "SUPPORTED_BY_SPAN"|"DERIVED_BY_RULE"|
 "REFUTED_BY_SPAN"|"UNDETERMINED"|"SOURCE_MISSING";
interface SourceBackedFact {
 factId:string; predicate:string; kind:ValueKind;
 canonicalValue:string|null; // exact decimal / ISO / enum, null=unknown
 operator:"EQ"|"NE"|"LTE"|"GTE"|"IN"|"NOT_IN"|"EXISTS"|"NOT_EXISTS";
 unit?:string; currency?:string; timeZone?:string;
 taxTreatment?:"INCLUDED"|"EXCLUDED"|"UNKNOWN";
 feeTreatment?:"INCLUDED"|"SEPARATE"|"UNKNOWN";
 modality:"ASSERTED"|"NEGATED"|"CONDITIONAL"|"UNKNOWN";
 conditionRefs:string[];
 sourceArtifactRef:string; locatorRef:string; sourceSpanDigest:string;
 parserRevision:string; observationRef?:string;
 observedAt?:string; validUntil?:string;
}
interface SemanticVerificationReceipt {
 claimId:string; sourceBackedFactRefs:string[];
 support:FactSupport; checkedSourceRefs:string[];
 checkedLocatorRefs:string[]; checkedAt:string;
 deterministicRuleRef?:string; verifierRevision:string;
 counterEvidenceRefs:string[];
}
~~~

**独立したGoal型**: GoalはWeb出典ではなく本人が明示したUser Message/GoalRevisionが正本。`SourceBackedFact`をGoalに流用して`sourceArtifactRef`を要求しない。`GoalConstraintValue`は値・operator・unit/currency/TZ・肯定/否定・REQUIRED/FORBIDDEN/SOFT・条件スコープ・曖昧性を持ち、`TypedGoalAnchor`にはversion/digestと構造化した制約値を保持する。詳細正本は`docs/02-runtime/goal-and-planner.md`の「P0 Typed Goal」節。

**8人・2泊・48,000円・駐車別途2,000円**という資料では、`accommodation_total=48000 JPY`、`parking_fee=2000 JPY, feeTreatment=SEPARATE`を別Factにする。「駐車込み48,000円」は正しい出典が付いていても`REFUTED_BY_SPAN`。合計50,000円の導出には決定的なdecimal演算と、駐車台数・日数・税・その他料金の適用条件の検証が必要。条件不明なら`UNKNOWN`（確定総額とは言わない）。

**照合順序**: ACL/retention/source digest→original locator span取得→version固定parserのField抽出→claimの型付き正規化→値・演算・否定/conditional・含有条件を突合→`SUPPORTED_BY_SPAN` / `DERIVED_BY_RULE` / `REFUTED_BY_SPAN` / `UNDETERMINED`。LLMやNLIの同意は参考であり決定的合格証明ではない。出典が記載することと、現実の真実・最新性・予約可能性は区別する。反証/資料間競合を消してはいけない。

### 18.19 P0: Disjoint Token Budget と Sealed ModelRequestSnapshot

**Token会計**: 各最終Provider Request内のContent Partは一つだけの`accountingPath`を持つ。A0/Policy最小Projectionは`Tsys`、A1/Goalは`Tgoal`、A2/Controlは`Tctrl`に一度だけ入れる。`Tframe`はRole区切りやProvider付加情報のみ、`Ttool`はTool schemaのみ。`Tmandatory`と`Tinput`は*最終シリアライズされた全要求*をtokenizeした値であり、項目別Tokenの足し算がTokenizer境界の影響で一致しない場合は最終値を優先し差分を記録する。`Tinput+R+S<=W`の最終Adapter admissionを必須とする。Tokenizer不明なら保守的な上限証明か拒否。Output上限がShared Windowか独立なのかもModelProfileで定義する。

**ModelRequestSnapshot**は候補選択用`ContextManifest`とは独立した、保護された**最終送信入力**の監査・復元契約。Manifestの`contextDigest`だけではModelへの実際の入力は再現できない。

~~~ts
interface ModelRequestSnapshot {
 snapshotId:string; runId:string; stepOrdinal:string; attemptId:string;
 contextManifestRef:string; promptTemplateRevision:string; promptTemplateDigest:string;
 serializerRevision:string; canonicalizationRevision:string;
 providerAdapterRevision:string; modelId:string; tokenizerId:string; schemaVersion:1;
 messages:ReadonlyArray<{
  ordinal:number; role:"system"|"developer"|"user"|"assistant"|"tool";
  parts:ReadonlyArray<{ordinal:number; kind:"text"|"image"|"file_ref"|"tool_result";
   sourceLocatorRef?:string; sealedContentRef?:string;
   redactedProjectionRef?:string; partDigest:string}>;
 }>;
 toolSchemas:ReadonlyArray<{ordinal:number; schemaRef:string; digest:string}>;
 continuation?:{mode:"NONE"|"PINNED_OPAQUE"|"UNAVAILABLE";
  providerId?:string; sealedProviderStateRef?:string; stateDigest?:string};
 providerPayloadDigest:string;
 policyVersion:string; goalRevision:string; memorySourceEpoch:string;
 modelInputTokens:string; outputReserveTokens:string;
 recipientAdmissionDecisionRef:string; dispatchAdmissionRef:string;
}
~~~

- Promptテンプレート、Role/順序、Content Partの形式・正確なSource Locator、Tool schemaの内容/順序、SerializerとProvider変換のRevision、Opaque continuationを記録。`ContextManifest`は選択の正本、`ModelRequestSnapshot`は実際の送信組立の正本、Event/Tool receiptは別正本。
- 可逆なPrompt本文を保存する必要がある場合は**暗号化済みSealed Content**と厳格ACL/保持期間/鍵破棄。通常Trace・UI・非信頼Cellに平文PromptやSecretを出さない。保存禁止/失効/原本消去で本文がないなら`UNAVAILABLE`、Digestしかない場合も完全再現とは表現しない。
- 再開に必要なProvider continuationはProvider固定とし、Provider切替時は`NEW ATTEMPT`で新Snapshotを作り、旧Attemptの再送で重複Tool/課金を発生させない。Crashとlost ACKの扱いはCheckpoint/Ledgerに従う。
- 保存順: Final serialization→Sealed snapshot durability→Memory/Goal/Policy/Provider admission→送信開始→Usage/Reply commit。Sealed dataの書込みが拒否された場合は再現性を偽装せず`BLOCKED_SNAPSHOT`か、Policyで明示的に許可された*内容非保存の非再現可能モード*を別状態として記録し、二重送信はしない。

### 18.20 P0: Memory削除・Model送信のAdmission線形化

モデル送信直前の`sourceEpoch`再読込だけでは、確認直後の削除競合を防げない。**Memory Writerの削除commit**と**Protected Model Gatewayの送信Admission commit**を、同一のMemory scope/tenant/subject epoch rowを使った短いDB transactionで順序づける。Provider送信I/OのためにDB row lockを保持しない。

~~~text
build request snapshot (still unsent)
protected ModelGateway short DB tx:
  lock scope_epoch + Task/Run current version
  check Memory sourceEpoch, revocations, Goal, policy, lease, budget
  check recipient policy/consent for exact provider + data classification
  unique (tenant, attemptId), requestDigest/idempotencyKey
  INSERT dispatch_admission(ADMITTED, epoch, recipient, payloadDigest)
  COMMIT
sender:
  recheck ticket/revocation in protected sender; mark STARTING / MAY_HAVE_SENT
  initiate bounded network I/O; persist response/usage receipts (or UNKNOWN)
memory delete/correction:
  same scope row lock + CAS tombstone + increment sourceEpoch + outbox
  find admitted/in-flight attempts; invalidate unstarted tickets, cancel if feasible
  record possible exposure for started/ambiguous attempts
~~~

**線形化保証**: DELETE commitが先なら旧Memoryを含む**新規Admissionは0**。Admissionが先なら送信が始まった可能性があるので、取消しを試みても**プロバイダ到達を否定できない**。Admissionと物理1byte目の送信は同一atomic transactionにできず、「DELETE commit後に一切のbyteが絶対届かない」とは主張しない。`NOT_ADMITTED` / `ADMITTED_NOT_SENT` / `MAY_HAVE_SENT` / `CONFIRMED_SENT` / `CANCEL_REQUESTED`を区別。ACK喪失やネットワーク断は`MAY_HAVE_SENT`であり「未送信」にしない。

複数Gateway replicaのin-process mutexだけに頼らず、scope row lock、epoch CAS、unique Attempt、lease fenceを正本としてテストする。Memoryの一件削除でもSnapshotの各Projection SourceRefを照合し、すべてのTask/子Task/Cacheで失効を強制。既にモデルへ送った情報は回収不能であることをユーザーへ説明する。認証の失効やRecipient変更もAdmissionで同じく再検査する。

### 18.21 P1: 補完仕様と運用上の境界

- **Sealed OriginalとRedacted Projection**: 正当に保持できる原本は暗号化し、用途別のRetention、ACL、Hashを与える。LLM向けRedacted Projectionは別のDerived Artifactで、別Hash・原文と投影のLocatorMapを持つ。Cookie/APIキー等の不要なSecretは保存禁止/隔離。原本消去後にProjectionだけを新しい原本だと偽装しない。
- **A0/A2最小化**: PolicyとEffect/Approval台帳はProtected Servicesの正本。LLMへは安全性・状態説明に必要な最小の`ModelVisiblePolicyProjection`/`ControlProjection`だけ渡す。未公開Policy全文・認証トークン・Sentinel管理操作・秘密は出さない。Tool GatewayはProjectionとは独立して必ず最終再検査。
- **HistoryとCurrentを分離**: Sourceが期限切れでも、歴史的観測として問われた時点に限り観測日時/有効時点/出典付きで使用できる。現在値の判定からは除外する。期限切れ情報を0円/空室なし/現在値へ自動変換しない。
- **Token効率**: まずP0/P1を確保し、P2/P3はToken残余下でmarginal utility（採用済みSourceとの重複を除いたCoverage/Quality ÷追加Token）最大の候補を繰り返し選ぶ。`rankScore`はAuthorityにならない。Score先頭ソートによるutility死文化は禁止。
- **Provider別データ転送**: Fallback前にProvider ID/地域/保持・学習利用設定/契約・データ感度/Recipient許可/Consentを保護Policyで再判定。不許可なら別Provider送信0、明示同意かRedaction/Partial。旧Providerの許可を再利用しない。
- **Child Result受入**: Parentは現行Goal/lease/authorization/child lineage、Source ACL/digest/time/Locator、Evidence矛盾、Effect Ledgerを再確認し、結果を`CHILD_STALE`/`UNAUTHORIZED`/`PARTIAL`等に分類。遅延した子の「成功」だけでParentをSUCCESSにしない。
- **GC/物理参照**: Artifact/Manifest/ModelRequestSnapshot/Checkpoint HEADの生存参照・TTL・retention・legal eraseを管理。Live HEADが示す資料を無断GCせず、保持義務の削除時はHEAD/SourceをEXPIRED・DEGRADEDとし、引用不可にする。孤立Manifestはpending writer/leaseの可能性を考慮した短期GC待機。外部StorageとDBの非原子的な境界を復旧/監査で検出。
- **失敗履歴Task Journal**: immutable`{attemptId, logicalToolCallId, inputDigest, failureCategory, effectState, retryEligibility, retryAfter, rejectedApproachRef, evidenceRefs, nextPlannerEligibility}`を保持。Plannerは同じ失敗原因のblind retryをしない。UNKNOWN side-effectはどのSummaryからも再実行指示へ変えない。
- **Context差分復旧**: ManifestのGoal/Policy/Memory/Recipient/Prompt/Source digestと現行ACLを比較し、変更されていない認可済みEvidence部分のみ再利用。Steerで古いGoal・Memory・Approval・Provider Promptを使い回さず、新Snapshotとdiff reasonを保存。
- **Context Cache**: cache key案はtenant/owner/task/GoalRevision/Policy Epoch/Source digest+ACL/Memory Epoch/Model+Tokenizer+Prompt/Redaction/Recipient class。跨tenantのPrompt/Content共有は不可。Public immutable cacheはprivate tenant contextと物理/論理分離し、cache hit後もdispatch admissionを再実行。削除/失効/Source GC/Consent変更ですぐ無効。
- **Coverage Checker**: Dispatch前にGoal condition IDs、Evidence必須slot、否定条件、Source freshness、Unresolved、Tool call/result pairing、UNKNOWN effectsを検査。Tokenが足りていてもSource不足なら`COVERAGE_INCOMPLETE`。有限なread-by-locatorで補完できなければ`PARTIAL_INSUFFICIENT_CONTEXT`か`BLOCKED_CONTEXT_CAPACITY`。検証可能な出典を追加取得せずに「揃っている」と扱わない。

### 18.22 追加の受入テスト (ACM-T21..T36、全て設計のみ)

| Test | Input/fault | Oracle |
|---|---|---|
| ACM-T21 | 正しいSourceRef、別途駐車料金を「込み」に変造 | Source-backed Fact support REFUTED、verified claim 0 |
| ACM-T22 | A0 Policyをsysとanchorへ二重計上 | unique accountingPath、最終Provider input tokens一致 |
| ACM-T23 | Template/Message順/Role/Tool schema変更後resume | Snapshot digest違い検出、exact replay誤認0 |
| ACM-T24 | Memory v10取得→削除commit→Model Admission | 旧Projection Admission 0、送信0 |
| ACM-T25 | Admission commit後に削除→Provider ACK lost | Cancel試行、MAY_HAVE_SENT、未送信と断定0 |
| ACM-T26 | 8人・2泊・禁煙・金額税込/税別・日付未指定・否定条件 | 型付きHard/Soft/Unresolvedと元Value保持 |
| ACM-T27 | 原本PDFとRedacted Projection、原本GC | 各Digest/LocatorMap分離、失効Source verified 0 |
| ACM-T28 | Expired価格の現在検索と歴史検索 | current reject / historicalに時点付き |
| ACM-T29 | Provider A同意、B未同意でfallback | B送信0、Consentの再取得またはPartial |
| ACM-T30 | Cross-tenant Cache hitとMemory delete | hit拒否・invalidate、旧Prompt admission 0 |
| ACM-T31 | Parent Steer中にChild DONE | Goal/ACL/Evidence再検査、旧Child統合0 |
| ACM-T32 | Artifact/Manifest GCとHEAD CAS競合 | dangling ref成功0、Orphan適切GC |
| ACM-T33 | 同じTool失敗3回・異なる代替案 | Journal履歴をPlannerが読みblind retry 0 |
| ACM-T34 | Model切替とGoal Steer | 未変更Evidenceのみ安全reuse、Prompt再生成 |
| ACM-T35 | Context上限内でも必須Evidence slot不足 | COVERAGE_INCOMPLETE、bounded rehydrate/Partial |
| ACM-T36 | Sealed snapshot保持不可・Continuation失効 | 再現不能を宣言、無条件再送0 |

**現状は仕様書/静的検証のみで実装Fixtureは未実行。** QA Ownerがgolden source spans、期待TypedFacts、DB行/Attempt、Provider request transcript、Killpoint/CAS競合、Budget/Consent/Artifact GCを含む実行可能fixtureへ落とし込む。

## 18.23 External Context Optimization Adapter: RTK / Caveman / Headroom（2026-11-09、Draft）

**前提**: これはOSSコードと公開ベンチマークの静的調査から導いた実装候補であり、aiagent-md本体の実装・性能測定・Owner承認ではない。18.1〜18.22のTypedGoalAnchor、P0/P1保護、ContextManifest、Sealed ModelRequestSnapshot、Memory deletion dispatch barrier、Evidence entailmentを置き換えず、**LLMへ渡す許可済みの非信頼データ投影**だけを最適化する。元のTool Result、原本、Evidence、認可、支払/投稿等の副作用、Run stateは不変。

### 18.23.1 採用/見送りと一次資料

| OSS（調査commit） | コード/方式 | 選定と注意 |
|---|---|---|
| RTK [develop 08a261a](https://github.com/rtk-ai/rtk/tree/08a261a0d081ffe11a32986e2f4d76f3fdc33abb) | CLI output filters; [src/filters](https://github.com/rtk-ai/rtk/tree/develop/src/filters)、[lossy truncate](https://github.com/rtk-ai/rtk/blob/develop/engine/src/methods/truncate.rs)、[editions](https://www.rtk-ai.app/docs/resources/editions/) | SandboxのGit/test/buildの表示出力のみ検証。rtk-ossはApache-2.0、fullのengineとrtk-proxyはBUSL-1.1。pre-exec command rewrite、共有API proxyは見送り |
| Caveman [main 3dc497a](https://github.com/JuliusBrussee/caveman/tree/3dc497a7f858c774d6e15b25e8c6120ef3a0eda8) | [compressors](https://github.com/JuliusBrussee/caveman/tree/main/engine/compressors)、[Browse AX](https://github.com/JuliusBrussee/caveman/blob/main/browse/README.md)、[CCR](https://github.com/JuliusBrussee/caveman/blob/main/docs/technical/context-recovery.md)、[licensing](https://github.com/JuliusBrussee/caveman/blob/main/LICENSING.md) | AXのfocused snapshot・構造化表示を優先検証。独立browser_act/eval、Chrome CDP接続、外部CCR authority、Cloud Autoは見送り。OSS v3+全体Apache-2.0、旧版は別 |
| Headroom [main bb8d18a](https://github.com/headroomlabs-ai/headroom/tree/bb8d18a56abb07ca7e811235c86ea6b626eb1f8b) | [Python transforms](https://github.com/headroomlabs-ai/headroom/tree/main/headroom/transforms)、[smart_crusher.py](https://github.com/headroomlabs-ai/headroom/blob/main/headroom/transforms/smart_crusher.py)、[security](https://docs.headroomlabs.ai/docs/security-model) | JSON/ログ/検索結果の**in-process selective**圧縮を第一実験候補。共有Proxy、global SQLite CCR、auto memory、第三者宛送信・管理APIは不採用。Apache-2.0、依存/NOTICEは別監査 |

宣伝される90%や21–57%等は対象コマンド・fixtureでの**局所的な削減**であり、タスク当たりの支払額、精度、aiagent-md実測ではない。RTKのbytes/4やCaveman・Headroomのoffline tokenizer計測値は推定としてラベル付けし、Provider usage/cache read/write/課金は別カウンターとする。Caveman Browseは大ページで有利でも小フォームで負になる場合がある。圧縮率だけでなく原文再取得と誤判断コストを測る。

### 18.23.2 内部Adapter契約案（共有スキーマ/DDLではない）

~~~typescript
type OptimizeMode = "disabled" | "shadow" | "enabled";
type SourceClass = "cli_stdout" | "cli_stderr" | "json_data" |
  "search_hits" | "browser_ax" | "log" | "source_code" | "prose" | "unknown";
type OptimizeOutcome = "PASSTHROUGH" | "OPTIMIZED" | "OFFLOADED" |
  "SKIPPED_UNSAFE" | "INCONCLUSIVE" | "BLOCKED";
interface ContextOptimizationCandidateV1 {
  rawArtifactRef: string; redactedProjectionRef: string; sourceDigest: string;
  ownerUserId: string; tenantId: string; runId: string; goalRevision: string;
  contentClass: SourceClass; trustTier: string; toolAttemptId?: string;
  observedAt?: string; locatorRef?: string; policyEpoch: string;
  memoryScopeEpoch?: string; browserGeneration?: string; recipientId?: string;
}
interface ContextOptimizationReceiptV1 {
  outcome: OptimizeOutcome; originalRef: string; inputProjectionHash: string;
  outputProjectionHash?: string; outputArtifactRef?: string; locatorMapRef?: string;
  codecId?: string; codecVersion?: string; pipelineDigest: string;
  protectedFactRefs: string[]; reasonCode: string; qualityPolicyVersion: string;
  estimatedBeforeTokens?: string; estimatedAfterTokens?: string;
}
~~~

内部型は CHAT_02 proposal。Server-supplied IDs、Version、ACL、Current Run/Goal、Tool Intent/Receipt、Original/ProjectionのStorage authorityは01/03/09/10が持つ。LLM出力、外部HTML・JSON、MCP descriptorがcodec/policy/model recipientを変更できない。強い条件(A0 Policy/A1 Goal/A2 control、金額/日時/数量/否定、承認状態、UNKNOWN effect、署名、Tool role/call/result、出典根拠span)は原則非圧縮の保護領域。JSON schemaや正確な編集対象bytesを要求するReadは既定skip。OSSがロスレスを名乗っても同一性/情報保持の試験なしには信頼しない。

### 18.23.3 データフロー・選択・再取得

1. Policy/Gatewayで source/recipient/tenant 認可とretentionを確認し、CHAT_01/10の耐久 Raw Artifactを先に確定。Sealed Originalとモデル用Redacted Projectionは別digestとし、Secret/headers/cookie/token/環境変数を**エンジンに渡す前に**排除する。Redaction不能または機密分類unknownなら最適化/モデル送信を拒否する。
2. Context Managerがtyped anchors/Tool schema/Raw Evidenceと圧縮可能な投影を切り分ける。source type・Tool attempt・許可済みformat・codec pin・固定Policyにより**最大1つのlossy最適化方式**を選ぶ。RTK=CLI表示、Caveman=AX focused read、Headroom=JSON/log/searchが第一候補。圧縮済み結果へさらに別のlossy compressorを重ねない。
3. 元のRedacted ProjectionとOutput Projectionのdigest、codec/pipeline revision、locator map、provenance、入力/出力推定tokensをReceiptとして検証・保存。成功/失敗・ゼロ件・極値・列挙範囲・対立するSource・否定・税/手数料・タイムスタンプ・unitと原文の意味の不整合があれば使わない。引用が存在するだけでFact verifiedへ昇格させない。
4. Parser失敗、lossyで重要値消失、出力増加、compression timeout/crash、recovery原文欠損/容量不足、codec unknown、Host/Provider未対応の場合は許可済みの原文Redacted Projectionへ戻す。ただしBudget/window超過なら無断で大量の原文送信せず既存Artifact Offload、必要rangeのread、PARTIALまたはBLOCKEDとする。失敗したHookをPASS扱いしない。
5. LLMからの原文取得要求は固有の headroom_retrieve / browser_recover / rtk_retrieve APIを許可せず、既存Gateway認可済み read-only Artifact range Toolに正規化。所有権・tenant・current lease/GoalRevision・memory epoch・policy epoch・recipient・digest/expiry/locator・byte/token/time quotaを**取得時にも**再検証。Handleの知識はアクセス許可ではない。復元後もUntrusted data。
6. Snapshot/Manifestへ codecId+version、pipelineDigest、Original/Projection SHA、locator、scope/recipient/epoch、cacheVersionをbindする。Providerへ送ったcached prefixは同じConversationで**byte-identical**に再利用。各turnで過去の圧縮結果を再圧縮しない。Goal/Memory revoke/Cancel/Source ACL変更/Model switchではepochと能力を再評価し、旧Projectionを破棄・再構築する。
7. 最終serialized Provider requestの一意token bucketで再見積し、Tinput+R+S<=W、Budget reserve、Model dispatch admissionを再実施する。Provider input/output/cache使用量とモデル呼び出し/再取得回数を別集計。圧縮のためのLLMやembedding費用もRun Budgetに含める。

### 18.23.4 保存・Privacy・Service境界

Caveman/HeadroomのデフォルトローカルCCRは単一OSユーザーの信頼を前提とし、tenant隔離/個人情報削除/既存Artifact lifetimeに直接適合しない。共有Memory DB、Remote Proxy、Cloud Auto routing、telemetry/beacon、任意Plugin install、再取得Tool独立公開は製品既定にしない。採用するOSSはversion pin・SBOM/NOTICE・transitive dependency・環境変数・network endpoint/analytics・モデルダウンロード/remote inferenceを監査し、隔離されたTransform Workerか制限されたin-process libraryで使用。計測外部送信はopt-inのみ、デフォルトOFF。API資格情報を最適化エンジンに渡さない。

Artifact削除・revoke・TTL時は元データと全Projection、codec cache、locator、recovery handle、Memory index、backup復旧経路まで無効化し、古いcache keyを別Tenantに再利用しない。実際にProvider送信済みのデータを取り戻せるとは主張しない。Caveman CCRがhash-checkを持つことと、認可・暗号化・永久保存があることは別。原文復元の可否は**品質保証ではない**。

### 18.23.5 実装スライスと受入基準

OPT-A: Contract/disabled・shadow mode/原本とreceipt/計測; OPT-B: Headroom JSON/log選択的Library; OPT-C: Browser Worker内AX focused projection; OPT-D: RTK OSS post-exec display filter; OPT-E: stable cache/model switch/revocation/fault/quality comparison。最初の実装ではProxyを直列化せず、各エンジン独立flagとalways-off fallbackを用意する。品質・安全Gate OPT-T01〜T20（CHAT_11）を満たさないエンジンはenabled化しない。実測値に基づき、全て不採用も許容する。

**Owner handoff**: 01 Artifact/Provenance、02 Context/Middleware/Model、03 Retrieval Tool、04 Browser、06 Memory delete、08 UI、09 Tenant/Secrets/Sentinel、10 cache/GC/persistence、11 E2E/fault/benchmark。最新のCommit・ライセンス・APIは実装前再確認し、全員の合意前にLOCKEDとは呼ばない。

## 19. OSS Hybrid Compaction — DeepAgents JS / Mastra OM / OpenClaw（2026-11-09、Draft）

> **DESIGN ONLY / NOT IMPLEMENTED / NOT RUN**。既存18節のA0/A1/A2、typed Goal、SourceBackedFact、ContextManifest、Sealed ModelRequestSnapshot、dispatch_admission、CHAT_03/09 Sentinel/Tool Gatewayを置換しない。ここは上流コード・テストを静的閲覧した採用案であり、本Harnessのruntime検証や共有DTO/DDLの正式承認ではない。

**18.23との併用規則（統合監査）**: RTK/Caveman/HeadroomによるSourceClass別のOptimized Projection（18.23）と、DeepAgents/Mastra/OpenClaw型の履歴Compaction（19節）は、独立した候補として記録する。同一SourcePartRangeに対するlossy変換の多段連鎖や、圧縮結果のみを根拠にした再圧縮・Fact verificationは禁止。各Auditorは保存済みの許可済みOriginal/Redacted ProjectionとSource spanを直接検査する。組合せ固有のquality・tenant/recipient・deletion・cache/price・Tool pair・UNKNOWN effectの負例が実行PASSするまでは、両方式を同一SourceRangeへ同時enabledにせず一方をdisabled/shadowに保つ。どちらのAdapterもModelRequestSnapshot/HEAD CAS/dispatch_admissionを省略できない（OPT-T04/T16/T20、ACM-OSS-T03/T10/T11、全てNOT RUN）。

### 19.1 上流OSSの固定版と採用範囲

| OSS / inspected SHA | 検証したソース・テスト | 借用する機構 | 変更・不採用とする挙動 |
|---|---|---|---|
| [DeepAgents JS](https://github.com/langchain-ai/deepagentsjs/tree/ea67c7ec9d3d4fa1fa074220339325cd9a82b1d1) `ea67c7ec`、MIT | [summarization.ts](https://github.com/langchain-ai/deepagentsjs/blob/ea67c7ec9d3d4fa1fa074220339325cd9a82b1d1/libs/deepagents/src/middleware/summarization.ts)、[tests](https://github.com/langchain-ai/deepagentsjs/blob/ea67c7ec9d3d4fa1fa074220339325cd9a82b1d1/libs/deepagents/src/middleware/summarization.test.ts) | trigger/keep、Tool call/result原子境界、原本Offload、overflow時の有界再計画 | Backend offloadが失敗してもSummaryを採用する既定経路、Summary入力truncationで未検証範囲を消す経路 |
| [Mastra Observational Memory](https://github.com/mastra-ai/mastra/tree/7358ad73a381765572858d520f8170ab59e88514) `7358ad73`、対象OSS memory package Apache-2.0 | [実装](https://github.com/mastra-ai/mastra/tree/7358ad73a381765572858d520f8170ab59e88514/packages/memory/src/processors/observational-memory)、[ARCHITECTURE](https://github.com/mastra-ai/mastra/blob/7358ad73a381765572858d520f8170ab59e88514/packages/memory/src/processors/observational-memory/ARCHITECTURE.md)、[fault tests](https://github.com/mastra-ai/mastra/blob/7358ad73a381765572858d520f8170ab59e88514/packages/memory/src/processors/observational-memory/__tests__/om-error-and-persistence.test.ts) | Observer→Reflectionの階層圧縮、非同期Buffer、原本範囲付きRecall、失敗中の未観察保持 | `observedMessageIds`単位の全Part除外、timestamp cursorだけのcoverage認定、in-process lockだけの分散排他 |
| [OpenClaw](https://github.com/openclaw/openclaw/tree/bbf0d7380315f660bf79561223da1d34eadd4ac3) `bbf0d738`、MIT | [quality](https://github.com/openclaw/openclaw/blob/bbf0d7380315f660bf79561223da1d34eadd4ac3/src/agents/agent-hooks/compaction-safeguard-quality.ts)、[safeguard](https://github.com/openclaw/openclaw/blob/bbf0d7380315f660bf79561223da1d34eadd4ac3/src/agents/agent-hooks/compaction-safeguard.ts)、[tests](https://github.com/openclaw/openclaw/blob/bbf0d7380315f660bf79561223da1d34eadd4ac3/src/agents/agent-hooks/compaction-safeguard.test.ts) | structured summary、未解決User ask/Exact IDs監査、修正retry、cancel/Tool pair保護 | `qualityDegraded`を自動採用、timeout時の無要約tail破棄、custom Providerの品質監査迂回 |

ライセンス・Copyright/NOTICEと依存先は流用する**具体的ファイル単位**で再確認し、Mastra Enterprise対象物は流用しない。丸ごと依存の導入はVM/Agent Loop/Memory/Storage境界のレビュー後のみ。固定SHAは調査時点の挙動であり上流変更時にはdiffと試験を再評価する。

### 19.2 圧縮Adapterの責務と型（CHAT_02内部提案）

段階: **CTX-OSS-A**＝DeepAgents式Offload＋Tool pair保護、**CTX-OSS-B**＝OpenClaw式構造監査＋aiagent-md独自SemanticVerificationReceipt、**CTX-OSS-C**＝Mastra式Observer/Reflector（最初はshadow mode）、**CTX-OSS-D**＝本実行fault/benchmarkの合格後opt-in。

~~~ts
// 新しい共有Schema/DDLの強制ではなく内部Portの提案
type CompactionTier="RAW_TAIL"|"GROUNDED_WORKING"|"OBSERVATION"|"REFLECTION";
type CompactionStage="PREPARING"|"RAW_DURABLE"|"PROPOSED"|"AUDITED"|"COMMITTED"|"REJECTED";
type CompactionFailure="OFFLOAD_FAILED"|"MODEL_FAILED"|"QUALITY_FAILED"|
 "SOURCE_MISMATCH"|"COVERAGE_GAP"|"CONCURRENT_REVISION"|
 "CANCELLED"|"BUDGET_EXHAUSTED"|"ARTIFACT_UNAVAILABLE";
interface CompactionCandidate {
 runId:string; attemptId:string; expectedHeadRef:string; leaseEpoch:string;
 goalRevision:string; policyVersion:string; memorySourceEpoch:string;
 sourcePartRangeRefs:string[];
 rawArtifactRefs:Array<{ref:string;digest:string;locatorRef:string}>;
 proposedSummaryRef:string; summaryDigest:string; producerId:string;
 tier:CompactionTier; stage:CompactionStage; auditReceiptRef?:string; failure?:CompactionFailure;
}
interface CompactionAuditReceipt {
 candidateRef:string; structureValid:boolean; toolPairsValid:boolean;
 latestUserAskValid:boolean; exactIdentifiersValid:boolean;
 hardAnchorsEqual:boolean; sourceCoverageComplete:boolean;
 semanticVerificationValid:boolean; checkedSourceSpanRefs:string[];
 rejectedClaimRefs:string[]; verdict:"PASS"|"RETRYABLE"|"REJECT"; reasons:string[];
}
~~~

OSS Adapterは非信頼候補を生成するだけ。Task Goal、Policy、Memory Writer、Tool Approval/Effect Ledger、Result Verifier、Event Store、Run HEAD、Provider Gatewayの正本を書き換えない。組込み/custom Provider、Observer、Reflectorのすべてに共通の`auditCompactionCandidate`を適用し、Providerの自己申告を合格証明にしない。

### 19.3 原本を保護した二相公開

「原本保持」はretention/consent/ACLが許すRaw Message/Event、Tool call/result、Artifactの**耐久正本をSummaryから独立させること**であり、秘密の永久保存やLLMによる100%の意味保存ではない。

1. 圧縮対象のMessage Role/Content Part順序、Tool call/result対応、Raw Event Cursor、Sealed Originalを先にProtected Storeへ保存。Cookie/API key/Private envはCHAT_09に従い不保存・分離し、LLM用Redacted Projectionと別digest/locator mapで管理。Raw Blobのdigest/locators/ACL/retentionと**全source range coverage**のdurability確認までは`RAW_DURABLE`に進まない。
2. Offload失敗でもSummaryを続行するDeepAgents既定動作は**採用禁止**。`OFFLOAD_FAILED`はHEAD不変、元Contextとエラー記録だけ残し、収まらない場合はPARTIAL/BLOCKED。要約・観察・Reflectionの生成失敗も旧HEADを維持する。
3. Summarizer/Observer/Reflectorが生成するのは`PROPOSED`候補とsourcePartRangeRefsだけ。原本の参照・出典・未解決条件なしの新しい主張を`VERIFIED_WITH_SOURCE`にしない。
4. OpenClaw式の5セクション（Decisions、Open TODOs、Constraints/Rules、Pending user asks、Exact identifiers）と直近User依頼/重要IDを確認。上流の一部識別子検査のみでは不十分なので、18.18のSourceBackedFactとSemanticVerificationReceiptを用いて金額、税、別料金、人数、日時、TZ、肯定/否定、条件付きSource spanと矛盾資料を照合。引用だけではMeaning supportを保証できず、UNVERIFIED/REFUTEDのClaimはReject。
5. Canonical typed Goal/否定/最新Steer、P0/P1、未解決疑義、Tool Effect UNKNOWN、Source coverage、ToolペアをProtected serviceで再検査。Summaryの「成功した」という記述からEffect LedgerやApproval、Task successを更新しない。
6. `PASS`したCandidate＋Audit receipt＋ContextManifest＋Source Coverage＋Sealed ModelRequestSnapshotをimmutableに保存。新Goal/Policy/Memory sourceEpoch/ACL/Recipient consent/Run leaseで再検証し、`expectedHeadRef`付きRun HEAD CASだけで`COMMITTED`として公開。失敗したCandidateはモデルへ非公開、旧HEADは不変。
7. 公開後もProtected Gatewayの`dispatch_admission`を別途必要とする。前回Model attemptやTool EffectがMAY_HAVE_SENT/UNKNOWNなら無条件再送禁止。Crash、Steer、Memory DELETE、Cancel、Provider fallback/Token計数変更は新規ModelRequestSnapshotで再評価。
8. GCはLive HEAD/保存期間/処理中Writer/Legal eraseを区別。原本が消えた場合はEXPIRED/ARTIFACT_UNAVAILABLE、digestしかないSummaryを可逆な原本扱いしない。

**Fail closedは無限再送ではない**。Safe tailだけでWindowに収まらない、P0/P1が完全に保持できない、または予算が尽きた場合はPARTIAL/BLOCKED。Quality警告を表示しても危険な外部操作を継続する権限にはならない。

### 19.4 Mastra型Buffer・Reflection・Recallの整合性

Mastraの監査資料にあるReflection rollover、Buffer遅延、古いHeadへのActivation、観察済みMessageへ追加されたPartの欠落（H7）を故障注入対象とする。`observedMessageIds`やTimestamp cursorだけでは新Partの未観察状態を示せない。

Chunkは不変の`[partCursorFrom,partCursorTo)`、sourcePartRangeRefs、digest、generation、attemptIdに束縛。**Buffer完了とActivation確定は別**であり、連続したprefix coverageとHEAD CASに成功した範囲だけを既観察に移す。後から追加されたPartは同じmessage IDでも未観察。Reflectionは未採用Chunkを世代切替時に原子的にcarry-forwardするかRawから作り直す。異なるWorkerの書込みはDBロック/CASで線形化し、process-local queueだけに依存しない。

Observation/ReflectionはCHAT_06のLong-term Memoryへ勝手に書込まない。RecallはProtected Gatewayのread-only `ArtifactRef+locator+expectedDigest`、ACL/retention/Memory tombstone/source freshness/byte+token budget再認可を経由。原本なしの場合は`ARTIFACT_UNAVAILABLE`、捏造して修復しない。初期はshadow mode（結果を主モデルに投入しない）からcoverageと競合を測定し、有効化はゲート合格後だけ。

### 19.5 OpenClaw式品質監査・失敗リトライの上書き禁止

上流の`auditSummaryQuality`は形式・直近依頼・一部IDの保持を確認するが、出典とSummaryの意味論的同値性を証明しない。上流では`qualityGuardEnabled`がfalseの経路やCustom Provider出力の監査迂回があり得るため、aiagent-mdのAuditorを最終公開Portへ独立して設置する。

- Structural/Pending User Ask/IdentifiersとSource spanに基づくSemanticVerificationを両方PASSさせる。LLM/NLIの主観的PASSだけで安全・根拠を認めない。
- Quality correctionは初期最大**1回**だけ（別attemptにBudget予約）。Provider Overflow縮小の既存1回契約も維持し、総BudgetとAttempt限度を超える場合は追加モデル呼出0。
- 修正失敗時の`qualityDegraded` fallback、timeout時の無要約partial history、Custom Providerによる品質監査迂回を**COMMITTEDへ昇格させない**。元HEAD/Artifactを残してPARTIAL/BLOCKEDまたは安全に検証済みの限定回答。
- Provider切替時はRecipient地域・保持/学習利用設定・User Consent、Token Window、Tool Schema、Template revisionを新規確認。監査のために秘密原文を別Providerへ送信しない。
- ReceiptにはreasonCode・attempt・source locators・費用・latencyと観測状態だけを残す。秘密、原文全文、Cookie、APIキーはTrace/UI/Cellへ漏らさない。

### 19.6 故障・復旧Matrix

| 障害 | Head/送信 | 期待する挙動 |
|---|---|---|
| Raw offload write/digest/locator failure | 変更禁止 | OFFLOAD_FAILED、前HEAD＋原本保持 |
| Summary empty/timeout / unsupported Fact | 変更禁止 | 有界修正→REJECTED、PARTIAL/BLOCKED |
| Degraded fallback/custom compactor bypass | 変更禁止 | 共通Auditorで拒否、Warningだけ表示 |
| Tool pair破断 / Part coverage gap | 変更禁止 | Rawから再計画、旧Tool実行の重複0 |
| Reflection rollover / stale chunk / CAS race | 変更禁止 | chunk prefix・世代・HEAD再検査 |
| Goal Steer/Memory revoke/Provider change | stale snapshot送信禁止 | 再構築・Consent/Admission更新 |
| Original GC/Retention expiry | 偽のsource復元禁止 | EXPIRED、原本取得不可を明示 |
| 必須文脈過大/総予算枯渇 | unsafe採用禁止 | finite PARTIAL/BLOCKED |
| UNKNOWN Tool/ACK lost | Tool再実行禁止 | Ledgerで別途照合、MAY_HAVE_SENT |

### 19.7 追加Acceptance Fixture（ACM-OSS-T01〜T14、すべてNOT RUN）

| Test ID | 故障注入 | 期待Oracle |
|---|---|---|
| ACM-OSS-T01 | Offload write失敗、Summaryだけ生成成功 | RAW_DURABLE未到達、HEAD不変 |
| ACM-OSS-T02 | 要約モデルtimeout・空文字 | 原本維持、旧HEAD不変 |
| ACM-OSS-T03 | 48,000円＋駐車別2,000円を「込み48,000」に変造 | SourceBackedFact REFUTED、採用0 |
| ACM-OSS-T04 | 最新User ask/禁止条件/必須IDを削除 | Structural/Goal audit拒否 |
| ACM-OSS-T05 | qualityDegraded fallback | 警告のみ、COMMITTED=0 |
| ACM-OSS-T06 | Custom Providerがauditを迂回 | 全Providerに共通Auditor適用、Reject |
| ACM-OSS-T07 | Buffer書込みとReflection世代変更が競合 | chunk連続性保持、重複/欠落0 |
| ACM-OSS-T08 | 観察済message IDへ別Workerが新Part追記 | 未観察Partを残す |
| ACM-OSS-T09 | HEAD CASを旧Workerが遅延実行 | stale commit 0、旧Tool再実行0 |
| ACM-OSS-T10 | Provider fallbackとMemory削除が競合 | 旧Memory送信0、新Recipient consent |
| ACM-OSS-T11 | Toolペア付きPrompt overflow＋ACK lost | ペア維持、Effect UNKNOWNを再送しない |
| ACM-OSS-T12 | 100回のObservation/Reflection反復 | Goal/否定/Source coverage欠落0 |
| ACM-OSS-T13 | 原本Retention終了/GC後にRecall | EXPIRED、引用Verified 0 |
| ACM-OSS-T14 | Correction retryと総予算枯渇 | 有界回数、旧HEAD維持、PARTIAL/BLOCKED |

**比較ベンチ**: same Model/Tokenizer/Tool schema/Policy/Goal/Source/Total Budgetでsmall safe-full、safe-tail、offload-only、offload+audit、offload+audit+OMを比較。Recall@FixedBudget、Source locator精度、TypedFact support、否定/未解決条件、Tool pair、100-cycle fidelity、p50/p95 Latency、Model+Observer+Reflector+Recall総費用、GC/保存bytesを記録。P0違反が1件でもあれば速度が良くてもRelease不可。改善率は未測定。

## 20. Python-first移行時の圧縮OSS方針（2026-11-09、STEP 1 / Draft）

§19のOSS Hybrid Compactionは**DeepAgents JS**（`langchain-ai/deepagentsjs`）を静的閲覧した設計だが、Python-first（[ADR-036〜040](../00-foundation/architecture-decisions.md)）では**DeepAgents Python**（`langchain-ai/deepagents`、PyPI `deepagents 0.7.34`、MIT、requires-python >=3.11）を第一調査対象とする。main `0279f1d036a8a73ea0d99bf04583e9c9a9598ef2` に `libs/deepagents/deepagents/middleware/summarization.py`、`_blob_offload.py`、`_message_eviction.py`、`backends/store.py` の存在だけを確認した（**内容監査・JS版との挙動同等性・テスト実行は未実施**）。

- CTX-OSS-A（Offload＋Tool pair保護）はDeepAgents Pythonの対応middlewareで再監査し、§19.3の「Offload失敗でもSummaryを続行する既定動作は採用禁止」をPython版でも同じ故障注入（ACM-OSS-T01）で確認する。JS版で確認した挙動をPython版の事実として扱わない。
- CTX-OSS-B（OpenClaw式品質監査）とCTX-OSS-C（Mastra Observational Memory）はTypeScript実装にのみ存在するため**直接移植しない**。必要な機構（5セクション構造監査、Part cursor連続性、Buffer/Activation分離、Reflection世代切替）は§19の仕様どおり**aiagent-md独自のPython Port**（`CompactionAuditor`、`ObservationBuffer`）として実装し、上流コードは設計参照に留める（分類C）。
- 候補B（DeepAgents Python＋LangGraph）はSTEP 2でADR-038に選定された（下記§20.1）。採用しなかった場合の規定として、DeepAgents Pythonのsummarization/offloadはContext Manager内部の交換可能Adapterとして評価できる。いずれもRaw履歴の耐久保存、Source-backed Summary、A0〜A2固定、ContextManifest、Sealed ModelRequestSnapshot、Memory consent/削除、Quality Audit、Failed Compaction Recoveryの既存契約に従い、Framework側Summaryを正本にしない。
- Long-term Memory候補（PostgreSQL＋pgvector、Graphiti、Mem0）は[Memory Retrieval](../06-memory-skills/memory-retrieval-and-update.md)の2026-11-09節に従う。

### 20.1 STEP 2: DeepAgents Python圧縮の統合規則（2026-11-10、PoC試験済み・NOT LOCKED）

ADR-038の選定に伴い、圧縮候補の生成はDeepAgentsの`SummarizationMiddleware`（`create_summarization_middleware`、`deepagents 0.7.34`）に委譲できる。PoC（PYRT-T09）で確認した事実と規則:

- DeepAgentsのsummarizationは`state["messages"]`を書換えず、private fieldと`wrap_model_call`で要約後のModel入力を作る（ソースのdocstringで確認）。Summary生成のModel呼出しは通常のModel Gateway Adapterを通り、aiagent-mdでは**別stream（`summarize`）**として記録し、main transcript（Run HEAD）にSummaryを混入させない。PoCでHEADのRaw履歴12件が保持されることを確認。
- 発火閾値はModel profileの`max_input_tokens`から自動計算される。Model Gateway Adapterはprofileを明示し（PoCでは8,000 token）、閾値・保持数を設定値として記録する。
- 退避履歴は`/conversation_history/…`としてbackendへ書かれ、`read_file`で再読込みされうる。backendは`StateBackend`（Graph state内）に限り、ホストFSを使わない。Raw Artifactの耐久保存・ContextManifest・Source-backed Summary・Quality Audit（§19）はaiagent-md側の責務のまま。
- **未検証（STEP 3）**: §19.3「Offload失敗でもSummaryを続行する既定動作は採用禁止」のPython版での故障注入（ACM-OSS-T01）、summary品質監査、A0〜A2固定との統合。
