# Core Domain Models — Detailed Implementation Specification

> Owner: CHAT_01 | Canonical path: docs/01-contracts/core-domain-models.md | Status: **Draft / NOT LOCKED** | Revision: 0.1 | Research date: 2026-10-30 (JST) | Main baseline: 92b5827ed16a7d4ae61e8146ce382795398293cb  
> Dependencies: project-brief / architecture-rules / acceptance-scenarios; 01-02 event-envelope-and-stream, 01-03 task-run-state-machines, 01-05 evidence-artifact-contracts; CHAT_02, 04, 07, 08, 09, 10.  
> This is a proposed implementation contract, **not existing runtime code, not a reviewed ADR, and not an assertion of executed tests**. Pending team approvals are marked [PROVISIONAL: CHAT_XX].

## 0. 結論と採用案

Universal Personal Agent Harnessは、セルフホストUbuntu/Docker上で、自然言語による任意のWeb調査、根拠付き比較、ログイン付きブラウザの人間への引継ぎ、継続監視とTelegram通知を扱う。Amazon・ホテル・Xは受入例であり、固定ワークフロー用DBやコード実行エージェントではない。

**採用:** アプリ固有の永続ドメインIDを定義し、(1) Conversation/Thread、(2) Task、(3) Run、(4) BrowserSession、(5) Watch を区別する。さらにTenant/Userの認可境界、論理データversionと実行lease epoch、UTC instantとIANA timezone、親子Task関係を区別する。**RunはTaskの実行試行、Watchは実行を発生させる設定、BrowserSessionはブラウザ資源、Conversationはユーザーとの文脈。いずれも同義ではない。**

**原則:** App DBがアプリの意味論・所有権・最新状態の正本。LLM providerのconversation ID、LangGraph thread_id、OpenAI Agents SDK Session.session_id、ブラウザworker内のpage IDは**外部/内部アダプターID**であり、正本IDを置換できない。SDKのスナップショットは実行補助データ。LLMが提示したIDを権限根拠として扱わない。

**暫定:** Single-user運用でもtenant_idは必須で、初期セットアップ時の明示的な単一Tenantを使う。認可主体、状態enum、Watch実行規則、BrowserSession詳細、物理DDLと標準的な上限値はそれぞれのOwnerが最終承認する。本書のDomain shapeは提案正本であり、レビューを経るまでLockedではない。

## 1. 責務・非責務・受入シナリオ

**責務:** 全共有エンティティの同一性、参照関係、所有権と変更authorityの境界、version/createdAt/updatedAt、論理的な参照整合性とtenant境界、ライフサイクルを越える識別、APIへ出す最低限の公開projectionを定義する。初回実装に必要な関係図、validator、command境界、整合性不変条件もここに置く。

**非責務:** Task/Runの具体的state enum・許可遷移（01-03）、イベントenvelope/sequence/SSE（01-02）、Observation/Evidence/Artifactの形（01-05）、ツールIO（01-04）、エラー分類（01-06）、Agent loop（CHAT_02）、Browserの詳細実行・Cookie形式（CHAT_04）、Watchのtrigger評価・通知先詳細（CHAT_07）、REST/Telegram UI（CHAT_08）、認証・資格情報・ACL（CHAT_09）、CREATE TABLE/索引（CHAT_10）。

**SC対応:** SC-01/03/04/05：会話から独立した調査Taskと制約・結果の帰属。SC-02：Watchと個々の実行Taskの分離。SC-06：BrowserSession帰属とhuman handoff。SC-07：Runとlease、再起動・Steer・Cancel。SC-08：tenant境界・認可。SC-09：Run単位の費用/計測関連付け。全SCに影響するが、このMDのみでユーザーレベルの完遂を証明しない。

## 2. 既存OSS横断調査と採否

コードは下記の**指定commitで静的に閲覧**した。調査したコードの動作を当プロジェクトが実測したものではない。いずれもMITライセンスのLICENSE本文を確認した（再利用可能性は各LICENSEの表示・著作権条件に従い、今回はコード複製を行わない）。

| Source | 確認した実装・テスト | 得られる設計 / 移植できない点 | Grade |
|---|---|---|---|
| **LangGraph** SHA ffc3336367e8139668d11e5f0b0e6def57e7c762 | checkpoint/base/__init__.py の CheckpointMetadata, CheckpointTuple, BaseCheckpointSaver.get_tuple / put / put_writes; checkpoint/memory/__init__.py の InMemorySaver.put / put_writes; tests/test_memory.py の保存・祖先write試験。[S1][S2][S3] | thread_id、checkpoint_ns、checkpoint_idと親の履歴、run_idを分離。実行のcheckpoint保存へ参考採用。ただしテナント認可、Web監視、BrowserSessionの所有権を代行しない。in-memory saverは本番永続層として不採用。 | **Code verified** |
| **PydanticAI** SHA c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9 | conversation.py の Conversation（messages、usage、conversation_id、deferred_tool_requests）、tests/test_conversation.py の test_conversation_is_a_branch_point / test_a_paused_conversation_carries_what_it_is_waiting_on。[S4][S5] | 会話messageだけでは予算/承認待ち/継続データが落ちることをコードとテストで確認。会話と実行制御情報の分離に反映。SDK Conversationを永続ドメイン正本には採用しない。 | **Code verified** |
| **OpenAI Agents SDK (Python)** SHA cd26bbbc52847845fde73b9952b51e62e2bcbdc0 | memory/session.py の Session Protocol、memory/sqlite_session.py の SQLiteSessionのsession_id / messageデータ分離、run_state.py の RunStateとresume記録、tests/test_run_state.py のschemaVersion validation、tests/memory/test_session.py のconcurrent accessとpost-commit cancellation試験。[S6][S7][S8][S9] | session historyとrun snapshotを分ける。RunStateの途中writeと再開制御は良い参照。ただしSDK自身も「独立復元RunStateを同一Sessionへ同時resumeしてはいけない」と明記。**アプリ側のCAS+leaseが必要**。 | **Code verified** |

**追跡したIssue（事実の証明ではなく報告）:** LangGraph #8115 はput_writesの並行競合によるpending write喪失を報告（2026-09-24時点open）[S10]。OpenAI Agents SDK #4677 は永続化境界での「commit前に例外」「commit後ACK喪失」を試験で区別する提案（2026-08-26時点closed）[S11]。PydanticAI #6452 はUI経由の承認にserver-issued pending stateとの結び付けを追加する提案（2026-10-28時点open）[S12]。これらは採用要件の**反証・fault fixture**として扱う。Issue本文の発生頻度、修正完了、脆弱性認定を本書で推定しない。

