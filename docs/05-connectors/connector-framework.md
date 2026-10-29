# Connector Framework — Detailed Implementation Specification

> Owner: CHAT_05 | Canonical path: docs/05-connectors/connector-framework.md | Status: **Draft / Provisional** | Revision: 0.1 | Research date: **2026-10-30** | Base main: `92b5827ed16a7d4ae61e8146ce382795398293cb`
>
> Scope: 外部の公式API・EC・ホテル・SNS等を汎用Agent Harnessへ接続するための **Provider Adapter共通機構**。個別Amazon/Keepa・買取・X・ホテルの詳細仕様は05-02〜05-05で定義。**本書は実装・試験済みではない。**

## 0. 結論と採用案

**独立した、Manifest-first・Fail-closed・Capability-scopedなAdapter境界**を採用する。Connectorは「どの情報源から何を取得可能か」「取得した外部事実を正確に説明する」責務に限定し、AgentのTool実行承認、タスク状態、LLM判断、DB正本、Browser Session、秘密管理、予算の最終判断を持たない。

1. **Control plane**: 管理者が検証・許可した静的Manifestから能力、入力Schema、API/API利用資格、利用可能な資格情報種別、コストモデル、出力鮮度条件、制限を取得。未信頼コードのロードや外部HTTPアクセスを **discovery時に行わない**。配置・有効化・公開は別操作。
2. **Execution plane**: Agent → CHAT_03 Tool Gateway（schema/policy/budget/approval/intent ledger）→ ConnectorDispatcher → approved Adapter → 制限付きTransport / Credential Broker → 外部Provider。結果を証拠候補へ正規化し、CHAT_01の正本契約に従って記録してから返す。
3. **Provider compliance**: 有効な利用資格・対象用途・robots/ToS・API quota・network egressを個別に検証。対象サービスの利用権限を仮定しない。「robotsでAllow＝利用規約上許可」ではない。
4. **信頼性**: 送信前の有限quota/費用予約、tenant/credential/providerを横断した分散競合制御、期限付きretry、OAuth single-flight refresh、cancel/restart、結果不確定の明示。価格や空室の時刻・条件を捏造しない。
5. **実装採用**: TypeScript hostの小さな独立APIとAdapter SDK。PythonのScrapling/Airbyte CDK等は **参考実装**であり、Coreへの直接依存なし。ScraplingによるWeb抽出はCHAT_04のSidecar経由、MCP Tool discoveryはCHAT_03の正本を使う。

**非確定**: shared Tool/Evidence/Error/Task schema、DBテーブル、Secret Broker実装、provider別ToS・資格・請求価格、quota coordinatorの物理実装。各所有者へ `[PROVISIONAL: CHAT_XX]` として送る。

## 1. 目的・責務・非責務・受入シナリオ

### 1.1 責務と入力/出力
- Input: Gatewayで承認された **読取型Provider request**（provider、capability、version、正規化済query、tenant、deadline、権限/コストcontext参照）、公開済Manifest、Secret Broker上の認可済CredentialRef。
- Output: 可用/利用不可診断、観測事実 **候補** とSource URL/取得時刻/元の宣言時刻/条件/不確実性/Artifact参照、構造化Provider error、quota使用量と課金見積/実績。外部副作用はprovider APIへの読取リクエストと課金のみ。APIの「読取」が分析用quota消費・監査対象であることを明記。
- Adapterは**Task/Run/Watch/Notificationを作らない**。予約確定、購入、送金、投稿を提供しない。将来のwrite-capabilityは別の審査・承認・idempotency設計を要求し、本仕様のv1 admissionでは不許可。
- API、HTTP、ブラウザのフォールバック選択はCHAT_04 Search RouterとCHAT_03 Policyの合意事項。Providerの失敗を理由にBrowser/別Providerへ **無断切替しない**。
- Scenario: **SC-01, SC-03, SC-04**。関連: SC-02（Watchの情報源）、SC-05（汎用調査）、SC-06（ログインはBrowser Owner）、SC-07（障害）、SC-08（境界）、SC-09（費用）。

### 1.2 非責務
- 共有Evidence/Event/Tool/Task型・physical DDLを再定義しない（CHAT_01/03/10）。
- LLMによる自由なネットワークアクセス、任意コード/パッケージ実行、任意MCPサーバ導入、ホストShell/SSH/Docker Socketアクセスなし。
- Provider横断の商品の同一性・利益確定・ホテル総額/予約状態・投稿の真偽をConnector単独で確定しない（05-02〜05-05とResult Verifier）。
- 課金・価格・空室・API契約は常に変動しうる。静的Manifest/Cacheだけで「今利用可能」「リアルタイム確認」と表示しない。

## 2. 既存OSS横断調査と採否

| Source | 実際に確認した関数 / テスト（固定SHA） | 使う着想 | 差異・採否・license |
|---|---|---|---|
| **OpenClaw** [S1][S2][S3] | `buildPluginManifestRegistry`（manifest発見、schema/バージョン検査、ID衝突、trust）、`resolveManifestActivationPluginIds`（activation filter）、`manifest-registry.test.ts`（manifest欠落/大文字小文字衝突/公式ID spoof）、`activation-planner.test.ts` | Manifest-first、control/runtime分離、disabled・configured・readyを区別 | **パターン採用、コードコピーしない**。本製品は任意の実行Pluginロードを認めず、安全な静的allowlistだけ。MIT |
| **Airbyte Python CDK** [S4][S5] | `HttpClient._send_with_retry`（max retries/time）、`_handle_error_resolution`（OAuth更新判定）、`test_default_max_retries`、`test_refresh_token_then_retry_refreshes_once_and_succeeds`、`test_refresh_token_then_retry_does_not_refresh_twice_for_the_same_request` | 429と転送障害の区別、認証更新1回上限、再試行の有限化、provider固有エラー変換 | **アルゴリズム参考**。同期Python CDKをAgent Tool Runtimeには組み込まない。MIT（`LICENSE.txt`） |
| **Scrapling** [S6][S7][S8] | `AutoThrottle.delay_for/record`、`parse_retry_after`、`RobotsTxtManager._get_parser/can_fetch`、`test_retry_after_wins_over_the_doubling`、`test_5xx_response_disallows_everything` | domain throttling、429後の待機、robots取得不能をfail-closed | **HTTP抽出はCHAT_04に委譲**。同実装のin-memory domain cacheはcredential/tenant分離とTTLがなく、そのまま使用しない。BSD-3-Clause |
| **RFC 9309 / RFC 9110** [S9][S10] | robots応答/redirect/cache規範、HTTP `Retry-After` のHTTP-dateと整数秒 | robotsの判定、Retry-Afterの解釈 | **規格を採用**。ToSとアクセス許可は別途確認 |

**高影響の比較**:

| 争点 | 案A | 案B | 決定 |
|---|---|---|---|
| 能力登録 | Runtime importして反射・自動register | 静的manifestを管理者が検証し、明示enable | **B**。起動副作用/任意コード注入のリスクを減らす |
| API integration | CoreにProvider別if/else/secretを内包 | 不変のAdapterインターフェイスと外部Credential/Transport | **B**。将来拡張/監査/入替え |
| Rate limit | 各Workerのメモリ内sleep | 分散で予約/共有したquota + Provider backoff | **後者**。多Worker時の超過を防ぐ |
| HTTP failover | エラーで自動的にスクレイプ | Gateway承認済み候補だけを選び直す | **後者**。ToSと予算の無断拡張を防ぐ |
| 認証 | Provider SDKが環境変数を自由読取 | Credential Brokerによる実行時のscope-bound取得 | **後者**。モデル/ログへトークン非公開 |
| 観測 | 取得値のみ保存 | 取得値、条件、元URL/元時刻/取得時刻、raw artifact | **後者**。検証可能性と誤認抑止 |

