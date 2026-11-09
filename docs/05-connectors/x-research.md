# X Research Provider — TwiFork / User Login Specification

> Owner CHAT_05 | path `docs/05-connectors/x-research.md` | Status **Draft / provisional** | Rev 0.2 | 2026-11-10 | Python-first / ADR-036 | 関連: CHAT_04 Browser、CHAT_08 UI、CHAT_09 Credentials、CHAT_13 External Events
>
> **変更決定:** 旧Rev 0.1（2026-10-30）の「公式X APIを既定の読取Provider」とする方針を置き換える。**ユーザーがX接続を要求したときはTwiForkを標準候補とし、ユーザー自身のXログインと利用許可の確認を必須とする。** ただしTwiForkは非公式スクレイピング実装であり、Xが必要とする書面許可がなければ通信を実行しない。実装・E2Eテストは未実施。

## 0. 採用判断と禁止条件

Xの検索、投稿の読取、アカウントの投稿監視を必要とするAgent Toolは、CHAT_05の **`XResearchProvider` → `TwiForkReadAdapter`** に集約する。`twifork` は `d60/twikit` のPythonフォークで、配布パッケージ名は `twifork`、インポート名は `twikit`。X公式APIキーの入力やX Developer契約をデフォルトのセットアップ手順にしない [TF1–TF3]。

しかし、技術的にAPIキー不要ということは利用を許されているという意味ではない。**2026-10-09施行のX利用規約は、事前の明示的な書面許可なくスクレイピングすること、および技術的制限の回避を禁止**している [XT1, XT2]。接続先/利用目的/許可の適用範囲が満たされなければ `BLOCKED_POLICY` として停止する。**Xの規約に対するユーザーの同意やログインだけでは、Xの書面許可の代わりにならない。** CAPTCHA、Cloudflare、ログイン保護、429、凍結、レート制限の迂回やアカウントの自動切替をしない。TwiFork READMEにある `impersonate` オプションは回避用途で有効化しない。

TwiForkの現行READMEでは `Client.login()` のパスワードログイン経路が利用できず、ゲストアクセスも利用不可と明記されている。**ユーザーにXの公式画面でログインしてもらい、その認証状態を保護されたBrowser/Credential Brokerから連携する** [TF1]。パスワードやCookieをチャット、Agentのプロンプト、通常のUIフォーム、CLIへ入力させない。未ログインなら検索を開始せず `LOGIN_REQUIRED` / `WAITING_USER_LOGIN` を返す。

公式X APIは**別の認可済みProvider設計として歴史的調査内容を残すだけ**であり、TwiForkが失敗したときに無断で切り替えない。公式APIへの切替は別途ユーザーの明示承認、資格・費用・仕様更新が必要。書面許可や安全なログイン連携ができない環境では `UNAVAILABLE` とする。規約違反を避けるために非公式ミラーへ迂回しない。

## 1. Scope / 責務と非目標

- **In:** 利用条件の審査、ユーザー同意とXログイン要求、読み取り専用の検索/ユーザータイムライン/単独Post取得、取得間隔付きポーリング、ページネーション、出典と範囲・欠落の記録、再認証、セッションの切断。
- **Out:** `XPostObservationDraft`、著者の安定X User ID、Post ID/原文URL/投稿時刻/取得時刻、repost・quote・reply関係、coverage/partial、失敗理由、接続状態。原文は**非信頼データ**。
- **非目標:** Post/いいね/リポスト/フォロー/DM/Space操作、無人ログイン・パスワード収集、CAPTCHA解決の自動化、非公式streamの存在を仮定した即時通知、Xの有料・公式APIの無承認利用。
- **所有権:** CHAT_05がTwiFork Adapterと認証済みread能力を所有。CHAT_04がBrowser Session/Human Handoff、CHAT_09がCredential Broker/Vault/権限と回収、CHAT_08が接続UI、CHAT_13がSubscription・ポーリングschedule・イベントdedup/dispatch、CHAT_07がTask/通知、CHAT_10が物理DBを所有。本書の型は共有正本を変更しない**提案**。
- 対応SC: SC-03, 05, 06, 07, 08, 09, 11, 14, 19, 22。