**代替比較（最低2案）**

| 案 | 評価 | 決定 |
|---|---|---|
| A. provider/SDKのsession/threadを唯一のIDとする | 導入容易だがSDK入替・複数provider・Watch・human handoffに跨る識別不可。tenant強制や同時Runの排他もSDK任せ。 | **却下** |
| B. すべてを単一のAgentSession/Chatレコードに格納 | 最少テーブルだがWatchの周期実行と会話、Run再試行、Browser leaseと独立した寿命を表現できず、キャンセルや費用帰属が混ざる。 | **却下** |
| C. 役割別のdomain IDs + DB-managed CAS + 外部アダプタID | 保存・ACL・検索は増えるが、再開、監査、適切なライフサイクル、OSS差し替えに強い。 | **採用** |
| D. イベントソーシングのみを唯一の状態ストアにする | audit replayに強みはあるが初期段階のprojection/upgrade/restoreが複雑。event logは別途保持し、現在状態はtransactional rowに持つ。 | **初期却下/将来ADR** |

性能・処理速度の比較は未実施。MITコードの転写はせず、抽象設計のみ採り入れる。Pythonに依存した形はTypeScript中心+Python sidecar候補とは別の契約とする。

## 3. 依存関係と正本・一意な関係

~~~text
Tenant (1) ── (N) UserMembership [policy: CHAT_09]
  ├── (N) Conversation ── (N) Thread ── (N) Message
  │                        └── parentThreadId? (同一Conversation内)
  ├── (N) Task ── (N) Run
  │       │         └── checkpointRef? (runtime payload, CHAT_02)
  │       ├── parentTaskId? → Task（同一tenant、DAG）
  │       └── (N) BrowserSession [資源本体: CHAT_04]
  └── (N) Watch ── (N) Task（triggerされた各Task）
                      └── (N) Run
Task ── conversationId? / originThreadId? （ユーザー起点）
Task ── createdFromWatchId? （定期起点、Watchを参照）
Watch ── createdByTaskId? （設定した依頼Taskを参照）
~~~

**基数と独立性（MUST）**
1. Tenantは0..N Conversation/Task/Watchを所有。Userは認可主体だが、userId=tenantIdとは限らない。誰が何をできるかの正本はCHAT_09。
2. Conversationは1..N Thread（初期作成時root Threadを同一transactionで作る）、Threadは0..N Message。Threadは同一Conversation内に0..1 parentThreadを持つ。rootはparentなしが**ちょうど1つ**。Messageは1つのThreadの不変のmessageId+sequenceを持つ。
3. Taskは必ず1 Tenant、0..1 Conversationと0..1 originThreadを参照する。Conversationを持たないTask（API直接起動・Watch起動）は合法。originThreadが非nullならconversationId必須、かつThreadのconversationIdと一致する。Conversation削除によって進行中Taskを無断で消さない。
4. Taskは0..1 parentTask、0..N child Taskを持ち、**循環禁止**。parallel subagentはchild Taskを使用、単一Task内の無制限並列Runにしない。Taskは0..N Runを持つ。Runは必ず**ちょうど1 Task**。再試行・再実行は新しいrunId / attemptNoを使用。ただし**同じRunをcheckpointから再開**するときはrunIdを変えずleaseEpochを進める（後述）。
5. BrowserSessionは**独立ID**を持つ、tenant+taskに束縛されたブラウザ資源。複数Runが順次利用できるが、同時制御はCHAT_04の排他leaseに従う。BrowserSessionを「ユーザーの永続会話Session」と同一視しない。
6. Watchは**Taskの一種ではなく**、繰返し実行の永続的な定義。0..N Taskを生み、各Taskは0..1 createdFromWatchIdを持つ。Watch停止後も過去のTask/証拠は消さない。Watch→Task生成の冪等キーはCHAT_07と調整する。
7. 汎用語 **Session** は共通APIの型名として使用禁止。BrowserSession以外にAgent SDK固有のsession IDが必要な場合はAdapterSessionRefとして内部だけに格納し、Tenant/Task/Run IDに昇格させない。[PROVISIONAL: CHAT_02/04]

## 4. 要求定義

| REQ-ID | Level | 検証可能な受入条件 | SC |
|---|---|---|---|
| DOM-001 | MUST | 全ドメインレコードがtenantId+idで照合され、他tenantの参照・読取・変更が拒否される | 06,08 |
| DOM-002 | MUST | Conversation/Thread/Message/Task/Run/BrowserSession/WatchのIDを交換できず、異なる寿命を持つ | 02,05,06,07 |
| DOM-003 | MUST | parent-child/thread-origin/watch-linkは全て同tenantかつ参照先実在をtransactionで検査 | 02,07,08 |
| DOM-004 | MUST | Run.attemptNoはTaskごとに一意、同じTaskのactive Runは最大1、leaseEpochによりstale writeを拒否 | 07 |
| DOM-005 | MUST | 同じcommandId+scope+payloadの再送は同じ結果、同keyで内容違いは明示的409 | 02,07 |
| DOM-006 | MUST | 状態/所有権/リンク変更はrecordVersion CAS、失敗時に最新stateを再取得しblind overwriteしない | 07,08 |
| DOM-007 | MUST | 処理時点はUTC instant、予定の地域解釈はIANA TZ、価格/時点をlocal文字列だけに依存させない | 02,03,04 |
| DOM-008 | MUST | Watchから起動したTaskはWatchID・trigger identityを保持し、取得失敗でもWatch/Taskの同一性が保たれる | 02,07 |
| DOM-009 | MUST | Task goal/constraintsの改定はrevisionを上げ、Runが読んだrevisionを記録、Steerが競合で失われない | 01,04,07 |
| DOM-010 | MUST | SDK provider IDはAdapter boundary内のみ、公開IDを変えずにSDKを交換可能 | 05,07 |
| DOM-011 | MUST | credential/cookie/tokenや外部HTML上の命令をdomain公開projectionに含めない | 06,08 |
| DOM-012 | MUST | entity schemaVersionを認識し、未知majorをreject、既知旧版を明示migrationし無言のfield消失なし | 07 |
| DOM-013 | MUST | task/result/archive削除でもwatchやauditの参照がdanglingにならず、tombstone/retentionで処理 | 02,07,08 |
| DOM-014 | MUST | actor/commandのauthority判定とDB mutationが同一の論理境界で検証される | 06,08 |
| DOM-015 | MUST | task checkpoint復帰・新Run起動・Watch発火はDB commitとoutboxを同一transactionで確定 | 02,07 |
| DOM-016 | SHOULD | 親子Taskの最大深度・子数・同時実行数に有限のpolicy上限 | 07,09 |
| DOM-017 | SHOULD | 内部監査でcommandId/traceId/actorId/leaseEpochが相関可能（秘密値不要） | 07,08 |
| DOM-018 | LATER | 会話threadの明示的fork/merge UI（自動mergeは禁止） | 05 |
| DOM-019 | EXPERIMENT | 既存SDK conversation/checkpointer snapshotの輸入・復元adapter | 07 |

