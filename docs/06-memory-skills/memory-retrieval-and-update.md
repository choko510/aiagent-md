# Memory Retrieval and Update — Detailed Implementation Specification

> Owner: CHAT_06 | Path: `docs/06-memory-skills/memory-retrieval-and-update.md` | **Draft / Provisional** | 2026-10-30 | Main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Logical model: [PR #4](https://github.com/choko510/aiagent-md/pull/4) (NOT merged)

## 0. 結論と採用案

`MemoryRepository`の構造化レコード（CHAT_06 06-01）を正本とし、認可されたscopeごとに**二段階検索（metadata/keyword → optional semantic rerank）**を実行。全候補は**DBで最新状態を再検証**してからToken Budget内の**非命令データProjection**としてContextManager(CHAT_02)へ渡す。明示的なユーザー条件・現在のTask Goal/Steerは過去のMemoryより常に優先。Embeddingが停止してもmetadata/keywordへ退化し、禁止/削除されたMemoryを出さない。

記憶の提案生成とActivationは別段階。暗黙の会話・Web文章から永続的好みを勝手に確定しない。SC-16のフィードバックはCHAT_14が解釈し、CHAT_06の権限・履歴・訂正・Undo mutation APIだけを使う。Evidenceの事実本体とWatch条件を変更しない。

## 1. 目的と非責務

対象：曖昧なユーザーの過去の好み/文脈を適切に呼び出す、関連のない記憶を注入しない、訂正/否定/削除が将来の回答へ確実に伝播する、予算内で候補を抽出・更新する。

非対象：Task Goal/Run checkpoint正本（CHAT_01/02）、Evidence/Observation正本（CHAT_01/07）、共通実行Approval/Tool（CHAT_03）、実行可能Skill（06-03）、通知ランキング（CHAT_13/14）、Web収集や匿名検索全体、Vector DBそのものを認可境界とすること。

シナリオ：SC-01/03/04/05/06/07/08/09、特に**SC-16**（除外店舗の可逆的好み更新）。SC-02はWatchを上書きしない意味で関連。

## 2. OSS実装監査（静的閲覧・試験未実行）

| Project / pinned revision | 確認した実コード・テスト | 適用/不採用 |
|---|---|---|
| Hermes Agent `fcc26fbd9f1641755e49793d7ed608f3b7214c3d` MIT | [`tools/memory_tool_store.py`](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool_store.py) の `MemoryStore.load_from_disk`, `format_for_system_prompt`、[`tests/tools/test_memory_tool.py`](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/tools/test_memory_tool.py) `TestMemoryStoreSnapshot`、`TestExternalDriftGuard`。固定snapshotとバッチ安全弁を確認 | セッション中モデルprefixを勝手に書き換えない思想を採用。全記憶一括system注入、text-only永続化を不採用 |
| Deep Agents `3efa098130d6670fe713f841072bc11ff40545da` MIT | [`middleware/memory.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/memory.py) `MemoryMiddleware.before_agent/abefore_agent/modify_request`、[async test](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_memory_middleware_async.py) `test_before_agent_skips_if_already_loaded_async`/`test_load_memory_handles_missing_file_async` | backendの抽象化と読み込み失敗のテストを採用。`AGENTS.md`を無条件のSystem Message authorityにはしない |
| LangGraph `ffc3336367e8139668d11e5f0b0e6def57e7c762` MIT | [`store/base/__init__.py`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/langgraph/store/base/__init__.py) `BaseStore.search/put/get` とTTL capability、[PG tests](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/tests/test_store.py) `test_omit_expired_filters_read_paths`, `test_omit_expired_refresh_ttl_only_refreshes_live_rows` | namespace/expiry filter test採用、Store内部のsemantic hitを認可済み結果と扱わない |
| Letta Code `981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b` Apache-2.0 | [`src/tools/impl/memory.ts`](https://github.com/letta-ai/letta-code/blob/981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b/src/tools/impl/memory.ts) `memory` はパス検証・rootの二重読み込み防止・上限/シンボリックリンク脱出拒否、[`reflection-memory-v2.ts`](https://github.com/letta-ai/letta-code/blob/981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b/src/cli/helpers/reflection-memory-v2.ts) `buildMemfsV2ParentMemorySnapshot` は投影量を制限 | progressive/deferred memoryの参考。Git管理やHost filesystemを実行権限込みで移植しない |

比較：**案A全件system prompt注入**は費用・誤情報・注入面で却下。**案B embedding上位kだけ**は期限/アクセス/意味衝突を無視するため却下。**案C 検索+決定的フィルタ+信頼ラベル付きprojection**を採用。全文semantic indexは後付け/停止可能。SDKの性能とバージョン互換は未実証。

## 3. 依存・責務境界

`MemoryQueryService` / `MemoryChangeService` がCHAT_06所有、`MemoryRepository` はCHAT_10へ論理契約、`ContextBudgetPort` と再生成のStep境界はCHAT_02、`AuthenticatedPrincipal`はCHAT_09、ID/Event/Error envelopeはCHAT_01、フィードバックイベント正本と自然言語の「好み」解釈はCHAT_14。

**重要な既存ドラフト差分**: CHAT_01 PR #2の公開IDはUUIDv7、`recordVersion`は1開始、Taskは`conversationId=null`可。CHAT_10 PR #10の`gen_random_uuid()`/`revision default 0`/`workspace_id`は衝突あり。MemoryはChat_01の公開IDへ合わせ、DB adapterが対応できるまで`[PROVISIONAL: CHAT_01/09/10]`。Memory-local CAS revisionはTask`goalRevision`でもRun`leaseEpoch`でもない。Run-originated compaction/learningにはfencing tokenを別途必要とする。

## 4. Requirement table

| ID | Level | 決定的な要求 | SC |
|---|---|---|---|
| MEM-RET-001 | MUST | principal由来のscope以外へQuery不可、index hitも再認可 | 06,08 |
| MEM-RET-002 | MUST | `active`＋有効期限＋scope＋revocation epochをDBで最終確認 | 04,07,08 |
| MEM-RET-003 | MUST | 現Task Goal/明示User条件を過去Memoryが変更しない | 01,04,16 |
| MEM-RET-004 | MUST | query+rerankは有限件数/時間/token/費用で打ち切る | 05,09 |
| MEM-RET-005 | MUST | retrieved textをRole命令・Tool approvalへ昇格しない | 03,08 |
| MEM-RET-006 | MUST | 訂正/Undo/削除で古いprojectionが次モデル境界で失効 | 07,08,16 |
| MEM-RET-007 | MUST | auto抽出はcandidate化、ユーザー明示/同意範囲のみactive化 | 04,08,16 |
| MEM-RET-008 | MUST | 同scopeの反証/同時変更を競合として保留し自動上書きしない | 04,07 |
| MEM-RET-009 | MUST | Task起源の自動更新はRun fence+goal revision+cancel再確認 | 07 |
| MEM-RET-010 | MUST | SourceRefのアクセス取消・欠損時に事実確度を上げない | 03,05,08 |
| MEM-RET-011 | MUST | インデックス停止/欠落は再構築可能、削除済Index行を復活させない | 07,09 |
| MEM-RET-012 | MUST | Memoryなしで回答する時は根拠不足・条件の不明を隠さない | 04,05 |
| MEM-RET-013 | MUST | 実行ログ/Traceに記憶本文/PII/Secretを流さない | 06,08 |
| MEM-RET-014 | MUST | preference否定/除外は可逆で、Evidenceや強制Watchを変更しない | 02,16 |
| MEM-RET-015 | SHOULD | explainableな採否理由とsource/effective dateをユーザーに提示 | 04,16 |
| MEM-RET-016 | EXPERIMENT | vector/hybridの再現可能なoffline評価、精度・費用計測 | 09 |

## 5. 実装用契約（owner内部の案、外部共有DTOは[PROVISIONAL]）

`MemoryRecord`/`MemoryScope`/`MemoryStatus` は [06-01](https://github.com/choko510/aiagent-md/pull/4) に定義（まだDraft）。ここでは再定義せず読み取りviewを定義する。

```ts
type MemoryQueryRequest = {
  taskId?: string; // canonical UUIDv7; absent allowed on conversation-only tasks
  goalRevision?: number; // never inferred from memory
  purpose: "current_preference"|"past_event"|"task_hint"|"evidence_context";
  queryText: string; locale: string; nowUtc: string;
  maxItems?: number; maxTokens?: number; // service clamps
};
type MemoryProjection = {
  memoryId: string; recordRevision: number; sourceRefs: readonly string[];
  kind: "profile"|"episodic"|"procedural_hint"|"task_note";
  content: string; assertedAt: string; expiresAt?: string;
  factualStatus: "user_stated"|"historical"|"source_verified"|"uncertain";
  instructionAuthority: "none"; // MUST always be none
};
type MemoryQueryResponse = {
  projections: readonly MemoryProjection[];
  sourceEpoch: string; policyVersion: string;
  truncated: boolean;
  degraded: readonly ("VECTOR_UNAVAILABLE"|"SOURCE_UNAVAILABLE"|"BUDGET_EXHAUSTED")[];
};
type MemoryUpdateCandidate = {
  operationId: string; sourceRefIds: readonly string[];
  kind: "profile"|"episodic"|"procedural_hint"|"task_note";
  claim: unknown; origin: "user_explicit"|"agent_inference"|"feedback";
  expectedRecordRevision?: number; proposedScope?: "task"|"project"|"user";
  expectedGoalRevision?: number; runFence?: {runId:string;leaseEpoch:string}; // wire decimal bigint per CHAT_01 event contract; validate <= PG BIGINT
};
```

Validation: UUIDv7 canonical lowercaseはCHAT_01 draftに合わせる。`queryText`上限例2,000 chars、`maxItems`既定8/最大20、`maxTokens`既定1,500/最大2,000（**仮上限、ベンチ未実施**）、sourceRefs実在性、policy/consent有効性を確認。Clientから`tenantId/userId`は受けず、Principalはサーバーで導出。`recordRevision`はMemory-local版（名称はCHAT_01へ統合提案）。不明なkind/status/enumはreject。ExpiryはDB UTC、時間比較をtimestamp文字列lexicalで代用しない。`instructionAuthority`は固定const値。

イベント提案は`memory.proposed` / `memory.activated` / `memory.revoked` / `memory.projection.invalidated`の**名称だけ暫定**。正式Event envelopeとdelivery semanticsはCHAT_01/10。公開APIはCHAT_08へ提案。

## 6. シーケンス / ライフサイクル

```text
[Task/Chat user intent] --(trusted AuthContext)--> [ContextManager / CHAT_02]
      | request bounded Memory hints
      v
[MemoryQueryService] -> [Auth/Policy CHAT_09] -> [PG metadata + keyword]
                                      |                  |
                                      |        optional vector candidate IDs
                                      |                  v
                                      +<------ [DB latest status/version/scope check]
                                               |
                                    [SourceRef verification / budget]
                                               |
                                    [data-only formatted projection]
                                               |
                               [ContextManager token allocation]
                                               |
                         [LLM context: rules > current goal > labeled memory]
```

変化：`candidate -> pending_review -> active -> superseded/contested/tombstoned`。Approvalが必要なcandidateはread対象にならない。Undoは取り消した「操作」に対する新revisionを作る。元Observation/Evidenceは改変しない。削除は最初にread障壁を原子的commit、index/backup消去は後で追跡。SourceRevocationはactiveのsource usable flagを下げ、本人確認まで再注入を停止。

## 7. アルゴリズム / 具体的停止条件

```ts
async function selectMemory(principal, req, contextBudget) {
  const permitted = await policy.authorizedMemoryScopes(principal, req.taskId);
  const now = clock.utcNow();
  const cap = clamp(req.maxItems, 1, 20, 8);
  const budget = Math.min(clamp(req.maxTokens, 1, 2000, 1500), contextBudget.remainingMemoryTokens());
  if (budget <= 0) return {projections:[], truncated:true, degraded:["BUDGET_EXHAUSTED"]};
  // fixed max candidates=80, service deadline e.g. 500ms config-driven
  let candidates = await db.keywordMetadataSearch(permitted, req.queryText, 80);
  try { candidates = unionIds(candidates, await semantic.search(permitted, req.queryText, 40)); }
  catch { degraded.add("VECTOR_UNAVAILABLE"); }
  const fresh = await db.getVisibleAuthorizedRecords(principal, candidates, {
    exactScope:true, currentEpoch:true, activeOnly:true, asOf:now
  });
  const ranked = stableRank(fresh, {
    scopeSpecificity:true, explicitPreference:true, lexicalScore:true,
    recencyByAssertion:true, sourceIntegrity:true, semanticScoreIfAvailable:true
  });
  const selected = filterContradictionsAndSourceAccess(ranked, req.purpose, req.nowUtc);
  return tokenizeBounded(serializeAsQuotedData(selected), cap, budget);
}
async function acceptUpdate(actor, candidate, runState) {
  if (candidate.origin !== "user_explicit")
    await requireEnabledConsentOrStageReview(actor,candidate);
  if (candidate.runFence) await assertRunFenceAndGoalRevisionUnderSameCommit(
    candidate.runFence, candidate.expectedGoalRevision, runState);
  // invalidate in-flight projections via DB epoch in memory-data-model
  return memoryService.mutateWithCASAndIdempotency(actor,candidate);
}
```

**Wire / revision audit:** CHAT_01 PR #23 Event Envelope uses positive decimal strings for BIGINT counters; Run `leaseEpoch` is a fencing token rather than Task `goalRevision` or Memory-local CAS. A JSON number is not acceptable for arbitrary DB BIGINT, even if most present values happen to fit JavaScript safe integers. Normalize with strict decimal parser, reject leading zeros/negative/out of-range, and compare as BigInt in the trusted adapter. CHAT_10 PR #22 queue design still suggests safe-number bounds and needs a common codec decision. This is [PROVISIONAL: CHAT_01/02/10].

**Rankingの順序**: hard filter（ACL/expiry/status/source validity）→ conflict collapse → deterministic score。同点は`updatedAt DESC, memoryId ASC`固定。Scoreは序列専用で「事実の確信度」ではない。Taskのgoal/date/party size/price constraintsと不一致ならmemory候補を抑制し、勝手にGoalを更新しない。`validUntil`が経過した価格はpast-event質問に限りhistoricalとして扱う。

**Read freshness race**: 読み取り直後にdeleteされた場合、Model callの直前に`sourceEpoch`/policyVersionを再照合し再構築または停止。すでに外部モデルへ送った内容は取り戻せず、明示した限界として監査。バックグラウンドTaskのwriteは`leaseEpoch/goalRevision`を確認する（型/DB mappingはP0）。

## 8. 故障マトリクス

| 障害 | 応答 / recovery | 外部副作用 |
|---|---|---|
| Vector provider unavailable | keyword-only、degraded表示、bounded retryなし | なし |
| DB stopped/timeout | memory injectionせず最新User入力のみ; 未知事項はUncertain | なし |
| ACL/scope変更 | 直前のDB revalidationで除外; cache invalidation再試行 | なし |
| Indexにdeleted hit | revocationEpochで除外、index修復outbox | なし |
| Time validity expired | current answerから除外、historical指定なら限定利用 | なし |
| Two writers同一profile | CAS conflict、最新versionを提示、無言上書きなし | なし |
| user delete vs auto summarization | CAS/fencing失効、復活拒否、再レビュー | なし |
| cancelled/steered Run | old fence/goalを拒否、candidate破棄 | なし |
| Index worker killed after commit | outbox replay、readerはDB正本でfail-closed | なし |
| Prompt injection in source | escaped data only、tools/approval変更拒否 | なし |
| Token/LLM budget exhausted | Memory skip / partial; 終了条件維持 | なし |
| Lost ACK after mutation | idempotency receipt照会、二重記録なし | なし |

## 9. Security / privacy

User/tenantをQuery入力から信用しない。PR #9のPrincipalとOwner scopeを使い`[PROVISIONAL: CHAT_09]`。Vector indexはACL正本でない。Cross-user timing/count漏洩をテスト。OS Shell、SSH、Docker socket、plugin exec、Secret managerの能力をMemoryから発行しない。Source textに`SYSTEM`/Tool spec/commandがあっても引用ラベルのみでモデルRoleを上げない。PIIは自動記憶せず、必要時に本人の許可を取得する。本文・raw vectors・Web URL query・個人情報をmetrics labelへ出さない。SSRF: memory URIは表示用source referenceであり、検索時に自動fetchしない。source再取得はCHAT_04/03のGatewayでnetwork policy経由。

## 10. Observability / finite budget

仮設定：candidate max80、semantic max40、projection max8/20、token1,500/2,000、DB retrieval timeout500ms、同一Run自動候補生成最大1回、memory provider費用はTask内上限に従いモデル呼出0を既定。実測前にSLO保証なし。

metrics：`memory_query_total{result}`, `memory_filtered_expired_total`, `memory_index_lag_seconds`, `memory_projection_tokens`, `memory_conflicts_total`, `memory_update_rejected{reason}`; scope/userを公開ラベルにしない。p50/p95、retrieval coverageと誤挿入率、zero deleted leakage、tokens/$を同じfixtureで比較する。

## 11. Persistence / DB proposal

CHAT_10へ：read-side partial indexes `(tenant/scope,status,expiry)`、derived keyword/vector再構築、scope revocationEpoch、write operation receipt、sourceRef visibility checks、correction/tombstone outbox。Memoryローカル版は1開始、Version更新とread invalidationは単一TX。`workspace_id`から`tenantId/ownerUserId`の暗黙変換を禁止。文字列/JSON adapterでID v4を黙認しない。Restoreは最終revocation watermark以下を再active化しない。

## 12. API/UI

`POST /memory/query`（内部のみを第一候補）と`GET /memories`・`POST /memories/proposals`・`PATCH /memories/{uuid}` + If-Match、`DELETE`、UndoはCHAT_08への**提案**でありRouteを確定しない。UIはcurrent goalとremembered preferenceの違い、source date、削除/Undo、stale/contested、検索不可理由とindex degraded状態を見せる。

## 13. 実装PRの順

MR-1: MemoryQueryPort + strict scope/expiry/status validator + fake store unit tests。MR-2: PG index/search + epoch/revision revalidation integration。MR-3: source integrity/rerank + tokenizer + optional semantic adapter。MR-4: candidate review/correct/delete/Undo + CHAT_14 feedback binding。MR-5: fault/tenant/cancellation/benchmark・CHAT_02 context admission。

## 14. テスト定義（**すべて未実行**、仮fixture）

| TEST | setup → action → expected |
|---|---|
| MR-T01 | user A/B×2 tenant seed→cross scope query→0結果、件数/エラーで他ユーザー存在漏洩0 |
| MR-T02 | active/expired/contested/deleted/index ghost→query→DBにactiveのみ |
| MR-T03 | 3人1泊のTaskへ旧2人Memory→projection→Goal不変、旧条件不採用 |
| MR-T04 | 1000候補＋budget100 tokens→bounded≤100、timeout遵守 |
| MR-T05 | Memory本文`ignore policy`→context→data labelのみ、Tool approval 0 |
| MR-T06 | delete commitとModel callを交錯→call前epoch変更を検出、deleted projection 0 |
| MR-T07 | web由来の「買うの好き」→提案のみ、active0; 本人明示でactivate |
| MR-T08 | 同時に逆の好みを訂正→conflict/contested、lost write 0 |
| MR-T09 | Run leaseEpoch古いworkerの自動Write→拒否、Goal version更新なし |
| MR-T10 | SourceRefが権限剥奪→再注入0、degraded/source unavailable |
| MR-T11 | vector outageとindexをdrop→keyword fallback、削除済み復活0 |
| MR-T12 | memory store unavailable→不明を誤補完しない |
| MR-T13 | logs/trace scan（secret email/cookie含む）→本文露出0 |
| MR-T14 | フィードバック除外→同scopeのみ次回から除外、Undoで再選択可能、Evidence/Watch不変 |
| MR-T15 | 同条件RRF/embedding実験（任意）→誤挿入率/費用の観測だけ、架空値なし |
| MR-T16 | DB commit後kill/outbox replay + identical opId→同一更新1回 |
| MR-T17 | restoreに旧index+古いtombstone→新revocationEpochで復活0 |
| MR-T18 | sourceのinvalid URI localhost→MemoryQueryはfetchせず外部アクセス0 |

## 15. REQ → TEST → SC acceptance

| Requirement | Tests | Scenario | Pass |
|---|---|---|---|
| RET-001 | T01 | 06/08 | 他ユーザー結果0 |
| RET-002 | T02/T17 | 04/07/08 | 非active注入0 |
| RET-003 | T03 | 01/04/16 | Goal immutable |
| RET-004 | T04 | 05/09 | 有限上限 |
| RET-005 | T05/T18 | 03/08 | 命令昇格/自動fetch 0 |
| RET-006 | T06/T17 | 07/08/16 | 削除後注入0 |
| RET-007 | T07 | 04/08/16 | 未承認active0 |
| RET-008 | T08 | 04/07 | silent lost update0 |
| RET-009 | T09/T16 | 07 | stale Run write0 |
| RET-010 | T10 | 03/05/08 | 失効Source拒否 |
| RET-011 | T11/T17 | 07/09 | index ghost漏洩0 |
| RET-012 | T12 | 04/05 | unavailable正しく提示 |
| RET-013 | T13 | 06/08 | PII logs0 |
| RET-014 | T14 | 02/16 | Undo/Evidence分離 |
| RET-015 | T14 | 04/16 | explainability |
| RET-016 | T15 | 09 | optional experiment |

## 16. Unresolved / Cross-team proposals

| Owner | Severity | 要求・受入 |
|---|---|---|
| CHAT_01 | P0 | source identity/Event schema、UUIDv7、memory CAS naming、nullable Task.conversationId→T03/T09 |
| CHAT_02 | P0 | context budget確定・model-call前epoch recheck・Run leaseEpoch/goalRevision admission→T06/T09 |
| CHAT_09 | P0 | principal/consent/restricted category/erase epoch→T01/T13 |
| CHAT_10 | P0 | workspace→tenant+owner mapping、UUIDv7 generation、version1、Outbox→T06/T16/T17 |
| CHAT_14 | P1 | Feedbackイベント/Undo/除外設定の正本。Memoryはmutation正本、Evidenceは改変しない→T14 |
| CHAT_08 | P1 | 入力/結果UIとIf-Match/Undo、SC-11〜16最新仕様へのREST記述更新提案 |
| CHAT_11 | P1 | kill/restore/tenant multi-user/semantic regression fixture |
| CHAT_03/04 | P1 | SourceRefを直接fetchしない、外部取得/Tool policyはGateway経由 |

## 17. Sources / verification

[S1–S4] ``##2``のcommit固定リンク（Code verified: source & tests inspected; tests not run）、2026-10-30。
[S5] [Hermes issue #85858](https://github.com/NousResearch/hermes-agent/issues/85858) concurrent profile Memory loss report（Issue report、closed 2026-10-13、現行コードの不具合保証でない）。
[S6] [Project Brief](https://github.com/choko510/aiagent-md/blob/b45d4e989998663a00ef09673e69ae1f4191f00d/docs/00-foundation/project-brief.md)（Docs verified）。
[S7] [Current SC-01..16](https://github.com/choko510/aiagent-md/blob/b45d4e989998663a00ef09673e69ae1f4191f00d/docs/00-foundation/acceptance-scenarios.md)（Docs verified）。
[S8] 設計上限・score・Adapter・APIはHypothesis/Proposal。実Harness未実装、ベンチ/テスト未実行。
