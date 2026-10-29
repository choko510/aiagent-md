# ROUND 2 — Contract Reconciliation / P1矛盾の統合プロンプト

> 対象：CHAT_00/01/02/03/08/09/10（特にCHAT_01とCHAT_10）、統合COORDINATOR。
> 目的：初稿で発見された、Shared Contracts / Runtime / DDL / REST / Securityの互換性問題を**実装開始前に**解消する。
> 注意：本ファイル自体は変更権限を与えない。各担当の既存Branch/PRで**自分のファイルのみ**修正する。

あなたは`choko510/aiagent-md`仕様統合の契約調停者です。前ラウンドで11件のDraft PRが作成されました。優れた個別初稿を消さずに統合し、型・保存方式・状態・APIのP1不一致を、所有権とレビューワークフローを守りながら解消してください。

## 必須確認
最新mainのProject Brief、Architecture Rules、Spec Index、Review Gates、Acceptance Scenarios、各CHAT専用プロンプト、`ROUND2_AUTHOR_CONTINUE.md`を読む。加えて、最新のPR本文だけでなく、PR #1 (CHAT_02 AgentLoop), #2 (CHAT_01 Core Domain), #8 (CHAT_08 REST), #9 (CHAT_09 Auth), #10 (CHAT_10 DB) の**変更MD全文**をそれぞれのHEAD refで実際に読む。依存するPR #6 (CHAT_03 Tool Registry) と #11 (CHAT_07 Scheduler) も必要箇所を読む。

## 初回に作る契約相違台帳
`Conflict ID / severity / exact source path+section+symbol / source A claim / source B claim / affected runtime path / recommended canonical owner / alternative designs / chosen proposal / migration and contract tests / status`。
最初に少なくとも次を再検証する：
1. Public UUIDv7 vs Postgres`gen_random_uuid()` default。
2. Nullable Task.conversationId vs NOT NULL DB / required REST projection; 特に監視起点Task、外部イベントTask。
3. RecordVersion CAS initial 1 vs DB revision 0。要件が本当に同一か/Adapter分離するか。
4. `leaseEpoch`/fencing tokenの型・DB列・更新・Stale write rejection。
5. `goalRevision`, `activeRunId`, `ownerUserId`等の共通モデル属性の永続性/派生元。
6. `tenantId` vs `workspace_id` vs user ownership。Auth/Worker/SSEで一貫した強制認可になるか。
7. POST Create Task/Watcher/ApprovalのAtomic Commit/Event/Outboxの制約。
8. Run Replay/Tool Replayの`UNKNOWN`/不確定副作用を自動再実行できてしまわないか。

## 決定と実装指示への変換
- **CHAT_00**：競合するアーキテクチャ判断のADRを作る（正本の定義の仕組み）。
- **CHAT_01**：ID/Entity/State/Event/Versionの論理正本を確定する。自分のPR #2に必要な修正を入れる。
- **CHAT_02**：契約に適合するAgentLoop/Checkpoint/Leaseの不変条件を確定、必要ならPR #1を修正。
- **CHAT_03**：ToolCall/Policy/Side-effectとUUID/Version/Approvalの境界を検証し、PR #6修正の必要性を判定。
- **CHAT_08**：REST DTO/nullable/If-Match/Idempotency/エラーの契約を正式型へ整合、必要ならPR #8を修正。
- **CHAT_09**：認可主体、Tenant/Workspace/Ownerの関係と資源ごとのアクセス判定を合意、必要ならPR #9を修正。
- **CHAT_10**：論理正本に整合するSQL migrationを再設計し、CAS/fencing/unique/FK/indexが満たせるか確認。必要ならPR #10を修正。
- **CHAT_11**：互換性Contract TestとCrash Recovery Testの受け入れ要件を設計（自分の担当MDに限る）。

全体で一つのMDへ統合して済ませない。それぞれのOwnerが自領域の正本を持つ。まだ別Ownerの決定を得ていない場合は`[PROVISIONAL: CHAT_XX]`、解決後に必要なPRのみ更新する。

## 優先解決順（推奨）
1. Tenant/Workspace/User/Ownershipの関係を決める（AUTH + CORE + DB）。
2. Task/Conversation/Run identityとnullable/parentとGoalRevisionを決める。
3. UUID generation/versionとRecordVersion/CAS/Fence単位/シリアライズを決める。
4. State machine/Event envelope→DDL/REST/Tool Gateのmappingを決める。
5. Transaction outbox、run checkpoint、double deliveryとExternal side-effectの照合を決める。
6. すべてについてUnit + Contract + DB integration + fault testsを作る。

## 例として必要な具体的テスト
- Server生成UUIDの全経路をShared Validatorが受け入れる。
- Conversationを持たないWatcher/Event起点Taskの作成・検索・認可が仕様どおり。
- 同じTaskに二人のWorkerが同時Claimしても古いLeaseの書込みを拒否。
- 旧GoalRevisionに基づく承認・Tool intentがユーザーSteer後に無断実行されない。
- DB commit直前直後でWorker kill、再配達しても副作用を盲目的に重複実行しない。
- 他workspace/tenantのTask、Artifact、BrowserSession、Notificationを読めない。
- 更新競合はIf-Match/CAS違反として検出され、知らないうちに上書きしない。

## 出力形式
矛盾台帳、Owner別の具体的修正提案、採用する差分と退ける代替、依存順序、対応PRリンク、合格Test ID、未決事項。読めなかったファイルは未調査とする。

## GitHub書込ルール
各チャットは自分のPR/担当MDだけ更新。COORDINATORはレビューや提案を作るが他人のMDを無断Pushしない。Draft PRのmainへの無断マージ禁止。競合が解決した証拠なしに`Locked`と宣言しない。