## 5. 共通型・Schema・Validation（CHAT_01の論理的正本案）

**境界上の型（言語非依存）:** 公開IDはUUIDv7文字列、canonical lowercase、正規化済で検証。生成は認証済みServerのみ。UUIDv7を時間順や認可の証拠とみなさず、DB unique constraintで衝突検知する。型名のBrandはTypeScript内で取り違えを防ぐが、wire上は単なるstringなので必ずruntime validatorも必要。UUIDv7ライブラリ/性能は未選定。

~~~ts
type Brand<T, Name extends string> = T & { readonly __brand: Name };
type TenantId = Brand<string, "TenantId">;
type UserId = Brand<string, "UserId">;
type ConversationId = Brand<string, "ConversationId">;
type ThreadId = Brand<string, "ThreadId">;
type MessageId = Brand<string, "MessageId">;
type TaskId = Brand<string, "TaskId">;
type RunId = Brand<string, "RunId">;
type BrowserSessionId = Brand<string, "BrowserSessionId">;
type WatchId = Brand<string, "WatchId">;
// Instants: ISO 8601 UTC Z, millisecond precision. Validation rejects naive local time.
type UtcInstant = Brand<string, "UtcInstant">;
// Timezone: validated against configured tz database, e.g. "Asia/Tokyo".
type IanaZone = Brand<string, "IanaZone">;
type SchemaVersion = "1.0";
type RecordVersion = number; // safe positive integer, starts at 1, CAS on each mutation
type Revision = number;      // positive integer, immutable task goal snapshot
type LeaseEpoch = number;    // non-negative fencing token, monotonic
type OpaqueRef = Brand<string, "OpaqueRef">; // only server-resolved, no embedded secret
type ActorRef = { type: "user" | "service"; id: string }; // authorization: CHAT_09
type EntityMeta = {
  id: string; tenantId: TenantId; schemaVersion: SchemaVersion;
  recordVersion: RecordVersion; createdAt: UtcInstant; updatedAt: UtcInstant;
};
type Conversation = EntityMeta & {
  id: ConversationId; ownerUserId: UserId; rootThreadId: ThreadId;
  title: string | null; lastActivityAt: UtcInstant;
};
type Thread = EntityMeta & {
  id: ThreadId; conversationId: ConversationId; parentThreadId: ThreadId | null;
  forkedFromMessageId: MessageId | null; nextMessageSeq: number;
};
type Message = EntityMeta & {
  id: MessageId; threadId: ThreadId; seq: number;
  role: "user" | "assistant" | "system-record" | "tool-record";
  contentRef: OpaqueRef; sourceTaskId: TaskId | null; sourceRunId: RunId | null;
};
type Task = EntityMeta & {
  id: TaskId; ownerUserId: UserId; conversationId: ConversationId | null;
  originThreadId: ThreadId | null; parentTaskId: TaskId | null;
  createdFromWatchId: WatchId | null; triggerKey: string | null;
  goalRevision: Revision; goalRevisionRef: OpaqueRef; budgetRef: OpaqueRef;
  requestedTimeZone: IanaZone; requestedLocale: string; state: TaskState;
  activeRunId: RunId | null;
};
type Run = EntityMeta & {
  id: RunId; taskId: TaskId; attemptNo: number;
  inputGoalRevision: Revision; state: RunState;
  leaseEpoch: LeaseEpoch; leaseOwnerRef: OpaqueRef | null;
  leaseExpiresAt: UtcInstant | null; checkpointRef: OpaqueRef | null;
  startedAt: UtcInstant | null; endedAt: UtcInstant | null;
};
type BrowserSessionIdentity = EntityMeta & {
  id: BrowserSessionId; taskId: TaskId; activeRunId: RunId | null;
  leaseEpoch: LeaseEpoch; expiresAt: UtcInstant | null;
  // Browser status, context profile and credential storage: CHAT_04/09
};
type WatchIdentity = EntityMeta & {
  id: WatchId; ownerUserId: UserId;
  createdByTaskId: TaskId | null; timeZone: IanaZone;
  // schedule/condition/status/notification policy: CHAT_07
};
// TaskState and RunState are imported ONLY from 01-03; NOT declared in this file.
// Evidence/Artifact refs use the future 01-05 contract. Physical schema: CHAT_10.
~~~

**契約上の境界:** 上記のBrowserSessionIdentity/WatchIdentityは**参照に必要な最小フィールド**で、各ownerの詳細型を奪わない。[PROVISIONAL: CHAT_04/07]。TaskState/RunStateが未発行の段階では、このDraftをコンパイル可能な最終TSと主張しない。Message contentRefは01-05に要調整。TaskのgoalRevisionRefは未承認の生HTMLや秘密を直結させず、承認済みuser goalを参照する。

