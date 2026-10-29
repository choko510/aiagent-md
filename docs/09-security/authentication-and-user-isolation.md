# Authentication and User Isolation — Detailed Implementation Specification

> Owner: **CHAT_09** | Canonical path: `docs/09-security/authentication-and-user-isolation.md` | Status: **Draft / PROVISIONAL** | Revision: 0.1 | Research date: **2026-10-30** | Based on project main: `92b5827ed16a7d4ae61e8146ce382795398293cb`
>
> Dependencies: CHAT_00 (ADR), CHAT_01 (User/Task/Run/Event/Artifact & error contracts), CHAT_02 (runtime/recovery), CHAT_03 (Tool policy & approval), CHAT_04 (BrowserSession), CHAT_07 (Watch), CHAT_08 (REST/SSE/Telegram/UI), CHAT_09 (secrets, capability, egress, privacy), CHAT_10 (DDL/RLS/deployment), CHAT_11 (test harness).
>
> **Verification label:** This is a specification, not implemented/verified code. OSS code was inspected at the exact refs in `17`; this product's end-to-end behavior has not been executed. All provisional shared contracts require their canonical owner's acceptance before Locked.

## 0. 結論と採用案

**採用する設計:** 単一ユーザー開始でもサーバーの全入口を認証必須とし、生成時から永続データに所有者境界を持たせる。ユーザーを識別する`principal`は検証済みセッションからのみ発行し、Task/Run/BrowserSession/Watch/Evidence/Artifact/Memory/Connector/Approval/イベント配信を参照する際は**サーバー側のowner/tenant照合**を必須にする。URL中のID、LLM指示、MCP引数、Telegramの表示名、Workerのペイロードは認可の根拠にならない。

1. **認証層**: サーバー管理の失効可能な不透明セッション、host-onlyの`Secure; HttpOnly; SameSite=Lax` Cookie、Origin/CSRF制御、ログイン試行制限。推奨候補はBetter Authのセッション/CSRF機能を`AuthProvider`アダプターで利用すること。ただし導入前にNext.js/Fastify/DB統合と失効保証をスパイク検証する（`[PROVISIONAL: CHAT_08/10]`）。認証方式とドメインモデルは分離する。
2. **認可層**: `authorize(principal, action, resource)`をAPI・Worker・SSE・Browser・Artifact・Approvalで利用し、実行直前に所有者をDBの正本で照合する。`owner_id/tenant_id`は信頼済みprincipalとDBから決定する。認可結果をリソースIDだけでグローバルキャッシュしない。
3. **単一→複数ユーザー**: 初回インストールのローカル管理操作でownerを1名登録し、後からユーザーと各人専用の隔離領域を増やせる`ownerId`/`tenantId`を初日から永続化。初期実装ではユーザー間共有、委譲、組織間アクセスを**許可しない**。共有機能が必要になればADR・独立の権限モデルが必要。
4. **独立した信頼境界**: UI/Telegram/AuthProvider/Agent/Worker/Browser/MCP/DBは同一ではない。Web入力やLLMのロール宣言が権限を昇格させることはない。非可逆操作にはCHAT_03の承認台帳とfresh principalの再照合が必要。
5. **DBを防御の一層として利用**: アプリの所有者付きクエリを必須とし、可能ならPostgreSQLのRLSを導入。RLSだけを最終防壁とは呼ばない（高権限DBロール、漏えいしたSET権限などの限界あり）。
6. **安全な公開**: 初期セットアップ画面を認証なしにインターネットへ露出しない。Web UIの公開有無にかかわらず認証は無効化不可。リバースプロキシのforwardedヘッダーを無条件に信用しない。

**この文書で採用しないもの**: OpenClawの共有gateway tokenだけで「ユーザー」認可を完了させる方法、LLM側promptでの隔離、URL/Task IDを持っているだけでのアクセス許可、単一ユーザーだから`userId`列を省く方式。Web調査Agentであり、ホストShell/SSH/Dockerソケット/privileged実行は設計対象外。

## 1. 目的・責務・非責務・受入シナリオ

### 1.1 Threat modelと守る資産
- 攻撃者: 未認証のLAN/Internetクライアント、ログイン済みだが別ユーザーのデータを探るアカウント、盗まれたブラウザCookie、悪意あるHTML/X/MCP応答、偽のTelegramメッセージ、古いWorkerジョブ、誤設定のreverse proxy。
- 資産: Taskと会話の内容、Cookie/ブラウザ保存状態、Evidence/Artifact、私的な検索履歴、Watch/通知宛先、外部サービス資格情報、課金予算、承認操作。
- 信頼境界: Browser→API、Telegram→channel adapter、API→queue、queue→Worker、Worker→Browser Worker、API→DB、SSE/WS→client、Tool/MCP→Gateway。全境界でIdentityとAuthorityの付け替えを禁止する。
- ネットワーク到達範囲、MCP実行権限、Cookie暗号化・監査保存の詳細は、それぞれ同カテゴリ別MDの正本へ委譲する。認証成功はSSRF・prompt injection・権限外ツール実行の許可ではない。
- SC-06: 他ユーザーのBrowserSession/Cookie不可、Human Handoffは本人だけ。SC-07: cancel/steer/crash/再配信後も所有者不変。SC-08: forged principal/header/tool outputでの越境不可。派生影響はSC-02/03/04/05/09。

### 1.2 非責務
SSO/OIDC、組織共有、ゲストリンク、公開APIキー発行、外部ユーザー招待、承認済み共有Resource、host administrative RPCは初期リリースに含めない。多要素認証とPasskeyは`SHOULD`/段階導入候補、匿名ログイン/`auth.mode=none`は許可しない。第三者提供のIdPを追加する際もowner-boundな認可を変更しない。

## 2. 既存OSS横断調査