## 2. 調査と互換性（2026-11-10）

| 根拠 | 確認できたこと | 仕様への反映 |
|---|---|---|
| **TwiFork** `PawiX25/twifork` [TF1, TF2] | Python 3.10+ / MIT。`pip install twifork`、`from twikit import Client`、`set_cookies` / `load_cookies` / `is_logged_in`、`search_tweet`、`get_user_tweets` がREADMEに記載 | 既定のPython read adapter候補。導入時にversion/hash固定とコード/APIシグネチャ再検証。README確認のみでライブ接続実績とはしない |
| **TwiForkのログイン制限** [TF1] | 2026年のXログイン変更後、`Client.login()` のパスワード経路とゲスト取得を使えないと明記 | 人間によるブラウザログインを接続必須条件とする。ライブラリへのパスワード注入を行わない |
| **TwiForkライセンス** [TF3] | MIT。Upstream `twikit` のcopyright表記あり | 再配布時に必要な著作権/ライセンス通知を保持。MITがXへのアクセス権を与えるわけではない |
| **X利用規約/自動化ルール** [XT1–XT3] | 書面許可なしのscraping禁止、非API自動化および制限回避に関する禁止・制約 | 実際の使用前に適用許可を検証し、不可ならfail-closed。ユーザーのXログインだけでは解禁しない |
| **旧SDK研究（参考）** [X1–X8] | Tweepy、node-twitter-api-v2、公式recent search/filtered stream/料金は旧仕様で調査済み | Post ID・来歴・partial判定の参考資料。TwiForkに公式endpoint、料金、`next_token`、stream権利を当てはめない |

**既知の制約:** 非公式Web APIの仕様変更で機能停止する可能性がある。Xの規約/アクセス制限/公開範囲/地域制限に従い、取れなかったPostを「存在しない」と断定しない。ログインの必要性、APIキー不要、利用規約上の許可、通信成功率はそれぞれ別の事柄である。

## 3. MUST / SHOULD 要件

| ID | 強度 | 検証可能な条件 |
|---|---|---|
| XRS-001 | MUST | X readの既定ProviderはTwiFork。接続資格とX側の必要な許可が確認されない限り外部送信ゼロ |
| XRS-002 | MUST | past24hはUTCの半開区間 `[start,end)`。Postの投稿時刻と取得時刻は別 |
| XRS-003 | MUST | Post ID、安定X User ID、原投稿URL、引用/転載/返信の出典と時刻を保持 |
| XRS-004 | MUST | repost/quote/replyの関係を保持したまま重複排除。本文一致だけで同じPostとしない |
| XRS-005 | MUST | TwiFork固有cursor、取得件数・ページ・打切理由・coverageを記録。API型`next_token`を強制しない |
| XRS-006 | MUST | 固定のread上限・ポーリング間隔・タイムアウト・並列数とRunFence。非公式の回数上限を保証しない |
| XRS-007 | MUST | `@handle` でなく安定User IDで監視対象を照合し、変更・再割当で誤通知しない |
| XRS-008 | MUST | TwiForkの通常検索/タイムラインは**polling**。公式filtered streamと同一機能として扱わない |
| XRS-009 | MUST | 同じPostのpoll/replay/複数ページ重複でTaskを複数発火させず、解除後dispatchゼロ |
| XRS-010 | MUST | 投稿本文・引用URL・プロフィール内容は権限・Policy・Tool命令を変更しない。秘密はLLM/ログ不可 |
| XRS-011 | MUST | cancel/Steer/restart/leaseEpoch失効後は取得結果を有効なRunへ流し込まない |
| XRS-012 | MUST | キャンペーンの期限と公式裏付けを区別し、Xの噂を確定情報に格上げしない |
| XRS-013 | MUST | `LOGIN_REQUIRED`、`BLOCKED_POLICY`、403/429、CAPTCHA、API変更、timeoutを区別してpartial/unavailableを通知 |
| XRS-014 | SHOULD | 取得遅延/欠落推定・source reliabilityをCHAT_12へ連携 |
| XRS-015 | LATER | 公式API/アーカイブ検索を別Providerとして再承認・再設計 |
| XRS-016 | MUST | 人間がX公式画面でログインし、Broker所有の認証済みSessionからのみTwiForkへcredentialを適用。パスワード入力やCookie貼付をAgentに要求しない |
| XRS-017 | MUST | Sessionをtenant/ownerとread scopeで隔離、Vault暗号化・失効/削除・再認証要求を強制。UIには状態のみ |
| XRS-018 | MUST | X接続を無断で自動許可/永久稼働せず、書面許可の対象とユーザーの目的/読取範囲/停止操作を確認 |