**Validation rules（すべてServer側）**
- UUIDv7 variant/version、最大長、lowercase正規化。クライアント生成ID・推測可能なIDでACLを代替しない。opaque参照はDB所有の参照検証を必須とする。
- tenantId、id、ownerUserId、parentTaskId、originThreadId、createdFromWatchIdは**同Tenantかつ認可済み**でなければwrite不可。clientからtenantIdを信頼せずauthenticated contextから付与。
- createdAtは挿入後不変、updatedAtはcommit時刻で更新（UTC）、recordVersionは整数1開始・更新毎に+1。非整数・0・オーバーフローはreject。wall-clockでは更新競合を解決しない。
- timestampは厳密なYYYY-MM-DDTHH:mm:ss.SSSZにserialize。offset表記入力は正規化して保存、timezone未指定で日時解釈が必要なcommandは拒否/確認を求める。DSTのambiguous/nonexistent local timeは推測せずCHAT_07のpolicyへ。
- schemaVersionは直列化payload契約の互換性version、recordVersionはCAS、goalRevisionはuser constraints世代、leaseEpochはworker fencing、event sequenceは01-02。**互いに代用禁止**。
- 不明な公開入力fieldはstrict reject。読込時の旧schemaVersionはversioned migrationを通す。新majorはreject。既存のsnapshotをparseしただけでは認可を満たさない。
- MessageのseqはThread内一意・単調、任意の編集は新revisionまたは新Messageにする。論理削除はtombstone。情報抽出/証拠の鮮度は01-05、金額の通貨/単位は01-05+各domain担当へ。
- TaskのactiveRunIdはそのTaskのRun以外を参照不可。Run.inputGoalRevisionは実在のimmutable TaskGoalRevisionに一致。WatchTask triggerKeyは定義済みbounded string（発火時刻・schedule version等の正規化ハッシュ、詳細CHAT_07）で同一Watch内一意。
- parentTaskIdは自己参照や間接循環を拒否。分岐深度・最大child数は明示の有限policy、未設定/無制限を許さない。

**Wire projection例（公開APIの正式pathはCHAT_08、フィールド形は例示）**

~~~json
{
  "schemaVersion": "1.0",
  "id": "019a2fc0-4b21-7af0-9c22-34f78b6a1d04",
  "goalRevision": 1,
  "recordVersion": 2,
  "requestedTimeZone": "Asia/Tokyo",
  "activeRunId": null,
  "status": "see-task-run-state-machines"
}
~~~

このJSONのIDは**形式例**であり、実行ログ/本番IDではない。statusの値は未Lockedの01-03から輸入するため、この例は完全なTask応答schemaではない。実装時はschema code generation（Zod等は要選定）でDB DTO / service / RESTを単一sourceから生成し、受信では余剰fieldとenum未知値を拒否。APIレスポンスにtenantId、ownerUserIdなどを含めるかはCHAT_08/09が公開projectionを決定する。

## 6. Authority、state、sequenceとライフサイクル

**Authority matrix（ドメイン側制約の案。RBAC/ABACの認可正本はCHAT_09）**

| Action | 受付主体 | 実体のowner / 前提 | 永続的効果 |
|---|---|---|---|
| CreateConversation, PostMessage | authenticated user + API | userがtenant member、thread同tenant | conversation/thread/message、version上昇 |
| SubmitTask, SteerTask, CancelTask | authenticated user + API | taskのownerまたは明示委譲権限 | goalRevision/command intent、CAS |
| ClaimRun, CheckpointRun, FinishRun | authenticated runtime service | schedulerからのfenced lease、Task activeRun一致 | Run/Task checkpoint/result |
| CreateBrowserSession, AttachSession | browser worker | task scoped、leaseOwner一致、egress/secret policy通過 | BrowserSession lease、資源参照 |
| CreateWatch, DisableWatch | authenticated user + Watch service | authorization + 条件/通知policy確認 | Watchを永続化/停止 |
| FireWatch | watch scheduler | 生存・許可されたWatch、unique triggerKey | 新しいTask / dispatch outbox |
| ApproveExternalEffect | authenticated authorizing principal | server-held pending approval、対象run/task一致、期限内 | CHAT_03の承認台帳を更新 |

LLM/外部HTML/SNS/MCP tool responseは**どのactionでも直接の権限主体にならない**。表示上のapprove/continueボタンから送るIDも、Serverのpending stateと認可を再照合する。Run/BrowserSessionのleaseはTTLだけに依存せずfencing epochで古いworkerを拒否する。

**実行世代:**
- **Submit**: TaskIdを1つ作り、初期goalRevision=1、Runなし。DB commit後dispatch。UIにTaskIdを返す。
- **Start**: RunId新規・attemptNo=前回+1、Task.activeRunId設定。Task/Run stateの正当な遷移は01-03の表のみ。RunとTaskの行更新を一transactionでCAS。
- **Pause/Approval wait**: TaskId/RunId/goalRevisionは変わらない。承認待ちの決定はserver側保存。関連するRun/Task stateを01-03に従って更新。
- **Worker crash recovery**: 有効なcheckpointから**同RunId**を再ClaimしleaseEpochだけ増やす。安全な実行可否はledgerを確認し、非冪等な不確定操作は自動再送しない。
- **Retry after terminal failure**: **新RunId/attemptNo**。成功/部分成功/失敗/取消を表すstateと再試行可否は01-03で判定。過去Runのresultは変更しない。
- **Steer**: Task goalRevisionをCASで+1し、新しいimmutable revisionを保存。実行中RunのinputGoalRevisionは**そのRunが読んだ値のまま**、安全境界で改定を読み直すかRunを再開/切替する責務はCHAT_02。改定を適用したcheckpointは改定番号を記録。
- **Watch fire**: WatchIdを変えずtriggerKeyごとに別TaskIdを生成する。生成Taskが失敗してもWatchを勝手に削除せず、観測失敗は価格0と扱わない。
- **Conversation fork**: ThreadId新規、forkedFromMessageIdは当該ConversationのMessage。元ThreadとMessageは不変で、並行forkは別々のThread。merge機能はLATER。
- **Cancel**: Requestと実際の停止完了を区別し、キャンセル受付後に実行中のtoolが既に副作用を発生した可能性を保持する。terminal扱いを文章だけで発明しない（01-03）。

## 7. 実装可能なService APIと疑似コード

クラス配置提案（**本番repoの実在ファイルではない**）:

~~~text
packages/domain/src/ids.ts             // branded IDs + strict parse/serialize
packages/domain/src/entities.ts        // canonical logical records
packages/domain/src/validators.ts      // cross-field and FK validators
packages/domain/src/revisions.ts       // immutable goal revision encoding
packages/domain/src/commands.ts        // command DTO, expectedVersion, idempotency key
apps/api/src/services/conversation.ts   // service uses tenant auth context
apps/api/src/services/tasks.ts          // user mutation boundary
apps/worker/src/services/run-claims.ts  // lease/fencing; state import from 01-03
apps/browser-worker/...                 // BrowserSession authoritative implementation: CHAT_04
apps/watch-worker/...                   // Watch authority: CHAT_07
packages/infra/...                      // physical transactional repository: CHAT_10
~~~