| 参照OSS・ref | 読んだ実コード/テスト | 実際に確認できた機構 | 本件への採否・制約 | ライセンス |
|---|---|---|---|---|
| **Better Auth 1.7.14** / `95d158b` | `packages/better-auth/src/api/middlewares/origin-check.ts`: `originCheckMiddleware`, `validateOrigin`, `validateFormCsrf`; `origin-check.test.ts`: trusted/untrusted origin、first-login CSRF、request-scoped origin隔離 | Cookieつき変異要求のOrigin検証、Cookieなしのログインに対するFetch Metadata、callback URLのallowlist。`disableCSRFCheck`等のskip設定も存在 | **参考・AuthProvider実装候補**。危険なskipオプションを許可しない。検証なしで本プロジェクトに導入済みとは扱わない [S1–S3] | package metadata MIT |
| **Better Auth 1.7.14** / 同ref | `api/middlewares/authorization.ts`: `requireResourceOwnership`, `requireOrgRole`; `api/routes/session.ts`: `getSession`; `api/routes/session-api.test.ts` | 認証済みuserとresource ownerの比較、組織member条件、セッション取得レスポンスの`no-store`、Cookieと未ログイン処理テスト | **所有者検証パターンを独立実装**。本件の永続Task/Watch/ArtifactはBetter Authのresource model外なので自動で保護されない [S4–S6] | MIT |
| **OpenClaw 2026.10.9** / `8b8c33e` | `src/gateway/auth.ts`: `authorizeGatewayConnect`, `authorizeSharedSecretAuth`, trusted proxy/Origin判定; `auth.test.ts`; `method-scopes.ts`と`method-scopes.own-session.test.ts` | 接続方式・proxy attribution・rate limitを区別し、operator methodをscope分類。接続時認証とメソッド権限の分離 | **責務分離を参考に独立実装**。Gateway共有tokenモデルは多人数向けresource ownershipの代替ではない。OpenClaw自体の機能・権限は流用しない [S7–S10] | MIT |
| **Playwright 1.65.0-next** / `38fb53b` | `packages/playwright-core/src/server/browserContext.ts`: `BrowserContext.storageState`, `clearCookies`, `setStorageState`; `tests/library/browsercontext-storage-state.spec.ts`: `ST-001`〜`ST-005`等 | storageStateにはCookie/localStorage/条件によりIndexedDB/OPFSが含まれ、新しいContextへの復元がテストされている | **BrowserSessionをユーザー別に隔離する設計の根拠**。Playwright自体はHarnessのuser/tenant認可を行わない。storageStateは機密として扱う [S11–S13] | Apache-2.0 |
| **OWASP**（公式） | CSRF Prevention、WebSocket Security、API1 BOLA、Session Managementの各ガイダンス | CookieとOrigin、サーバー側のobject-level authorization、WS接続後の継続的な認可の必要性 | 脅威モデルと悪用テストの基準。コードではなく`Docs verified` [S14–S17] | ドキュメント参照のみ |

**ソース由来の限界:** Better Authのorigin-checkコードには後方互換のskip挙動がある。したがって「ライブラリを入れたから必ずCSRF安全」とは断定しない。OpenClawはGateway/Operator設計の参考であって、Taskごとの`tenant_id`隔離を証明する資料ではない。PlaywrightのBrowserContextを作っただけで保管済みstorageStateのcross-tenant混入を防げるわけでもない。上表の確認はコード読解であり本プロジェクトへの組み込みテストは未実施。

### 2.1 代替案の比較
| 論点 | 採用候補 | 却下/保留 | 判断の根拠・改訂条件 |
|---|---|---|---|
| セッション | 失効可能なDB backed sessionを`AuthProvider`で隠蔽。Better Authが第一候補 | 署名済み長寿命JWTのみでstateless認可 | logout/漏えい/利用停止直後の強制失効が難しくなる。短命JWT + revocation registryは代替可能だが初期複雑性が増す。DB負荷は測定する |
| 初期管理者登録 | ローカルoperatorが初期登録、ネットワークからの任意bootstrapを禁止 | Web公開画面に「最初の訪問者がadmin」ボタン | 不特定の最初のアクセスに奪取され得る。明示的operator操作のみ [PROVISIONAL: CHAT_10] |
| 隔離 | appのowner付きDB query + scoped storage + RLS(適用可能なら) | Browser/UIのみで隠す、ID推測困難性だけで守る、共通Bucketを公開する | BOLAを防げない。RLSの所有ロール/connection poolingは別途検証 [S16] |
| 多人数 | 初期でも個別User/tenant; sharing未実装でdeny | 固定`singleton_owner`をIDにせず省略 | 将来migration時に過去のBrowser/Cookie/Watch所有者が不明になる |
| 前段認証 | アプリセッションを必須にし、reverse proxy認証は追加防御 | 信頼未検証の`X-User`/`X-Forwarded-*`だけでログイン | 直接アクセスやヘッダー偽装に脆弱。proxy側で消去・上書き/送信元IP固定する場合に限定して再検討 [S7] |
| 組織機能 | 後日ADR/新しいRole&membership contract | 初日から任意のユーザー間共有を実装 | 認可行列・監査・データ漏洩の検証範囲が急増する |

## 3. 依存関係・正本リンク・所有権

~~~text
[Browser UI] --cookie+CSRF--> [Auth adapter] --validated principal--> [API authorization]
[Telegram webhook] --verified channel binding---------------------------^
[API] --authorized task/owner immutable lineage--> [PostgreSQL canonical resource owners]
[Queue] --taskId/runId ONLY--> [Worker rehydrate owner from DB] --authorize--> [Tool Gateway]
[Tool Gateway] --approved capability--> [Browser Worker / MCP / HTTP]
[Browser Worker] --owner-scoped lease--> [Context/storageState scoped to user]
[Events/Artifacts] --per-event/per-object authorization--> [UI/SSE/Downloads]
~~~

- **CHAT_09の正本**: `AuthProvider`/server-only principal取得・信頼境界・所有者認可・セッション失効とCSRF要件。このMDは提案段階。
- **[PROVISIONAL: CHAT_01]** User ID/tenant ID、Resource Owner、principalを受ける契約、Event envelopeのaudience/owner、全体Errorの語彙。ここで列記したTS型は**認証モジュール内部の候補**であり共有型の最終正本でない。
- **[PROVISIONAL: CHAT_03]** Approvalの申請者/決定者の照合、Tool Callのowner-binding、Side-effect ledger。所有者をモデルに承認させない。
- **[PROVISIONAL: CHAT_04]** BrowserSessionとCookie storageStateのowner、handoffの一時grantと排他ロック。BrowserContextのsession IDは秘密ではなく、認可の代わりにならない。
- **[PROVISIONAL: CHAT_07]** Watch/notificationのowner、通知先との連結と権限失効時の凍結。
- **[PROVISIONAL: CHAT_08]** Login/logout、全REST routeの認可、SSE re-auth、CSRF、Telegram account binding。
- **[PROVISIONAL: CHAT_10]** users/sessions/owner FK/tenant_id、RLS/DDL、暗号鍵の管理、migration/bootstrap/backup。
- CHAT_02はRun/queueと再開時の再認可、CHAT_11は横断E2Eの正本を保持する。

## 4. Requirements — 検証可能なMUST/SHOULD

