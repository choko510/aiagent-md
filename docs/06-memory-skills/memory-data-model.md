# Memory Data Model — Detailed Implementation Specification

> Owner: **CHAT_06** | Canonical path: `docs/06-memory-skills/memory-data-model.md` | **Status: Draft / PROVISIONAL** | Revision: 0.1 | Research: **2026-10-30** | Main baseline: `92b5827ed16a7d4ae61e8146ce382795398293cb` | Next: `memory-retrieval-and-update.md`
>
> **重要**：本repoは仕様のみで実装なし。以下は **提案するHarness独自仕様** であり、外部OSSの現在の実装や稼働結果ではない。共有型、認可、物理DDLはそれぞれの担当がLockedにするまで未確定。

## 0. 結論と採用案

**採用する：PostgreSQL上の構造化・版管理された論理Memory Recordを唯一の正本とする。** LLMに渡す短い記憶はその時点の *読み取り専用Projection* であり、原本でも権限命令でもない。会話履歴、Tool Observation、Evidence、Artifact、Watch、Skill、Run/Task Stateは別資産として扱う。ユーザーの明示した訂正・削除を最優先し、出典/時刻/有効期限/同意/スコープと並行更新を機械的に検証する。

- **MUST**: User Profile（好み・継続属性）、Episodic（過去の出来事）、Procedural Hint（再利用可能な助言。ただし実行可能Skillではない）、Task Note（実行中の調査に限定する補助情報）を `kind` で区別する。
- **EvidenceはMemoryではない**。価格、在庫、空室、投稿、ソースHTMLをMemory本文の「最新の事実」として保存しない。外部Observation/Evidenceへの参照を保持するだけ。
- **認可権限は記憶から生成しない**。「以前OKだった」記憶では購入・予約・通知・Secrets参照の許可にならない。全ツールは正本のPolicy/Gatewayを通る。
- **同時書込み**: compare-and-swap revision + DB transaction + idempotency key。last-writer-winsや可変なMarkdown全体の上書きをしない。
- **削除**: ユーザーに返答する前に検索/注入から即時除外する論理失効をコミットし、派生Index/Cache/Backupは別途ポリシーに従い処理する。削除完了の範囲を偽らない。
- **未確定**: tenant/user principal、共通Event/Evidence ID、物理DDL、プライバシー・保持規定。各オーナーへ `[PROVISIONAL: CHAT_XX]` を提出する。

## 1. 目的・責務・非責務・受入シナリオ

### 1.1 解決したい失敗

同じユーザーが別の会話で「予算は1.5万円」と言ったとき、別の日付・別の旅行にも無条件に当てはめるのは誤り。商品価格が1週間前の45,000円であっても今日の実測値ではない。ユーザーが古い好みを訂正した後も古い要約から復活してはいけない。2つのWorkerが記憶を更新しても一方が他方を消してはいけない。

本モジュールは **何を記憶と呼ぶか・どの所有者に属すか・どう矛盾/更新/削除を記録するか** の正本。検索ランキング・コンテキストへの実際の挿入順・抽出LLMのプロンプト・ユーザーUIは次の仕様/他担当の正本。

### 1.2 分離したリソース

| リソース | 責務・正本オーナー | Memoryとの関係 |
|---|---|---|
| Conversation / Messages | CHAT_01 | 生の会話履歴。Memoryはsource locatorで参照し、コピーを正本としない |
| Task / Run / checkpoint | CHAT_01/02 | 実行状態はMemoryに置かない。task_noteは期限付きの補助メモ |
| Observation / Watch | CHAT_07 | 価格など時系列観測の正本。Memoryに観測値を常設しない |
| Evidence / Artifact | CHAT_01 | 引用・日時・Raw contentを保持。Memoryはstable evidence refだけ参照 |
| Long-term Memory | CHAT_06 | 本MDが意味論・版履歴・同意/失効を所有 |
| Skill | CHAT_06の別MD | 検証済み手順/依存/能力を表す。procedural_hintはSkillではない |
| Authorization / Secrets | CHAT_09、Tool PolicyはCHAT_03 | 記憶が権限を追加・更新・上書きできない |
| DB DDL / migration | CHAT_10 | このMDの論理要件を物理スキーマへ落とす |
| Chat / Telegram UI | CHAT_08 | Userの閲覧・訂正・削除・保留表示を提供 |

**適用SC**: SC-01（型番・価格の混同防止）、SC-02（WatchをMemoryへ誤移行しない）、SC-03（SNSの主張/注入）、SC-04（旅行条件と日付の限定）、SC-05（出典の鮮度）、SC-06（ユーザー隔離）、SC-07（再起動/競合）、SC-08（権限境界）、SC-09（取得費用）。

**Non-goals**: 学習用モデル重みの更新、無承認自動Tool実行、Webページ全文の長期保存、全会話のMemoryへの複製、Skill自動昇格、LLMを動かし続ける常駐Memory Agent、実行時のシステム命令のMemoryによる変更。

## 2. 既存OSS横断調査（ソースとテストを実際に閲覧）

調査日 2026-10-30。全て **commit SHA pinned**。タグへの対応は未検証につき「特定tag未確認」。`Code verified` はリンク先のソース/テスト本文を確認した意味であり、**ローカルでテストを実行した意味ではない**。