**Command envelopeの概念:** actorContext（サーバー認証から構築）、commandId（ユーザー再送時も同じ）、expectedVersion、type、payloadHash、causationRef。HTTP仕様・実際のevent envelopeはCHAT_08/01-02へ委譲し、ここで複製しない。

~~~ts
async function submitTask(ctx, command) {
  requireAuthenticatedTenant(ctx); // CHAT_09
  assertCommandValid(command);
  return db.transaction(async tx => {
    const prior = await tx.idempotencyFind(ctx.tenantId, ctx.actorId, command.commandId);
    if (prior) return sameHash(prior, command) ? prior.originalResponse : conflict409();
    const thread = command.originThreadId
      ? await tx.readThreadForTenant(ctx.tenantId, command.originThreadId)
      : null;
    assertOriginConversationMatches(thread, command.conversationId);
    requirePermission(ctx, "task:create", command); // deny by default
    const task = makeTaskWithRevision1(ctx, command);
    await tx.insertTaskAndImmutableRevision(task);
    await tx.insertCommandReceipt(command, task.id);
    await tx.appendTransactionalOutbox("task accepted", task.id); // envelope assigned by 01-02
    return tx.responseFor(task);
  });
}
async function claimRun(ctx, taskId, expectedTaskVersion, previousEpoch) {
  // ctx is trusted worker principal, not LLM-provided identity.
  return db.transaction(async tx => {
    const task = await tx.taskLockAndAuth(ctx.tenantId, taskId);
    assertVersion(task, expectedTaskVersion);
    assertTransitionAllowedFrom0103(task);
    // Starting a fresh attempt requires no active run. Reclaiming a crashed
    // run MUST keep runId/attemptNo and increment leaseEpoch atomically.
    const run = chooseFreshOrRecoverableRun(task);
    await tx.compareAndSwapActiveRunAndLease(task, run, previousEpoch);
    await tx.appendTransactionalOutbox("run claim committed", run.id);
    return { runId: run.id, leaseEpoch: run.leaseEpoch };
  });
}
async function persistCheckpoint(ctx, runId, epoch, checkpoint) {
  return db.transaction(async tx => {
    const run = await tx.runForTenant(ctx.tenantId, runId);
    const task = await tx.taskForTenant(ctx.tenantId, run.taskId);
    if (run.leaseEpoch !== epoch || task.activeRunId !== runId) throw staleOwner();
    assertStateAllowsCheckpoint(run, task); // 01-03
    await tx.persistImmutableCheckpointAndUpdateRefs(run, checkpoint);
    await tx.appendTransactionalOutbox("checkpoint committed", run.id);
  });
}
async function fireWatch(ctx, watchId, triggerKey, observedAt) {
  return db.transaction(async tx => {
    const watch = await tx.watchForTenantWithPolicy(ctx.tenantId, watchId);
    assertActiveWatchAndTrigger(watch, triggerKey, observedAt); // CHAT_07
    const existing = await tx.findWatchTaskByUniqueTrigger(watchId, triggerKey);
    if (existing) return existing; // do not enqueue a second task
    const task = await tx.insertWatchTaskWithUniqueTrigger(watch, triggerKey);
    await tx.appendTransactionalOutbox("watch task ready", task.id);
    return task;
  });
}
~~~

**重要:** この疑似コードのoutboxメッセージ文言は説明用であり正式Event typeではない。正式なEvent ID/sequence/公開payloadは01-02、state遷移は01-03、実物の表/制約はCHAT_10が所有する。ここでいうtransactionとはTask書込+command receipt+outboxを同じDB commit境界にすること。外部web/API実行は同じDB transactionに含めない。

## 8. 障害・Race・Timeout・Cancelマトリクス

| 発生条件 | 検出 | 処理と不変条件 | 結果 / 通知 |
|---|---|---|---|
| ユーザーがSubmitTaskを二重送信 | 同一commandId + hash | 同一結果のリプレイ。hash違いは409。 | Taskは1件、Runも増えない |
| API応答直前にプロセス死亡 | DB receiptはcommit済み | 再送はreceipt取得。未commitなら新規作成。 | taskIdを同一再提示 |
| 2 Workerが同TaskをClaim | recordVersion CAS + activeRunId + leaseEpoch | **一方だけ成功**。敗者は他Runを実行しない。 | conflict監査 |
| 停止したWorkerの遅延checkpoint | epochが現行と不一致 | 拒否、artifactは孤児回収候補、現行Runを上書きしない。 | stale-owner監査 |
| 非冪等Tool後、DB保存前死亡 | Tool ledgerにindeterminate疑義 | 盲目的リトライせず照会/人間確認を要求（CHAT_03）。 | statusは01-03の不確定系 |
| Run checkpointとCancel競合 | Task/Run version + state rule | commitが先ならCancelが再判定、Cancelが先なら後続effectの開始不可。 | 状態差をUIへ公開 |
| Steerとcheckpoint競合 | goalRevisionとcheckpoint input revisionを比較 | 安全境界でreconcile、古いgoalへ暗黙に戻さない。 | 未適用revisionが見える |
| 同時parentTask生成、再帰・多段fork | ancestry validation + bounded policy | cycle/上限超過は拒否、途中のchild作成をrollback。 | 明示的拒否 |
| Watch schedulerが同tickを再実行 | unique(watchId,triggerKey) | 既存Taskを返す。ネットワーク失敗を「観測0」としない。 | 通知重複はCHAT_07 |
| ブラウザWorker再起動 | BrowserSession lease / expiresAt / epoch | 新ownerだけ接続、旧Cookieやcontextは適切な隔離から復元（CHAT_04）。 | 不可時は再認証待ち |
| Conversation削除と実行Taskの参照 | soft delete/retention policy | Taskの履歴を無断cascade deleteしない、公開閲覧権を再評価。 | audit保全 |
| IDが他tenantまたは違う型 | tenant-qualified query / runtime validator | 存在を推測可能なエラーで漏らさず拒否。 | 権限エラー |
| schemaVersionが新major/破損 | strict decoder | quarantine + migration要求。適当にdefault埋めしない。 | needs repair |
| 期限/予算切れ | runtime budget policy（CHAT_02） | 新規run/effectの許可をpolicyで停止。再Claimでも再評価。 | limit exceeded |
| DB commit済みoutbox未publish | outbox worker再処理（CHAT_10） | delivery重複を許容、consumerはeventId等でdedup（01-02）。 | event lost防止 |