| ID | 優先 | 要件と可観測な結果 | SC |
|---|---|---|---|
| AUTH-001 | MUST | 空のDBでnetwork経由の登録/匿名session発行不可。local operatorのみが初回ownerを作成できる | 08 |
| AUTH-002 | MUST | 初期1名でも全私的API/UIの各リクエストを認証必須、未認証401。health endpointsは秘密情報無し | 06,08 |
| AUTH-003 | MUST | 失効可能な不透明sessionでlogout/expiry/disable後の新規要求を拒否、session IDをURL/ログ/LLMに出さない | 06,07,08 |
| AUTH-004 | MUST | Cookieに`HttpOnly; Secure; SameSite`、host-only、path固定を設定。TLSを満たさぬ本番起動は拒否 | 08 |
| AUTH-005 | MUST | Cookieを用いる全state-changing操作はOriginの厳格allowlistとCSRF検証。unsafe GETなし、例外は明示監査 | 08 |
| AUTH-006 | MUST | ログインとrefresh/パスワード変更にIP＋アカウント単位の試行制限を設ける。応答からアカウント存在を漏らさない | 08 |
| AUTH-007 | MUST | URL/body/header/query/LLMが指定したownerIdよりもサーバーで復元したprincipal/DB ownerが常に優先 | 06,08 |
| AUTH-008 | MUST | Task/Run/Conversation/Watch/Artifact/Evidence/Memory/ユーザー作成Skill/BrowserSession/Connector/Approvalにowner/tenantベースの認可を強制し、越境アクセスを拒否。共有システムSkillはoperator管理とし一般ユーザーによる編集は禁止 | 02,06,08 |
| AUTH-009 | MUST | list/search/export/count/stream/notification/thumbnail/attachment/cacheも所有者で絞り、内容・件数・存在を漏らさない | 06,08 |
| AUTH-010 | MUST | Workerはqueue payloadを権限として信頼せず、job再開と外部操作直前にDBのresource ownerと実行権限を再確認 | 07,08 |
| AUTH-011 | MUST | ユーザー/tenantを跨ぐBrowserContext、storageState、Cookie、handoff leaseの再利用不可。所有者照合前に開かない | 06,08 |
| AUTH-012 | MUST | Handoff/Takeover tokenは利用者/BrowserSession/Task/audience/期限に束縛し、単回または失効可能。LLM/MCPへ渡さない | 06,08 |
| AUTH-013 | MUST | SSE/WS handshakeと購読先を認可し、配信直前にもaudience確認。失効/ユーザー変更時は購読解除する | 06,07,08 |
| AUTH-014 | MUST | Approval決定・予約/購入/投稿確定は、同一ownerかつfresh authとCHAT_03の承認ゲートで再評価 | 04,07,08 |
| AUTH-015 | MUST | Telegram webhookは署名/秘密や送信元検証、確認済みaccount binding、本人の権限照合を通す | 02,08 |
| AUTH-016 | MUST | owner/tenant変更、user disable、session revokeは監査記録へ。password/cookie/session/token/PIIはマスク | 08 |
| AUTH-017 | MUST | DB/cache/キュー/artefact keyは所有者を含み、回復・重複delivery・cancelでもowner lineageが変わらない | 06,07,08 |
| AUTH-018 | MUST | セッション/権限/DBが不明・期限切れ・照合失敗ならdeny。外部作用の再試行は自動継続しない | 07,08 |
| AUTH-019 | MUST | 本番/テストで認証無効化スイッチ`none`、全Origin wildcard、認可skipを使えない。診断・管理エンドポイントも同様 | 08 |
| AUTH-020 | MUST | ルートごとにauthorization mapを明示し、未分類ルート・未分類Worker tool・未分類イベントはdefault deny | 06,08 |
| AUTH-021 | MUST | 認証状態とドメイン所有者を分離し、単一ユーザーから追加ユーザーへ移行しても既存データを他者に見せない | 06,07,08 |
| AUTH-022 | MUST | bootstrap、proxy、session、cookie、origin、owner、handoff、logout、SSEに対するsecurity negative fixtureを用意 | 06,07,08 |
| AUTH-023 | MUST | 認証関連処理は有限のtimeout、bounded connections、session revalidation、rate limitingを持ち、無制限再試行しない | 07,08,09 |
| AUTH-024 | SHOULD | 高危険操作/資格情報・通知先変更で再認証し、可能ならWebAuthn/PasskeyまたはMFAを備える | 08 |
| AUTH-025 | SHOULD | PostgresでRLSと専用最小権限DB roleを追加し、アプリ認可の防御層を増やす | 06,08 |
| AUTH-026 | LATER | 招待、他者との共有、Org RoleとResource ACLを独立ADRのもとで実装する | 06 |
| AUTH-027 | EXPERIMENT | Better Auth adapterを導入しDBセッション失効、CSRF/Origin、proxy/TLS、Fastify/Next.js共存を試験 | 08 |

## 5. 内部契約候補・API・入力検証

> ここにある型はSecurity内部の設計候補。共有のTask/Run/Event型を再定義せず、正本が確定すれば置換する。[PROVISIONAL: CHAT_01, CHAT_08, CHAT_10]

~~~ts
// security/auth/AuthProvider.ts — SDK依存を隔離する唯一の入口
interface AuthProvider {
  authenticateHttp(request: SanitizedHttpRequest): Promise<AuthenticatedPrincipal | null>;
  revokeSession(sessionHandle: ServerOnlySessionHandle): Promise<void>;
  revokeAllUserSessions(userId: string): Promise<void>;
  requireFresh(principal: AuthenticatedPrincipal, maxAgeSeconds: number): Promise<void>;
}

// API/UI/Workerの境界を越える際には raw cookie/tokenを渡さず、ownerを再取得する。
type AuthenticatedPrincipal = Readonly<{
  userId: string; tenantId: string; sessionId: string; issuedAt: string;
  authTime: string; role: "owner" | "member"; identityVersion: number;
}>;
type ResourceRef = Readonly<{ kind: string; id: string }>;
type Action =
  | "read" | "create" | "modify" | "cancel" | "subscribe"
  | "download" | "handoff" | "approve" | "manage_credentials";
interface AuthorizationService {
  requireAccess(p: AuthenticatedPrincipal, action: Action, resource: ResourceRef):
    Promise<AuthorizedResource>;
  // AuthorizedResourceはsecretやprovider tokenを含めず、特定処理内だけ有効
}

// Queue上のUserIDは認証証明ではない。
// jobには識別子/実行世代のみを載せ、DBからrun->task->ownerをロードする。
type JobPointer = Readonly<{ runId: string; taskId: string; generation: number }>;
~~~

**入力検証**: IDはcanonical UUID/length制約を確認。UserID/tenantIDはclientから入力を受けない。URLの`/tasks/:id`を読み取るのは単なる識別のためで、問合せは必ず`WHERE id = :id AND tenant_id = :principalTenant`等で行う。Bearer token、session cookie、CSRF token、Telegram secretはLLM引数へ直列化しない。危険なURLトークンはRefererやアクセスログへ漏れるため、handoffは原則`POST`で短命ticketを交換し、query tokenを避ける。JSON schema, body size, content-type, CORS allowlistはCHAT_08のAPI Gatewayで統制する。

**Auth/sessionの推奨値（設計初期値、実測済み基準ではない）**:
- Cookie名`__Host-agent_session`（認証ライブラリが同等のhost-only Cookieを提供できるかスパイク確認）、本番TLS上で`Secure`、`HttpOnly`、`SameSite=Lax`、`Path=/`、`Domain`なし。必要なら`Strict`を比較試験。
- Session idle timeout **12時間**、absolute lifetime **7日**、security-sensitive action re-auth maxAge **5分**。`authTime`は単なるrefresh時刻へ更新しない。曖昧なidle更新方式は製品実装時に確定する。
- Login: username/subjectとIPの複合単位で**5失敗/15分**を初期値とし、分散構成で共有・上限のあるストアを使う。攻撃元IPがproxy spoofで変わらないことを確認。ユーザー列挙防止の同形エラーとバックオフを併用。
- Session cacheは初期オフまたはstrict bounded（最大30秒を仮の試験値）で、logout/disable/revoke時には即時無効化。**副作用・承認はDB authoritative checkを毎回要求**。障害時にキャッシュだけで承認しない。
- UI/API originは明示的`APP_PUBLIC_ORIGIN`の完全なscheme+host+port一致。wildcardや部分文字列一致は禁止。HTTPS/TLS終端・プロキシ設定はCHAT_10が実体を所有。
- 以上の数値は`[PROVISIONAL: CHAT_00/08/10]`であり、費用/安全性/利便性の評価後にADR化する。標準が値を保証しているという主張ではない。