**License/変更耐性**: 依存ライブラリの導入時にはbundled依存とLICENSE/NOTICEをSBOMに記録する。MIT/BSDであってもAPIサービスの利用資格・公開/再配布権を意味しない。GitHub main SHAを固定して調査したが、特定リリースタグ互換は導入時に別途確認する。

## 3. 依存関係・正本・所有権

~~~text
User/UI (CHAT_08) --> Task/AgentLoop (CHAT_02)
                           |
                     Tool Registry/Gateway (CHAT_03) <--- Policy/Approval (CHAT_03/09)
                           |            |
                     intent/ledger      Budget (CHAT_02/03)
                           |
                Connector Dispatcher [CHAT_05]
                | catalog / resolution |
                | quota / normalization|
                +------ + -------------+
                       |
           +-----------+----------------+
        Provider adapter(s) [CHAT_05]
           |                 |
       Credential Broker [09]    Authorized Transport [09 + 04]
           |                 |
       Secret Storage        External API / approved HTTP
                                 |
             Artifact/Evidence persistence [01 + 10]
                                 |
                   Tool result / Runtime [03 + 02]
~~~

| 契約・領域 | 正本Owner | 本書での扱い |
|---|---|---|
| Tool IDs、公開Tool schema、execution ledger、approval | CHAT_03: `docs/03-tools/` | Adapterの戻り値変換、Provider-specific methodの実行のみ |
| Task/Run/Artifact/Evidence、event envelope、error | CHAT_01: `docs/01-contracts/` | プライベートのdraft型と変換提案のみ |
| Task retry/Cancel/Steer/Budget | CHAT_02: `docs/02-runtime/` | Gatewayから渡るdeadline/cancel signalを尊重 |
| HTTP/search/browser owner | CHAT_04: `docs/04-web-browser/` | Authorized HTTPを呼ぶ、Browser制御は行わない |
| Secret/egress/SSRF/tenant isolation | CHAT_09: `docs/09-security/` | 仲介されたscopeだけを消費、境界の変更提案 |
| quota保存/コスト計測/DB物理設計 | CHAT_10: `docs/10-infrastructure/` | atomic quota API要件のみ提案 |
| Watch/trigger/通知 | CHAT_07: `docs/07-automation/` | Watchから同一Adapter実行経路を利用 |

**契約が未Lockedの間は本章の型を実装設計上の暫定境界として扱い、共有型へ暗黙に追加しない。**

## 4. 要件定義（安定ID）

| ID | Priority | 要件と観測可能な合格条件 | Scenario |
|---|---|---|---|
| CON-FW-001 | MUST | 許可済Manifestでのみ登録し、scan時にAdapterコード/ネットワークを実行しない。壊れた・未知schemaを拒否 | SC-08,05 |
| CON-FW-002 | MUST | capability/version/feature/地域を照合し、disabled・unconfigured・unhealthy・not-entitledを区別 | SC-01,03,04 |
| CON-FW-003 | MUST | Gatewayを経由しない外部Adapter実行を拒否、未承認write actionを拒否 | SC-07,08 |
| CON-FW-004 | MUST | 入力の境界値とquery/地域/条件をSchemaで検証し、不正なら送信前に停止 | SC-01,04 |
| CON-FW-005 | MUST | tenant/credential scopeで認証し、Secretをprompt/tool result/log/artifactに含めない | SC-06,08 |
| CON-FW-006 | MUST | allowlist/SSRF/redirect/DNS rebindingを許可済Transportで制御。Adapterから任意fetch禁止 | SC-08 |
| CON-FW-007 | MUST | Provider契約/利用資格・ToS・robots/アクセス頻度条件を評価し、未確認/禁止なら対象Provider実行不可 | SC-01,03,04 |
| CON-FW-008 | MUST | 複数Workerの全試行をquota枠で予約。429とRetry-Afterを共有し、未消費を確信できない試行の返金禁止 | SC-02,09 |
| CON-FW-009 | MUST | 最大試行回数/total deadline/byte/records/parallelism/請求額を有限に制限 | SC-07,09 |
| CON-FW-010 | MUST | 安全なGET等だけを分類付きで再試行。非冪等/不確定副作用を盲目的再試行しない | SC-07 |
| CON-FW-011 | MUST | 認証失効時はscope単位single-flight refresh、1 requestに原則1 refresh、永続invalid_grantは停止 | SC-06,07 |
| CON-FW-012 | MUST | 取得値と観測時刻・データ時点・出典・条件・評価不能項目を保持。推定を実測と偽らない | SC-01,03,04 |
| CON-FW-013 | MUST | partial/unavailable/uncertainの区別。取得失敗を0円・在庫なし・空室なしと変換しない | SC-01,02,04 |
| CON-FW-014 | MUST | Cancel/Steer/restartに対応し、古い世代の結果が新Taskの観測を上書きしない | SC-07 |
| CON-FW-015 | MUST | Manifest/Adapter/schema versionの互換チェックとrollback、既存runへの版pin | SC-07 |
| CON-FW-016 | MUST | 実行コスト、rate reason、証拠参照、provider診断を秘匿化して監査可能 | SC-05,08,09 |
| CON-FW-017 | SHOULD | capability/地域/費用/鮮度から候補を提示。実行先切替はGatewayへ戻して再認可 | SC-05,09 |
| CON-FW-018 | SHOULD | 正規化前のraw responseをサイズ制限付きArtifactとして保持しprovenanceを付す | SC-01,03,04 |
| CON-FW-019 | SHOULD | manifest検証用offline CLI、provider契約/資格expireの管理UI診断 | SC-05 |
| CON-FW-020 | LATER | 実行時導入の第三者Plugin marketplace。署名・sandbox・審査完了まで導入不可 | SC-08 |
| CON-FW-021 | EXPERIMENT | 独立のProvider selectorが複数ソースの精度/費用を測る。実測前の優劣保証なし | SC-09 |

## 5. 内部I/O・契約・validation

> 以下は**CHAT_05内部の提案型**。`Task`/`ToolInvocation`/`Evidence`/`Error`/`Event`等の共有正本ではない。外部露出への変更は `[PROVISIONAL: CHAT_01/03]`。TypeScript pseudo-typesのため、実装時にZod/JSON Schemaと一致するランタイム検証を生成する。

~~~typescript
type ProviderId = string; // ^[a-z][a-z0-9]*(?:[-.][a-z0-9]+)*$, max 64
type CapabilityId = string; // ^[a-z][a-z0-9.-]{0,79}$
type Mode = "read_only"; // v1: writeは禁止
type CredentialScheme = "none" | "api_key" | "oauth2" | "session_ref";
type ProviderEligibility =
  | "ready" | "disabled" | "unconfigured" | "unauthorized"
  | "unhealthy" | "unsupported_version" | "tos_unknown" | "quota_blocked";
type ObservedClaimKind = "observed" | "derived" | "estimated" | "unverified";