**停止保証の上限:** すでに外部に送られたリクエストやブラウザ操作を完全撤回することはできない。Taskのcancel受付と副作用不存在は別命題。at-least-once dispatch、at-most-one fenced active owner、冪等なDB mutationを別々に試験し、外部副作用のexactly-onceを保証しない。

## 9. セキュリティ・Privacy・trust boundary

- [PROVISIONAL: CHAT_09] TenantUserMembershipにより、auth principalとtenantの組を認可した後、リポジトリへのqueryは**必ずtenant_idで絞る**。IDの知識はアクセス権ではない。単一ユーザー構成でもtenant境界を省略しない。
- Web HTML、SNS、PDF、MCP metadata、ツール出力は非信頼。そこから取り出した文字列がownerUserId、tenantId、Role、GoalRevision、Watch trigger policyを上書きできない。
- LLM model contextにAPIキー、Cookie、session secret、credential referenceを埋め込まない。BrowserSessionにはserver-resolved secret handleのみ。秘密を含むraw artifact / logはmaskとretention policyを別Ownerへ要求。
- Task/Watchの副作用（購入、予約、投稿、送金、許可外メッセージ）は承認台帳と再認可が必要。Task/Run IDとclient-submitted approval JSONだけでは実行しない。Approval token/TTL/署名方式の正本はCHAT_03/09。
- Host Shell/SSH、Docker socket、privileged、LAN無制限アクセスの能力を型やRoleへ追加しない。Worker tool gatewayは許可済みegressのみ。
- Guessable ID、stolen taskId、browserSessionId、Watch ID、Watch trigger replay、old revision replay、expired approval、cross-tenant parent linkをnegative testで検証。
- raw payloadの個人情報・監視履歴・session復元情報は暗号化/retentionをCHAT_09/10が決定。公開projectionで内部artifact参照を無条件配布しない。

## 10. Trace、性能・有限上限

- 構造化監査にactorId（内部参照）、tenantId（アクセス制御済み）、commandId、taskId、runId、attemptNo、goalRevision、leaseEpoch、recordVersion、traceId、durationMs、result categoryを関連付ける。内部URLと秘密をログに露出しない。Event correlation正式形は01-02。
- Latency観測: command受信→DB commit、DB commit→dispatch、claim→start、checkpoint書込、crash→fenced recovery、Watch trigger→Task commit。値をこの段階で成功保証にしない。
- [PROVISIONAL: CHAT_02/07/10] 初期デフォルト案: 1Task最大8 child、親子深度最大3、1Tenantの同時active Run最大4、Watch triggerの1回当たり新規Task最大1、操作command payload最大64KiB（本文の長大Artifactは参照化）。**数値は設計候補で、測定前の確定SLOではない。** 設定値は必ずfiniteかつ上限付き、overrideにはAdmin policy。
- DB unique invariantと書込競合は一貫性を優先、ID型/JSON検証コストはbenchmarkで観測。比較相手のOSSベンチマーク数字は転記しない。

## 11. 永続化・Transaction・Migration（物理DDLはCHAT_10）

**論理ストア案**: tenants、users/memberships（CHAT_09）、conversations、threads、messages、tasks、immutable task_goal_revisions、runs、browser_sessions（CHAT_04）、watches（CHAT_07）、command_receipts、idempotency_keys、outbox_events（01-02 / CHAT_10）、checkpoints（CHAT_02）、tool_execution_ledger（CHAT_03）。**表名は物理実装の決定ではない。**

**要求する物理invariant:** tenant-qualified FKまたは同等のDB内検証、unique (tenant, id)、unique (tenant, task_id, attempt_no)、unique (tenant, watch_id, trigger_key) where triggerKey is set、unique (tenant, thread_id, seq)、CAS対象recordVersion、leaseEpoch、1 Task 1 activeRunを強制するtransaction/制約。RLSの採否、index配置、DDL、FK delete actionはCHAT_10の権限。

**Migration:** domain schemaVersion=1.0新規導入。旧schema readerはexplicit version transformer、migration dry-run、backfill snapshot、数値/日時正規化、forward/rollback手順が必要。未知versionの自動強行読込は禁止。既存root Thread追加時にConversation/Messageの一意整合性を検証。Watch過去発火のtriggerKeyは推測で再生成しない。

**Recovery:** transactional command receiptとoutboxの同時保存。leaseEpochをcommitで更新し、帰還workerの古いackを落とす。削除はtombstone→retention期限→referential cleanupの二段階。バックアップ復元では外部副作用・Watch triggerの過去状態を無条件再実行しない。

## 12. UI・HTTP/API・Channel影響（CHAT_08提案）

Web chatはConversation/Thread IDで継続、タスク進捗は**Task ID**、試行詳細は**Run ID**で表示。Monitor dashboardは**Watch ID**と直近の発火Task IDを別ラベルで表示。Browser takeoverにはBrowserSession IDの短期・認可済み操作参照を用い、生のブラウザプロファイル情報を返さない。Telegramへはサーバー側に紐付いたTask/Watchの状態のみ表示し、メッセージ文字列のIDを認可とみなさない。

CreateTask APIはcommandIdを受け取れること、更新APIはexpectedVersion/409 conflictを扱うこと、UIはretriable通信障害で新taskIdを作らないことを要求する。api route/JSON error形式/SSE Last-Event-IDはCHAT_08、Event envelopeは01-02に委譲。ユーザーがwatch作成を指示した際はWatch ID/保存条件/次回予定を返す必要がある（SC-02）。

## 13. 実装フェーズとPR分割

1. **PR-DOM-A:** branded IDs、strict validator、entity meta、tenant-qualified repository interface、logical invariant unit tests。外部APIをまだ公開しない。
2. **PR-DOM-B:** Conversation/Thread/MessageとTask immutable goal revision、Submit command receipt CAS、transactional outbox。mock UIから連結。
3. **PR-DOM-C:** Run attemptNo、fencing lease、checkpoint参照、crash/restart/cancel競合テスト。01-03、CHAT_02/10の契約がLocked後。
4. **PR-DOM-D:** BrowserSession identity/lease（CHAT_04）、Watch trigger relation（CHAT_07）、client projection（CHAT_08）、tenant adversarial suite（CHAT_09）。
5. **PR-DOM-E:** schema migration/backup, durable E2E, metrics/perf calibration（CHAT_10/11）。

