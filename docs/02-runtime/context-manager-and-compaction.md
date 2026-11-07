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
  "HYPOTHESIS" | "CONFLICTED" | "UNKNOWN" | "EXPIRED";

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
  canonicalGoalRef: string;
  goalRevision: string;
  mandatoryFields: string[]; // Typed validated field references, not summary text
  unresolvedConflictRefs: string[];
  pendingEffectLedgerRefs: string[];
  approvalStateRefs: string[];
}
interface ContextManifest {
  schemaMajor: 1;
  runId: string; stepOrdinal: string; leaseEpoch: string;
  goalRevision: string; policyVersion: string; memorySourceEpoch: string;
  modelId: string; modelCapabilityRevision: string; tokenizerId: string;
  rawEventCursor: string; checkpointHeadRef: string;
  selected: Array<{ref:string; digest:string; role:string; originalTokens:number}>;
  offloaded: Array<{rawRef:string; digest:string; locatorRef:string; reason:string}>;
  omitted: Array<{ref:string; reason:string}>;
  summaryRef?: string; summaryDigest?: string;
  sourceCoverageRef: string;
  contextDigest: string; // Canonical serialization + hash algorithm version
  estimatedInputTokens: number; reservedOutputTokens: number;
  hardAnchorDigest: string;
}
interface GroundedSummaryClaim {
  text: string;
  inputSourceRefs: string[];
  inputLocatorRefs: string[];
  claimState: ClaimState;
  observationTime?: string;
}
~~~

- 本文とハッシュは同一の暗号化されたArtifact/DB境界内で取得する。digestだけで認可や真正性を保証しない。ハッシュ方式/長さ/Canonical bytesの仕様はCHAT_01/10で確定する。
- Manifestは「次のLLM要求を再現できる*選択記録*」であり、Raw Evidence・会話・Tool receiptの正本ではない。選択省略理由は固定enumへ正規化し、秘密本文をTraceへ出さない。
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
utility = rankScore / max(1, tokenEstimate)
sort by (priority ASC, rankScore DESC, utility DESC,
         observedAt DESC NULLS LAST, stableRef ASC)
~~~

- スコアは**非権限データの配置順**のみ。数値が高くても期限切れ、出典欠落、アクセス失効、goalRevision不一致は除外。Model判断をScoreに混ぜる場合は説明可能なbounded featureとして別Version管理する。
- 同じURL/Sourceからの類似引用を無制限に採らず、独立ソースと未解決対立説のcoverageを確保する。対立する2つの資料を同一文へ統合して「確定」にしない。
- 未取得のEvidenceを「存在するはず」として召喚しない。source locatorが失効した場合は不明扱いにして選択し直す。
- Token見積が高い巨大本文は高得点でも先にoffloadして見出し・該当箇所のlocatorsへ分割。引用と選択理由はManifestから説明可能。
- スコア係数は機械学習で自動上書きしない。版を固定したoffline fixtureで比較し、品質・費用・偏りが改善した変更のみ明示採用。

### 18.5 モデル別の動的Token予算

毎回ModelRouterの当該versionからcontextWindow、maxOutput、tokenizer、tool schema/cached input仕様を検証する。実コストの課金とToken上限を混同しない。

~~~text
W = verified model context window
R = max(required structured output / Tool response reserve, user output request)
H = tokenCount(policy + mandatory tool schema + system framing)
S = max(1024 tokens, ceil(0.08 * W))  // safety margin, CONFIG provisional
I = W - R - H - S                  // content input capacity
A = exact tokenCount(A0+A1+A2 and mandatory P1)
if W <= 0 or I <= 0 or A > I: BLOCKED_CONTEXT_CAPACITY
optional = I - A
~~~

- Tokenizer不明時は保守的な上界見積とProvider preflightを用いる。上界を証明できなければ小さいModelへ無理に削って通さず、他Model選択またはPartial。Prompt cacheのhitを入力容量の拡大根拠にしない。
- 目安の上限: Memoryは min(2,000 tokens, optionalの12%)、直近Working Journalはoptionalの35%、Evidence excerptsはoptionalの45%。未使用枠はEvidence/Workingへ再分配。P1と重要User修正はこの割合で落とさない。これらは**各カテゴリへの初期cap**であって、総量を固定配分して浪費する義務ではない。
- Tool schema自体が大きい場合は最小の許可済Tool subsetを選び直す。未承認Toolを削除するだけで能力が不足する場合はその理由を表示し、Tool権限を記憶から補完しない。
- 予防圧縮: 予測入力がIの70%以上でP3/重複Bodyを退避し始める。Iの85%以上で構造化Summaryを優先し、Mandatory/P1を再検査。これは初期の調整値であり、モデル・fixture別測定で変更可能。
- コンテキスト長と**Run token/cost/time Budget**は独立のハード制限。圧縮に使うsummarizer/rerankモデル費用もTask Budgetへ予約し、予算不足なら無限に圧縮/再検索しない。
- Model providerのoverflow responseは、同じGoalRevision/Source snapshotで**最大1回だけ**縮小・再推定してリクエストを再発行。無変更入力の再送は不可。失敗なら明示的PARTIAL/モデル切替候補の提示。

### 18.6 Grounded Compactionと原本復元

**順序固定**: (1)権限・期限・Goal filter → (2)URL/Tool output/mediaの原本耐久保存・digest → (3)有界chunkとlocator → (4)独立claimのgrounded summary → (5)source coverageとhard anchor検査 → (6)新ManifestをCheckpointへ参照 → (7)LLMへ送信。

要約単位は見出し/段落/表row/page/range/exec stdout offsetなどの**復元できるロケータ**。生のBrowser Cookie、API key、Vault handle、private env、認証ヘッダーはArtifactへ平文で退避せずredaction-before-storeを適用（CHAT_09保持/監査規定）。画像・PDFをテキスト要約した結果はraw locator、ページ、図表のscopeを維持。

Summary claimには最低1個のsource refを要求。sourceがない新しい主張、引用に存在しない価格・日時・人数、承認済みとみなす状態変更はreject。LLMによるclaim整合判定は補助に限り、**typed hard-anchor equality** と source-ref coverage を決定的に検査する。複数sourceの矛盾、未完了作業、否定結果、UNKNOWN/UNVERIFIEDは要約後も明示維持する。

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