### 5.1 HTTP & stream候補 [PROVISIONAL: CHAT_08]

| Route/operation | 認証・保護 | 正常/失敗 |
|---|---|---|
| `POST /api/auth/login` | CSRF/login-CSRF + rate limit、local bootstrap時以外は登録不可 | 成功時Cookie再発行/rotation、一般化した401/429 |
| `POST /api/auth/logout` | session + CSRF; session revoke; active subscriptions close | 204; 新リクエスト401 |
| `GET /api/auth/me` | session必須・`Cache-Control: no-store` | principal public projectionのみ |
| `POST /api/auth/reauth` | 同一userで再認証、危険操作前のみ | `authTime`更新; raw token返さない |
| `GET /api/tasks` / `GET /api/watch` | authenticated principal + tenant-scoped list/pagination | 自分のものだけ |
| `GET /api/tasks/:id` / `GET /api/artifacts/:id` | resource-level owner check | 別userのIDは404（存在非開示） |
| `GET /api/events?taskId=...` | handshake + task owner + each event audience + revoked session検出 | 401/404/close |
| `POST /api/browser/:id/handoff` | owner、fresh auth、Browser Worker lease、one-time ticket | user-only手動操作 |
| `POST /api/approvals/:id/resolve` | owner、fresh auth、CHAT_03 ledger gate、single-use | 二重承認/失効拒否 |
| Telegram inbound command | verified webhook+binding+owner membership（Cookieではない） | account未連結拒否 |

Better Authを使う場合`/api/auth/*`の実際のroute/method、Cookie名、Hook、セッションのDB構造をアダプターで調整し、上表の仮ルートをそのまま重複実装しない。GETは副作用を起こさない。CORS `Access-Control-Allow-Credentials`と`*`の併用は禁止。認証不要なhealth/versionは限定されたallowlistのみ公開し、設定・User数・秘密・DB状態を返さない。

## 6. 状態遷移・シーケンス・ライフサイクル

### 6.1 Installation/bootstrap
~~~text
EMPTY -> OPERATOR_BOOTSTRAP_PENDING
  -> (local authenticated administrative setup; uniqueness transaction)
ACTIVE_SINGLE_OWNER -> (future migration by operator + ADR) MULTI_USER
  -> (user disabled / session revoked) ACCESS_DENIED
~~~
- ネットワークの`GET /setup`が未認証のまま「最初の管理者」作成を許す設計は禁止。
- ローカルoperatorが標準入力等の安全な経路で初期資格情報を登録する。これはホスト管理者の手動オペレーションであり、Agent/ToolにShell権限を与えるものではない。
- 1つのinstallationに初期ownerは1名。重複起動のbootstrapはDB unique constraintとtransactionで片方のみ成功。初期ユーザー/tenantを生成し、バックアップ復元時には重複登録を避ける。

### 6.2 BrowserログインとTask操作
~~~text
Browser -> TLS reverse proxy -> API
  [strict origin / rate limit for login] -> AuthProvider.verifyCredentials()
  -> rotate session ID -> Set-Cookie
  Browser -> API /tasks/:id [Cookie + Origin/CSRF when mutation]
  API -> AuthProvider.authenticateHttp -> principal
  API -> resource repository: SELECT ... WHERE tenantId = principal.tenantId
  API -> AuthorizationService.requireAccess(principal, action, resource)
  API -> canonical task manager / Tool Gateway (owner immutable)
  API -> output projection scoped to same principal
~~~

### 6.3 人間ブラウザTakeover（SC-06）
1. 認証済みブラウザ利用者がTaskの所有者として`handoff`を要求。CHAT_04のBrowser WorkerはBrowserSession所有者をDBから照合し、既存lease/current generationを確認する。
2. fresh auth + action許可を再確認。短命・audience bindingのone-time ticketを発行し、server-sideにhashを保存する。**Cookieや外部サイトのstorageStateは本人のUI/LLMへexportしない**。
3. 同一BrowserSession上の人間操作leaseとAgent操作leaseは排他。Human Handoff中のAgent actionは待機/拒否され、steerやcancelは安全境界で反映。
4. ticket利用時には`userId/tenantId/sessionId/browserSessionId/taskId/expiry/nonce`を全て照合し、atomic consume。Redis/DBで重複利用を拒否。ブラウザから危険な画面へ遷移しても勝手に予約・決済を確定しない。
5. logout、session revoke、Task cancel、user disable、lease timeout、Worker crashでticket/leaseを失効、ブラウザコンテキスト所有は変化しない。復帰前に再度所有者照合。

### 6.4 Worker/recovery（SC-07）
- Queue recordは`taskId/runId/generation`のみを識別子として持つ。ユーザーを名乗るjob metadataを認証せず、Run→Task→OwnerのDB正本からcurrent identity versionと権限を再評価する。
- 再開時、leaseの`generation`とRun stateをcompare-and-swapして同時実行の古いWorkerを弾く。先行Tool intentのcommit状態はCHAT_03のside-effect ledgerを参照する。
- Session logoutは当該端末の新しいUI操作・SSE/handoffを停止するが、既にownerが依頼し保存した正常なバックグラウンド調査の継続を**直ちに一律停止するわけではない**。User disable/tenant revoke/Watch disable/Task cancelは別のserver-side authorization versionを更新し、新たな外部Toolの前に必ず停止判定する。
- Worker crash後にowner情報が欠落/不整合であれば`Needs operator attention`/`blocked`として安全停止。現在のTaskやapprovalのownerを書き換えて復旧しない。

### 6.5 状態間の禁止
`ANONYMOUS -> PRIVATE_RESOURCE`、`USER_A -> USER_B_RESOURCE`、`EXPIRED_SESSION -> APPROVAL`、`REVOKED_TICKET -> HANDOFF`、`STALE_WORKER -> TOOL_EXECUTION`はいずれも遷移不可。

## 7. 実装アルゴリズム・疑似コード

~~~ts
async function authenticateAndAuthorizeHttp(req, action, resourceRef) {
  // Server-only; no principal is accepted from request JSON, URL or untrusted headers.
  if (isUnsafeMethod(req.method)) requireValidOriginAndCsrf(req);
  const principal = await authProvider.authenticateHttp(req);
  if (!principal) throw Unauthorized401();
  if (await isUserDisabledOrVersionStale(principal)) throw Unauthorized401();
  return authorization.requireAccess(principal, action, resourceRef);
}

async function requireAccess(principal, action, ref) {
  if (!KNOWN_ACTIONS.has(action) || !KNOWN_RESOURCE_KINDS.has(ref.kind)) throw Denied();
  const resource = await resourceRepo.findByIdAndTenant(ref.kind, ref.id, principal.tenantId);
  if (!resource || resource.ownerId !== principal.userId) throw NotFound404();
  if (!policyFor(ref.kind).permits(principal.role, action)) throw Forbidden403();
  if (action === "approve" || action === "manage_credentials" || action === "handoff")
    await authProvider.requireFresh(principal, MAX_FRESH_AUTH_SECONDS);
  return projectAuthorizedResource(resource); // Never project cookie/secret.
}