各段階で常にmain導入の共通型を消費し、他担当の状態enum/物理DDLを本書から勝手に生成しない。

## 14. 設計したテスト（すべて未実行）

各testは本番外のin-memory fake transactionまたはテストPostgresとMock Tool/Browser/Notifierで行う。実サービス購入・予約・SNS投稿はしない。

| TEST-ID | Type / fixture & action | Expected（機械的に判定可能） | SC |
|---|---|---|---|
| DOM-T01 | Unit: UUIDv7 parserへv4・uppercase・invalid variant・overlong input | reject、brandの型混在はcompile failure | 08 |
| DOM-T02 | Integration: tenant A task IDをtenant B tokenでread/update | 0 records mutated、data非露出、auditあり | 06,08 |
| DOM-T03 | Integration: cross-tenant parentTask/originThread/watchRefを作る | transaction rollback、参照0件 | 02,08 |
| DOM-T04 | Unit: parentTaskが自分/祖先へ循環、過深/過多childを指定 | reject、既存親子関係不変 | 07 |
| DOM-T05 | Concurrency: 32 parallel SubmitTask with same commandId+payload | Task1件、receipt1件、同一Task ID、outbox1件 | 07 |
| DOM-T06 | Contract: same commandId but different payloadHash | 明示409、前回Task無変更 | 07 |
| DOM-T07 | Fault: Submit DB commit後/HTTP response前にKill→同command再送 | 既存Task ID、重複なし | 07 |
| DOM-T08 | Concurrency: 2 workers same expectedVersionでclaim | 成功1、敗者CAS fail、activeRun1 | 07 |
| DOM-T09 | Fault: Worker lease期限後再claim→旧workerからcheckpoint | old epoch reject、最新checkpoint不変 | 07 |
| DOM-T10 | Fault: Run checkpoint後worker Kill→resume | **同Run ID**、増加epoch、二重実行なし（Tool ledger mock） | 07 |
| DOM-T11 | Unit: terminal retry | **新Run ID**、attemptNo+1、旧Run不変 | 07 |
| DOM-T12 | Race: checkpoint + steerを交錯 | 最新goalRevision preserved、Run inputGoalRevision誤改変なし | 04,07 |
| DOM-T13 | Race: cancel + tool start/checkpoint | cancel後の新規effect gate拒否、既存不確定は保持 | 07 |
| DOM-T14 | Concurrency: same Watch triggerKey from 16 schedulers | Task1件、すべて同Task IDを受け取る | 02,07 |
| DOM-T15 | Fault: Watch trigger commit後queue dispatch crash→outbox replay | Task1件、dispatchは少なくとも1回、consumer dedup | 02,07 |
| DOM-T16 | Unit: UTC offset・DST gap/overlap・naive timestamps | UTC canonical、ambiguousはpolicy介入、無言変換不可 | 02,04 |
| DOM-T17 | Contract: unknown major schema / missing recordVersion / extra fields | strict rejectまたは明示migration、黙殺0件 | 07 |
| DOM-T18 | Security: browserSessionId/approvalIdだけで別tenantからtakeover | 拒否、Cookie/Secret/Artifactをログにも出さない | 06,08 |
| DOM-T19 | Integration: Conversation論理削除中にTask実行 | Task/Run reference残る、公開閲覧権だけ適切に遮断 | 05,07 |
| DOM-T20 | Unit: Pydantic/Agents SDK snapshot import失敗 | adapter error、domain IDやtenant権限が変化しない | 07 |
| DOM-T21 | Contract: root Thread作成と初Message追加 | root1、Message seq一意、forkは同Conversation内のみ | 05 |
| DOM-T22 | Benchmark: 1/4/16 workers × 1000 mock commands × multiple tenants | conflict率/DB p50/p95/p99/CPU/メモリ記録、成功判定は整合性違反0、性能SLOは後決定 | 09 |
| DOM-T23 | Fault: commit前例外 vs commit後ACK喪失のSession stub | outcome区別、確定済み命令を重複副作用として再実行しない | 07 |
| DOM-T24 | Security: web artifactからtenantId/role/approval変更指示を入力 | mutation authority不変、prompt injectionはdata扱い | 03,08 |

**未実行明記:** 上表はすべて計画されたテストで、repoに実装コード・fixture・CIが存在するわけではない。LangGraph/PydanticAI/OpenAI Agents SDKの上流テストファイルは**読んだだけ**で、実行していない。

## 15. Acceptance criteria / traceability

| REQ-ID | TEST-ID | 合格根拠 |
|---|---|---|
| DOM-001 | T02,T03,T18 | cross-tenant read/write 0 |
| DOM-002 | T10,T11,T14,T21 | 各IDの寿命が異なり、変換・混同なし |
| DOM-003 | T03,T04,T21 | FK/ancestry関係で不正参照0 |
| DOM-004 | T08,T09,T10,T11 | 競合claim成功1、stale write 0 |
| DOM-005 | T05,T06,T07 | 同一key同一結果、different hashは409 |
| DOM-006 | T08,T12,T13 | CAS失敗が安全、上書き0 |
| DOM-007 | T16 | すべてUTC +明示的TZ |
| DOM-008 | T14,T15 | Watch 1 trigger→Task 1件 |
| DOM-009 | T12 | Steerしたgoalが永続化される |
| DOM-010 | T20 | Adapter失敗でDomain ID不変 |
| DOM-011 | T18,T24 | 秘密・権限変更0 |
| DOM-012 | T17 | 未知schema reject |
| DOM-013 | T19 | dangling linkなし |
| DOM-014 | T02,T03,T18,T24 | non-authorized mutation 0 |
| DOM-015 | T07,T10,T15,T23 | crash境界でlost commit/二重effectなし |
| DOM-016 (SHOULD) | T04,T22 | bounded concurrency |
| DOM-017 (SHOULD) | T09,T22 | correlation auditが追跡可能 |
| DOM-018 (LATER) | T21（基盤） | fork/merge UIは別計画 |
| DOM-019 (EXPERIMENT) | T20 | converter失敗を隔離 |

