# REST API v1 — Detailed Implementation Specification

> Owner: CHAT_08 | Canonical path: docs/08-interfaces/rest-api.md | Status: **Draft / provisional** | Revision: 0.1 | Research date: 2026-10-30 | Research baseline main: [92b5827](https://github.com/choko510/aiagent-md/commit/92b5827ed16a7d4ae61e8146ce382795398293cb) | Primary dependencies: CHAT_01, CHAT_02, CHAT_03, CHAT_04, CHAT_07, CHAT_09, CHAT_10

**規範の優先順位:** [Project Brief](../00-foundation/project-brief.md) → [Architecture Rules](../00-foundation/architecture-rules.md) → Locked shared contracts（現時点で未確認）→ 本Draft。これは実装済み機能でもLocked契約でもない。**本書が定義するのは外部HTTP境界のみ**。State/Event、認証方式、Tool承認ルール、DB DDL、スケジューラの意味論を別Ownerに代わって確定しない。

## 0. 結論と採用案

**採用:** versioned REST (/api/v1)、持続的なCommand受付（202/201＋Resource URL）、DB-backed冪等性台帳とoutbox、明示的な主体・所有権チェック、強い更新前提（If-Match）、cursor pagination、RFC 9457 Problem Details、read/write別のDTO projection、長時間処理のSSE別経路。HTTPはAgent実行のホストにならない。作成/制御APIはトランザクション内で命令を永続化してから返し、workerが後で処理する。通信断は**RunのCancelではない**。

**意図する製品:** Amazon買取差益、商品監視、SNS/X、ホテル、未想定の一般Web調査を、同一Task/Run/Watchの資源モデルで扱うセルフホスト型Personal Agent。WebとTelegramの操作は同一Auth主体・永続DBへ収束し、ModelとToolは許可/実行/承認の正本を迂回できない。

**高リスク依存:** Shared Domain/State（CHAT_01）、Pause/Resume/Steerの可否（CHAT_02）、承認状態・Side-effect gate（CHAT_03）、Watch定義（CHAT_07）、Authentication/RBAC（CHAT_09）、DDL/Outbox（CHAT_10）が未Locked。該当する提案は各所に **[PROVISIONAL: CHAT_XX]** を付記し、正式合意まで安定外部契約としてリリースしない。

## 1. 目的・責務・非責務・受入シナリオ

**責務:** (a) 入力検証と容量制限、(b) ユーザー/tenant/roleに基づくread/write権限チェック、(c) Task/Run/Watch/Approval/BrowserSessionの安全なHTTP表現、(d) 二重送信・再送・並行更新に耐えるCommand登録、(e) ページネーション・検索条件・メタデータ、(f) エラー/制限/費用・結果不明の明確な表示、(g) OpenAPI生成と公開契約テスト。

**非責務:** 実際のAgent loop、直接Tool呼出し、承認判定ロジック、実Browser操作、Web/SSE画面実装、Telegram固有配送、DBテーブルの正本、任意shell、SSRF突破、EC購入・予約確定。RESTから任意Toolを実行する **POST /tools/execute は公開しない**。ApprovalはTool Gatewayが生成するのでクライアントCRUDを許可しない。BrowserSessionもBrowser Worker所有で、任意Cookie投入・新規Browser直接生成APIを公開しない。

**関連SC:** SC-01（比較元と条件）、SC-02（Watch登録と次回実行）、SC-03（情報鮮度）、SC-04（人数/TZ/料金と予約非自動確定）、SC-05（汎用検索）、SC-06（browser handoff）、SC-07（クラッシュ/キャンセル/Steer/重複）、SC-08（認可/漏洩）、SC-09（費用）。

## 2. 既存OSS横断調査・採否

| OSS / pinned ref | 実際に読んだコード・テスト | 観測した実装機構 | 採否/制約 |
|---|---|---|---|
| **Mastra** [S1-S6], main ec3ba11d | server-adapter/routes/route-builder.ts の createRoute / pickParams / wrapSchemaForQueryParams、handlers/schedules.ts の LIST/CREATE/UPDATE/DELETE/PAUSE/RESUME/RUN_*_ROUTE、schemas/schedules.ts の z.strictObject・target union、handlers/error.ts の handleError、route-builder.test.ts・schedules.test.ts・error.test.ts・permissions.test.ts | schema-driven request/response定義、unknown field reject、agent/workflow scheduleの識別、pause/resumeの冪等性試験、既にclaimされたrunに対する409、path+method由来の権限導出テスト | **採用:** API schema定義と回帰試験、409 conflictの区別。**不採用:** エージェント/スケジュール用の型を本製品のWatch正本に直輸入。pathから権限を推測するだけでは誤認可リスク。本製品は明示的capability表。ルートモジュールの一部は @mastra/core/auth/ee に型依存。 |
| **OpenClaw** [S7-S11], main 8b8c33ef | gateway/http-endpoint-helpers.ts の handleGatewayPostJsonEndpoint、tools-invoke-http.ts の handleToolsInvokeHttpRequest、http-common.ts の readJsonBodyOrError / sendGatewayAuthFailure / watchClientDisconnect、auth.test.ts・http-utils.authorize-request.test.ts・tools-invoke-authorization.test.ts・auth.redaction.test.ts | method・body上限・auth・scopeチェック、長いbody読込後の認可再評価、HTTP切断をAbortSignalと連動、失効した認可の再検証、セッション別ツール可視性、redaction sentinel拒否 | **採用:** auth再検証、入力上限、失効/誤権限を試験する姿勢。**不採用:** OpenClawの互換HTTPにある共有secretが広いoperator権限を得るモデル。本APIはuser/tenant単位で絞る。またHTTP disconnectと長期Task cancelを同一にしない。 |
| **HTTP標準** [S12-S14] | RFC 9457, RFC 9110、IETF Idempotency-Key draft-08 | Problem Details、If-Match/412の条件付き更新、明示されたIdempotency-Keyとpayload fingerprintの設計 | **採用:** problem+json、If-Match、冪等性キー。**注意:** Idempotency-Keyは2026-10-30時点でdraft。確定RFCとして誤記しない。 |

**ライセンス:** Mastraの通常領域は Apache-2.0。ただし [LICENSE.md](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/LICENSE.md) が ee/ 別ライセンスを明記。OpenClawは [MIT](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/LICENSE)。本仕様は挙動の研究に基づく**独立設計**で、コード/テスト/ライブラリのコピペを要求しない。依存導入時にtransitive licenseとversion互換を再検証する。

**比較した設計案:**
1. **A: TaskをHTTP同期リクエスト内で完遂** — UI実装は簡単だが長時間調査でproxy/browser切断、再接続、再試行、二重副作用、24時間監視に適合せず却下。
2. **B: Commandとして永続受付しWorkerで処理** — DBトランザクション＋Outbox/Queueが必要だが再起動・別チャネル・監査に適合し採用。
3. **C: 生RPC/自由Tool Invokeを外部に開放** — 機能追加は容易でもTool Policyと境界を飛び越えうるため却下。
4. **D: 全てを単純なCRUD更新として扱う** — approval/Cancel/Resumeは遷移前提があり「PATCH status」だけで正しく表現できないため却下。**明示的Command endpoint＋If-Match**を採用。

## 3. 正本・所有権・依存図

~~~text
Web UI / Telegram Adapter / API client
         | authenticated HTTPS /api/v1
         v
  API Gateway (CHAT_08; transport, DTO, validation, idempotency)
         |                |
         | read query     +--> redacted audit / telemetry
         v                |
  AuthN/AuthZ CHAT_09 ----+
         |
  Command Service (ownership & expectedVersion, transaction)
         |             |
         |             +--> Command/Idempotency/Outbox persistence (CHAT_10)
         v
  Task/Run Manager (CHAT_02) ----> Tool Gateway (CHAT_03)
        |                                |
        +--> Watch Service (CHAT_07)     +--> Browser Worker (CHAT_04)
        |                                |
        +--> Shared event & state (CHAT_01)
         |
     Event projector --> SSE (CHAT_08 realtime-events.md; not specified here)
~~~

| データの正本 | Owner / 参照すべき文書 | APIの役割 |
|---|---|---|
| Conversation/Task/Run/State/Error/Evidence/Artifact | CHAT_01: core-domain-models.md / task-run-state-machines.md / error-taxonomy.md | 安全な**read projection**と公開ID。状態の語彙を上書きしない |
| Task作成、開始/停止/Steer、retry、budget | CHAT_02: task-run-manager.md / steer-queue-pause-resume.md / budget-and-limits.md | commandを委譲。実行の可否やstep境界を独断で判断しない |
| Approval/side effect/ポリシー | CHAT_03: tool-approval-and-policy.md / side-effect-ledger.md | 決定を渡すだけ。ブラウザ操作やTool起動はしない |
| BrowserSession所有権 | CHAT_04: browser-session-manager.md / login-challenges-human-handoff.md | 読み出しと人間へのhandoff開始・解除requestのみ |
| Watch/Observation/Notification | CHAT_07: watch-lifecycle.md / scheduler-and-recurring-tasks.md / notification-outbox.md | watchを取得/制御; 価格比較・定期計画の真実は変更しない |
| User/tenant/actor/capabilities/secrets | CHAT_09: authentication-and-user-isolation.md | 認証済みprincipalとauthorization decisionの消費 |
| DBテーブル、index、outbox/queue | CHAT_10: database-schema-and-migrations.md / job-queue-and-outbox.md | transaction semanticsの要件を提案 |
| E2E基盤 | CHAT_11: test-harness-and-fixtures.md | 試験要求/fixturesを引き渡す |

**原則:** UIに渡すIdentityはserver-side principalから派生し、JSON body中の userId / tenantId / actorRole / policyApproved を信頼しない。Tenant scopeはDB queryにおいて強制（ブラウザ/Telegram/RESTで同じ）。

## 4. 要件（規範）

| REQ-ID | 優先 | 検証できる要求 | SC |
|---|---|---|---|
| REST-001 | MUST | /api/v1 のroute群をOpenAPI 3.1で公開し、未知fieldを拒否、入力/出力を別schemaで検証 | 01,05 |
| REST-002 | MUST | 全私有resourceで認証・object-level 認可・tenant隔離。bodyのuserIdで昇格不能 | 06,08 |
| REST-003 | MUST | Task/Runの受付はDB永続化後202/201を返し、HTTP切断で長時間Runをcancelしない | 05,07 |
| REST-004 | MUST | POST commandsにIdempotency-Keyを必須とし、同key+異bodyを409、同key+同bodyを既存結果として返す | 02,07 |
| REST-005 | MUST | 更新系にIf-Match/expected versionを必須、競合は412・遷移不能は409で区別 | 02,07 |
| REST-006 | MUST | cursorを使い固定sort順で一覧を返し、異tenant横断を防ぐ | 02,08 |
| REST-007 | MUST | APIでのWatch作成成功時、watchId・保存条件・次回時刻・通知設定の投影を返す | 02 |
| REST-008 | MUST | Approvalsは閲覧・期限確認・単一決定のみ。期限切れ・競合・欠権で実行不可 | 04,06,07 |
| REST-009 | MUST | BrowserSessionは読み出し/正規handoffに限定しcredential/cookie/tokenを非公開 | 06,08 |
| REST-010 | MUST | レスポンスでEvidenceのobservedAt/source・estimate/unknown、使用量の確定/推定を区別 | 01,03,04,05,09 |
| REST-011 | MUST | RFC 9457の機械可読エラー・requestId・Retry-After・安全な文言を提供 | 07,08 |
| REST-012 | MUST | 受付とjob enqueueをoutbox等で整合させ、クラッシュ時に二重外部副作用を作らない | 02,07 |
| REST-013 | MUST | すべての副作用endpointにCSRF/Origin検証などCHAT_09のpolicyを適用し、rate/body/timeout上限を設ける | 08,09 |
| REST-014 | MUST | Task/Watchのpause/resume/cancelは遷移Command。Ackと完了状態を別々に表す | 02,07 |
| REST-015 | MUST | Read responses/監査ログにsecret、raw cookie、外部HTMLをそのまま混ぜない | 06,08 |
| REST-016 | MUST | ApprovalとBrowser操作は対象owner/current actorの権限を**決定直前にも再評価** | 06,07,08 |
| REST-017 | SHOULD | If-None-Match ETag・gzip/br・PATCH read-model diffを導入してポーリングを抑える | 07,09 |
| REST-018 | SHOULD | OpenAPI typeをフロントとAPI clientで共有し、契約破壊をCIで検出 | 01,05 |
| REST-019 | LATER | 運用者向けbatch read export（監査付き、rate capped） | 05 |
| REST-020 | EXPERIMENT | 通知チャネル差の少ないread-through cache。tenant leak test後に検討 | 02,09 |

## 5. HTTP公開契約と型

### 5.1 Transport, negotiation

- Prefix **/api/v1**。JSON UTF-8、HTTPS（loopback以外の平文不可）。個人情報を持つレスポンスに Cache-Control: private, no-store（運用管理画面経由も同様）。OPTIONS/CORS allowlist、X-Request-Idを返す。機密なリンクの query token は禁止。
- write系は Content-Type: application/json、Accept: application/json、指定以外415/406。GETで副作用禁止。
- Instant responseはread projection。Workを開始するCommandは **202 Accepted**、新規read resourceを確定保存済みなら **201 Created + Location**。ACKは成果達成を意味しない。再送時は初回と同じstatus/body/locationを（可変statusを作り直さず）返す。
- **タイムゾーン:** wireはUTC RFC3339 (Z) に統一、UIは元指定TZを保持。金額は currency と decimal string（例 "45000"）で表しfloat換算禁止。
- セキュリティ上 endpointを公開せずに403を使い分けたい場合、object-scoped readで存在と権限なしの両方を404で返す（policyの正本はCHAT_09）。
- Bearer Authorization or secure session cookieの実装、CSRF方式、token/role managementは [PROVISIONAL: CHAT_09]。Webhook/Telegramのアダプターは権限のないヘッダーで他人になりすまさない。
- Client API-VersionはURI majorで固定。additive optional fieldのみminor互換。値の意味、state string、idempotency fingerprint、pagination sortを破る変更は /api/v2 として移行。OpenAPI manifestとgenerated clientのcontract diffをCIに追加する。

### 5.2 REST route matrix（v1 proposal）

ここでいう POST /commands は公開Resource command。**Run/Approval/Sessionの無制限CRUDは許さない**。実行前の入力・statusの意味はOwnerの正式契約に従う。

| Resource | Method + Path | 権限案 [PROVISIONAL: CHAT_09] | 成功と意味 |
|---|---|---|---|
| Conversation | POST /conversations | conversation:create | 201 Conversationを保存。Idempotency-Key必須 |
| Conversation | GET /conversations, GET /conversations/{id} | conversation:read | 200一覧/詳細。作成主体、linked Task ID |
| Task | POST /tasks | task:create | 202 persisted Task＋initial Run command。Idempotency-Key必須 |
| Task | GET /tasks, GET /tasks/{id} | task:read | 200 status, result/evidence refs, budget summary |
| Task | PATCH /tasks/{id} | task:update | 200 **未実行のeditable metadataのみ**（If-Match必須） |
| Task | POST /tasks/{id}/commands | task:control | 202 pause/resume/cancel/steer。If-MatchとIdempotency-Key必須 |
| Run | GET /tasks/{id}/runs, GET /runs/{id} | run:read | 200 steps/attempts/cost/terminal-or-unknown read model |
| Run | POST /tasks/{id}/runs | run:create | 202 既存Taskの安全な新Run request。Idempotency-Key+If-Match |
| Watch | POST /watches | watch:create | 201 Watch保存と次回確認予定。Idempotency-Key必須 |
| Watch | GET /watches, GET /watches/{id} | watch:read | 200 current state + nextCheckAt/lastObservedAt |
| Watch | PATCH /watches/{id} | watch:update | 200 編集可能な条件のみ。If-Match必須、スケジューラ再評価 |
| Watch | POST /watches/{id}/commands | watch:control | 202 pause/resume/cancel（or delete semanticsはCHAT_07決定） |
| Approval | GET /approvals, GET /approvals/{id} | approval:read | 200 target action summary, scope, expiry, decision status |
| Approval | POST /approvals/{id}/decision | approval:decide | 202 approve/denyのみ、If-Match + Idempotency-Key |
| BrowserSession | GET /browser-sessions, GET /browser-sessions/{id} | browser:read | 200 redacted handoff status, own task linkage |
| BrowserSession | POST /browser-sessions/{id}/handoff | browser:takeover | 202/200 **単回・期限付き**takeover request（Browser Worker delegate） |
| BrowserSession | POST /browser-sessions/{id}/release | browser:takeover | 202 ownership返却要求。If-Match + Idempotency-Key |
| Evidence | GET /evidence/{id} | evidence:read | 200 provenance projection。raw artifact閲覧は別許可 |
| Usage | GET /runs/{id}/usage | usage:read | 200 billed/estimated/currency/source timestamps |
| Progress | GET /tasks/{id}/events | task:read | 200 text/event-stream **詳細は realtime-events.md** |

**Delete:** Task/Run/Approval/BrowserSessionへのDELETEはv1では公開しない。監査証跡と副作用台帳を消せないため。User向けデータ削除/retention/法令遵守は [PROVISIONAL: CHAT_09 + CHAT_10] の管理経路で実施し、Cancelとdeleteを同義にしない。Watchの不可逆削除も監視履歴と通知outboxの整合が必要なのでCHAT_07合意後に導入する。

**検索:** GET /tasks は query status?, createdFrom?, createdUntil?, conversationId?, limit=20, cursor?。GET /watches は status?, source?, limit, cursor。全filterはallowlistでfieldごと検証。limitは 1..100、初期20（この数値はプロジェクト仮置き、測定未実施）。sortは createdAt DESC, id DESC固定でfilter+tenantをcursorに結合。無効cursor 400。cursorはHMAC認証されたopaque byte列としactor/tenant、filters hash、sort key、snapshotUpperBound、expiresAtを含み、JSON decodeで権限を決定しない。ページング途中の新規行はsnapshot boundary以降に挿入されない設計。削除・更新によるページ差はあり得るため一覧にsnapshotAsOfとeventual-consistency注記を付ける。cursor expiry暫定24h。

### 5.3 API-owned projection（共有domain型ではない）

以下は **wire上の構造案**。TaskState / RunState / ApprovalStatus / WatchStatus 等のenum自体は [PROVISIONAL: CHAT_01 / CHAT_07 / CHAT_03] である。ここに実装して同名domain型を正本化してはいけない。

~~~typescript
type Id = string;                 // opaque; parse as server-generated only
type ISOInstant = string;         // validated RFC3339 UTC
type MoneyWire = { amount: string; currency: string };
type PublicRef = { id: Id; href: string };
type EvidenceSummary = {
  evidenceId: Id;
  sourceUrl?: string;             // URL policy + private origin filtering
  observedAt?: ISOInstant;        // observed vs emitted distinction
  capturedAt?: ISOInstant;
  certainty: "observed" | "inferred" | "unknown"; // PROVISIONAL CHAT_01
  title?: string;
};
type ResourceMeta = {
  id: Id;
  resourceVersion: number;        // strictly increasing, domain-owner supplied
  createdAt: ISOInstant;
  updatedAt: ISOInstant;
};
type TaskView = ResourceMeta & {
  conversationId: Id;
  state: string;                   // from CHAT_01, not enumerated here
  currentRunId?: Id;
  goalSummary: string;
  budget: {
    configured?: MoneyWire;
    spentConfirmed?: MoneyWire;
    estimatedPending?: MoneyWire;
    isFinal: boolean;
  };
  resultKind?: "complete" | "partial" | "uncertain" | "unavailable";
  evidence: EvidenceSummary[];    // bounded summary, not raw text
};
type WatchView = ResourceMeta & {
  state: string;                   // CHAT_07-owned
  conditionSummary: string;
  scheduleTimezone: string;
  nextCheckAt: ISOInstant | null;  // null when paused/never scheduled
  lastObservedAt: ISOInstant | null;
  notificationTargetSummary: string; // no auth secrets or chat IDs
};
type ApprovalView = ResourceMeta & {
  taskId: Id;
  runId: Id;
  actionSummary: string;
  requestedScopeSummary: string;
  expiresAt: ISOInstant;
  decisionState: string;          // CHAT_03-owned
  canDecide: boolean;             // advisory; actual command revalidates
};
type BrowserSessionView = ResourceMeta & {
  taskId: Id;
  ownerRole: "worker" | "human" | "none"; // PROVISIONAL CHAT_04
  handoffAvailable: boolean;
  handoffExpiresAt?: ISOInstant;
  // never return cookies, token, raw storage state or internal CDP endpoint
};
type CommandAck = {
  commandId: Id;
  resource: PublicRef;
  acceptedAt: ISOInstant;
  observedVersion: number;
  disposition: "accepted" | "already_recorded"; // accepted != performed
};
type Page<T> = {
  items: T[];
  nextCursor: string | null;
  snapshotAsOf: ISOInstant;
};
~~~

**Read projection rules:** 全ResourceのJSONはexplicit allowlist serializer（ORMやQueueのobjectを直接return禁止）。message要約やURLが未検証な場合はURL未提供/uncertainとする。Evidenceには出典原URL・取得時刻を明記し、watchの結果が取得失敗なら null/unknown にして0円を作らない。raw HTMLやLLM hidden promptを返さない。

### 5.4 Request examples（試験用の非実行サンプル）

~~~http
POST /api/v1/tasks HTTP/1.1
Authorization: Bearer <REDACTED>
Content-Type: application/json
Idempotency-Key: "b5c93e0e-d441-43c2-b106-d6c75db76f8a"

{
  "conversationId": "c_abc",
  "goal": "11月20日から3人1泊、駐車場ありで合計15,000円以下のホテルを探す",
  "constraints": {"guests":3,"nights":1,"maxTotal":{"amount":"15000","currency":"JPY"},"parkingRequired":true},
  "timezone": "Asia/Tokyo",
  "budget": {"maxCost":{"amount":"1.00","currency":"USD"}},
  "requestedModel": "default"
}
~~~

Response **202**, Location: /api/v1/tasks/t_abc, commandIdとstateを持つackを返す。最大費用USD 1は**サンプル入力**でありサービスのdefaultや推奨値ではない。constraintsは任意のドメインキーを無制限に通すのではなく、versioned goal/constraint codecをCHAT_01/02と合意する。resourceIDやbudget超過を後でモデルが勝手に書き換えない。

~~~http
POST /api/v1/approvals/a_123/decision HTTP/1.1
Idempotency-Key: "e72b1990-9688-47c1-a150-799b3a35c8e7"
If-Match: "7"
Content-Type: application/json

{"decision":"deny","reason":"予約確定はしない"}
~~~

Response **202** は決定要求が永続化されたことのみを示す。**202を返したから外部サイトで処理が完了したとは言わない**。Approvalに対するレスポンスは新しいapproval versionとcommandIdを返し、実行可否はCHAT_03ゲートが最終判定。

### 5.5 Validation / ceilings

- BodyはUTF-8 JSON、unknown key拒否、array/depth/string lengthの上限を設定。暫定値: max body 64KiB（非添付）、goal 8KiB、constraints 16KiB、batch array禁止、JSON nesting 16、query length 4KiB、route timeout 15s。これらは**仮のガードレールで性能実測ではない**。添付Artifact uploadは別署名経路が必要 [PROVISIONAL: CHAT_10]。
- Idempotency-Keyはdraft-08を参考にquoted UUID header（128 chars以下、clientごとに強い乱数）を受け付け、fingerprintは **HTTP method + normalized route + principal tenantId + canonical validated body + normalized semantic headers（If-Match含む）** のSHA-256。別keyに同一bodyを自動マージしない。
- key namespaceは tenant + principal + endpoint operation + key にしてDB UNIQUEを想定。同じkey/同じfingerprintなら保存済みHTTP status/response/locationを返す。違うfingerprintは409 KEY_REUSED_WITH_DIFFERENT_REQUEST。実行中同keyは受付レコードがcommitされるまでDBで直列化し、completedのreceiptを再利用する。TTLは暫定30日、**期限後の重複防止は保証しない**。依頼者は初回intent IDを保持し、期限経過して不明ならTask/Watch一覧やreceipt照会で明示的に照合する。
- ETagは強い version token: ETag: "7"。If-Match不足428、値不一致412（レスポンスへ最新の個人情報を過剰表示しない）、遷移不許可409。If-Matchとcommand指示の期待stateは別検証（version一致でも状態不正は409）。
- 全routeにrate limit、concurrency cap、body read timeout、request deadline、server requestId生成。正式な公平性/値はCHAT_09/10の正本に提案。429はRetry-After秒を返す。Budgetと認可の正本はUIの表示値でなくServer。
- **Direct model-specified tools、unbounded free-form JSON、opaque SQL filter、client-controlled actor/tenant、global includeSecrets=trueは禁止。**

## 6. 状態遷移・HTTP sequence・実行境界

~~~text
Client                API           AuthZ         DB+Outbox       Runtime/Worker
  | POST Task+key       |              |              |                |
  |-------------------->| validate     |              |                |
  |                     |------------->| scope+object |                |
  |                     |<-------------|              |                |
  |                     |       BEGIN SERIALIZABLE or fenced tx       |
  |                     |  reserve idem fingerprint                  |
  |                     |  persist task/run-intent + outbox          |
  |                     |  record stable response receipt           |
  |                     |-----------------------------> COMMIT       |
  |<--- 202 Location ---|              |              |                |
  |    [may disconnect] |              |              |-- dispatch --->|
  | GET /tasks/id       |              |<--- read model persisted ------|
  |<--- 200 progress ---|              |              |                |
  | [SSE optional]      |              |              |                |
~~~

- Client connection lost **before commit**: no acknowledged command; retry same idempotency key。
- Commit succeeded / response lost: retry same key returns same command receipt, never a second command。
- Enqueue lost after commit: outbox dispatcher re-drives; Worker dedup by commandId and domain constraints. External irreversible tools are governed by CHAT_03 side-effect ledger; DB uniquenessだけで外部サイトexactly-onceとは言えない。
- Worker restart/partial results: Task/Run/Evidenceの正本状態を再投影し、未知の結果は uncertain/needs-review; APIが成功へ丸めない。
- Pause/Resume/Cancel/Steer: command accepted → runtimeの安全境界で適用 → Event/Task version更新。二重cancelは同一keyでreceipt再生。別keyのcancelで既に終状態なら409またはno-opの可否をCHAT_02と合意（現Draftは409提案）。Cancel以降新しい外部副作用を起動しない（すでに出た外部リクエストはreconciliation必須）。Steerは将来stepに適用し進行中不可逆操作を巻き戻さない。
- Approval: client決定前にresourceVersion・expiry・principalを照合し、DBでclaim fence。2人が同時承認/否認した場合1人のみ成功、敗者409/412。理由/誰が決定したかはaudit正本、外部通知はoutbox。
- Browser handoff: Sessionの現在owner/leaseとuserIdを照合し、Browser Workerから短命のtakeover capabilityを**別の安全なチャネル**で発行。REST projectionには生CDP URL/認証キー/永続Cookieを返さない。handoff中の通常agent browser操作はCHAT_04 lockに従い待機。期限切れ・owner失効で解放、ユーザー再認証を促す。CAPTCHAの自動突破を行わない。

## 7. アルゴリズム・擬似コード

~~~typescript
// API-only application service; pseudocode, not a proposed shared domain type.
async function acceptCommand(ctx, route, rawBody, headers) {
  const requestId = makeRequestId();
  const principal = await authn(ctx);                      // CHAT_09
  await checkOriginCsrfRateAndAdmission(principal, ctx);   // CHAT_09
  const body = route.strictSchema.parseBounded(rawBody);   // reject extras
  const target = await repository.findTenantScoped(route.target(body), principal.tenantId);
  await authz(principal, route.capability, target);        // deny before mutation
  const key = parseStructuredIdempotencyHeader(headers);
  const fingerprint = sha256(canonicalEncode({
    method: route.method, path: route.normalizedTemplate,
    tenantId: principal.tenantId, principalId: principal.id,
    body, conditional: headers.ifMatch ?? null
  }));
  return db.transaction(async tx => {
    const receipt = await tx.idempotency.tryReserveOrReadForUpdate(
      principal, route.operation, key, fingerprint
    );
    if (receipt.kind === "same_finished") return receipt.originalHttpResponse;
    if (receipt.kind === "mismatch") throw conflict("IDEMPOTENCY_KEY_REUSED");
    if (receipt.kind === "in_progress") throw retryableConflict("REQUEST_IN_PROGRESS");
    const resource = await tx.resources.lockForUpdateTenantScoped(target, principal.tenantId);
    await assertStillAuthorizedAndCurrent(principal, route, resource);
    checkIfMatchOrThrow(headers.ifMatch, resource.version); // 428/412
    const command = await domainOwner.validateAndPersistCommand(tx, resource, body, principal);
    await tx.outbox.addUnique(command.id, command.dispatchPayload);
    const response = buildSafeReceiptAndLocation(command);
    await tx.idempotency.complete(key, fingerprint, response);
    return response;
  });
}
// Caller controls the HTTP request. It does not await a worker outcome.
// tx and outbox must be on the same durable transactional store, or use
// a proven equivalent with explicit crash/replay semantics.
~~~

**Creation routes:** Target entity absent by design; authorize capability on parent conversation/tenant, then insert resource and its initial command atomically. fetch-before-begin only acts as preliminary check, **must revalidate inside fenced transaction**。If-Matchは既存resourceのmutationにのみ要求。DB異常時、idempotency receiptとresource/outboxを別々のcommitで保存するのは禁止。

**Idempotency race:** 동일 keyの2requestはDB UNIQUEとrow lockでlinearize; winnerがtransactionをroll backするとloserがreserveする。winner commit後loserは同じreceiptを得る。既にin-flightのnontransactional workを発生させず先にDB commitする。別keyによる同resourceのPATCHはIf-Match/version fenceで1件のみ成立。

**Approvalの追加条件:** Approval token/decision nonceなど機密状態はBody任せにせずCHAT_03の保存側で検査。approved by API はexternal side effect実行の免罪符ではなくTool Gatewayの最終許可。Policy変更・権限失効・期限切れならdeny優先。取得済みのRead DTOにcanDecide:trueと表示されても再検査を省略しない。

## 8. 障害・競合マトリクス

| 発生場面 | 検出・status | 復旧と可視状態 | 外部副作用の扱い |
|---|---|---|---|
| 未認証 / auth expired | 401、WWW-Authenticate（Bearer時） | client再ログイン。秘密をエラーに入れない | command未登録 |
| 他tenant / 見えないresource | 404（policy proposal） | 対象を列挙できない | command未登録 |
| 権限不足（known capability） | 403 | 正当な権限申請が必要 | 未実行 |
| JSON破損/unknown key/過長 | 400/422/413 | field pointerと制限を返す | 未登録 |
| key欠落/不正 | 400 | 新しいclient intent keyで再送 | 未登録 |
| key同一/同一payload | original 201/202 response | receipt再利用; 新Runを作らない | 1 command |
| key同一/異payload | 409 IDEMPOTENCY_KEY_REUSED | clientで別意図として確認 | 未追加 |
| If-Match欠落/不一致 | 428/412 | GETで現在version取得し**ユーザー意図を再確認** | 未追加 |
| 状態としてpause不可、cancel後run | 409 INVALID_TRANSITION | 最新read modelを表示 | 未追加 |
| 承認期限切れ / 二重決定 | 409/410 [PROVISIONAL CHAT_03] | 更新したapproval viewを表示 | 盲目的実行なし |
| rate/budget exceeded | 429/409 or 422 [PROVISIONAL] | Retry-After/limit or review-needed | Workerゲートでも防御 |
| Task受付commit前クラッシュ | connection reset | same key retry。DB未commitなら再受付 | なし |
| DB commit済みHTTP応答消失 | retryで既存receipt | 利用者はGETでTask状態を確認 | command重複なし |
| DB commit済みenqueue不達 | outbox backlog / 202 | 再dispatchと監視alert | Side-effect ledger再照会 |
| Workerの結果が不明 | 200 resultKind=uncertain | 根拠欠落明示、承認/照会へ | 非冪等操作を再実行しない |
| Browser lease expiry | 409/423 [PROVISIONAL CHAT_04] | owner/状態再照会、人間handoff再要求 | 直接browser操作禁止 |
| Read projectionがlegacy schema | 500 + traceId、内部alert | 秘密/raw DB列を返すfallbackは禁止 | なし |
| SSE中切断 | stream disconnect | Last-Event-ID resumeは別MD | Runをcancelしない |

**Retry契約:** GETは指数backoff+jitter（finite retries）可。POSTは**同じidempotency key・同じbody**の場合のみ自動retry可。409/412は自動再送せずrefresh/人間確認。ネットワークtimeoutだけで別keyを作って再投稿しない。API 503はRetry-After; Worker/DB一時障害が長引く場合も受付可否を正直に返す。

## 9. Security / Privacy / Trust boundary

- Authenticationはserverで一度だけ判定するのではなく、read/query、command登録、承認決定直前にprincipalの現行権限と対象tenant一致を確認。内部service tokenと外部user tokenを混同しない。Clientにpolicy decision booleanを渡して信頼しない。
- 同一ユーザーがWebとTelegramでアクセスした際、チャネルは別scopeで本人紐付けを検証。Telegram chatIdをHTTPのuserId/tenantIdとして受けない。
- 調査対象のWebページ、X投稿、Evidenceから得た文言は**非信頼データ**。URL/Titleはencode/sanitizeし、HTMLをrenderせず、redirectされた添付URLを任意fetchしない。secretを原文で返すようなAgent textもprojection firewallで除去/検証。
- Browser handoffの資格情報、cookie、localStorage、CDP websocket、cloud credentialをRESTレスポンス、SSE、ログ、trace、OpenAPIサンプルに含めない。Secret accessorはCHAT_09のみ所有。
- SSRF/egressはCHAT_09とBrowser Worker側でチェック。URLが内向き/private IPへredirectした場合にAPIサーバが直接取得しない。read evidenceのsourceUrlは表示用テキストでありfetch capabilityではない。
- Reverse proxy構成ではtrusted proxy listからのみForwarded/X-Forwarded-*を受理。CORS origin allowlist、cookie-mode CSRF、SameSite、TLS、request limits、secure headersをCHAT_09レビューで確定する。PUT/PATCH/POSTはOrigin検証、認証のみで安全とは判定しない。
- Result URLにはtenant-private resource id以外の認証情報を埋め込まない。API logへAuthorization、Cookie、Idempotency-Key全文、approval proof、raw user prompt、External URL query secretを保存しない。Auditは requestId/actorId/resourceId/action/decision/reason code/resultのみ（data retentionはCHAT_09）。
- **未承認の購入・予約・投稿・送金は禁止**。ApprovalのREST decisionはポリシーを飛び越える「万能許可」ではない。

## 10. Metrics / 費用と性能評価

- Metrics: route+method別 request count、p50/p95/p99 ack latency、4xx/5xx、authz-denied、429、idempotency hit/mismatch、412、409、outbox lag、command accepted→worker observed latency、projection errors、tenant isolation violations（目標0）。
- Trace: requestId ↔ commandId ↔ taskId/runId ↔ eventIdをserver-sideで相関。Token/Raw evidence/Secretsをspanへ載せない。**APIが伝える費用**は確定請求額・推定・未確定を区別し、通貨とモデルを明記。API Ackの高速化を優先して事実検証/承認をスキップしない。
- Benchmark案: mock AuthN・Postgresローカル/Compose・固定5万Task/5万Watch・10/100同時actorのisolated read/write、重複key bursts、outbox lag、body 64KiB境界、cursor 100件・10ページを測る。専用環境/ハード/DB/ソフト版/負荷条件を必ず記録。**未計測なのでSLO値を断定しない**。
- 暫定上限: 100 items/page、64KiB request body、15s route timeout、cursor 24h、idem 30d。CHAT_10の負荷計測で調整し、API docsとtestsを同時更新。

## 11. 永続化・Transaction・Migration提案 [PROVISIONAL: CHAT_10]

**論理要件**（物理DDLの正本ではない）:
- idempotency receipt: (tenant, principal, operation, key_hash), fingerprint, reserved/committed, response_status, response_body_redacted, location, createdAt, expiresAt, traceId。UNIQUE keyとtimeout recoveryを要する。
- command: commandId, targetResource, targetVersion, actorId, requestedAt, acceptedAt, outcome, correlation IDs。DomainのCommand/Runテーブルに対応付け、独自のState enumを増やさない。
- outbox: UNIQUE commandId、dispatch attempts、nextRetryAt、processedAt（メッセージ単位のat-least-once）。DBトランザクションでresource mutationとreceiptとoutboxをatomically commit。
- Resource version: 行更新ごとstrict monotonic増加、fencingを支える。Listには (tenantId, createdAt DESC, id DESC) 相当の索引。ApprovalのCASはCHAT_03/10。
- DB障害復旧: reserved未commitなら消え、committed receiptは再生。receiptと実体が不整合な場合は503と監査/修復対象、黙って新規コマンド作成禁止。
- Migration: 初回はv1新設 → feature flag shadow read (外部非公開) → internal adapter契約テスト → v1 route公開 → idempotency/permission alerts。後方互換変更はprojection additive-only、破壊変更はversioned routes。migrationで旧pending approvalを暗黙auto-approveしない。
- retention時の削除順: ユーザー要求/監査保持期間/個人情報配慮に従う。idempotency receipt expiresAt経過後のretry不確定は明記。purgeしながら動作中Run参照を壊さない。

## 12. UI・外部チャネルへの影響

- Web chatは TaskView/RunView/EvidenceSummaryを読む。CommandAckを「完了」と描画しない。202のあとSSE/GETを併用、SSEの契約は realtime-events.md。
- Dashboardはrun step/costとWatch nextCheckAt/lastObservedAt、paused/uncertain/needs-approvalを表示。削除と停止を混同しない。
- Approval UIはaction summary/対象URL・費用・期限・irreversible副作用を確認し、GETでETag取得→decisionへIf-Match付与。期限切れ409はユーザーに理由表示。
- Browser takeover UIは短命handoffの表示だけ。raw credentials/automation endpointは出さない。
- Telegram adapterもRESTまたは等価のCommand application serviceを利用し、**同じDB principal/idempotency/permissions** を通過。通知重複はCHAT_07のNotification Outbox、チャネル跨ぎSteerの整合はCHAT_02と共同設計。
- Externally visible errorsは requestId と安全なlocalized message（languageはAccept-Language）、永続Resource URLで事後確認できること。

## 13. 実装PR順序（提案）

1. **08-A (dependency):** CHAT_01/09/10とID・version・tenant/authn contractを照合し、OpenAPI skeletonとstrict schemas（副作用なし）を実装。
2. **08-B (reads):** GET Conversation/Task/Run/Watch/Evidence/Approval/BrowserSession projection・pagination・ETag。tenant isolation contract tests。
3. **08-C (commands):** atomic receipt + outbox + Task/Run/Watch Command受付、If-Match/race/failure injection、worker側command dedup。
4. **08-D (HITL):** Approval decisionとbrowser handoff; CHAT_03/04 ownerの承認/lease契約確定後に公開。
5. **08-E (channel/UI):** SSEとの再接続整合、Web/Telegram parity、E2E、OpenAPI breaking change gate、metrics/ops。

依存が未合意の段階では docs only PR とし、実装を強行しない。

## 14. テスト設計（実行ではない）

**本仕様策定ではAPIアプリのテストを実行していない。以下は実装後に作るべきテスト**。OSS側テストファイルは閲覧済みであるが、そのCI/ローカル再実行は未実施。

| TEST-ID | レベル / Setup → Action | Expected（定量/状態） | SC |
|---|---|---|---|
| REST-T01 | Contract: OpenAPI/generator、unknown key・不正timezone・decimal float・array overlimit送信 | 4xx、field location、安全なerror。valid requestでschema準拠 | 01,04,05 |
| REST-T02 | Security: tenant A/B fixture; AがBのTask/Approval/Watch/BrowserSession idをGET/POST | 他tenant objectは404、side effect/情報漏洩0件 | 06,08 |
| REST-T03 | Integration: Task POST後直ちにclient socket close | Task/commandは1件、Worker進行継続 | 05,07 |
| REST-T04 | Race: same key+body 100 concurrent posts | command/Task/Outboxそれぞれ1件、全successful responseは同じresource id | 02,07 |
| REST-T05 | Contract: same key different body/If-Match | 409、元receiptは変化せず新Command0件 | 02,07 |
| REST-T06 | Race: 2 PATCH with identical If-Match version | 成功1、失敗1 (412)、version+1のみ | 07 |
| REST-T07 | Fault injection: kill (a) precommit (b) aftercommit/before send (c) after commit/before enqueue | (a)再受付1件 (b)再送receipt同一 (c)outbox再drive、効果二重なし | 07 |
| REST-T08 | Contract: all list routes 105 items + cursor 20; cross-tenant + new inserts + tampering | no duplicates among stable rows, tenant leak0、tampered cursor 400、全件件数はfilter条件と一致 | 02,08 |
| REST-T09 | E2E mock Watch create→read→pause→restart→resume | watchId・nextCheckAt・timezone・通知設定表示、停止中の新実行0 | 02,07 |
| REST-T10 | Approval race: 2決定/expired/revoked actor、mock external-effect ledger | 最初のvalid decisionのみ登録、expired/revokedは拒否、未承認外部操作0 | 04,06,07,08 |
| REST-T11 | Browser handoff user A/B + expired lease + malformed session ID | Aのみ一時takeover、Bは拒否、cookie/CDP tokenはJSON/traceに0件 | 06,08 |
| REST-T12 | Security: hostile webpage prompt textやURL query secretをEvidenceに注入 | outputはuntrusted/sanitized、secret漏洩0、Policyを変更しない | 03,08 |
| REST-T13 | Contract: state conflict / rate cap / unavailable store / request timeout | 409 /429 + Retry-After /503 / timeout、安全で安定したproblem+json | 07,08 |
| REST-T14 | Channel parity: WebとTelegramが同じTaskを操作 | 1つのrun lineage、version一致、権限一致、不正重複Command0件 | 02,07 |
| REST-T15 | Benchmark: pinned environment、mock LLM/Browser、50k fixture | ack latency/throughput/RSS/outbox lagを記録。目標値は測定後レビューで決定 | 09 |
| REST-T16 | Resilience: cancel accepted during active/unknown external tool action | APIはackedとappliedを区別、外部結果不明ならuncertainで通知、blind retry0 | 07 |
| REST-T17 | Auth lifecycle: request body読込中にtoken/role失効、承認時に再失効 | commit前に401/403、command追加0、秘密露出0 | 06,08 |
| REST-T18 | Cache: Conditional GET、secret/no-store、version increment | 304は本当に不変のprojectionのみ、private情報がshared cacheへ残らない | 08,09 |

テストは全てMock/fixtureを基本とする。Amazon購入、実ホテル予約、実Telegramへの未承諾通知、実サイトの負荷試験を行わない。SC-11という全体シナリオは存在しないため、新しいSCを捏造しない。

## 15. Acceptance / REQ-ID → TEST-ID → SC-ID

| MUST requirement | TEST | SC |
|---|---|---|
| REST-001 | T01, T13 | 01,05 |
| REST-002 | T02, T17 | 06,08 |
| REST-003 | T03, T07 | 05,07 |
| REST-004 | T04, T05, T07 | 02,07 |
| REST-005 | T06, T10, T16 | 02,07 |
| REST-006 | T08 | 02,08 |
| REST-007 | T09 | 02 |
| REST-008 | T10 | 04,06,07 |
| REST-009 | T11 | 06,08 |
| REST-010 | T01, T09, T12 | 01,03,04,05,09 |
| REST-011 | T13 | 07,08 |
| REST-012 | T04, T07, T16 | 02,07 |
| REST-013 | T01, T13, T17 | 08,09 |
| REST-014 | T09, T16 | 02,07 |
| REST-015 | T11, T12, T18 | 06,08 |
| REST-016 | T10, T11, T17 | 06,07,08 |

TXX は本節の REST-TXX を指す。**合格閾値:** functional testはexpected通りの0 unauthorized effects/0 duplicate commands、no exposed secrets。性能は環境別ベースラインのレビュー承認まで未固定。

## 16. 未決事項 / Cross-team proposals

| ID / severity | Owner | 確定してほしい契約・選択理由 | 完了条件 |
|---|---|---|---|
| API-P01 / **P0** | CHAT_01 | Task/Run/Conversation/EvidenceのID・version・state enum・uncertain/partial表現をLockedに。APIではstring placeholdersのみ使用 | 正本のvariantとprojection対応表、破壊変更試験 |
| API-P02 / **P0** | CHAT_02 | command受付とappliedのstate/steer boundary、cancel後の既存inflight副作用、同じTaskに新Run作成可能条件、Budget実行前チェック | 正確なcommand transition表とtests |
| API-P03 / **P0** | CHAT_03 | Approvalの状態/期限/決定CAS/再照会、tool side-effect ledgerとcommandId再送の結合 | expired/duplicate/unknown resultの承認policy |
| API-P04 / **P0** | CHAT_09 | tenant/actor/rbac、browser vs machine auth、CSRF/Origin、scope名、404隠蔽、trusted proxy構成 | 役割/Resource policy matrix＋脅威試験 |
| API-P05 / **P0** | CHAT_10 | receipt+outbox+resourceのatomic transaction、claim/lease、index、TTL、異常回復 | 具体DDL + migration + fault tests |
| API-P06 / P1 | CHAT_07 | Watch condition schema、次回実行時刻/通知先・Pause/Resume・delete semantics | Watch DTOとのcontract tests |
| API-P07 / P1 | CHAT_04 | BrowserSession read projection、short-lived handoff URL/leaseとowner fencing | takeover/logout/expiryのセキュリティ試験 |
| API-P08 / P1 | CHAT_08(realtime-events.md) + CHAT_01 | CommandAck/Task progressとSSE cursorの整合。読み出しからsubscribeまでのgapを埋める | Last-Event-IDとsnapshot consistency tests |
| API-P09 / P1 | CHAT_11 | 100 concurrent command/raceとworker kill matrixを正式fixtureへ | CI実行可能なfault injection suite |
| API-P10 / P2 | CHAT_00 + CHAT_09 | self-host単独ユーザーから複数tenant移行、role revocation / delete/export/retentionのUX | ADRとmigration条件 |
| API-P11 / P2 | CHAT_10 | 64KiB/15s/30d/24h上限の運用値 | 環境記録と負荷結果、Doc/version更新 |

**暫定運用:** 共通型を勝手に別ディレクトリへコピーしない。Ownerがreject/修正した決定は本Draftを修正して初めてLocked化可能。

## 17. Sources（確認日：2026-10-30、原典とgrade）

**Pin:** upstream SHAは調査時のmain head。タグやリリース版との対応は検証していない。全Code verifiedは「ソース内容を読んだ」であり、**プログラムを実行したという意味ではない**。以下のURLは特定commitのfileを指す。

- **[S1] Code verified:** [Mastra route-builder.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/server-adapter/routes/route-builder.ts) — createRoute, pickParams, wrapSchemaForQueryParams。commit ec3ba11d、Apache-2.0（ee例外はS6）、実行未検証。
- **[S2] Code verified:** [Mastra route-builder.test.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/server-adapter/routes/route-builder.test.ts) and [permissions.test.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/server-adapter/routes/permissions.test.ts) — query wrapper/permission path derivationのtest fixture、実行未検証。
- **[S3] Code verified:** [Mastra schedules.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/schedules.ts) and [schemas/schedules.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/schemas/schedules.ts) — route定義、agent/workflow schedule・strict union。
- **[S4] Code verified:** [Mastra schedules.test.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/schedules.test.ts) — ambiguous create body、Pause/Resume冪等、trigger list/lastRun/404などのテストコード。
- **[S5] Code verified:** [Mastra error.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/error.ts) and [error.test.ts](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/error.test.ts) — lost concurrent claimへの409・unsupported capabilityの501。
- **[S6] Docs verified:** [Mastra LICENSE.md](https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/LICENSE.md) — standard paths Apache-2.0、ee/別ライセンス。導入時のdependency reviewが必要。
- **[S7] Code verified:** [OpenClaw http-endpoint-helpers.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/http-endpoint-helpers.ts) — handleGatewayPostJsonEndpoint、認証/authority revalidate。
- **[S8] Code verified:** [OpenClaw tools-invoke-http.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/tools-invoke-http.ts) / [http-common.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/http-common.ts) — body limit、auth/abort、HTTP status、disconnect、SSE headers（本APIのSSE詳細は別MD）。
- **[S9] Code verified:** [OpenClaw http-utils.authorize-request.test.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/http-utils.authorize-request.test.ts), [auth.test.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/auth.test.ts) — revoked authority、origin/rate authentication。テスト実行未検証。
- **[S10] Code verified:** [OpenClaw tools-invoke-authorization.test.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/tools-invoke-authorization.test.ts), [auth.redaction.test.ts](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/auth.redaction.test.ts) — session authorization/secret sentinel拒否のテストコード。
- **[S11] Docs verified:** [OpenClaw MIT License](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/LICENSE) — MIT、本仕様では直接コードコピーしない。
- **[S12] Docs verified:** [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html) — application/problem+json、2023-07。
- **[S13] Docs verified:** [RFC 9110 HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html) — conditional request/If-Match/412、2022-06。
- **[S14] Docs verified (draft, not RFC):** [IETF Idempotency-Key header draft-08](https://www.ietf.org/ietf-ftp/internet-drafts/draft-ietf-httpapi-idempotency-key-header-08.html) — structured header format, uniqueness/expiry/fingerprint。2026-10-30閲覧、仕様変動リスクあり。

**未検証:** upstreamテストの実行結果、Mastra/OpenClawの当該commitとrelease tagの対応、他環境での互換性、本ハーネスへの統合、Postgres index設計の性能、本人認証実装方式。文書検査・引用参照以上をVerifiedと主張しない。