async function workerStep(jobPointer) {
  const run = await runRepo.load(jobPointer.runId);
  const task = await taskRepo.load(run.taskId);
  if (task.id !== jobPointer.taskId || run.generation !== jobPointer.generation)
    throw StaleWorker();
  if (run.ownerId !== task.ownerId || run.tenantId !== task.tenantId)
    throw OwnerIntegrityViolation();
  if (task.isCancelled || task.ownerDisabled || run.isRevoked) throw StopWithoutTool();
  // Internal authority is an immutable runtime-owned persisted intent, not caller-provided userId.
  await gateway.ensureOwnerBoundIntent(task, run, jobPointer.generation);
  // Delegates tool approvals, budget, retry ledger to CHAT_03.
  return gateway.executeAuthorizedNextStep();
}

async function subscribeEvents(principal, requestedTaskId, stream) {
  await requireAccess(principal, "subscribe", {kind:"task", id:requestedTaskId});
  for await (const event of stream.forTask(requestedTaskId)) {
    if (stream.closed || event.taskId !== requestedTaskId) break;
    if (!await authProvider.isSessionStillValid(principal.sessionId)) break;
    if (!await authorization.eventAudienceIncludes(principal, event)) break;
    await stream.send(redactedProjection(event));
  }
  await stream.close();
}
~~~

**注意**: 上の`isSessionStillValid`、`eventAudienceIncludes`、`requireFresh`などは**新規提案のインターフェース**であり、Better Authに同名APIが存在するとは主張しない。実装はheartbeat上限とlive revocation signalを組み合わせ、イベントがないときにも閉塞できる。DB poolのtransaction contextを跨いでowner検証結果を持ち回らず、保護対象のread/writeを同一scopeで行う。

### 7.1 競合・race防止
- **TOCTOU**: approval後、execute直前に対象リソースのowner/version/stateを再評価。ownerが変化していたらfail closed。所有者変更を暗黙に許可しない。
- **同時ログイン/ログアウト**: 旧session revokeとcookie rotationは原子的。旧Cookieの照合を遅延cacheで成功させない。再配信されたlogoutは冪等。
- **Worker同時取得**: lease generation fencingとDB uniquenessによりcross-run context混同を防止。副作用のexactly-onceは保証しない。
- **SSE race**: stream開始時だけでなく各eventのaudienceとperiodic revocationをチェック。revocation notificationが失われても有界な再検証で切断。
- **Browser crash**: Context再生成時にstorageStateを`tenantId/ownerId/browserSessionId`に紐づく暗号化ストアから取り出し、すべて照合した後にloadする。未知ownerのprofileをdefault ownerにfallbackしない。
- **Steer**: 条件変更は同一ownerのTask上でのみ受理。payload内の他の`tenantId`が効力を持たない。
- **Cost**: username/subjectとIPの認証試行制限に加え、Task/LLM/tool budgetはCHAT_02/03が正本。認証失敗だけでLLMを起動しない。

## 8. 障害・悪用のマトリクス

| 条件 | 検出 | 再試行/復元 | 利用者に見える状態 |
|---|---|---|---|
| Cookie無し/改ざん/expired | AuthProvider拒否 | 新しいログインのみ | 401; login required |
| DB session store停止 | authoritative session query失敗 | bounded DB retry、fail closed | 503（秘密非開示） |
| Login brute-force | rate limit bucket | 有限の待機後のみ | 429; Retry-After、存在非開示 |
| Origin/CSRF不一致 | allowlist/token比較 | 自動変異再送なし | 403; セキュリティ監査 |
| 他ユーザーのresource ID | DB owner照合不一致 | 再試行不可 | 404; 件数/詳細は非公開 |
| session revoke中のSSE | revocation signal + heartbeat | 再ログインして再購読 | stream close |
| takeover ticketの再利用 | atomic consume failure | 新ticket+fresh auth | 403/expired |
| owner停止後のqueued job | identityVersion/version検出 | Tool開始せず保留/停止 | blocked; review |
| Recovery時にRun owner不一致 | integrity invariant | 手動調査。別ownerへ自動結合禁止 | Needs operator attention |
| Telegram chat未登録/送信元不明 | webhook & binding不一致 | 人間がUIで再バインド | ignored/denied |
| Proxy spoofed headers | trusted proxy設定不一致 | 拒否、proxy誤設定を診断 | 403/503 |
| 認可未実装の新API | route manifest欠落 | 閉じる。レビュー後開放 | 404/deny |

## 9. Security / Privacy / Trust Boundary

- **Creds**: password, session token, cookie, provider credential、Browser storageState、CSRF tokenはclient UI/LLM prompt/Tool args/Event/Traceへ出さない。HTTPS送受信時にもアプリロギングでheader/bodyをdrop/redact。保存鍵、ローテーション、再暗号化は`secrets-and-credentials.md`で確定する。
- **CSRF**: SameSiteだけを防御の正本にしない。unsafe methodはOriginを厳密比較、session-bound CSRF tokenか検証可能なnon-simple request防御を実装。欠落/`null` Originを無条件で許さない（正規のフォームに必要なら独立したCSRF tokenと明示例外）。login CSRFにも対処。Better Authのskipオプションはproduction policyで禁止。
- **Session fixation**: ログイン/権限変更でセッションを再生成、logoutで失効。古いrefreshや別account cookie混入を拒否。Cookieはhost-onlyでURL query/Refererに流さない。
- **Authorization/BOLA**: HTTPルートだけでなく、`GET /artifact/raw`、画像proxy、partial output、検索索引、件数、通知履歴、Browser video/trace、memory retrieval、ダウンロード署名URLの発行にもowner check。
- **WS/SSE**: WS採用時Originをhandshakeで検証、cookie sessionを照合。接続後すべてのcommand/subscriptionでもresource level auth。SSEは最後のEvent IDが他ownerや別taskからのcursorなら拒否。イベントpayloadにownerの秘密を載せない。
- **Telegram**: Telegram usernameは不変のIDではない。Webhook secret/verify済み数値user/chat IDとWeb UIで確認済みbindingを使い、チャット参加・転送されたメッセージでは所有者を推測しない。shared group/bot送信の境界はCHAT_08が定義する。
- **Prompt injection**: external HTML/MCP instructionから`principal`/owner/role/headersを受け取らない。Tool Gateway側で実際のcapabilityとapprovalを検証（CHAT_03、prompt-injection-defense）。
- **No host-admin**: 許可済みユーザーでもshell/SSH/Docker Socket、privileged container、内部LAN無制限アクセスをAgentへ委譲しない。
- **Logging**: allowed/denied、action種別、request correlation ID、resource kind、ownerの非復元可能な監査用参照、decision reasonを構造化記録。password, rawCookie, CSRF, third-party auth, full page URL query, private contentは記録禁止。
- **Reverse proxy**: TLS終端の保護、direct API公開不可、trusted proxy IPの固定、`X-Forwarded-Host/Proto/For`はtrusted proxyで上書きされた値のみ使用。自身を正当なproxyと誤認するloopback/ローカル転送についても負のテストを作る。
- **Data residency**: 永続データの削除/retention、バックアップの隔離、エクスポートは`privacy-audit-and-retention.md`に委譲。session/access revokeと証拠データの削除は異なる操作。