| 参考OSS / pinned commit | コード/テストで確認した具体的な処理 | 適用判断・弱点 | LICENSE |
|---|---|---|---|
| **Hermes Agent** `fcc26fbd9f1641755e49793d7ed608f3b7214c3d` | `tools/memory_tool_store.py:MemoryStore.load_from_disk` で `MEMORY.md`/`USER.md` の読み込み・scan・session開始時snapshot固定。 `_mutate` はロック後再読込し一括保存、`apply_batch` は全件検証後に書込。 `tools/memory_tool.py:_gate_or_stage` で承認のstage、`_background_delete_gate` で無人削除を保留。 `tests/tools/test_memory_tool.py` に注入、重複、overflow、drift、snapshot、batch、削除gate等のテスト。 [S1][S2] | 更新上限・保留・安全性のテスト構成を参考。2ファイル/文字列全文上書きはtenant分離・個別TTL・出典・revisionに不足。 | MIT [S9] |
| **Deep Agents** `3efa098130d6670fe713f841072bc11ff40545da` | `middleware/memory.py:MemoryMiddleware.before_agent/abefore_agent` がBackendから複数ソースを読み、`memory_contents` を生成。`modify_request`で注入。`backends/store.py:StoreBackend._get_namespace` がnamespace解決。 `test_memory_middleware_async.py` は単一・複数・missing source・既読状態を検証。[S3][S4] | backend抽象・on-demand文脈を採用。任意 `FilesystemBackend` はHostファイル操作権限があり本製品には不採用。Memoryの要件をMarkdown/AGENTS.mdへ全面委譲しない。 | MIT [S10] |
| **LangGraph Store** `ffc3336367e8139668d11e5f0b0e6def57e7c762` | `store/base/__init__.py:BaseStore.put/get/search/delete` はnamespace/key/valueと任意TTL・Indexを持つ。`supports_ttl=False` が基底のdefault。`checkpoint-postgres/tests/test_store.py:test_omit_expired_filters_read_paths` は物理行が未掃除でもexpiredならread/list/searchから隠す、`test_omit_expired_refresh_ttl_only_refreshes_live_rows` はexpiredを復活させないことを検証。[S5][S6] | namespace/TTLテストパターンを採用。ただしStore APIは本製品のユーザー訂正、出典、同意、version conflictまで保証しない。optional Adapterにとどめる。 | MIT [S11] |
| **Letta Code（現行系）** `981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b` | `src/agent/memory.ts:getDefaultMemoryBlocks` はpersona/humanブロックをMDXより読込。`src/tools/impl/memory.ts:memory` は相対path検証、`..`・隠し要素・skills予約・`realpathSync` で外への脱出を拒否、読み取り上限も設ける。`src/agent/prompts/letta_root_memfs.md` はmemory/skills分離、coreを短く保ち、Secretsを書かないことを要求。[S7][S8] | プロンプトに常駐する最小Memoryと外部Memory/Skillsの分離が有用。ただしMemFSのgit操作/実行環境を本製品へそのまま移植しない。今回確認した範囲では現行LettaのMemory同時更新テストまでは確認していない。 | Apache-2.0 [S12] |

**事故・Issueの参考**: Hermes issue #85858 は共有profileへの並列書込みでMemoryが失われ得るという報告（Issue report）。**2026-10-13 closed**、現在の `MemoryStore._mutate` ではロック後再読込が実装されているため「現行Hermesで未修正」とは断定しない。採用理由は競合試験を明示するため。[S13]

### 2.1 設計案の評価

| 案 | コスト/保守 | 障害・一貫性/プライバシー | 結論 |
|---|---|---|---|
| A. `USER.md`＋`MEMORY.md` だけ | 初期は簡単、全文コンテキストに安い | 個別の出典/TTL/削除証跡/同時更新/検索ACLを管理しにくい | **却下（唯一の正本にはしない）** |
| B. LangGraph Store/Letta APIを正本とする | OSSの機能に迅速に依存できる | SDK版変更、独自同意/権限/対立関係の差異を吸収しにくい | **却下（Adapterとしては選択肢）** |
| C. 独立した論理Memory Service＋Postgres transactional store＋bounded projection | モデル/Storage Adapter差替え可能。初期実装負担はAより大 | 明示revision、削除、source ref、ACL、閲覧・監査が一貫 | **採用** |

外部OSSのコードを直接コピーしない。ライセンス遵守が必要な再利用が生じる場合は、範囲・NOTICE/帰属・依存更新方針を別途法務/メンテ審査する。

## 3. 依存関係・正本・所有権

```text
[Chat/Telegram / Task/Run]  (CHAT_08 / CHAT_01/02)
              |
              | trusted principal / explicit user intent / source pointers
              v
[MemoryService: CHAT_06] -------- [Policy & privacy: CHAT_09]
      |        |                           ^
      |        +--> [Tool Gateway: CHAT_03] |
      |        +--> [Evidence: CHAT_01] / [Observation: CHAT_07]
      v
[MemoryStore adapter / PG transaction: CHAT_10]
      |
      +--> durable records + revision log + deletion ledger/outbox
      +--> rebuildable search index/cache (NON-authoritative)
      |
      +--> [MemoryProjection] --> [ContextManager: CHAT_02]
                                     (data, not instruction authority)
```

本モジュール内部の契約は `MemoryService`, `MemoryRepository`, `MemoryPolicyAdapter`, `MemoryProjectionPort`。**principal情報をLLM引数から受け取らない**。User/tenant識別と全アクセス検証はCHAT_09が正本。[PROVISIONAL: CHAT_09]

- Memoryが所有: 保存可否、意味分類、conflict key、Memory revision、status、有効時刻、source validation、append-only change historyの意味。
- CHAT_01が所有: Conversation/Task/Run/Event/Evidence正本の識別子とEvent envelope。以下の `sourceRef` は暫定。
- CHAT_02が所有: Context注入のタイミング、ステップ境界、token budgeting、Steer/Cancel。
- CHAT_03が所有: approval/external side-effects。Memory書込みのみは「Toolの副作用承認」を代替しない。
- CHAT_07が所有: 観測値、Watch ID、周期・閾値・通知。
- CHAT_09が所有: sensitivity分類の組織方針、user isolation、erase/legal hold、ログ/鍵。
- CHAT_10が所有: 物理テーブル/Index/マイグレーション。

## 4. Requirements（安定ID、優先度、観測可能な結果）