interface ConnectorManifestV1 { // 静的・管理者承認済み: PUBLIC metadataのみ
  manifestSchemaVersion: "1.0";
  providerId: ProviderId;
  adapterVersion: string;       // strict semver: major.minor.patch
  adapterApiMajor: 1;
  capabilities: Array<{
    capabilityId: CapabilityId;
    readOnly: true;
    inputSchemaId: string;       // registryで許可したschema識別子
    outputSchemaId: string;
    regions: string[];          // 対応するISO国コード等。空配列は未対応扱い
    freshnessClass: "live-query" | "historical" | "unknown";
    supportedCredentialSchemes: CredentialScheme[];
    maxRecordsPerResponse: number;
  }>;
  outbound: { allowedOrigins: string[] }; // 管理者ポリシーの承認候補。自己付与不可
  compliance: {
    basis: "official_api" | "licensed_feed" | "permitted_public_web";
    evidenceRef: string;         // ToS/利用資格のレビュー記録参照
    reviewedAt: string;          // ISO datetime, NOT authorization
    expiresAt: string;           // 再審査期限
  };
  limits: {
    maxAttempts: number;
    maxDurationMs: number;
    maxResponseBytes: number;
    maxRecords: number;
    maxInFlight: number;
    publishedQuotaRef?: string;  // 実際の単位・料金はCredential planと結合
  };
  authProfileId?: string;        // secretの値ではない
  releaseDigest: string;         // 管理者が許可したartifact hash
}

interface ConnectorRequestV1 { // Gateway内だけが構築: LLMから直結不可
  providerId: ProviderId;
  capabilityId: CapabilityId;
  adapterVersion: string;
  query: unknown;              // schema validated before use
  runRef: string;              // CHAT_01 canonical runへの参照
  tenantRef: string;           // user isolation正本から取得
  approvedExecutionRef: string;// CHAT_03 policy/intent ledgerへの参照
  credentialRef?: string;      // unresolved secret reference
  deadlineAt: string;
  attemptGeneration: number;
  observationBudgetRef: string;
}
interface ProviderObservationDraftV1 { // 共有Evidenceへの直接代入は禁止
  sourceUrl: string;
  providerId: ProviderId;
  capabilityId: CapabilityId;
  fetchedAt: string;           // our clock; null禁止
  sourcePublishedAt?: string;  // 原記事/投稿
  sourceEffectiveAt?: string;  // 価格/レートの有効時点
  sourceExpiresAt?: string;
  locale?: string;
  currency?: string;           // ISO 4217 or unknown
  conditionMap: Record<string, string>; // 人数/日付/サイズ/新品等の条件
  claimKind: ObservedClaimKind;
  extractedFields: Record<string, unknown>;
  missingFields: string[];
  rawArtifactRef?: string;
  accessMethod: "official_api" | "licensed_feed" | "approved_http";
  parserVersion: string;
}
type ProviderFailureCode =
  | "INPUT_INVALID" | "CAPABILITY_UNAVAILABLE" | "AUTH_REQUIRED"
  | "AUTH_REVOKED" | "TOU_NOT_APPROVED" | "ROBOTS_DISALLOWED"
  | "EGRESS_DENIED" | "QUOTA_WAIT" | "RATE_LIMITED"
  | "PROVIDER_UNAVAILABLE" | "TIMEOUT" | "CANCELLED"
  | "RESPONSE_INVALID" | "COST_LIMIT" | "UNCERTAIN_OUTCOME"
  | "INTERNAL_ADAPTER_ERROR";
interface ProviderOutcomeV1 {
  status: "complete" | "partial" | "unavailable" | "uncertain";
  observations: ProviderObservationDraftV1[];
  failure?: { code: ProviderFailureCode; retryAfterAt?: string; safeToRetry: boolean };
  usage: { attempts: number; responseBytes: number; providerUnitsReserved: number; providerUnitsReported?: number };
}
interface ProviderAdapterV1 {
  readonly providerId: ProviderId;
  readonly adapterVersion: string;
  execute(input: ConnectorRequestV1, deps: RestrictedDeps): Promise<ProviderOutcomeV1>;
}
interface RestrictedDeps {
  authorizedTransport: AuthorizedTransport; // CHAT_09 egressチェック・CHAT_04 HTTP owner
  credentialBroker: ScopedCredentialBroker; // CHAT_09: opaque handle only
  quotaCoordinator: QuotaCoordinator;        // CHAT_05 semantics / CHAT_10 storage
  artifactSink: RestrictedArtifactSink;      // CHAT_01/10の正本へ委譲
  abortSignal: AbortSignal;
  clock: Clock;
}
~~~

**Validator規則**:
- Manifestは最大256KiB、UTF-8 JSON。未知のsecurity/authorityに関するfieldを無視して権限拡張しない。重複IDはcase-fold後に拒否、未知manifestSchemaVersion/adapterApiMajor/semver不整合/重複capabilityは拒否。schemaは安全な静的スキーマIDを参照（リモート`$ref` fetch不可）。versionは構築物digestにpin。
- 外部から送られるqueryは各Capabilityのruntime schemaで検証し、最大64KiB・depth 16・array 100・文字長上限を適用。URL queryは絶対https、userinfo禁止、認可済origin以外不可。Service固有IDは任意長にしない。Providerの返答は未信頼として再検証。
- output claimについて日時はISO 8601 + offsetを必須、異なる`sourcePublishedAt`/`sourceEffectiveAt`/`fetchedAt`を潰さない。数値はparse可否・単位・税/送料/人数などの条件を失わない。nullや未取得は0やfalseに置換しない。
- プロンプト/結果にはcredential値・Cookie・bearer・署名付きURL・自由なresponse headerを入れない。raw artifactもSecret/PII scannerを通す。元レスポンスは最大2MiB、抽出済み最大100レコードとする **v1提案上限**。既存の制限より厳しい場合は厳しい方を優先。
- `compliance.evidenceRef`は監査済みの文書/契約IDであり、Manifest提供者が自由に書いた文字列だけで許可になるわけではない。tenant/用途/プラン/地域のentitlementを実行時に再判定する。

### 5.1 ドキュメント化するAPIと責務

~~~typescript
// CHAT_05内のみ; Gatewayでintent recorded/policy approved済みのtokenが必須
catalog.validateManifest(candidate): ValidatedManifest | Rejection
catalog.resolve(providerId, capabilityId, version, tenantRef): EligibilitySnapshot
dispatcher.execute(approvedInvocation, abortSignal): ProviderOutcomeV1
quota.acquire(scope, units, deadline): Lease | Deferred | Rejection
quota.commitAttempt(lease, providerReport?): void // unknown deliveryも消費扱い
quota.applyProviderBackoff(scope, retryAfterAt): void
quota.inspect(scope): QuotaSnapshot
~~~

**Publish-to-Gateway bridge [PROVISIONAL: CHAT_03]**:
- capability候補をTool Registryが公開する前に、管理者enable + policy allow + schema hash + readOnly=trueを確認。Featureがruntime readyでもuser権限がなければ表示しない。
- Adapter registryの `ready` は「現在の実行成功」の保証ではない。`healthProbe`は明示依頼時のみ行いquotaを消費しうるため、UI discovery pathではネットワークを触らない。
- Provider実行中の返却は `ProviderObservationDraftV1` をCHAT_01 canonical Evidenceへ変換する。mapping失敗は`RESPONSE_INVALID`で、partial claimはuncertain扱い。Adapterがeventを任意生成しない。

## 6. 状態遷移と正常シーケンス