## 10. 計測/Trace/性能・負荷試験

- 指標: auth success/failure/429、CSRF rejects、cross-owner rejects（機微IDをラベルにしない）、session lookup p50/p95/p99、resource authorization latency、owner mismatch fault、SSE logout→close遅延、Worker revalidation時間、login store容量。
- **提案された暫定SLO**: revocation後新しいprotected HTTP operationは100%拒否、SSEはrevokeイベント未着でも最大30秒の再検証窓で停止、workerは**各外部Tool intent直前**にrevalidate。数値は要件のテスト閾値であって実測値ではない。
- 仕様評価条件: 1ユーザー/2ユーザー、TLS proxyあり、APIとWorkerを再起動、各ユーザー同時に10 Task・Watch・BrowserSession、SSE 5本、数千artifactをfixtureで作り、有効/失効sessionを混在させる。現時点の実測性能値はなし。
- 予算/容量: ログイン試行・open SSE・Browser takeover・auth DB queryに有限のconfigurable limitを設定し、未指定/0を無制限扱いしない。具体値とリソース測定はCHAT_10/11と合意。

## 11. 永続化/Transaction/Migration提案 [PROVISIONAL: CHAT_10]

物理DDLはCHAT_10が所有し、本節は**必要な不変条件**のみ提示する。

1. `users`に安定ID/status/identityVersion、`tenants`相当のidentity namespace、`sessions`にuser FK/expiry/revoke/authTime/rotation、`login_attempts`相当の制限ストア、`channel_bindings`にTelegram IDとownerを紐づける。
2. Task/Run/Conversation/Watch/BrowserSession/Artifact/Evidence/Approval/Connector/Memory/notification/outboxには**直接または参照経由で必ず単一のownerを確定できる**不変条件を持たせる。大きな結合経由だけで保証するのではなく、高リスクデータはownerを冗長に持つ選択肢を検証する。`tenant_id NOT NULL`等の物理定義はCHAT_10と整合。
3. 初期セットアップはoperatorトランザクションでowner userとtenantを生成。既存データのない新規インストールを前提とするが、将来バックアップや過去バージョンからのmigrationでは所有者のない行を**default ownerへ黙って割当てない**。quarantineして手動照合。
4. RLS案: user data tableは必要に応じ`ENABLE ROW LEVEL SECURITY`と`FORCE ROW LEVEL SECURITY`を検討、DBアプリロールは`BYPASSRLS`不可、postgres superuserをアプリに使わない。transaction単位でset local owner contextし、pool跨ぎ漏れを試験。RLSは`SET`可能なGUCをDB侵入者が変更し得るため、単独で完全分離の証拠にならない。ownerとアプリ認可が第一層。
5. sessionの失効はDB commitを境に新規Tool/UI operationへ適用。revocation event/outboxを使用し、実イベント喪失を補う定期再検証を設ける。`identityVersion`の増分で全session/Worker grant invalidation。
6. backup restore後のuser/tenant UUID、旧Cookie、BrowserContext保管場所の整合をチェック。cookie/credentialの復号不可時は「空のものとして継続」せず明示的な再認証が必要。

## 12. UI/外部APIへの影響 [PROVISIONAL: CHAT_08]

ログイン、セッション一覧と失効、本人確認/再認証、Task一覧/Watch一覧に各自のデータのみ、Browser Handoffの本人承認UI、Telegram account bindingの確認/解除画面、期限切れ時のsafe redirectが必要。frontendのroute guardだけに頼らず、SSR/Server Action/REST/SSE/downloadの各server requestでも検証する。本人がログアウトしても保存されたWatchが勝手に削除されるわけではない。user disableとWatch freezeは別操作。401は再ログイン、404は「対象を表示できません」、403は権限不足、429は試行制限、503は保管先障害として明瞭に出す。セキュリティエラーに他人のIDやCookieを表示しない。

## 13. 推奨実装フェーズ（別実装repoでのPR分割）

1. **SEC-AUTH-PR1:** AuthProvider POC、DB session revoke、TLS/cookie/Origin/CSRF、登録bootstrap、login tests。Better Auth 1.7.14の採用可否を確定。
2. **SEC-AUTH-PR2:** 全Resource ownership query/authorization middleware、route manifest default deny、security context propagation、RLS/DB migrationのCHAT_10連携。
3. **SEC-AUTH-PR3:** Worker/run/watch/connector owner lineage、identityVersion、restart/revoke/fencing、Tool Gateway/side-effect ledgerとの接続。
4. **SEC-AUTH-PR4:** BrowserContext + storageState/handoff lease、SSE/WS、Telegram account binding、Credential denial negative cases。
5. **SEC-AUTH-PR5:** adversarial fixtures、各プロセスKill test、2-user E2E、負荷/監査/段階的ロールアウトとbackward compatibility。

本仕様repoでの変更はこの1MDのみ。共有型・DDL・UI文書は各ownerに提案し、横断レビュアーが承認するまではDraftのまま。

## 14. テスト仕様（**全件設計のみ・未実施**）