| ID | 優先 | 内容・合格時の振る舞い | SC |
|---|---|---|---|
| MEM-DATA-001 | MUST | `profile/episodic/procedural_hint/task_note` を型で区別し、Evidence/Skill/Watchを別正本とする | 01,02,04 |
| MEM-DATA-002 | MUST | 認証済principalのtenant/user/scopeで読み書きを認可し、他ユーザーを取得できない | 06,08 |
| MEM-DATA-003 | MUST | sourceRef・記録日時・原情報日時・信頼区分・confidenceを維持 | 01,03,05 |
| MEM-DATA-004 | MUST | 外部価格/空室/在庫を永続的な現在値へ昇格しない | 01,04,05 |
| MEM-DATA-005 | MUST | 主張の事実有効期限・検索可否期限・データ消去期限を別管理 | 02,04,05 |
| MEM-DATA-006 | MUST | revision CAS、同一key競合のdeterministic conflictで無言の上書きを防止 | 07 |
| MEM-DATA-007 | MUST | request idempotencyで再配達/再起動後に論理書込みが1回のみ | 07 |
| MEM-DATA-008 | MUST | ユーザー訂正を優先し旧versionは即時検索不可、source historyへ追跡可能 | 04,07 |
| MEM-DATA-009 | MUST | User削除後に検索/投影は即時失効、Index/Cacheは非正本として消去キューへ | 06,08 |
| MEM-DATA-010 | MUST | Secretと危険な指示をMemoryから権限へ昇格させない | 03,08 |
| MEM-DATA-011 | MUST | 許可済scopeのみ検索・Projectionへ出し、期限切れ/対立/保留を黙って確定としない | 04,08 |
| MEM-DATA-012 | MUST | 人間による明示保存/禁止/訂正/削除と自動提案を区別し監査 | 04,08 |
| MEM-DATA-013 | MUST | Durable writeと更新履歴・Index invalidation意図を同一transactionで確定 | 07 |
| MEM-DATA-014 | MUST | 書込みは停止・キャンセル・Budget超過時の安全な境界で遮断、復帰で重複しない | 07,09 |
| MEM-DATA-015 | MUST | 未確認の外部引用/ページ指示を直接user profileやpolicyに格上げしない | 03,08 |
| MEM-DATA-016 | MUST | Read/Write/Eraseに上限、入力サイズ制限、レート制限、可視の容量超過を持つ | 09 |
| MEM-DATA-017 | MUST | 監査ログは本文/Secretを出さずrequest ID・対象・分類・判定のみ記録 | 08 |
| MEM-DATA-018 | SHOULD | dedupe・source provenance統合・候補のreview UIを用意する | 05,09 |
| MEM-DATA-019 | SHOULD | keyword/vector indexを置き換え可能にし、Index欠損で誤確信せずdegradedを返す | 05,09 |
| MEM-DATA-020 | LATER | End-user export（限定scope）とMemory quality feedbackを追加する | 06 |
| MEM-DATA-021 | EXPERIMENT | 要約・競合解消支援をoffline modelで試すが自動採用しない | 09 |

REQの例示されたID体系はCHAT_06ローカル。共有Error CodeやEvent名はCHAT_01承認前の正本を僭称しない。

## 5. 論理契約・型・Validation（内部提案）

### 5.1 意味分類

| `kind` | 例 | 生存条件・注意 |
|---|---|---|
| `profile` | 「比較表は税込総額を優先」「応答は日本語を希望」 | 通常user scope。推測を恒久属性にしない。本人訂正で即置換 |
| `episodic` | 「2026-10-29に候補ホテルAを比較した」 | 過去イベントとして日付固定。経過した事実が現在も成立するとは言わない |
| `procedural_hint` | 「宿泊比較では人数と駐車場条件を先に検証すると有用だった」 | 非実行の助言。自動Tool権限、コード、hookではない。Skillへの昇格は別審査 |
| `task_note` | 「Task Xでは11/20・3人・総額15,000円」 | Task ID必須、task scope限定。Task/Run正本の制約を上書きせずTask完了後に期限処理 |

`evidence` はこのunionの `kind` に **存在しない**。Evidence/Observationへのread-only pointerは `sourceRefs` で保持する。

### 5.2 TypeScript提案（**module-private**。共有IDは `[PROVISIONAL: CHAT_01/09]`）

```ts
type MemoryKind = "profile" | "episodic" | "procedural_hint" | "task_note";
type MemoryStatus =
  | "candidate" | "pending_review" | "active"
  | "contested" | "superseded" | "rejected" | "tombstoned";
type MemoryOrigin = "user_explicit" | "user_correction" |
  "agent_inference" | "tool_observation" | "import";
type MemorySourceKind = "message" | "task" | "run" | "evidence" |
  "observation" | "user_form" | "import";

type MemoryScope = {
  // AuthContextからのみ設定。入力JSONにtenantId/userIdを受けない。
  tenantId: string;          // [PROVISIONAL: CHAT_09]
  subjectUserId: string;     // [PROVISIONAL: CHAT_09]
  level: "user" | "project" | "task";
  projectId?: string;
  taskId?: string;
};

type MemorySourceRef = {
  kind: MemorySourceKind;
  opaqueId: string;         // shared canonical ID proposal: CHAT_01/07
  observedAt?: string;      // external observation timestamp (RFC3339 UTC)
  capturedAt: string;       // Harness取得時刻 (RFC3339 UTC)
  uri?: string;             // HTTPS only; sanitized metadata, not credential URL
  contentDigest?: string;   // evidence integrity hint, not authority
};

type MemoryClaim = {
  subject: string;          // canonical entity/user-relative label
  predicate: string;        // controlled vocabulary / app-specific namespaced key
  objectText: string;       // plain data, max length validated
  qualifiers: Record<string,string>; // date/locale/project/model/conditions
  language: string;         // valid BCP-47
};
type MemoryRecord = {
  id: string;               // server-generated opaque ID
  scope: MemoryScope;       // immutable; cross-scope = new record
  kind: MemoryKind;
  claim: MemoryClaim;
  conflictKey: string;      // service-generated, normalized & scoped
  status: MemoryStatus;
  origin: MemoryOrigin;
  sourceRefs: MemorySourceRef[]; // >=1 for active; raw material is NOT copied
  confidence: "user_stated" | "verified_source" | "inferred" | "uncertain";
  sensitivity: "normal" | "personal" | "restricted"; // CHAT_09 owns policy
  consentBasis: "user_explicit" | "approved_setting" | "reviewed_import";
  createdAt: string;
  updatedAt: string;
  assertedAt: string;       // 時点: user/fact originally asserted
  validFrom?: string;       // 事実が成立する期間、検索期限とは別
  validUntil?: string;      // fact-time validity
  expiresAt?: string;       // selection eligibility deadline
  retentionUntil?: string;  // physical deletion requested deadline (policy-owned)
  supersedesId?: string;
  revision: number;         // >=1, monotonically increasing per record
  policyVersion: string;    // read-time revalidationにも使用
};

type MemoryMutation = {
  operationId: string;      // client-generated idempotency key, scoped to actor
  targetId?: string;        // correction/erase
  expectedRevision?: number;// required for update, status change
  intent: "propose" | "activate" | "correct" | "revoke";
  kind?: MemoryKind;
  claim?: MemoryClaim;
  sourceRefs?: MemorySourceRef[];
  taskId?: string;
};

type MemoryWriteResult =
  | { status: "applied"; recordId: string; revision: number }
  | { status: "pending_review"; proposalId: string }
  | { status: "conflict"; currentRevision: number; recordId: string }
  | { status: "rejected"; reason: MemoryRejection };
type MemoryRejection =
  | "NOT_AUTHORIZED" | "INVALID_SOURCE" | "POLICY_BLOCKED" | "INVALID_SCOPE"
  | "SIZE_LIMIT" | "BUDGET_EXCEEDED" | "STOPPED" | "STORE_UNAVAILABLE"
  | "TOMBSTONED" | "UNKNOWN_REVISION" | "REVIEW_REQUIRED";
```