**運用E2E判定:** SC-01/03/04/05ではTask→Run→Evidence参照と公開状態を示せること、SC-02ではWatch→Task→Runの履歴とWatch停止後の非発火、SC-06ではBrowserSessionのtenant隔離とhandoff継続、SC-07ではkill/restart/dual-worker/steer/cancel、SC-08ではID改竄、SC-09では計測条件の記録。実装する横断E2E harnessと性能閾値はCHAT_11が所有する。

## 16. 未決事項とCross-team proposals

| ID / Owner | 優先度 | 提案・争点 / 暫定解 | Locked条件と検証 |
|---|---|---|---|
| DOM-P01 **CHAT_09** | P0 | 単一利用でもtenant + user membershipを必須とする。service principal/ACL/Row Level Securityの正式契約 | tenant-crossing E2Eに合格、auth設計レビュー |
| DOM-P02 **CHAT_02** | P0 | TaskRun所有権、Run resume vs retry、GoalRevision/Steer/Cancelの安全境界とleaseEpoch利用 | 01-03とruntime integration契約の一致、T08-T13 |
| DOM-P03 **CHAT_10** | P0 | Composite FK、CAS、unique active run / watch trigger、transactional outbox、lease fencing、command receiptの物理実装 | DDLと隔離レベルの競合テスト T03,T05,T08,T14,T15 |
| DOM-P04 **CHAT_07** | P0 | Watch status/triggerKey/scheduleVersion、Watch→Task生成、tz/DST、抑止済み通知と再試行 | Watch replay/停止試験 T14-T16 |
| DOM-P05 **CHAT_04** | P1 | BrowserSessionIdentityの寿命・lease・handoff・worker復元。activeRunIdは参照のみ | Browser ownership/fault/security T09,T18 |
| DOM-P06 **CHAT_00** | P1 | ThreadをConversationのbranchとし、generic Sessionを公開型にしないADR | 用語集/ADRで承認、横断参照更新 |
| DOM-P07 **CHAT_08** | P1 | REST/SSEはTaskとWatchの別ID、If-Match/commandId、匿名化projection、409 conflict対応 | Contract tests T05-T07,T17 |
| DOM-P08 **CHAT_03** | P0 | Approvalはserver-held pending intentへの参照で検証し、client historyから復元した指示を承認扱いしない | HITL/Tool gateway security test T18,T24 |
| DOM-P09 **CHAT_11** | P1 | Cross-module invariantのfixtureとfault matrix、p95/p99 measurement setup | T01-T24＋SC-01〜09のハーネス |
| DOM-P10 **CHAT_01 self** | P0 | 01-02のEvent envelopeと01-03のstate enumおよび01-05のEvidenceRefが未完成。型リンクを確定する | 3MDのDraft→Reviewed、API/DBとschemaVersionを照合 |

**提案の変更影響:** 01-03のstate names変更はRun/Taskのfield型とvalidator、event payload、テストT08-T13に波及。CHAT_10がtenant FK不可と判定した場合は同一transactionで行う参照検査・Race testを代替案として必須にする。Watch triggerKey構成変更は既存trigger履歴と重複通知に影響し、migration必須。現行の[PROVISIONAL]は勝手にLockedへ昇格しない。

## 17. Sources（commit pinned、2026-10-30閲覧）

[S1] **LangGraph checkpoint types/interfaces**, Code verified: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/langgraph/checkpoint/base/__init__.py#L39-L145 and #L199-L344 — CheckpointMetadata / Checkpoint / CheckpointTuple / BaseCheckpointSaver. LICENSE: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/LICENSE (MIT).  
[S2] **LangGraph in-memory storage**, Code verified: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/langgraph/checkpoint/memory/__init__.py#L430-L514 — put / put_writes.  
[S3] **LangGraph tests**, Code verified: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/tests/test_memory.py — TestMemorySaver, delta channel/ancestor write tests（static inspection only）.  
[S4] **PydanticAI Conversation**, Code verified: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/conversation.py#L47-L110 — Conversation、usage、deferred requests、conversation_id。 LICENSE: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/LICENSE (MIT).  
[S5] **PydanticAI tests**, Code verified: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_conversation.py#L197-L268 — immutable fork-like continuationとHITL継続テスト（not run）。  
[S6] **OpenAI Agents SDK Session protocol**, Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/memory/session.py#L69-L159 — Session/SessionABC. LICENSE: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/LICENSE (MIT).  
[S7] **OpenAI Agents SDK SQLiteSession**, Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/memory/sqlite_session.py#L229-L281 — session ID PK / messages FK.  
[S8] **OpenAI Agents SDK RunState**, Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_state.py#L842-L1000 — resumable state and warning about simultaneous resume.  
[S9] **OpenAI Agents SDK tests**, Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_run_state.py#L508-L530 and https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/memory/test_session.py#L841-L940 — schemaVersion rejection / concurrent sessions / lock recovery（not run）。  
[S10] **LangGraph issue #8115**, Issue report only: https://github.com/langchain-ai/langgraph/issues/8115 — PregelLoop.put_writes TOCTOU report（open as observed）。  
[S11] **OpenAI Agents SDK issue #4677**, Issue report only: https://github.com/openai/openai-agents-python/issues/4677 — Session persistence fault-harness proposal（closed as observed）。  
[S12] **PydanticAI issue #6452**, Issue report only: https://github.com/pydantic/pydantic-ai/issues/6452 — HITL UI approval provenance proposal（open as observed）。  
[S13] **Project primary requirements**, Docs verified: https://github.com/choko510/aiagent-md/blob/92b5827ed16a7d4ae61e8146ce382795398293cb/docs/00-foundation/project-brief.md ; architecture-rules.md ; acceptance-scenarios.md ; review-gates.md（同一baseline）。  
[S14] **Design decisions proposed by CHAT_01**, **Hypothesis / not benchmarked**: UUIDv7 choice、Task parent DAG、single active Run、leaseEpoch、triggerKey、finite caps、command receipt/outbox boundaries。これらはOSSに「そのまま存在する」と主張しない。

**Self-review against review-gates.md:** 主要OSS3件の実関数・テスト・commit・LICENSEを静的確認。実行テストなし、性能実測なし。Owned path 1件のみ。未確定のP0はDOM-P01/P02/P03/P04/P08/P10のまま。Ready/Lockedは申請しない。外部サービスへの購入・投稿・予約・通知は一切実行していない。