### 6.1 Provider availability（Control plane内部）
~~~text
not_installed -> discovered -> validated -> disabled
                                   |       |
                                   +-> enabled -> eligible -> ready
                                           |          |
                                           +-> unauthorized / unconfigured
                                           +-> tos_unknown / unsupported_version
                                           +-> quota_blocked / unhealthy

any -> quarantined (signature/manifest/egress逸脱) ; quarantined -> admin review only
~~~
- `enabled` は**許可**であって到達確認ではない。Credentialの取り消し/契約期限切れ/地域変更で `ready` は変化。
- manifestの変更はstaged→validate→owner review→atomic version promotion。新旧runは版pinする。緊急revoke時は進行中も実行停止。

### 6.2 実行（内部試行状態、canonical Task stateではない）
~~~text
validated -> waiting_quota -> leased -> sending -> received -> normalized -> committed
                 |                         |          |             |
              deferred              timeout/unknown   partial      invalid
                 |                         |          |             |
          Gateway scheduled          uncertain        partial     failure
cancel before sending -> cancelled (no external attempt)
cancel after sending  -> cancelled + external outcome uncertain if no provider acknowledgement
~~~

1. Gatewayが権限・請求上限・予算・入力Schemaを検証しTool Intentを**永続記録**（CHAT_03）。
2. Dispatcherはrun世代・deadline・versionを照合し、管理者/tenant用のentitlement確認。未承認Webアクセスは止める。
3. CredentialRefの有効性をBrokerへ確認し、計量単位/planに基づく費用上限をGatewayと確認。
4. QuotaCoordinatorがprovider/credential/tenant/global/domainでatomic予約。予約できなければ`QUOTA_WAIT`と次回許容時刻を返し、Worker slotを長時間sleepさせない。
5. `AuthorizedTransport`が実際のendpointと各redirect/DNSを検証し、セキュアにcredentialを付加。Adapterに生Secretは戻らない。
6. レスポンスはstatus/headersを最小限抽出し、Retry-Afterと課金情報を取り込み、暗黙の再実行をしない。bodyを安全にサイズ制限しparse。
7. 取得原本の保管要否を判断、出典/観測時刻/条件をdraftへ正規化、validation/secret masking。
8. CHAT_01/03の永続化成功を確認してからGatewayへ返す。永続化失敗は `UNCERTAIN_OUTCOME`/recoverableとして扱い、取得値が成功として消失/偽装されない。
9. Cancellation/Steerが入ったら**次の送信前**にrun generation再検証。応答後にもgenerationを照合し、古い結果を現行runへ昇格させない。

## 7. アルゴリズム・quota・timeout・retry

### 7.1 実行（擬似コード）
~~~typescript
async function executeApproved(approved, signal) {
  assert(approved.toolGatewayAttestation && approved.intentPersisted);
  const manifest = catalog.getExactVersion(approved.providerId, approved.version);
  assert(manifest?.approved && manifest.digest === approved.pinnedDigest);
  assertCanReadCapability(manifest, approved.capabilityId);
  validateAgainstPinnedSchema(approved.query, manifest);
  await policy.verifyTenantEntitlementAndCompliance(approved);
  const expiresAt = min(approved.deadlineAt, now() + manifest.limits.maxDurationMs);
  const observations = [];
  for (let attempt = 1; attempt <= min(manifest.limits.maxAttempts, 3); attempt++) {
    assertNotCancelled(signal, approved.runGeneration);
    if (now() >= expiresAt) return failure("TIMEOUT", observations);
    const lease = await quota.tryAcquireAllScopesAtomically({
      providerId: approved.providerId,
      credentialRef: approved.credentialRef,
      tenantRef: approved.tenantRef,
      origins: manifest.outbound.allowedOrigins,
      worstCaseUnits: estimateWorstCaseUnits(approved),
      deadlineAt: expiresAt
    });
    if (!lease.granted) return deferred("QUOTA_WAIT", lease.nextAt, observations);
    let credentialHandle: ScopedHandle | undefined;
    try {
      credentialHandle = await broker.scopedHandle(approved);
      // 送信開始前にleased attemptを永続的にsent-or-unknownへ更新
      await ledger.markExternalAttemptStarting(approved, lease);
      const response = await transport.executeApproved(
        approved, credentialHandle, signal, { timeoutAt: expiresAt, maxBytes: 2_097_152 }
      );
      await quota.commitAttempt(lease, response.accounting);
      if (response.status === 401 && canRefreshOnce(approved, attempt)) {
        const refresh = await broker.singleFlightRefresh(approved.credentialRef);
        if (refresh.rejected) return failure("AUTH_REVOKED", observations);
        continue;
      }
      if (response.status === 429 || response.status === 503) {
        const nextAt = parseAndValidateRetryAfter(response.headers, now());
        quota.applyProviderBackoff(lease.scopes, nextAt);
        return deferred("RATE_LIMITED", nextAt, observations); // retryは後続run境界で
      }
      if (!response.ok) return classifyProviderError(response.status, observations);
      const draft = adapter.normalize(response, approved.query);
      validateAndScrubDraft(draft);
      await evidenceSink.persistDraftAndReceipt(approved, draft, lease);
      return completeOrPartial(draft);
    } catch (err) {
      await quota.markUnknownAsConsumed(lease);
      if (signal.aborted) return failure("CANCELLED", observations);
      if (!isSafeIdempotentRead(approved) || !isKnownPreSendFailure(err))
        return failure("UNCERTAIN_OUTCOME", observations);
      if (attempt === min(manifest.limits.maxAttempts, 3))
        return classifyTransportFailure(err, observations);
      await boundedBackoffBeforeNextAttempt(attempt, expiresAt, signal);
    } finally { if (credentialHandle) broker.releaseHandle(credentialHandle); }
  }
  return failure("TIMEOUT", observations);
}
~~~

**重要**: 上記は概念順序。`ledger.markExternalAttemptStarting`はCHAT_03の正式APIではない。実装で送信前記録のトランザクション境界を確定するまで `[PROVISIONAL: CHAT_03]`。複数スコープのatomic quotaの物理実現は`[PROVISIONAL: CHAT_10]`。Providerから明示的なidem-key/結果照会が無ければexternal exactly-onceを宣言しない。

### 7.2 Default finite limits（製品仕様上の提案値、外部で実測した値ではない）
- request timeout: connect 10秒、network-response 20秒、1 Gateway execution 60秒、hard 3 attempts (initial + max 2 retries)。各Provider公式上限やユーザーdeadlineが厳しければそちらを優先。
- 1 response body 2MiB、100 observations、Provider in-flight 2、tenant in-flight 4、Host Connector in-flight 10（**提案値**）。大規模ページはCHAT_04 Artifactへ移し、本文をLLMへ直接渡さない。
- 料金モデル未確認のProviderは課金制API実行不可（明示的に登録した最大単価が必要）。各Attemptは **worst-case** usageを予約。レスポンス未受信時は消費可能性ありとして確保枠を保持・調整、勝手に解放しない。実請求額が確定できないときは`unknown`として請求照合待ち。
- Retry-after（429/503/3xx）は整数秒またはHTTP-date、時刻はClockとserver Dateの信頼範囲を考慮。**指定された待ち時間がTask deadlineを超える場合、短く切り詰めて送信せず deferred/timeout**。無制限待ち・429でcredentialを自動ローテートして制限回避は禁止。
- quota scope: (`providerId`, `credentialRef`, `tenantRef`, `origin`, optional API operation/plan) + shared provider-wide/global cap。全Scopeを**同時に**取れないなら全て未取得、leaseに世代・expiresAt・計測IDを付ける。
- quotaの時刻精度/ウィンドウ（token bucket、fixed window、月次請求等）はProvider契約の単位に依存する。初期値を「公式のquota」と捏造しない。quota unknownならsend拒否。
- Providerへ到達したかもしれない送信はquotaを消費扱いにする。接続開始前の明確な拒否は返金可能。**Provider usageの請求とユーザーTask budgetの会計は別**（CHAT_02/03/10と合意）。
- OAuth refresh: 同一credentialRef/versionでsingle-flight、refresh tokenはSecret Broker内で更新、refresh無限ループ禁止。401が次のリクエストでも継続なら`AUTH_REVOKED`。