**注意**：これらはMemoryService内の説明用型。CHAT_01が正式なID/Event/Error envelopeを確定したら整合させる。権限Principalの正本とtenant discriminatorはCHAT_09。timestampsはUTC保存、UIだけ利用者TZで表示する。

### 5.3 バリデーションと不変条件

1. `scope.tenantId/subjectUserId` はサーバーでAuthContextより強制。LLMやMCPが任意のprincipal/scopeを与えても拒否。異なるscope間の推測による自動共有を禁止。
2. `task_note` は `scope.level="task"` と `taskId` 必須。`project` は許可対象のprojectId必須。`profile`をtask scopeに留めることは可能だが、user scopeへの自動コピーは禁止。
3. `active` は非空の`claim`、少なくとも1個の検証可能なsourceRef、同意根拠、取得時刻を要求する。sourceは存在確認とprincipal認可を通す。取得できないsourceは `candidate`/ `uncertain` に保留。
4. `sourceRefs.uri` は読取り表示用。認可済Evidenceから派生、資格情報/URL userinfo/追跡token削除。URIをそのままfetchしたりツール操作を誘導しない。
5. `conflictKey` は `(tenant,user,level,project/task,subject,predicate,stable qualifiers)` のcanonicalized hash。大小文字・Unicode normalizeと日付/通貨/型番の取り扱いを別の決定的normalizerで統一。JAN/ASIN/容量などidentifierを勝手に単純化しない。
6. `validUntil` は主張の真偽の時間、`expiresAt` はretrieval eligibility、`retentionUntil` はデータの保持期限。無期限保存をデフォルトにしない。期限のない長期好みもプライバシー設定に従い再確認対象とする。
7. 修正は既存revisionと新revisionを履歴として保存。破壊的なUPDATEで旧claimを消さない。ただし削除要求ではポリシーに従い履歴本文も消去/暗号鍵破棄し、最低限の非本文tombstoneだけ保持する。
8. `confidence` は権限レベルでない。userが言ったこととexternal evidenceで独立検証されたことを区別。LLMのconfidence数値を真実性の保証にしない。
9. `procedural_hint` にJSON Tool schema、shell、network side effects、policy overrideを書いても **単なる未信頼テキスト**。executionは常に独立のSkill/Tool承認に従う。
10. `revision` と `scope` が異なるときの `correct` は拒否。移動は元記憶のrevokeと新scopeでの明示承認付きcreateに分割。

### 5.4 契約例（架空、価格の実測値は記録しない）

```json
{
  "id": "mem_01EXAMPLE",
  "scope": { "tenantId": "t_example", "subjectUserId": "u_example", "level": "task", "taskId": "task_example" },
  "kind": "task_note",
  "claim": {
    "subject": "hotel-search:task_example",
    "predicate": "trip.constraints",
    "objectText": "3人1泊、税込総額15000円以下、駐車場あり",
    "qualifiers": { "checkin": "2026-11-20", "timezone": "Asia/Tokyo" },
    "language": "ja-JP"
  },
  "status": "active",
  "origin": "user_explicit",
  "sourceRefs": [{ "kind": "message", "opaqueId": "msg_example", "capturedAt": "2026-10-30T00:00:00Z" }],
  "confidence": "user_stated",
  "sensitivity": "personal",
  "consentBasis": "user_explicit",
  "createdAt": "2026-10-30T00:00:00Z",
  "updatedAt": "2026-10-30T00:00:00Z",
  "assertedAt": "2026-10-30T00:00:00Z",
  "expiresAt": "2026-11-21T15:00:00Z",
  "revision": 1,
  "policyVersion": "policy-provisional-v1",
  "conflictKey": "opaque-server-derived"
}
```

実際の旅行Taskの条件正本はCHAT_01/02。これは説明用補助Projection候補であり、後続のSteerによる変更があればTask正本を優先する。

## 6. 状態遷移・シーケンス・ライフサイクル

```text
external text / user message / prior task
   |
   v
[Candidate] --unsafe/no consent--> [Rejected] (not prompt-injectable)
   |  \
   |   +--requires approval--> [PendingReview] --approve--> [Active]
   |                              |--deny/timeout--> [Rejected]
   +--authorized safe write-----------------------> [Active]
                                                    |  \
                         higher-authority correction|   +--> [Contested]
                                                    |           |--verified resolution--> [Active or Superseded]
                                                    +--> [Superseded] (new active revision/reference)
                                                    +--> [Tombstoned] (explicit revoke/expiry policy)
```

`candidate` はLLMが提示した未承認仮説、`pending_review` はUI承認待ち。`active` のみが通常のretrieval対象。`contested` は背景の明示警告には使えても、意思決定の確定条件としては使わない。`superseded` と `tombstoned` は絶対にプロンプト選択しない。`rejected` は反復提案防止の短期メタデータだけ残し、本文保持は同意方針に従う。

**S1: 手動remember**: Web UIのユーザー操作 → Auth/Scope評価 → validation → policy/consent → DB transaction(CAS insert + history + invalidation intent) → commit → Memory ID/revision応答 → 次のLLMステップ以降のprojection更新。

**S2: 自動抽出**: Task/Conversationから候補抽出 → 実ユーザーの発言と外部文章を分離 → sensitivity/consent gate → `candidate` / `pending_review` または明示許可範囲内の`active` → indexed projection。SNS/HTMLをuser statementに偽装してはいけない。

**S3: user correction**: 正本のMemory IDと`expectedRevision` → 旧statementの競合確認 → 新statementを版履歴に記録・旧版を不可視化 → 同一transactionでindex revocation message → 新projectionへ。別Taskで古い記憶を参照していた場合は次の安全な境界でrevalidate。

**S4: user erase**: ACL → 対象および派生記憶のscope graph識別 → transactionで選択不可状態とrevocation epochを増加 → commit後に成功と「論理削除済」表示 → index/caches/artifacts/backupsの保持/消去は所有者に要求・完了を別管理。**全バックアップまで即消滅したと偽らない**。

**S5: restart**: durable event/historyを読み、未完了index invalidationを再処理。Readは常にDBのstatus/revision/expiry/scopeを再確認し、遅延した旧vector index結果を採用しない。

## 7. 内部アルゴリズム・疑似コード

### 7.1 Write path