| Test ID / 種類 | Setup / action | Expected (機械判定可能) | 関連REQ |
|---|---|---|---|
| AUTH-T01 / Integration | fresh DBでInternet側から`signup`/bootstrap試行、同時2回bootstrap | ネット経由は失敗。operatorのみ1 owner登録、重複0 | 001,019,021 |
| AUTH-T02 / Contract | CookieなしでTask/Watch/Artifact/Approval/Debug access | 401; private byte 0、未分類route deny | 002,019,020 |
| AUTH-T03 / Integration | login→logout→旧cookieでmutation / new SSE | 旧tokenでの新規UI操作/Tool起動要求は401。既存の承認済みバックグラウンドRunは別途owner状態で判定 | 003,017 |
| AUTH-T04 / Unit | Cookie flags/TLS無し起動/Domain指定 | 本番安全設定以外拒否、host-only属性検証 | 004 |
| AUTH-T05 / Security | forged Origin/Referer、`Origin:null`、cross-site form、CSRF missing、GET mutation | 403、DB side effects 0。例外以外にunsafe GET無し | 005,019 |
| AUTH-T06 / Security | 5+ bad login/15分、2アカウントの有無比較、proxy spoofed XFF | bounded 429、外からの存在推測不可、spoofで回避不可 | 006,023 |
| AUTH-T07 / Integration | User A/BでURL ID交換、bodyにAを名乗るownerId/ヘッダー注入 | BにAのbyte・存在情報が出ない、Aだけ成功 | 007,008 |
| AUTH-T08 / E2E | A/BでTask Run Conversation Watch Artifact Evidence Memory/Skills/Connector交差取得 | 全種別でBにはAのIDが404/deny; collectionは0件 | 008,009,020 |
| AUTH-T09 / Contract | Cursorとcount、全文検索、CDN/サムネイル、ダウンロード署名発行にAのID | BはAの件数、内容、URL、cursorを得られない | 009,017 |
| AUTH-T10 / Fault | queueに`ownerId=B`を偽装、kill worker、run generation更新 | DBのA owner保持、古いjob/tool起動0 | 007,010,017,018 |
| AUTH-T11 / E2E | AがログインしたBrowserContext storageStateをBのTaskでload試行 | refuse; BにA cookie/storageState byte 0 | 011 |
| AUTH-T12 / Fault | Handoff one-time ticket 2回同時consume、logout後再使用 | exactly one consume、以降403、Agent同時操作0 | 011,012,023 |
| AUTH-T13 / Integration | AのTask SSEにBでsubscribe、revoked session、改ざんLast-Event-ID | 他owner event 0、revoke後有界時間でclose | 009,013 |
| AUTH-T14 / Security | forged approval body、別user、期限切れfresh auth、二重決済意図 | accepted approval 0、Tool Gatewayの副作用0 | 014,018 |
| AUTH-T15 / Integration | Telegram webhook偽造、他user chat ID偽装、unbound user、group forward | Task実行/状態取得/Watch変更 0 | 015,020 |
| AUTH-T16 / Unit | logging under successful/failed auth、exception dump、trace exporter | ログにtoken/cookie/password/full payload 0、監査decisionあり | 003,016 |
| AUTH-T17 / Fault | user disabled時にWorkerをkill/requeue、Watch outbox再配信 | revoke後新規Tool 0、古ownerから新ownerへ移動0 | 010,017,018 |
| AUTH-T18 / Negative | DB auth store failure・unknown owner・incomplete migration | deny / 503、旧ownerへのfallback 0 | 017,018,021 |
| AUTH-T19 / Route scan | 全REST/Server Action/SSE/WS routeにauth manifest照合 | unclassified route 0; もしあればCI失敗 | 002,019,020,022 |
| AUTH-T20 / Concurrency | 2 user×10 Tasks, 5 SSE, 10 Contexts, 1000 artifactsで並列 | owner cross-leak 0、上限到達時bounded deny | 008,009,017,023 |
| AUTH-T21 / DB | RLS適用時にpool reuse、set local漏れ、app roleでBの行をSELECT | cross-owner rows 0; bypass roleなし | 025 |
| AUTH-T22 / Compatibility | AuthProvider POCをFastify/Next.js reverse proxy下で実行 | login/CSRF/logout/revoke/freshの同一契約に合格 | 027,003,005 |
| AUTH-T23 / Adversarial | Webページ/MCP応答が`userId=admin`と指示/ヘッダーを埋め込む | server principal変更0、秘密出力0、auditある | 007,008,018 |
| AUTH-T24 / Browser recovery | A/B storageStateを保管後Browser Worker強制kill→restore | 正しいownerのみ復号/restore、旧lease無効 | 011,012,017 |
| AUTH-T25 / Session refresh | logoutと並列のauth cache hit、user disableと承認要求をrace | disable commit後protected action 0、承認0 | 003,014,018 |
| AUTH-T26 / Backward migration | 旧rowがowner nullable、duplicate user初期化、backup restore | quarantineし本人への自動帰属0、migration fail-visible | 017,021 |
| AUTH-T27 / WS | origin不正・Cookieなしupgrade、接続後別Taskにsubscribe | deny/close、他owner event 0 | 002,005,013 |
| AUTH-T28 / Fresh auth | session自体は有効だが`authTime`が閾値超過 | 普通のread可/approval等は再認証要求 | 014,024 |
| AUTH-T29 / audit | CSRF deny/owner mismatch/disable/session revoke | secretを含まないcorrelation audit全件あり | 016,022 |
| AUTH-T30 / Policy | Web UIが匿名モードや`disableCSRFCheck`を要求する設定で起動 | config validationでfail startup | 005,019 |

全fixtureは専用の仮アカウント/ローカルWeb/MCP・仮の決済/予約executorのみ使用し、実サイトへ購入/投稿/予約を送信しない。実行したテスト: **なし**。OSS上流のテストソースは読んだが、実行・統合成否は未確認。

## 15. Acceptance / Requirement → Test → SC Traceability

| REQ | テストID | SC | 完了条件 |
|---|---|---|---|
| 001 | T01 | 08 | local operator以外が初期ownerを作れない |
| 002 | T02,T19,T27 | 06,08 | 全私的入口は認証必須 |
| 003 | T03,T16,T22,T25 | 06,07,08 | 失効後の新規権限獲得0 |
| 004 | T04 | 08 | Cookie/TLS属性の負の試験成功 |
| 005 | T05,T22,T27,T30 | 08 | CSRF bypass経由DB mutation 0 |
| 006 | T06 | 08 | 試行制限を回避できない |
| 007 | T07,T10,T23 | 06,08 | client/queue由来owner spoof成功0 |
| 008 | T07,T08,T20,T23 | 02,06,08 | resource越境読書き0 |
| 009 | T08,T09,T13,T20 | 06,08 | 列挙/stream/派生資料の越境0 |
| 010 | T10,T17 | 07,08 | stale/無権限WorkerのTool起動0 |
| 011 | T11,T12,T24 | 06,08 | ownerを跨ぐContext/cookie reuse 0 |
| 012 | T12,T24 | 06,08 | ticket replay/lease競合0 |
| 013 | T13,T27 | 06,07,08 | 他人のイベント0、失効後切断 |
| 014 | T14,T25,T28 | 04,07,08 | stale/他人のapproval受理0 |
| 015 | T15 | 02,08 | 未binding inboundを実行しない |
| 016 | T16,T29 | 08 | 必要なdecision audit、秘密ログ0 |
| 017 | T10,T17,T18,T20,T24,T26 | 06,07,08 | reboot/migration後もowner固定 |
| 018 | T10,T14,T18,T23,T25 | 07,08 | あいまい状態のfail-closed |
| 019 | T01,T02,T05,T19,T30 | 08 | auth bypass設定0 |
| 020 | T02,T08,T15,T19 | 06,08 | 未分類operationはdeny |
| 021 | T01,T18,T26 | 06,07,08 | 既存データowner一意で移行 |
| 022 | T19,T29,T30 | 06,07,08 | security negative CI fixture設計 |
| 023 | T06,T12,T20 | 07,08,09 | limit/timeoutsで無制限仕事がない |
| 024 (SHOULD) | T28 | 08 | 重要操作のfresh auth |
| 025 (SHOULD) | T21 | 06,08 | RLS policy leak 0 |
| 026 (LATER) | 将来独立計画 | 06 | ADR承認までsharing禁止 |
| 027 (EXPERIMENT) | T22 | 08 | 現行スタック互換のPOC結果がある |

テストIDは`AUTH-Txx`のprefixを省略表示している。Statusは全て**Designed / Not executed**。

## 16. 未決事項・Cross-team proposals