### 7.3 冪等性と再送
- Safe read GETの再試行もProvider billingは再度発生しうる。単一retryには新しいquota reservationと独立Attempt IDが必要。
- POSTでも検索APIの「読取」を表すことがある。Method名で安全判定せず、Provider operation declaration + 対象API仕様 + side-effect annotationを全て確認。判定不明は自動再送禁止。
- `UNCERTAIN_OUTCOME`のrecoveryはread receipt/usageの状態照会→reconciliation→人間確認、の順。Agentによる再実行で「直ったこと」にしない。
- Cacheはtenant・権限・query・provider/adapter/normalizer version・地域・通貨・source freshness policyごとに隔離。未認可のログイン済みキャッシュ再利用禁止。

## 8. 障害・Raceマトリクス

| 原因 | 検知 | 次の挙動 / 最終可視状態 | 再送 |
|---|---|---|---|
| 未登録/manifest破損/schema major不一致 | admission | capability unavailable; quarantine/doctor diagnostic | しない |
| ToS/資格/plan不明・期限切れ | compliance gate | not entitled / needs admin review | しない |
| 401 / refresh拒否 | Credential Broker | auth required/revoked（Secret非公開） | 最大1回refresh後に停止 |
| 403/robots禁止 | policy | permission denied | しない |
| 429 / rate reset | status/headers | quota waiting, nextAtとsource note | 期日内なら新attemptを再スケジュール |
| 5xx/timeout | transport | provider unavailable、partialがあれば保持 | 終点到達可能性とread-safe判定に従う |
| DNS rebinding/内部IP/redirect | egress | blocked + audit | しない |
| response schema drift/malformed | validator | partial/reponse-invalid + evidence不確定 | 仕様確認まで止める |
| 送信直前Worker kill | durable attempt ledger | pre-sendなら安全再開、sent-or-unknownなら照会優先 | 不確定な非冪等送信なし |
| 受信後/保存前Worker kill | ledger + artifact receipt | atomic保存再検証; no success without evidence | raw receipt有効なら再parse |
| 同時429/同時OAuth refresh | coordinator + lock | quota shared cooldown、1 refresh generation | lock loserは新token観測 |
| Cancel/Steer中の古い応答 | run generation | staleとして隔離、current run非反映 | 新世代で新承認 |
| 観測はあるが価格/空室の有効条件なし | completeness validator | partial/unverified、比較で条件欠損表示 | 同条件で再問い合わせ |
| コストが不明/上限越え | admission/usage | budget exceeded/awaiting reconciliation | しない |

## 9. Security / privacy / trust boundary

- **Provider response、SNS投稿、HTML、広告、URL queryは非信頼**。どれもSystem/Developer指示、Tool選択、Agent permissionを変更できない。Adapter側の「検出した命令」はデータとして脱権限化。
- **SSRF**: 初期URI・CNAME・DNS解決結果・redirect毎にpolicy。loopback/link-local/metadata/internal/private/reserved IP deny。credential scopeとorigin scopeが一致しないredirectへの資格情報転送不可。TLS検証を無効化しない。
- **Browser隔離**: Cookie/ログインが必要ならCHAT_04の専用session/handoff。Connectorが生Cookieを保存しない。APIがrobotsを適用しない場合でもToS/契約の承認は必須。
- **Auth**: API key header・OAuth exchangeはBroker/Authorized Transport内部でのみ。生tokenをAdapterへ渡す設計は却下。Token change後の古いリクエスト再送による漏洩を防ぐ。
- **Dependency**: adapterはbundled allowlist/固定hash/署名審査。アップロードされた任意JS/Pythonを同一プロセスで実行しない。供給網監査・ライセンス/脆弱性スキャンとrollbackを行う。
- **PII**: provider dataを最小収集、ログはURL query token等をredact。raw artifactのTTL/暗号化/閲覧権限はCHAT_09/10の正本へ委譲。ユーザー提供の会員データを別tenantへ再配布しない。
- **ToS/robots**: RFC 9309ではrobotsへの4xxはアクセス可、5xx/network不可は原則disallowだが、**本製品はこれとは別に利用資格・契約を判定**。robots.txt cacheにはTTL/authority別キーを設け、失敗結果の永久キャッシュを禁止。Scraplingの`_cache`をそのまま長期運用へ移植しない。
- **上位policy**: ManifestがallowedOriginsに書いていてもCHAT_09のegress許可やUser consentを拡張しない。許可境界はIntersectionのみ。

## 10. 計測・Trace・性能評価

各Attemptに `traceId, runRef, providerId, capabilityId, adapterDigest, tenantPseudonym, attemptId, outcome, authScheme(not secret), eligibility, statusClass, retryReason, quotaReserved/Reported, estimatedCost, request/parse/persistMs, responseBytes, evidenceCount, cacheAge` を記録。originは機密を含まない形へ正規化。失敗例のraw URL/headers/secretは観測ログへ載せない。

**測定プロトコル**: 同じfixture、同じ地域/認証プラン・同条件query、warm/cold別、mock networkと実Provider（資格がある場合）別、単Workerと多Worker、fresh/cache別に p50/p95 latency、API calls/task、quota overshoot、retries、unknown outcomes、USD/task、source accuracyを測る。現時点の性能数値・成功率は **未計測**。Provider実契約のrate/billingを無視したベンチマーク禁止。

## 11. 永続化・Transaction・Migration [PROVISIONAL: CHAT_10/01/03]

**物理DDLは作らず、必要な不変条件を提案する。**

1. `provider_manifest_versions`相当: providerId/version/digest/validation/approval/status/configRef/complianceReviewExpiry、原本と世代を保持。
2. `provider_eligibility`相当: tenant/plan/region/secret reference、expiry、lastHealthAtとそのstatus（動的、cache可能だがauthorization正本ではない）。
3. `quota_windows` + `quota_leases`相当: 複合scope key、bucket window/refill、atomic reservation、lease state（reserved/started/committed/unknown/expired）、cooldown、billing reconciliation。multi-Worker process crashでもoverbooking不可。月次のusage積算を残す。
4. `provider_attempt_receipts`相当: Gateway intentと1:n Attempt、request fingerprint、send-start boundary、response/unknown、raw artifact refs、schema/adapter version、observedAt、reconciliation。
5. data retentionはCHAT_09/10の正本。Adapter version変更/rollback時はsnapshot pin維持、旧schemaからのEvidence読み替えはmigration+fixtureで確認。
6. **送信開始→DB更新**の不可分性は外部HTTPとの間で保証不能。「sent-or-unknown」を保守的に記録してから送る。送信前killでunknownに寄ることは許容、二重副作用より優先。
7. atomic evidence persistence失敗時に同じ外部Providerを無条件再照会しない。保存済raw artifactとattempt receiptから再正規化。