```ts
async function mutateMemory(auth: AuthContext, cmd: MemoryMutation): Promise<MemoryWriteResult> {
  const principal = await authorizeMemoryActor(auth);   // never from LLM fields
  enforceLimits(cmd, principal.policy);                 // finite item/byte/ops budget
  assertNoHostCommandsSecretsOrPolicyEscalation(cmd);
  const scope = deriveScope(principal, cmd.taskId);     // server-side only
  await validateSourceRefs(principal, scope, cmd.sourceRefs ?? []);
  const decision = await memoryPolicy.evaluate(principal, cmd, scope);
  if (decision === "deny") return rejected("POLICY_BLOCKED");
  if (decision === "review") return stageBoundedProposal(principal, scope, cmd);
  if (isRunCancelledOrOverBudget(auth)) return rejected("STOPPED");

  return memoryRepo.transaction(async tx => {
    const prior = await tx.getIdempotencyResult(principal.id, cmd.operationId);
    if (prior) return prior;                            // durable replay
    const current = cmd.targetId ? await tx.lockRecord(scope, cmd.targetId) : null;
    if (cmd.targetId && (!current || current.revision !== cmd.expectedRevision))
      return tx.persistResult(cmd, conflict(current)); // prevent lost update
    const normalized = normalizeMemoryClaim(cmd.claim);
    const related = await tx.findAndLockConflictKey(scope, normalized.conflictKey);
    const resolved = resolveDeterministically(related, cmd, principal);
    // Correction by user wins over inferred memory in same scope.
    // Otherwise ambiguous claims => contested, never blind overwrite.
    const saved = await tx.appendVersionAndStatus(resolved);
    await tx.appendAuditNoBody(cmd.operationId, saved.id, saved.revision);
    await tx.enqueueProjectionInvalidation(scope, saved.id, saved.revision);
    return tx.persistResult(cmd, applied(saved));
  });
}
```

**補足**: Postgresで`UNIQUE(tenant,actor,operationId)`と`UPDATE ... WHERE revision = expectedRevision`（またはrow lock+revision check）を使う論理要件。[PROVISIONAL: CHAT_10] 。Idempotency記録のうちトランザクション中のvalidation失敗を永続化する範囲・TTLはCHAT_10/09と合意。衝突検出はkeyを同一TXでロック/ユニーク制約し、2 writerによる新規同一key作成も保護する。DB deadlock/serialization retryは有限回、元のoperationIdを保持し、LLMに解決を委ねない。

### 7.2 Deterministic conflict policy

1. explicit erase / denial は同一scopeの自動推論より優先。tombstoneを同じIDで自動復活させない（再保存には新規明示操作）。
2. user-explicit correctionは同一conflictKeyのagent-inferredを`superseded`化。情報出典と古いversionはPolicyで許される範囲で追跡。
3. user対userの明示発言が食い違い、対象/時点が異なるならqualifierを区別し両立可能。時点も条件も一致して意味が矛盾すれば自動決着しない。`contested` として本人確認/再質問へ。
4. 外部WebやSNSの主張はuser profileを更新不可。観測値はEvidenceに残し、memoryとして必要なら「その日に観測した過去の出来事」に限定。
5. task scopeはuser/projectへ暗黙に昇格しない。scopeの特異性は「許可の強さ」ではない。

### 7.3 Read / Projection guard（rankingは次MD）

```ts
async function getEligibleMemories(auth, request) {
  const scopeSet = await authorizedScopes(auth, request); // policy-owned
  const candidates = await memoryRepo.query(scopeSet, boundedFilter(request));
  return candidates.filter(m =>
    m.status === "active" &&
    isAllowedByCurrentPolicy(auth, m) &&
    !isExpired(m.expiresAt) &&
    !isPastFactValidityForCurrentClaim(m.validUntil, request) &&
    !isRevokedAtLatestEpoch(m) &&
    !isContested(m)
  ).map(toDataOnlyProjection);  // escape/label as untrusted facts, no role elevation
}
```

Readへの`validUntil`適用は「現在の事実として使う」時。過去の履歴検索では期限を経たepisodicを過去の事実として返せる（ただし `expiresAt` とプライバシーは常に適用）。`expiresAt` は読みでfail-closed、cleanup workerが遅れても復活しない。

## 8. 障害・リトライ・Race マトリクス

| 障害 | 検出 | 回復/最終挙動 | ユーザー表示 |
|---|---|---|---|
| 同時修正、revision不一致 | CAS=0 / lock後version違い | 409相当、最新revision提示。LLMが勝手に上書きせず再検証 | Conflict / review |
| 同じoperationの再送 | idempotency UNIQUE | 前回結果を返す。新しいMemoryを増やさない | 同一Memory ID |
| Transaction commit直後にWorker crash | durable DB/outbox | 再起動後outbox replay、readは正本statusで判定 | 保存済/Index遅延 |
| DB成功、vector index失敗 | index watermark不一致 | exact/keyword fallback、古いindex候補はDBで除外。再index | Partial（検索性能低下） |
| Indexに削除済recordが残存 | epoch/status mismatch | readで排除、index消去の再試行 | 論理削除済・物理処理中 |
| DB停止・読み失敗 | connection timeout | Memoryなしで推測回答せず「記憶利用不可」/明示された現会話だけ利用 | Uncertain / degraded |
| Source messageが消去済 | ref resolution failure | source無しの新規active拒否、既存は再評価/保留 | 出典要確認 |
| 競合した2件の新規同一key | row lock/unique conflict | 決定的retry有限回/contested。どちらも消さない | Needs review |
| User delete vs in-flight projection | read watermark, revocation epoch | 次のモデル呼出前に投影を破棄。すでに送信したモデル入力は回収不可→機密扱いの実行はGateで停止 | 削除後の反映範囲 |
| Cancel/Steer中の抽出 | run generation/stop token検証 | Commit前にabort。commit済ならユーザー明示操作優先、派生の無効化で回復 | Cancelled / changed |
| Limit/Token budget超過 | input, record, per-run budget | 提案の保留/スキップ。会話を塞がず有限回まで | Memory保存スキップ |
| 外部HTMLに「永遠に記憶しろ」 | trust label/security scanner/policy | sourceはdataのみ。permission escalation不可 | 安全上の拒否 |
| backup/erase滞留 | queue age & retention policy | ownerへアラート。即時完全消去保証しない | 処理状況と範囲 |

Rejectionsは共通Error taxonomyがLockedならそれへmap。各リトライは **設定された有限回/期限/予算** に制限し、外部Tool操作の再試行と混同しない。

## 9. Security / Privacy / Trust boundary