| ID / Severity | Owner | 具体的な提案・検証・受入条件 |
|---|---|---|
| AUTH-P01 / P0 | CHAT_01 | `AuthenticatedPrincipal(userId,tenantId,sessionId,identityVersion)`、Resource owner/audienceの共有契約を確定。別ownerのIDを指定できないこと、default denyを契約試験で証明 |
| AUTH-P02 / P0 | CHAT_10 | users/session/tenant/所有者FKとRLS/transaction pool、local bootstrap migrationを決定。2 userでDB・backup・cacheの隔離を確認 |
| AUTH-P03 / P0 | CHAT_08 | AuthProviderのcookie/CSRF/Origin、全REST/Server Action/SSE/WS route manifest、Telegram bindingを追加。HTTP/WS負のfixtureを通す |
| AUTH-P04 / P0 | CHAT_03 | approval/side-effect ledgerの`approver==owner`/fresh auth/Tool実行直前revocationを確定。Raceと二重実行が0 |
| AUTH-P05 / P0 | CHAT_04 | BrowserSession owner、Context/storageStateの隔離、one-time human ticket、lease generation。A/B storage混入0 |
| AUTH-P06 / P0 | CHAT_02/07 | RunとWatchのowner lineage、復帰時のidentityVersion/disable/cancel判定。stale jobによるTool実行0 |
| AUTH-P07 / P1 | CHAT_00/08/10 | Better Auth 1.7.14採用/バージョン固定、初期session/lockout数値、MFA/Passkey導入のADR。CSRF/セッションstore/インフラPOCをレビュー |
| AUTH-P08 / P1 | CHAT_11 | AUTH-T01〜T30を実際の2-user、Browser、queue、proxy環境で実行するQA計画。実行結果と失敗ログを追跡 |
| AUTH-P09 / P1 | CHAT_09(secrets/privacy) | key rotation、encrypted storageState、retentionとdelete、session invalidationの仕様を連動。zero-LLM-exposureの検査 |
| AUTH-P10 / P2 | CHAT_00/01/08 | 将来「ユーザーが他人に特定Taskを共有」する需要が出たらRole/ACLのADR、共有監査、撤回、approval非委譲を別設計。現時点はdeny |

**現在のP0 blocker:** プロジェクトのShared Contract・物理DB・Tool Gateway/UI/Browser契約が未Locked。上の依存はDraft提案であり勝手に既成事実化しない。担当外の文書をこのPRから変更しない。

## 17. Sources — verified actual refs and evidence grades

調査日: **2026-10-30 (JST)**。GitHub source URLは正確なcommit SHAにpin。`Code verified`はファイルを読解したことを指し、実行検証ではない。

- **[S1] [Code verified]** Better Auth `originCheckMiddleware` / `validateOrigin` / `validateFormCsrf` (ref `95d158b04cd8217cc17443e809c700f3b6969f97`), `packages/better-auth/src/api/middlewares/origin-check.ts`: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/src/api/middlewares/origin-check.ts#L63-L376
- **[S2] [Code verified]** Better Auth CSRF/Origin tests `origin-check.test.ts` (trusted/untrusted origin、first-login、`disableCSRFCheck`、request-scoped isolation): https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/src/api/middlewares/origin-check.test.ts
- **[S3] [Docs verified]** Better Auth security reference: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/docs/content/docs/reference/security.mdx
- **[S4] [Code verified]** Better Auth `requireResourceOwnership` / `requireOrgRole`: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/src/api/middlewares/authorization.ts#L4-L156
- **[S5] [Code verified]** Better Auth `getSession`, `cache-control: no-store`: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/src/api/routes/session.ts#L29-L94
- **[S6] [Code verified]** Better Auth `session-api.test.ts`（Cookie属性、未認証、DB失敗時の匿名化）: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/src/api/routes/session-api.test.ts#L36-L117 ; version/license: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/package.json
- **[S7] [Code verified]** OpenClaw `authorizeTrustedProxy`, `authorizeSharedSecretAuth`, `authorizeGatewayConnect`, Origin/rate-limit: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/auth.ts#L185-L563
- **[S8] [Code verified]** OpenClaw `auth.test.ts`（rate-limit、proxy、Origin、password等の負ケース）: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/auth.test.ts
- **[S9] [Code verified]** OpenClaw `resolveLeastPrivilegeOperatorScopesForMethod` / `authorizeOperatorScopesForMethod`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/method-scopes.ts#L194-L323
- **[S10] [Code verified]** OpenClaw `method-scopes.own-session.test.ts`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/method-scopes.own-session.test.ts ; license/version: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/package.json and https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/LICENSE
- **[S11] [Code verified]** Playwright `BrowserContext.storageState`, `clearCookies`, `setStorageState`: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/packages/playwright-core/src/server/browserContext.ts
- **[S12] [Code verified]** Playwright `ST-001...ST-005` storageState tests: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/tests/library/browsercontext-storage-state.spec.ts
- **[S13] [Code verified]** Playwright version 1.65.0-next / license Apache-2.0: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/package.json and https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/LICENSE
- **[S14] [Docs verified]** OWASP CSRF Prevention Cheat Sheet (Origin/Referer, token, Fetch Metadata、SameSite limitations): https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- **[S15] [Docs verified]** OWASP WebSocket Security Cheat Sheet (Origin/handshake, per-message auth, CSWSH): https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html
- **[S16] [Docs verified]** OWASP API1:2023 Broken Object Level Authorization: https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/
- **[S17] [Docs verified]** OWASP Session Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- **[S18] [Internal requirement / actually read]** https://github.com/choko510/aiagent-md/blob/92b5827ed16a7d4ae61e8146ce382795398293cb/docs/00-foundation/project-brief.md ; architecture-rules/acceptance-scenarios/research-protocol/review-gates/templates were likewise read at main.
- **[H1] [Hypothesis / unverified]** Better Authを`AuthProvider`としてFastify/Next.js/本番DBに統合したとき、要求される即時revocationとCookie/CSRF/fresh auth契約を満たせるかは**未検証**（`AUTH-T22`）。
- **[H2] [Hypothesis / unverified]** RLS、DBスキーマ、所有者データ設計、SSE切断閾値、10 task同時実行時のコストは**設計提案**であって実測/上流機能の主張ではない。

**License/maintenance:** OSSコードをコピーする仕様ではなくメカニズムを比較して独立実装する。各依存の採用時はライセンスとNOTICE、セキュリティ更新、version pinをレビュー。Better Auth/OpenClawはpackage metadata上MIT、PlaywrightはApache-2.0。上流テストは「読んだ」のであって「本Harnessで通った」わけではない。

## 18. Review Gate self-audit (2026-10-30)

- [x] Owned path only、Project Brief/Architecture/Acceptance/Research/Templateを実読、既存同名specがmainに無いことを確認。
- [x] 3 OSSのソース/テストをcommit pinして比較、公式脅威ガイドも併記。Docs/Code/Hypothesis区別。
- [x] 代替設計、内部API/owner invariant、recovery/timeout/CSRF、negative/security/2-user tests、REQ→TEST→SCの追跡を記載。
- [x] 外部副作用/secret/Host shellは禁止。実施した統合テストなし。
- [ ] Cross-team shared contracts/DDL/Browser/Tool/APIが未承認。
- [ ] 本製品の実装・テスト・負荷計測は未実施。
- [ ] Better Auth導入スパイクとRLS/proxy本番適合の検証は未実施。

**Disposition: Keep Draft.** 次のCHAT_09の優先仕様は`secrets-and-credentials.md`。この文書のレビュー後に別ブランチ/PRで進める。