## 12. UI / REST / Toolへの影響 [提案のみ]

- CHAT_03: registry public descriptorにcapability、read-only、eligible reason、cost class、source confidence、schemaVersionを渡す。Gateway経由以外のinvokeは不可。Tool結果のsecret filteringとEvidence commit receiptを確認する。
- CHAT_08: Provider設定画面に enabled / credential configured / entitlement / terms reviewed / last successful observation / last health check / next quota availableを**別々**に表示。「接続済み＝最新価格取得可能」としない。
- CHAT_08: タスク結果にsourceUrl、fetchedAt、sourceEffectiveAt、condition/locale/currency、partial/unverified、価格/空室の不確実性、provider費用上限と次実行許容時刻を表示。
- CHAT_04: API/HTTP/Browser選択に提供側の契約/認可状態を渡すが、API失敗からのBrowser無断fallbackは行わない。Scrapling Sidecarで生レスポンスを扱う際も同じEvidence要求を適用する。
- CHAT_07: WatchはConnectorの結果を観測として使い、失敗を数値0へ変換しない。通知重複制御はCHAT_07正本。

## 13. 実装PR分割

| 順序 | 実装PR（将来の別repo） | 依存・完了条件 |
|---|---|---|
| CF-01 | Static Manifest parser, version/digest validation, offline catalog | security manifest fixture / duplicate ID rejection |
| CF-02 | Gateway bridge, tenant/entitlement/provisional Tool mapping | CHAT_01/03契約Locked後、Gateway bypass禁止 |
| CF-03 | Credential Broker / Authorized Transport integrations | CHAT_09 egress+secret contract、mock credential |
| CF-04 | Distributed quota/cost reservation and retry scheduler | CHAT_10 DDL・CHAT_02 budget、multi-worker contention |
| CF-05 | Evidence draft normalization, artifact receipt, recovery | CHAT_01 Evidence & CHAT_03 ledger contract |
| CF-06 | 2つのread-only reference adapters（mock providerと利用許可のある実API） | UC横断・fault injection・security E2E |
| CF-07 | Diagnostics/UI/telemetry, rollout flag, migrations and rollback | rollout audit・benchmark・feature disable |

**機能フラグ**: default disabled。開始時はread-onlyのMock Adapterで常設契約試験。個別Providerの資格確認が終わってからexplicit opt-in。旧版Adapterはrunning invocation終了までpin、緊急停止ではoverride deny優先。

## 14. テスト仕様（全て **Designed / Not run**）

実Providerにアクセス・課金・投稿・購入・予約するテストはまだ行っていない。一般のE2EはMockServer + fakeClock + 2 Worker + fault injectionで設計し、外部実行は管理者許可時のみ。

| TEST-ID | 種別 | Setup → Action | Pass条件 |
|---|---|---|---|
| CON-FW-T01 | Unit | Manifest欠落/未知major/巨大JSON/重複casefold ID | 全件拒否。scan中のnetwork/import=0 |
| CON-FW-T02 | Unit | 同Provider複数capability/version/region、disabled、credential無効 | 正しいeligibility reason。無許可Tool公開なし |
| CON-FW-T03 | Security | Gateway attestation不正/未承認write request | 外部送信0、Policy audit |
| CON-FW-T04 | Contract | 空query、過大query、誤った人数/日付/地域、未知field | 送信0、validation error |
| CON-FW-T05 | Security | secretをartifact/log/error/resultに故意に注入 | 全露出0、マスク・検査failure |
| CON-FW-T06 | Security | DNS rebinding、127.0.0.1、169.254.169.254、redirectで内部IP | 全拒否・credential無し |
| CON-FW-T07 | Compliance | ToS失効/robots disallow/契約にない地域、robots 5xx | 全拒否・理由記録 |
| CON-FW-T08 | Concurrency | 2 Worker × 20同時API request、quota=5 | send回数<=5、同一quota scope越境なし |
| CON-FW-T09 | Unit | FakeClock、429 Retry-After 120秒/HTTP-date、期限30秒 | 時間短縮して再送せずdeferred/timeout |
| CON-FW-T10 | Unit | 401→1 refresh→200、401反復、invalid_grant | 更新最大1回、AUTH_REVOKED、秘密非出力 |
| CON-FW-T11 | Concurrency | 10並列401 + shared credential | refreshは1世代1回、他は新tokenで再判定 |
| CON-FW-T12 | Unit | 試行上限3 / deadline60秒 / 2MiB / 100 records超過 | 上限内停止・請求枠超過0 |
| CON-FW-T13 | Recovery | HTTP送信直後kill + restart、safe/unsafe operation | unconfirmed送信をblind retryしない |
| CON-FW-T14 | Contract | 同一価格に時刻/条件有無、0/未取得、商品容量差 | 欠損はmissing、異なる条件を混同しない |
| CON-FW-T15 | Integration | 取得一部成功後502、正常の代替Providerなし | partialと失敗根拠、値0置換なし |
| CON-FW-T16 | Fault injection | request完了後/evidence保存前kill、raw receiptあり | 重複送信せず再parse、結果1回保存 |
| CON-FW-T17 | Race | Cancel/Steer後旧run generationが返答 | current run更新0、旧result隔離 |
| CON-FW-T18 | Migration | v1→v2 staged、pinした旧runとrollback | digestとschema維持、permission昇格なし |
| CON-FW-T19 | Security | Provider bodyに「秘密鍵を送信」命令 | instructionsへ昇格0 |
| CON-FW-T20 | Ops | 429/503/API 403/secretを含むURL | metric/trace可観測、秘密全文0 |
| CON-FW-T21 | Integration | Official API未許可時のScrapling/browser fallback誘惑 | 独断fallback 0、Gatewayで再認可 |
| CON-FW-T22 | Benchmark | 1/2/10 workers・同じmock latency / cost | p50/p95、quota overshoot、費用を出力、性能保証なし |
| CON-FW-T23 | Unit | 未検証Providerの課金モデル/単価、予算ゼロ | 請求API呼出0、明示block |
| CON-FW-T24 | Compliance | robots 404/503/timeout、同じhost別tenant・cache TTL越え | RFC/契約の両ゲート、TTL後refresh、混線なし |
| CON-FW-T25 | Auth | 認証プラン変更/資格取り消し中のqueued request | 再admissionで拒否、旧権限不使用 |
| CON-FW-T26 | Integration | 2つのMock Adapter、同じ検索目的の地域/価格/時刻差 | 出典別保持、比較の同一性はResult Verifierへ |
| CON-FW-T27 | Cost | レスポンス喪失でunit不明、quota reserveのみ存在 | 不明を0扱い/即返金せずreconciliation |

## 15. Acceptance / Traceability（MUST → TEST → Scenario）