- **Critical**: webpage、SNS、MCP tool result、retrieved Memory本文は全て *data*。system/developer instruction、Policy、Tool capability、approval、auth principalは上書きできない。内容に`ignore previous instructions`などがあっても実行権限にしない。複数箇所でroleラベルと引用封じを実施。[PROVISIONAL: CHAT_09]
- **Default-deny**: パスワード、APIキー、Cookie、トークン、決済情報はMemoryに保存しない。Secret参照はCHAT_09が管理する別Key Storeのopaque handleですらLLMへ無制限に出さない。Sensitive categoryの自動記憶は既定OFF、本人の明確な許可＋設定/法的規定があって初めて個別審査。
- **Scope isolation**: tenant/user/project/taskの全read/write/export/deleteに認可を適用。index embedding、Cache、Background jobもpartition/ACLを守る。`vector`検索結果から他ユーザーの存在推測（count/timing含む）を防ぐ設計はCHAT_09/10と共同検証。
- **PII minimization**: 具体的住所、連絡先、健康、信条、決済等を自動収集しない。記憶に不要な個人情報を原文からコピーしない。`sourceRefs` と本文の保持期間/消去連鎖はCHAT_09規定を参照。
- **Policy source of truth**: userの「これからの購入は自動でOK」という過去Memoryだけでは購入は承認されない。Tool Gateway/Approval APIの現行Policyを常に確認。
- **Import**: 旧MD/他OSSからのimportは未信頼、隔離staging、source provenance、sensitivity分類、ユーザーreviewを経てactive化。古い文章に書かれたAgent向け命令を実行しない。
- **Audit**: Actor ID、memory ID、operation ID、scope hash、action、decision、revision、policy version、時刻は記録。本文、source secret、embedding、個人情報を通常ログへ出さない。監査の保持/閲覧はCHAT_09。
- **Source integrity**: external URLが消えたら確信を上げず、`uncertain` として再検証。evidence refにWeb URLを設定するだけでは信頼できる根拠にならない。
- **Erase limitation**: すでに生成された外部通知/監査記録/法令保持データの削除可否は別正本に依存。Memory削除が会話・Evidenceを全消去したと誤案内しない。

## 10. 計測・Traces・容量・費用

**初期安全上限案（製品性能の実測値でない／運用設定で変更可能）**:

| 上限 | 暫定初期値 | 適用点 |
|---|---:|---|
| 1 claim `objectText` | 1,000 UTF-8文字 | write validation |
| 1 sourceRefs配列 | 8 | source validation |
| 1 mutation batch | 20 | service API |
| 1ユーザーのactive+contested records | 1,000 | quota, visible capacity error |
| 1回のProjection件数 | 8 | next MDのランキングと共通調整 |
| 1回のProjection本文 | 1,500 tokens（モデル別tokenizerで計数） | CHAT_02 context budgetの**内側** |
| 1 runのMemory候補抽出 | 1回の最終抽出＋明示指示（回数上限はBudgetに従う） | cost/safety |
| 更新時DB serialization retry | 最大3回、その後Conflict/Unavailable | not infinite loop |

**計測メトリクス案**: `memory.write.applied/rejected/conflict`、`memory.staged`、`memory.revision_mismatch`、`memory.read.filtered_expired`、`memory.read.filtered_acl`（tenant別で公開しない）、`memory.index.lag_seconds`、`memory.erase.pending`、`memory.projection.tokens`、`memory.cost.usd`。本文やURL queryをtagにしない。性能評価は匿名fixtureでp50/p95 read/write、DB障害復帰、100並列CAS、erase→read遮断、token/推論費用を測る。数値SLOはベンチ後CHAT_11/10と設定する。

## 11. Persistence / Transaction / Migration（CHAT_10へ提案）

**論理集合（物理テーブル名・DDLはCHAT_10正本）**

- `memory_records`: 最新record status、owner/scope、claim、source refs、consent、`revision`、validity/retention、policy version、作成日時。
- `memory_versions`: append-only旧versionと変更理由。論理消去対象の本文も実際のerase policyで削除/暗号鍵破棄。
- `memory_write_operations`: `(actor, operationId)` UNIQUE、結果/処理時刻、finite retention。長期本文を持たない。
- `memory_revocation_epoch`: scope/memory単位のread watermark、cache invalidation。
- `memory_projection_outbox`: transactionally enqueueしたcreate/update/revokeをat-least-once配送、idempotent consumer。
- `memory_search_index`: あくまで再構築可能なderived index。record status/schema changesでrebuild。

**Migration手順**: v0は空DBから起動→logical schema v1→一部ユーザーに機能旗→read-only browsing→manual writes→opt-in inference。今後のschema changeは旧clientからの混在書込みにCAS/compatibility validator、backfill jobのcheckpoint、rollbackで既存source refsを失わない形式を要求。旧 `MEMORY.md` Importを実装する場合はread-only parser→quarantine→source=import→本人review→データ移行検証→旧原本の保持/削除選択。自動importで原文をsystem instructionにしない。

**トランザクション条件**: status更新、history、idempotency receipt、revocation epoch、outbox insertを同時commit。Outbox配送は遅れてよいが **read-time denyは遅れてはいけない**。ClockはDB側UTCを採用。backups/restore時はtombstone watermarkより古い記憶を復活させない（CHAT_10のrestore runbookへの必須提案）。

## 12. UI / HTTP / 運用面の影響（CHAT_08へ提案）

- Memory一覧: profile/episodic/procedural/task、scope、出典・確認日時、confidence、期限、保留/対立、最終更新、削除処理状態を可視化。未検証・失効値を「現在の事実」と表示しない。
- 操作: 明示保存、編集（expectedRevision付き）、否認、保留候補の承認、削除、長期記憶の停止。Memory無効化時の検索/バックグラウンド更新挙動も明示。
- API例 **[PROVISIONAL: CHAT_08]**: `GET /memories?kind=...`、`POST /memories/proposals`、`PATCH /memories/{id}`（If-Match/revision）、`DELETE /memories/{id}`（idempotency-key）、`GET /memories/erase-requests/{id}`。実際のendpoint・HTTP Error envelopeはownerが確定。
- UI feedback: `Applied`/`Pending review`/`Conflict`/`Partial (index delayed)`/`Erasure processing` を区別。Tool経由保存のユーザー通知はMemory正本で成功した時だけ。
- Logs/ops: invalid source、stale index、erase queue滞留、policy denial spikeに可観測性を付ける。user-specific本文はtrace収集しない。

## 13. 実装PRの分割順（このrepoにコード追加しない）