## 4. ユーザーログインと接続ライフサイクル

**ユーザー体験（UIの表現例）**

1. AgentがX検索を必要とする → **「Xに接続」** と表示。TwiForkが非公式手段であること、Xの必要な書面許可がある場合に限り利用できること、読取目的・取得対象・有効期間・自動監視の有無・停止方法を説明。接続を断る選択肢を用意。
2. 必要な許可の適用範囲を管理側で確認できなければ `BLOCKED_POLICY`。ユーザー同意だけでパスしない。許可が確認できた場合、**「Xでログインする」** を提示。
3. CHAT_04/09の**Broker管理下のHuman Browser Handoff**でX公式ログイン画面を表示。ユーザーが自身のXアカウントでログイン（2FA/passkeyが必要ならユーザー自身が完了）。Agent/LLMはその画面を操作しない・Cookie/パスワードを読めない。
4. Brokerが許可されたSession内から必要最小限のX Cookie（現行README例: `auth_token`、`ct0`）を抽出し、owner/tenant/provider/scopeに紐付く暗号化Vaultへ移管する。原CookieをLLM・Evidence・通常のBrowserツール・HTTPレスポンス・監査ログへ出さない。CookieがBrokerからPrivsep Adapterへ安全に渡せない場合は **`UNSUPPORTED`** とし、ユーザーにチャットへのCookie貼付を求めない。
5. CHAT_05のTrusted Privsep WorkerがBrokerから限定されたcredential leaseを受け、TwiFork Clientへ一時適用し `is_logged_in()` をチェック。成功時のみ `CONNECTED_READ_ONLY`。認証されたXアカウント識別子を記録して想定した接続主体か照合する（機密値は返さない）。次のread操作前にもpermission/owner/credential freshnessを検査する。
6. UIで「接続済み・閲覧のみ」「再ログインが必要」「切断」を示す。ログアウト、Cookie期限切れ、X側拒否、アカウント不一致、owner revoke時は`NEEDS_REAUTH`/`PAUSED`として新しい外部送信とEvent dispatchを停止。再ログインは人間によるHandoffで再実施する。切断時はlease無効化、Vault credential削除/復旧可能な複製の破棄、subscription停止と監査metadata保持を行う。

**状態の提案（CHAT_05内部）:** `DISCONNECTED → POLICY_CHECK → LOGIN_REQUIRED → WAITING_USER_LOGIN → VALIDATING → CONNECTED_READ_ONLY ↔ NEEDS_REAUTH`、任意状態から`BLOCKED_POLICY`/`REVOKED`/`UNSUPPORTED`。`BLOCKED_POLICY`/`REVOKED` は自動再試行しない。権限のないSessionをバックグラウンドで復活させない。

**重要:** 本書はXログイン画面をTwiForkで自動処理できるとは主張しない。受け渡し経路はCHAT_04/09の設計・E2E検証に依存し、現時点では**設計案**である。ログインが完了してもX側の書面許可・レート・規約条件を代替しない。

## 5. Provider-only I/O（共有型を上書きしない）