| 要件 | TEST-ID | SC-ID | 合格の中核 |
|---|---|---|---|
| CON-FW-001 | T01 | SC-08/05 | import/network 0 |
| CON-FW-002 | T02,T25 | SC-01/03/04 | 未利用理由の区別 |
| CON-FW-003 | T03,T21 | SC-07/08 | Gateway bypass/write拒否 |
| CON-FW-004 | T04 | SC-01/04 | 不正条件送信0 |
| CON-FW-005 | T05,T10,T11 | SC-06/08 | secret露出0 |
| CON-FW-006 | T06 | SC-08 | SSRF送信0 |
| CON-FW-007 | T07,T24 | SC-01/03/04 | 認可/規約両条件成立 |
| CON-FW-008 | T08,T09,T27 | SC-02/09 | quota越過0 |
| CON-FW-009 | T12,T23 | SC-07/09 | 全上限を厳守 |
| CON-FW-010 | T13 | SC-07 | blind retry 0 |
| CON-FW-011 | T10,T11 | SC-06/07 | refresh 1世代のみ |
| CON-FW-012 | T14,T26 | SC-01/03/04 | provenance/conditions維持 |
| CON-FW-013 | T15 | SC-01/02/04 | unknown≠0 |
| CON-FW-014 | T16,T17 | SC-07 | stale results 0 |
| CON-FW-015 | T18 | SC-07 | version pin/rollback |
| CON-FW-016 | T20,T22 | SC-05/08/09 | trace+秘匿 |

記載例T01は `CON-FW-T01` の省略記法。SHOULD/LATER/EXPERIMENTはblocking gateではない。横断E2E・評価Fixtureの最終正本はCHAT_11。

## 16. 未決事項・Cross-team proposal

| ID | Owner | Severity | 提案対象/未確定点 | 暫定案・移行/検証 |
|---|---|---|---|---|
| P05-01 | CHAT_01 | P0 | `docs/01-contracts/evidence-artifact-contracts.md`: 取得日時・原データ時刻・有効条件・rawArtifact・estimated/observedをどう表現するか | `ProviderObservationDraftV1`をcanonical Evidenceへ写像できる最小必須fieldにする。T14/T15/T26で検証 |
| P05-02 | CHAT_03 | P0 | `tool-io-schema.md`/`tool-execution-gateway.md`/`side-effect-ledger.md`: approved attestation、送信開始前intent、attempt receipt、quota/cost、結果永続化 | Gatewayにopaque approved handleとreceipt hookを提案。T03/T13/T16 |
| P05-03 | CHAT_09 | P0 | `secrets-and-credentials.md`/`network-egress-and-ssrf.md`: Token Broker、DNS/redirect enforcement、origin bound、API network isolation | raw tokenなしのauthorized transport。T05/T06/T10 |
| P05-04 | CHAT_10 | P0 | `database-schema-and-migrations.md`: 多Worker間Quota atomic admission、期限付lease、cooldown、usage reconciliation、manifest version pin | Postgres row lock等の採否はOwner決定。T08/T09/T27 |
| P05-05 | CHAT_02 | P1 | `budget-and-limits.md`: Task budgetとProviderの予約/実測/不明請求、deadlineとCancel世代 | max cost reservationをTool execution前に確定。T12/T17/T23 |
| P05-06 | CHAT_04 | P1 | `search-router.md`/`http-fetch.md`/`scrapling-sidecar.md`: Approved APIの能力情報、fallback再承認、HTTPのrobots/cache/egress | Connector APIを優先するが権限・価格次第。T21/T24 |
| P05-07 | CHAT_07 | P1 | `observations-and-trigger-evaluation.md`: undefined/partialの監視意味論 | 取得不能≠0、observedAt必須。T14/T15 |
| P05-08 | CHAT_08 | P2 | `task-and-watch-dashboard.md`: eligibleとhealthy/最新観測の区別、partial/uncertain表示 | Provider diagnosticsを出す。T02/T20 |
| P05-09 | CHAT_11 | P1 | `test-harness-and-fixtures.md`: multi-worker、fake broker、DNS rebinding、kill boundary、quota mock | T01〜T27をE2E fixtureに統合 |
| P05-10 | CHAT_05 | P1 | Provider個別の契約/料金/許容quota・JWT/OAuth・SKU/日時など | 05-02〜05-05に委譲、導入時公式情報・実アカウント資格確認 |
| P05-11 | CHAT_00 | P2 | read-only POSTをv1に含めるか、外部サイドエフェクト定義を共有ADRで固定 | API仕様で実質read-onlyでも課金/副作用はあり、個別審査前はfail closed |

**P0はDraftの間に解消が必要**。本書の独立記載は共有オーナーの同意を意味しない。各提案が棄却された場合は本MDと関連REQ/TESTを改訂し、breaking changeのmigrationを記録。

## 17. 調査Source・根拠等級

全て **2026-10-30 閲覧**。Githubの`main`は変動するため以下のSHAへpin。**Code verified** はソースとテストの読み取りのみで、我々の統合動作・CI・実Provider認可を検証したことではない。

- [S1] **Code verified** — OpenClaw `buildPluginManifestRegistry`, `src/plugins/manifest-registry-build.ts` lines 213–465; SHA `8b8c33ef334b3754f42afe2a2e212418abfa80f4`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/plugins/manifest-registry-build.ts#L213-L465
- [S2] **Code verified** — OpenClaw `manifest-registry.test.ts` lines 316–327,463–475,640–653; `activation-planner.test.ts` lines 53–130: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/plugins/manifest-registry.test.ts#L316-L327 and https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/plugins/activation-planner.test.ts#L53-L130
- [S3] **Docs/Code verified** — OpenClaw manifest docs/architecture, MIT LICENSE: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/docs/plugins/manifest.md ; https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/LICENSE
- [S4] **Code verified** — Airbyte CDK `HttpClient._send_with_retry` lines 330–393, `_handle_error_resolution` lines 609–710; SHA `d5dd299f4b2deaa259f6f6fab72182aa56ebbc45`: https://github.com/airbytehq/airbyte-python-cdk/blob/d5dd299f4b2deaa259f6f6fab72182aa56ebbc45/airbyte_cdk/sources/streams/http/http_client.py#L330-L393 and https://github.com/airbytehq/airbyte-python-cdk/blob/d5dd299f4b2deaa259f6f6fab72182aa56ebbc45/airbyte_cdk/sources/streams/http/http_client.py#L609-L710
- [S5] **Code verified** — Airbyte CDK unit tests lines 589–685,1189–1355、MIT `LICENSE.txt`: https://github.com/airbytehq/airbyte-python-cdk/blob/d5dd299f4b2deaa259f6f6fab72182aa56ebbc45/unit_tests/sources/streams/http/test_http_client.py#L1189-L1355 ; https://github.com/airbytehq/airbyte-python-cdk/blob/d5dd299f4b2deaa259f6f6fab72182aa56ebbc45/LICENSE.txt
- [S6] **Code verified** — Scrapling `parse_retry_after`/`AutoThrottle.record`, `scrapling/spiders/throttle.py` lines 10–101; SHA `26dc85a4fa307257fb5c669c80aa44b15acb84c4`: https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/spiders/throttle.py#L10-L101
- [S7] **Code verified** — Scrapling `RobotsTxtManager`/`can_fetch` `scrapling/spiders/robotstxt.py` lines 10–74、`tests/spiders/test_robotstxt.py` lines 121–145: https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/spiders/robotstxt.py#L10-L74 ; https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/spiders/test_robotstxt.py#L121-L145
- [S8] **Code verified** — Scrapling tests `tests/spiders/test_throttle.py` lines 121–211、BSD-3-Clause LICENSE: https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/spiders/test_throttle.py#L121-L211 ; https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE
- [S9] **Docs verified** — IETF RFC 9309 (Robots Exclusion Protocol, September 2022; `2.3–2.4), 2026-10-30: https://www.rfc-editor.org/rfc/rfc9309.html
- [S10] **Docs verified** — IETF RFC 9110 (HTTP Semantics `10.2.3 Retry-After, June 2022), 2026-10-30: https://www.rfc-editor.org/rfc/rfc9110.html
- [S11] **Issue report only; not proof of bug reproduction** — Airbyte CDK quota headerの課題 `#1151`: https://github.com/airbytehq/airbyte-python-cdk/issues/1151
- [S12] **Issue report only; not proof of bug reproduction** — OpenClaw plugin config validationの環境依存 `#187042`: https://github.com/openclaw/openclaw/issues/187042