1. **M06-01 Core logical model + validator**: kind/scope/status/sourceRef/CAS契約、epoch、unit tests。CHAT_01/09と整合するまではprovisional。
2. **M06-02 PG MemoryRepository**: transactional upsert/history/outbox/idempotency、並行性/DB回復テスト。CHAT_10 DDL合意が必要。
3. **M06-03 Policy/consent/erase**: policy adapter、pending review、sensitivity、delete index revocation、audit。CHAT_09/03連携。
4. **M06-04 Projection port**: Task/Runtime文脈へのread-only projection、bounded context、expired/contested filter。CHAT_02のContext Managerと接合。
5. **M06-05 UI/Import/E2E**: browse/correct/revoke、optional import、SC横断fault injection。CHAT_08/11と合意。

依存が未Lockedの段階でruntimeを書き始めるなら関数境界のみstub化し、共通Event/DB/APIの正本を独自実装で固定しない。

## 14. テスト計画（全件**設計のみ**、本Harnessは未実装・未実行）

| TEST-ID | 層 | Setup → Action → Expected（全てMock/fixture） |
|---|---|---|
| MEM-TEST-001 | Unit | 4種のvalid record、`evidence`をkindとして入力→4種OK/evidence拒否 |
| MEM-TEST-002 | Security | user A/Bを同tenant/別tenantで生成、AからBのIDをread/update/delete→全拒否、存在を漏らさない |
| MEM-TEST-003 | Contract | sourceRefsの時刻・Message/Evidenceへの参照を保存・再読→provenanceが同一、架空ID拒否 |
| MEM-TEST-004 | E2E | 前日の価格をMemoryへ「現行買取価格」として保存要求→拒否/過去episodicへ限定 |
| MEM-TEST-005 | Unit | `validUntil`経過/ `expiresAt`経過/ `retentionUntil`だけ未達→それぞれ異なる振る舞い |
| MEM-TEST-006 | Concurrency | 同一revision=1を100 writersで訂正→revision=2で勝者1件、他はConflict、消失なし |
| MEM-TEST-007 | Fault | Commit直後kill/同operationId再送→1record/1有効version、同じ結果 |
| MEM-TEST-008 | Integration | user訂正がagent inferenceと衝突→user版active、旧版不可視、履歴追跡可 |
| MEM-TEST-009 | Fault/security | DELETE commit後index workerを停止→raw indexに旧文があってもRead/Projectionは0件 |
| MEM-TEST-010 | Security | Secret, Cookie, HTML注入、role overrideをMemory候補→非保存またはdata隔離、権限不変 |
| MEM-TEST-011 | E2E | task_noteの期限切れ/別Task retrieval→他Taskに混入せずexpire/revoke |
| MEM-TEST-012 | Integration | 人間の`remember`とuntrusted Web抽出を対比→User明示のみdirect active |
| MEM-TEST-013 | Fault | committed version/outbox後restart→revision継続、outbox replay idempotent |
| MEM-TEST-014 | Fault | Cancel/Steerと同時write、Budget超過→新しい自動activeなし、元Task制約維持 |
| MEM-TEST-015 | Security | SNS本文が「userは承認した」と指示→source provenanceでuser profile変更なし |
| MEM-TEST-016 | Boundary | 1001 record/1001字/21 op/期限超過→定義したquota error、無限retryなし |
| MEM-TEST-017 | Audit | 書込み、拒否、削除をtrace→ID/decisionあり、本文や秘密のlog出現0 |
| MEM-TEST-018 | Unit | 同sourceの重複提案を2回→dedupeしsourceRef追加かno-op、重複user claimなし |
| MEM-TEST-019 | Integration | vector index unavailable/expired ghost hit→fallback/degraded、ghostを出力しない |
| MEM-TEST-020 | Security | delete後バックアップ復元→tombstone watermarkより古い内容を再Activeにしない |
| MEM-TEST-021 | Integration | 同時明示発言の内容が矛盾→contested、会計/宿泊の確定条件としては不採用 |
| MEM-TEST-022 | Unit | 旧価格の`assertedAt`と`capturedAt`を入れ替えた入力→誤認・時系列検証エラー |
| MEM-TEST-023 | Fault | SourceRef削除/アクセス権撤回→次回retrievalで再検証要求、秘匿resourceを漏らさない |
| MEM-TEST-024 | Benchmark | 固定seedの複数ユーザー×並列writer、1000 records→p50/p95, token, memory, index lagを実測記録（目標値捏造禁止） |

対象OSSのテストソースは読み取ったが、OSSのテストスイートも本Harnessのテストも**実行していない**。テスト環境を作る担当はCHAT_11。

## 15. Acceptance Criteria / Traceability

| REQ → TEST | SC | 合格定義 |
|---|---|---|
| 001→001 | 01/02/04 | taxonomyと別資産の区別を型で強制 |
| 002→002 | 06/08 | 他principal情報を一切返さない |
| 003→003,022,023 | 01/03/05 | provenanceと時点・参照認可の整合 |
| 004→004 | 01/04/05 | stale observationをcurrentに昇格させない |
| 005→005 | 02/04/05 | 3種類の期限を取り違えない |
| 006→006,021 | 07 | lost updateなし、競合の明示 |
| 007→007 | 07 | 再送しても二重論理書込みなし |
| 008→008 | 04/07 | 訂正即反映、旧版は非選択 |
| 009→009,020 | 06/08 | delete commit後にreadで復活なし |
| 010→010 | 03/08 | Memoryから秘密・権限が漏れない |
| 011→011,019 | 04/08 | scope/expiry/index ghostの安全な除外 |
| 012→012 | 04/08 | explicitとinferredのaudit差分 |
| 013→013 | 07 | TX/Outboxの耐障害 |
| 014→014 | 07/09 | Cancel/Steer/Budgetを尊重 |
| 015→015 | 03/08 | 未信頼Web文章はuser命令にならない |
| 016→016 | 09 | finite上限と観測可能な拒否 |
| 017→017 | 08 | PII漏れのない監査 |
| SHOULD 018→018, 019→019 | 05/09 | dedupe/Index劣化の明示 |
| LATER 020 / EXPERIMENT 021 | 06/09 | 別PRで定義し本Draftの完了条件には含めない |

**Review gate**: 上記MUSTテストのコード・Fixtureが実装され、CHAT_01/09/10のcontract ownerが合意し、独立レビューで根拠と安全性が検証されるまで **Draft**。Locked/Implemented/Verifiedを称しない。

## 16. 未決事項・Cross-team proposals