~~~typescript
interface XResearchQueryV2 {
  mode: "recent_search" | "user_timeline" | "post_lookup";
  query?: string;
  authorUserId?: string;    // X decimal ID string; @handleは表示・初期解決用
  postId?: string;
  startTime: string; endTime: string; // UTC ISO [start,end)
  excludeReplies?: boolean; excludeReposts?: boolean; includeQuotes?: boolean;
  maxPosts: number;         // 初期提案: 100、ハード上限500/Task
}
interface XPostObservationDraftV2 {
  postId: string; authorUserId: string; handleObserved?: string;
  text?: string; sourceUrl: string; createdAt?: string; retrievedAt: string;
  referencedPosts: {type:"retweeted"|"quoted"|"replied_to"; id:string}[];
  missingFields: string[]; mediaPresent?: boolean;
  isOfficialClaim: boolean; verifiedByIndependentSource: boolean;
  sourceQueryKind: string;
  coverage: {
    from: string; to: string; complete: boolean;
    returnedCount: number; pagesRead: number;
    truncatedReason?: string; providerVersion: string;
  };
  rawArtifactRef?: string;
}
interface XReadCursorV2 {
  provider: "twifork"; credentialVersion: string; queryFingerprint: string;
  authorUserId?: string; paginationCursor?: string; latestSeenPostId?: string;
  lastCompleteFetchedAt?: string; subscriptionGeneration?: string;
}
interface XConnectionStatusV1 { // UI projection: no secret/cookie/token
  provider: "twifork"; state:
    "DISCONNECTED"|"BLOCKED_POLICY"|"LOGIN_REQUIRED"|
    "WAITING_USER_LOGIN"|"VALIDATING"|"CONNECTED_READ_ONLY"|
    "NEEDS_REAUTH"|"PAUSED"|"REVOKED"|"UNSUPPORTED";
  connectedAccountId?: string; lastValidatedAt?: string; reasonCode?: string;
}
~~~

`XResearchQueryV1.filtered_stream` は**旧公式API前提のprovisional mode**として非対応扱い。既存保存データの読込では「`UNSUPPORTED_TRANSPORT`」へ明示移行し、黙ってTwiForkポーリングへ変換しない。CHAT_13への新たな transport識別は`x_twifork_poll`を**提案**し、現在の`x_timeline_poll`を実装済みと仮定しない。Post/author IDsはJS numberに変換せずdecimal stringで保持し、欠けた時刻・引用参照を補完して捏造しない。

## 6. シーケンスとPython-first実装方針

~~~mermaid
sequenceDiagram
 participant U as User
 participant G as Gateway/Sentinel
 participant B as Broker/Human Handoff
 participant X as CHAT_05 TwiFork Adapter
 participant E as Evidence/CHAT_13
 U->>G: X read intent
 G->>G: provider policy / X written permission / purpose / owner
 alt policy not satisfied
  G-->>U: BLOCKED_POLICY (no X call)
 else policy satisfied
  G-->>U: Ask to connect X and log in
  U->>B: Sign in on X official site, 2FA if required
  B->>B: Isolate and seal session credentials
  G->>X: Approved read, bounded lease, current fence
  X->>B: Obtain scoped credential inside Privsep
  X->>X: TwiFork is_logged_in()
  alt invalid session
   X-->>G: NEEDS_REAUTH
  else valid
   X->>X: Search / timeline / bounded page iteration
   X->>E: Normalize, provenance, coverage, dedup candidate
   E-->>G: commit/ack
   G-->>U: Results + evidence + partial/status
  end
 end
~~~

Python-firstの**擬似コード**（SDKの未検証な内部page APIを契約とみなさない）:

~~~python
from twikit import Client  # provided by the "twifork" distribution

async def read_x_from_trusted_worker(request, approved):
    await gateway.assert_owner_policy_permission_fence_and_read_scope(approved)
    credential = await credential_broker.obtain_scoped_session_for_privsep(approved)
    # セッションCookieはこのTrusted Privsep Workerのみで使用。LLM/Runtime Cellには渡さない。
    client = Client("ja")
    client.set_cookies(credential.cookies)
    if not await client.is_logged_in():
        return needs_reauth_no_retry()
    await gateway.recheck_fence_and_approval(approved)
    if request.mode == "recent_search":
        page = await client.search_tweet(request.query, "Latest")
    elif request.mode == "user_timeline":
        page = await client.get_user_tweets(request.authorUserId, "Tweets")
    else:
        return unsupported_unless_pinned_sdk_lookup_verified()
    return await normalize_and_commit_bounded_pages(
        page, request, approved, preserve_missing_fields=True
    )
~~~