**未実施検証**: ソースコードを実環境でcheckout/テスト実行していない。ライブラリの導入候補版と現在の各Tag/ChangeLog（このmainの特定リリース互換）、公式Provider APIの現行料金・許可条件・認証資格、HostでのEnd-to-End動作、並列quota実測、セキュリティペンテストはいずれも未確認。これらは後続の所有者審査・実装PRで実施する。

---

**Draft review checklist**: owned path only ✓ / Project Brief alignment ✓ / Source code + tests inspected ✓ / security & errors ✓ / finite budget ✓ / cross-team contracts provisional ✓ / tests designed ✓ / tests actually run ✗ / P0 cross-team contracts resolved ✗。したがって **Draftのまま維持**。


## 18. Round-2 契約整合と訂正（2026-10-30 / rev 0.2, Draft維持）

**検証した他担当Draft**: CHAT_01 [PR #2](https://github.com/choko510/aiagent-md/pull/2) の共有UUIDv7、nullable `Task.conversationId`、1開始の`recordVersion`、`Run.leaseEpoch`と`Task.goalRevision/activeRunId`。CHAT_03 [PR #6](https://github.com/choko510/aiagent-md/pull/6) の`descriptorRevision` digest / `registryGeneration`とTool Gateway。CHAT_09 [PR #9](https://github.com/choko510/aiagent-md/pull/9) の`tenantId/ownerUserId` ACL。CHAT_10 [PR #10](https://github.com/choko510/aiagent-md/pull/10) には現状`gen_random_uuid()`、`revision=0`、`tasks.conversation_id NOT NULL`、Run lease fencing列がないという相違がある。CHAT_04 [PR #3](https://github.com/choko510/aiagent-md/pull/3) のSearch RouterはProvider選択をGateway再認可へ委譲。これらのPRは**mainに未マージ**であり確定契約ではない。

### 18.1 実行境界 [PROVISIONAL: CHAT_01/02/03/09/10]

1. Gatewayが発行したopaqueな`AuthContext(tenantId,actorId,ownerUserId)`、`RunFence(taskId,runId,leaseEpoch,goalRevision,recordVersion)`、`ToolApproval`、`descriptorRevision`だけを採用する。Provider bodyやLLM queryから権限情報を生成しない。
2. 公開IDはServer生成の正規UUIDv7。Amazon ASIN / X Post ID / hotel IDは独立のProvider-native identifierでありUUIDを強要しない。`tenantId`とDBの`workspace_id`の同値性、`actorId`と`ownerUserId`の同値性を仮定せず、CHAT_09/10が決めるowner mappingを必須とする。
3. `Run.leaseEpoch`はSafe Integer範囲外でも正確な`bigint`または厳密decimal wireを保持（正式型はOwner決定）。本MDの`attemptGeneration`は別概念、fence tokenの代用品にしない。送信開始前に`leaseEpoch`、`goalRevision`、`approvalRevision`、`descriptorRevision`、Cancel/SteerをGateway側で再検証。旧Workerは送信も現在RunのEvidence更新も禁止。
4. Watch/Event起点の`Task.conversationId=null`を合法とする。ConnectorはConversationを要求しない。`recordVersion`とDB`revision`の0/1差を暗黙補正せず、migrationと明示的mapperで合意。DBがv4を発行した場合にvalidatorを緩めず、UUIDv7生成経路をOwnerが整備する。

### 18.2 Retry/Quota/OAuth擬似コードへの補足（実装MUST）
- `7.1`の制御順序は概念例。Quota acquire後に`CredentialBroker.scopedHandle`が失敗した場合、**送信前が証明できれば**一度だけLeaseを解放し、Budget reservationも一致させる。そこで例外が出ても`finally`でreceiptを残す。認証ハンドルの取得位置を本改訂で`try`内へ移した。
- `ledger.markExternalAttemptStarting`成功後は送信実態が不確定なら`sent-or-unknown`。Worker kill / ACK lossで無条件再送しない。lease expiryは外部課金0の証拠にならない。401を受信してrefreshする際もその試行のProvider usageを先に精算し、新しい試行はquota/budgetを再取得。
- HTTP 429 `Retry-After`がTask deadlineを越えるなら待機指示か期限切れを返し、期限短縮して再送しない。single-flight token refresh後にはtenant/scope/entitlement/lease generationを再検証。
- Evidence保存が失敗したがraw Artifact receiptがある場合、**同一Attempt**で再parse/commitを実施。再度HTTP実行するなら新しいGateway認可/attempt。Provider resultの古いgoalへの紐付け禁止。

### 18.3 Round-2 要件・追加テスト（**Designed, Not run**）

| REQ | Priority | 検証可能な要件 | TEST / SC |
|---|---|---|---|
| CON-FW-022 | MUST | UUIDv7/internal IDとProvider ID、Tenant/Workspace/Owner mappingを区別 | CON-FW-T28 / SC-08 |
| CON-FW-023 | MUST | leaseEpoch/goalRevision/descriptorRevisionを非損失で別々に保持し、stale送信/commitゼロ | CON-FW-T29 / SC-07 |
| CON-FW-024 | MUST | nullable Conversation、recordVersionの差をOwner mapperで明示処理。暗黙修正なし | CON-FW-T30 / SC-07 |
| CON-FW-025 | MUST | Lease取得後のCredential失敗/kill/timeoutでも未送信/unknownを区別して清算 | CON-FW-T31 / SC-07,09 |
| CON-FW-026 | MUST | CHAT_13 EventSubscriptionの呼出もGatewayを迂回せず承認/費用/ownerを検証 | CON-FW-T32 / SC-11 |

- `T28`: tenant AのUUIDv7、workspace B、外部ASINをInternal IDとして注入 → ACL deny、外部送信0。
- `T29`: 2 Worker、leaseEpoch=9007199254740993、旧descriptorとSteerを競合 → 古いWorkerによる送信/更新0、整数丸め0。
- `T30`: Conversation=nullのWatch起点Task、DB legacy revision=0 fixture → migrationのない暗黙1加算・偽Conversation追加0。
- `T31`: quota獲得直後Credential拒否、送信前kill、送信後ACK loss → 証明済み未送信だけ解放1回、不確定usageはunknown、blind retry0。
- `T32`: disabled X subscription / revoked user、悪意のあるX投稿 → Event Router→Gateway外の実行0。

**Cross-team proposals**: P05-12/P0 → CHAT_01/02/03のcanonical RunFence・descriptor pin・非損失wire。P05-13/P0 → CHAT_09/10のtenant-workspace-owner mapperとUUIDv7/nullable Conversation/revision/leaseEpoch/goalRevision schema migration。P05-14/P1 → CHAT_13 Event Router/Tool Gateway間の権限fencing。P05-15/P1 → CHAT_12 Entity/Source Reliabilityにはnative IDsとcondition付きEvidenceを渡すが、統合判断をConnectorがしない。

未解決のP0契約と未実行の統合試験があるため、`Reviewed`/`Locked`/`Verified`とは呼ばない。