| ID / Owner | Severity | Proposal / 理由 | 完了条件 |
|---|---|---|---|
| **M06-P01 / CHAT_01** | **P0** | `[PROVISIONAL: CHAT_01]` message/evidence/task/run IDs、source引用の削除イベントとcanonical envelope。Memoryはこれらを重複定義しない。契約変更でretrieval/source invalidationの互換試験が必要 | typed ref/versionと消去通知合意 |
| **M06-P02 / CHAT_09** | **P0** | `[PROVISIONAL: CHAT_09]` tenant/user/project/taskのPrincipalとsensitivity・consent・erase/backup/監査保持規則。user requestと自動推論の境界も明記 | ACL/Privacy matrixがLocked |
| **M06-P03 / CHAT_10** | **P0** | `[PROVISIONAL: CHAT_10]` transactional revisions、uniqueness、outbox、revocation epoch、RLS/物理隔離、restore resurrection防止、retention job。既存DDLへ独断で追加しない | migration plan・100 writer fault test合意 |
| **M06-P04 / CHAT_02** | P1 | Context boundaryと`memoryRevision` watermark、削除/訂正時の次回LLM呼出投影再構築、run abort policy | stale prompt testとrebuild条件 |
| **M06-P05 / CHAT_03** | P1 | Memory writeとTool approvalは別。ユーザー承認済external effectsの代用禁止、取消/外部リトライ制御 | gateway capability map合意 |
| **M06-P06 / CHAT_07** | P1 | Watch/ObservationのIDと正本、MemoryのsourceRefの失効時の動作 | stale price/Watch test合意 |
| **M06-P07 / CHAT_08** | P1 | Memoryの一覧・編集・取り消し・保留・Erase progressとHTTP条件付き更新 | consent UIとConflict feedback設計 |
| **M06-P08 / CHAT_11** | P1 | 全MUSTのintegration/kill/retry/concurrent/privacy test fixture | traceabilityとE2E runner登録 |
| **M06-P09 / CHAT_06（次MD）** | P1 | retrievalランキング、context混入防止、knowledge drift、re-extraction、訂正/削除の確認手順 | `memory-retrieval-and-update.md`で確定 |
| **M06-P10 / CHAT_00** | P2 | 自動保存の初期default（explicit onlyかopt-in verified）、一般ユーザーの可視性とコスト | ADRでユーザー体験判断 |

### 実装前に追加で確認すること

- `sensitivity`の分類基準、ユーザー削除要求から保持監査ログ/バックアップまでの法的ポリシーは **未確定**。保存期間の具体値をハードコードしない。
- LangGraph Store/Letta Codeを採用する場合の導入バージョンと公開APIは **未固定**。本調査のcommit pinは依存バージョン選択ではない。
- LangGraphのTTLはBackendにより差があり、BaseStoreのdefaultでは無効。Storeを使えば期限が必ず消えると想定しない。[S5]
- Embedding provider、暗号化アプローチ、Postgres RLS、Index engine、locale-specific conflict normalizerは **未実証**。CHAT_09/10/11で追加検証が必要。
- User explicitという事実だけで機微情報を自動保存できる訳ではない。Consent/Privacy policy決定が必要。

## 17. Sources（全て2026-10-30確認）

URLはソースcommitへpin。行番号は調査時点の参考、関数/テスト名が正確な錨。Evidence grade: **Code verified / Docs verified / Issue report / Hypothesis**。

- **[S1] Code verified** — Hermes Agent `fcc26fbd9f1641755e49793d7ed608f3b7214c3d`, `tools/memory_tool_store.py`, `MemoryStore.load_from_disk/_mutate/apply_batch/format_for_system_prompt`: https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool_store.py
- **[S2] Code verified** — 同commit `tools/memory_tool.py:_gate_or_stage/_background_delete_gate` and `tests/tools/test_memory_tool.py:TestMemoryBatch/TestExternalDriftGuard/TestMemoryStoreSnapshot/TestBackgroundReviewDeleteGate`: https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/memory_tool.py , https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/tools/test_memory_tool.py
- **[S3] Code verified** — Deep Agents `3efa098130d6670fe713f841072bc11ff40545da`, `MemoryMiddleware.before_agent/abefore_agent/modify_request` and `StoreBackend._get_namespace`: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/memory.py , https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/backends/store.py
- **[S4] Code verified** — 同commit `test_load_memory_from_backend_single_source_async`, `test_load_memory_from_backend_multiple_sources_async`, `test_load_memory_handles_missing_file_async`, `test_before_agent_skips_if_already_loaded_async`: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_memory_middleware_async.py
- **[S5] Code verified** — LangGraph `ffc3336367e8139668d11e5f0b0e6def57e7c762`, `BaseStore` and `put/get/search/delete` / `supports_ttl`: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/langgraph/store/base/__init__.py
- **[S6] Code verified** — 同commit `test_store_ttl`, `test_omit_expired_filters_read_paths`, `test_omit_expired_refresh_ttl_only_refreshes_live_rows`: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint-postgres/tests/test_store.py
- **[S7] Code verified** — Letta Code `981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b`, `src/agent/memory.ts:getDefaultMemoryBlocks` and `src/tools/impl/memory.ts:memory`: https://github.com/letta-ai/letta-code/blob/981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b/src/agent/memory.ts , https://github.com/letta-ai/letta-code/blob/981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b/src/tools/impl/memory.ts
- **[S8] Docs verified (repo prompt)** — Letta Code pinned prompt, memory/skills/secrets guidelines: https://github.com/letta-ai/letta-code/blob/981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b/src/agent/prompts/letta_root_memfs.md
- **[S9] Code verified (license text)** — Hermes `LICENSE` MIT: https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/LICENSE
- **[S10] Code verified (license text)** — Deep Agents `LICENSE` MIT: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/LICENSE
- **[S11] Code verified (license text)** — LangGraph `LICENSE` MIT: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/LICENSE
- **[S12] Code verified (license text)** — Letta Code `LICENSE` Apache-2.0: https://github.com/letta-ai/letta-code/blob/981f9fbbd2bdbdb9c584fdac6e3288b8e600e38b/LICENSE
- **[S13] Issue report (not current bug verification)** — Hermes #85858, 2026-10-13 closed, concurrency loss narrative: https://github.com/NousResearch/hermes-agent/issues/85858

**Readiness**: 本文のOSS部分はソース/テスト閲覧に基づく。選定したHarnessの論理契約、上限、transaction/index/outbox構造は **proposed design / Hypothesis**。動作試験、負荷試験、統合試験は未実施。Revision/Schema/Privacyのオーナーが承認するまで **Draftのまま**。