上記の `gateway`/`credential_broker`/`normalize_and_commit_bounded_pages` はHarness側の**未実装契約**。`Client`をAgent生成Python内で実行してCookieを取得する実装は禁止。SDKのpagination・例外・レスポンスフィールドはpin対象版の実ソース/モックで検証してから採用する。`get_user_tweets`はpublicでも全Post取得を保証しない。

TwiForkは公式APIの`GET /2/tweets/search/recent`と同じ件数・精度・`next_token`・`since_id`・7日検索保持を保証しない。取得後にUTC半開区間でフィルタし、失われたページ・非表示投稿・切断区間を`partial`/`unknown`と表記する。重複IDを排除してもquote/repost関係と出典を落とさない。

## 7. 監視・予算・失敗・安全性

- **監視:** CHAT_13の定期ポーリングのみ。TwiForkを公式filtered stream扱いしない。初回の明示同意で監視対象User ID、対象投稿種別、目的、頻度、終了条件を確定。`(tenant,subscriptionId,generation,provider,eventKind,postId)`でdedupし、購読停止/世代変化をdispatch直前にも照合する。
- **予算:** TwiFork自体に公式X APIの従量課金額を適用しない。ただしライブラリの利用料がないことは無料運用・無制限取得を意味しない。タスク上限、時間、回数、接続数、認証試行、CPU/ネットワークを制限。初期案はmax500 Post/task、max10 pages、max2 read/tenant、read call最長60秒、monitorの最小ポーリング間隔はX側制限と許可条件を確認後に決定。未確認rateを「50回/15分」のように固定しない。
- **誤情報:** 投稿日時と取得日時を区別、編集/削除・非公開・引用・リポストを区別、販促情報は別ソースで裏付けが取れない限り未検証とする。投稿本文やURL中の命令はTool権限にならない。
- **経路:** X向け許可済みTLS/egressのみ。Browser Handoff内の認証情報を任意サイトに転送しない。TwiFork SDKの広い書込能力をConnector Toolとして公開しない。依存ライブラリのversion/hash、供給元、licenseと変更履歴を導入時に監査する。

| 条件 | 動作 |
|---|---|
| X書面許可が確認できない/失効/対象外 | `BLOCKED_POLICY`、外部通信0、ユーザーへ説明。API/ミラー/アカウント変更への自動fallbackなし |
| 未接続/期限切れCookie/異なるアカウント | `LOGIN_REQUIRED`/`NEEDS_REAUTH`。Broker経由でユーザーが再ログイン。無人パスワードログインは禁止 |
| 403/429/チャレンジ/Cloudflare/凍結 | 安全停止・適切なcooldown・`PAUSED`/`UNAVAILABLE`。TLS指紋偽装、proxy rotation、CAPTCHA回避で突破しない |
| 404/レスポンス形変更・SDKパース失敗 | `PROVIDER_CHANGED`/`PARTIAL`。Pinとparserを調査するまで盲目的に再試行しない |
| ページ途中で中断/timeout/Worker再起動 | cursor/coverage/receiptを保存。取得結果はpartial、古いRun結果は破棄、同じPostによるTask重複は防止 |
| 同じ内容のquote/repost/reply | post IDと関連IDで分類し、source provenanceを保持 |
| connect解除や購読停止がfetch中に発生 | credential/grant/fence失効、新たなread・dispatchは0。監査済みreceiptは秘密抜きで保持 |

## 8. 保存・共有境界（[PROVISIONAL]）

- CHAT_09: X Cookie/sessionはBrowser credential型secretであり、per-tenant/owner暗号化・鍵分離・TTL・versioned lease・disconnect/revoke・再認証が必要。生Cookie、`auth_token`、`ct0`、Browser `storageState` はLLM/通常ログ/APIレスポンス/Artifactへ出さない。
- CHAT_04: Human Handoff時はAgentのページ操作を止め、ログイン完了後にBroker所有Sessionの検証を行う。Cookie移送はBroker-to-Privsepの保護経路に限定。安全な経路が未実装ならX接続をサポート済みと表示しない。
- CHAT_08: 接続手続き、許可状況、`LOGIN_REQUIRED`/`NEEDS_REAUTH`/`BLOCKED_POLICY`、scopeと切断UI。書面許可確認の証跡は一般Userに閲覧不能な内部metadataだけで管理し、無承認の個人申告を許可と判定しない。
- CHAT_13: `x_twifork_poll` のTransport追加、stable X User ID、cursorとcoverage、polling/停止・dedupとgap記録。既存`x_filtered_stream`はこのAdapterで`UNSUPPORTED_TRANSPORT`。変更はOwner合意後に共有型へ反映する。
- CHAT_10: 認証metadata、credentialRef、取得receiptとcursorをowner/tenant分離、CAS/fenceとpermission revisionで管理。物理DDLはここで定義しない。
- CHAT_12: 相関するリポスト群を独立裏付けとして加点しない。誤情報は他ソースの検証が必要。
- CHAT_03/07/09: User同意・Policy・Gateway・Sentinel・RunFenceは不可欠。read権限から投稿・DM等のwrite権限を派生させない。

## 9. 実装順序（別PRでコード実装する場合）

1. **XR-01:** X利用条件/許可のgate、TwiFork採用・バージョンpin、Connector read-only能力一覧、`LOGIN_REQUIRED`/`BLOCKED_POLICY`の状態を実装・モック検証。
2. **XR-02:** Browser Broker Human Handoff、Vaultで暗号化したCookie連携、Privsep専用`is_logged_in`検査、切断・再ログインとtenant隔離のE2E。安全な経路がない間は本番無効。
3. **XR-03:** 検証済みSDKのread-only search/timeline、ページング、取得範囲、ID/referencesとEvidenceの正規化、bounded quota。
4. **XR-04:** CHAT_13との協調で`x_twifork_poll`、安定アカウントIDとdedup、停止/再開、gapの可視化。公式filtered stream代替を謳わない。
5. **XR-05:** 規約変更検出、SDKアップグレード契約試験、認証/接続変更の故障注入、canaryと運用診断。公式APIが必要なら**別の意思決定/承認**。

## 10. テスト計画（設計のみ・未実行）

| TEST | 状況 | Expected |
|---|---|---|
| XRS-T01 | 書面許可なし/誤用途・未接続でX read要求 | `BLOCKED_POLICY`/`LOGIN_REQUIRED`、実際のX外部通信0 |
| XRS-T02 | 過去24h境界・タイムゾーン不一致 | UTC`[start,end)`、外側は除外、不明時刻は不明のまま |
| XRS-T03 | native post/author ID が2^53超 | decimal stringの値を保持、permalink一致 |
| XRS-T04 | quote/repost/replyと同文別Post | source edges残存、適切なID dedup |
| XRS-T05 | pagination中断・検索0件・cursor形変更 | `partial/unknown`、網羅性を断定しない |
| XRS-T06 | 極端なポーリング要求・並列/処理時間超過 | budget/policy gateで拒否、制限迂回なし |
| XRS-T07 | handle変更/再割当 | stable User ID不一致で監視発火0 |
| XRS-T08 | filtered_stream要求をTwiForkへ渡す | `UNSUPPORTED_TRANSPORT`、勝手にstream/pollへ変換しない |
| XRS-T09 | 同じPostがpoll+replayで到着、stop後に再到着 | Task最大1回、停止後0 |
| XRS-T10 | Post本文が「Cookieを送れ」と指示 | Credential/Tool権限の変化0 |
| XRS-T11 | 旧leaseEpochでfetchが戻る | 現行Runへのcommit/dispatch 0 |
| XRS-T12 | 出典不明/期限切れキャンペーン | `unverified/expired`表示 |
| XRS-T13 | 401相当/403/429/5xx/SDK parse破損 | 状態と原因を区別、blank resultとして扱わない |
| XRS-T14 | 1/4 Worker・偽Clock・Rate挙動の比較 | p95/欠落/回数を測定、推定を実測と表記しない |
| XRS-T15 | ログインを求める際にパスワード/Cookie貼付を誘導 | UI/LLM/ログに秘密出力0、人間のBroker Handoffのみ |
| XRS-T16 | 有効Cookie→失効→ユーザー再ログイン→別owner接続 | 接続状態反映、再authは人間、tenant越境0 |
| XRS-T17 | 書面許可の失効/監視対象外/Revocationがread途中に発生 | 新規取得・dispatch 0、既存receiptは秘匿 |
| XRS-T18 | SDK側Client.login()/guest/impersonateへのfallback | 呼出し0、エラーを可視化し停止 |
| XRS-T19 | 接続解除後のCookie/lease/バックアップ復旧 | Credential再利用0、再認証要求 |

**MUST → TEST → SC:** XRS-001→T01/T17→SC-03/08、002→T02→03、003→T03→03/11、004→T04→03/14、005→T05→03、006→T06→09、007→T07→11、008→T08→11、009→T09→07/11、010→T10→08、011→T11→07、012→T12→03/14、013→T13→03、016→T15/T18→06/08、017→T16/T19→06/08、018→T01/T17→03/08。SHOULDの014→T14→SC-14、LATERの015は次期provider受入時に別ケース追加。

## 11. 根拠・参照

- [TF1] **READMEを確認 / ライブ接続未実施:** [PawiX25/twifork README](https://github.com/PawiX25/twifork/blob/main/README.md) — Cookieによるログイン、`Client.login()`失敗、guest access不可、`set_cookies` / `is_logged_in` / `search_tweet` / `get_user_tweets`、非公式スクレイパーの注意書き。
- [TF2] **パッケージ情報を確認:** [TwiFork PyPI](https://pypi.org/project/twifork/) / [setup.py](https://github.com/PawiX25/twifork/blob/main/setup.py) — package `twifork`、import `twikit`、Python 3.10+、依存性。導入時にバージョン固定・再監査。
- [TF3] **LICENSEを確認:** [TwiFork MIT LICENSE](https://github.com/PawiX25/twifork/blob/main/LICENSE) — OSS利用許諾。Xアクセスの許諾とは別。
- [XT1] **現行サービス規約を確認:** [X Terms of Service（2026-10-09 effective）](https://x.com/en/tos) — 事前の書面許可なしのscraping禁止、技術制限回避禁止。
- [XT2] [X利用規約（日本語）](https://x.com/ja/tos) — 同内容の日本語説明。英語版と内容や施行日が異なる場合は現行適用版優先。
- [XT3] [X automation rules](https://help.x.com/en/rules-and-policies/x-automation) — 非API自動化、レート制限、アカウント利用に関する規則。
- [X1–X3] **旧仕様の一次資料（TwiFork契約ではない）:** [X Search](https://docs.x.com/x-api/posts/search/introduction)、[Filtered Stream](https://docs.x.com/x-api/posts/filtered-stream/introduction)、[公式料金](https://docs.x.com/x-api/getting-started/pricing)。
- [X4–X8] **旧Rev 0.1のコード調査:** [Tweepy](https://github.com/tweepy/tweepy)、[node-twitter-api-v2](https://github.com/PLhery/node-twitter-api-v2)、[Tweepy tests](https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/tests/test_client.py)、[node-twitter-api-v2 PR #655](https://github.com/PLhery/node-twitter-api-v2/pull/655)。ID/pagination/retryの一般設計のみ参考。

## 12. Rev 0.1からの移行と未確定事項

- **廃止:** 公式`search/recent`・Timeline・Filtered Streamを既定とした図、公式API token/billing/`next_token`、契約があればstream優先という旧方針。公式従量単価 `$0.005/Post` をTwiForkのコストと見なさない。
- **維持:** Evidence/provenance、stable ID、引用関係、coverage、RunFence、分離したEvent Router、read/write権限分離、秘密をLLMへ渡さない制約。
- **横断差分:** CHAT_13 `docs/13-event-ingestion/event-driven-agent.md` は旧公式APIベースのstreamを現在も提案しており、`x_twifork_poll` の契約へ**Owner側の後続変更が必要**。このPRで共有型やEvent Router正本を黙って書き換えない。CHAT_04/08/09へのHandoff・credential連携も未実装。
- **未検証:** 実際のユーザーX許可、ログイン済みCookieの安全な移送、SDKの現在の取得成功率、ユーザーID/投稿の取得欠落、現在のrate制限、CI/ライブ検証。これらをVerified/Production-readyと表示しない。
