# Safe HTTP Fetch — Detailed Implementation Specification

> Owner: CHAT_04 | Canonical path: `docs/04-web-browser/http-fetch.md` | Status **Draft / PROVISIONAL** | Revision 0.1 | Research date 2026-10-30 JST | Source main `b45d4e989998663a00ef09673e69ae1f4191f00d`  
> Dependencies: Search Router / CHAT_03 Tool Gateway / CHAT_09 network-egress-and-ssrf / CHAT_10 Artifact Store / CHAT_13 change-intelligence. **Implementation and application tests not run.**

## 0. 結論と採用案

Web調査では、検索候補URLの取得が最も広いネットワーク侵入面になる。**Redirectごとに送信先を再評価し、DNS解決結果と実接続先を一致させる**。Fetcherはread-onlyのHTTP GET/HEADをデフォルトとする。Contentを時間・サイズ・MIMEで制限し、元URL・最終URL・取得時刻・HTTP metadataを保持する。取得不能とデータ0を分離。

**採用案:** TSの明示的FetcherとPolicy-enforced transport、redirect手動追跡、固定されたDNS-resolution→connect、streaming byte cap、conditional GET/cache；Scraplingは専用sidecarの抽出選択肢。
**却下案:** 自動redirect+自由URLのglobal fetch（TOCTOU/SSRF）、Browser first、静的HTMLをLLMへ丸投げ、Curl外部CLIやhost shell。
**明確な非目標:** BrowserのContext/Cookie操作、外部副作用POST、CSS/XPath抽出、Watch閾値判定、robots.txtの法的効果の判定。

## 1. 責務・利用者GoalとScenario

入力は許可済Target URLとtask budget、出力はUTF-8/HTML等のopaque bytes artifact+解析可否+headers+status+resolved network metadata。robotsは利用規約確認とHTTP取得可否決定の一入力だがrobots許可は認可・規約適合を意味しない。
受入 Scenario: SC-08, SC-07, SC-06, SC-09, SC-05, SC-12, SC-02。認証・費用・観測を持たない検索結果を実測事実と断定しない。

## 2. 実ソース横断調査・代替比較

[S1] **undici** `1b7b18e59f6c1e9d59af188bb0488890166a0c84` / MIT, Code verified 2026-10-30: [`lib/interceptor/redirect.js — createRedirectInterceptor`](https://github.com/nodejs/undici/blob/1b7b18e59f6c1e9d59af188bb0488890166a0c84/lib/interceptor/redirect.js); [`lib/handler/redirect-handler.js — RedirectHandler`](https://github.com/nodejs/undici/blob/1b7b18e59f6c1e9d59af188bb0488890166a0c84/lib/handler/redirect-handler.js); [`test/redirect-request.js — HTTP 300/redirect chain and origin tests`](https://github.com/nodejs/undici/blob/1b7b18e59f6c1e9d59af188bb0488890166a0c84/test/redirect-request.js).
[S2] **Scrapling** `26dc85a4fa307257fb5c669c80aa44b15acb84c4` / BSD-3-Clause（固定SHAのLICENSE確認済み）, Code verified 2026-10-30: [`scrapling/fetchers/requests.py — Fetcher/AsyncFetcher`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/fetchers/requests.py); [`scrapling/engines/static.py — FetcherSession`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/engines/static.py); [`tests/fetchers/sync/test_requests_session.py — session/retry/proxy/header tests`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/fetchers/sync/test_requests_session.py).
**Undici**のredirect interceptorはmaxRedirectionsとhandlerを分離し、test/redirect-request.jsはorigin跨ぎ等を試験しているがSSRF対策を自動保証しない。**Scrapling**のFetcher/AsyncFetcherはcurl_cffiで静的HTTP、Sessionはretries/headers/proxyを持つが、このHarnessのPolicy所有者ではない。両者は直接importで無条件にネットワークを開かせず、制限されたtransportへ包む。
これらのソースとテストは**読解したが実行していない**。参照元の性能やサイト成功率を本Harnessへ流用しない。コードコピーせず、公開APIまたは独立したAdapterで利用する。対象OSSのlicenseと導入時のupstream変更は運用前再確認。Issue/PRの網羅調査は未完であり、未確認の事項をCode verifiedと呼ばない。

## 3. 契約境界・依存図・Owner

```text
Agent Run → CHAT_03 Intent/Budget → HTTP Fetch Coordinator → CHAT_09 destination grant & pinned DNS → HTTP transport (no implicit redirect) → byte-limited stream → ArtifactStore(01/10) → Extract(04-08)
```

[PROVISIONAL: CHAT_01] 外部公開Task/Run/BrowserSession/Artifact/Evidence/Event/Error IDはcanonical UUIDv7を要求するDraft #2と整合するまで正式型を定義しない。内部ステップIDは公開IDと混同しない。TenantId/OwnerUserId/WorkspaceIdは等値と仮定しない。[PROVISIONAL: CHAT_10] DB physical schemaを本書へ導入しない。共通Taskのnullable Conversation、recordVersion(1開始) vs DB revision(0)、Run leaseEpochのnumber vs bigintおよび欠落しているDB CAS/fencingは #2/#1/#10の未確定契約として明示する。

Fetcher WorkerはHTTP transportを所有するが、NetworkPolicyのルールはCHAT_09、認可と請求LedgerはCHAT_03、Artifactの正本はCHAT_01/10、Cacheの根拠時刻はCHAT_01である。DNS pinning実装をCH04側で曖昧に代用しない。

## 4. 安定要件・MUST/SHOULD/LATER

| ID | 強度 | 仕様上の不変条件 | 測定可能なExpected | Scenario |
|---|---|---|---|---|
| FETCH-001 | MUST | URLはhttp/https・userinfoなし・許可originのみ | 不正scheme/credentials URLの送信0 | SC-08 |
| FETCH-002 | MUST | DNS A/AAAAを送信直前に検査、実connectの宛先IPを固定 | 内部/loopback/linklocal/multicast/metadata IP到達0 | SC-08 |
| FETCH-003 | MUST | 全30x Locationを新URLとして再検査、hop数上限 | redirectによるprivate宛て到達0 | SC-08/SC-07 |
| FETCH-004 | MUST | proxy/CONNECT/DoH/IPv6/Alt-Svcでegressの迂回不可 | 別経路も同一Policy拒否 | SC-08 |
| FETCH-005 | MUST | GET/HEADのみ既定実行、cookies/credentialsの任意送信禁止 | unsafe method/headerなし | SC-08/SC-06 |
| FETCH-006 | MUST | 最大body解凍後byte数・圧縮率・read deadlineを制限 | zip bomb/無限stream遮断 | SC-08/SC-09 |
| FETCH-007 | MUST | HTTP status・MIME・charsetとparse失敗を区別 | HTML以外をHTMLとして扱わない | SC-05 |
| FETCH-008 | MUST | Cache keyにTenant/auth scope、URL/Accept/locale等を含める | 別Tenantのcache誤利用0 | SC-06/SC-08/SC-12 |
| FETCH-009 | MUST | 観測時刻/ETag/Last-Modified/304を原本時刻と分離 | 304を新価格観測と断定しない | SC-02/SC-12 |
| FETCH-010 | MUST | 429/503/Retry-Afterで有限retryとhost rate budget | 指定上限超過call0 | SC-07/SC-09 |
| FETCH-011 | MUST | cancel/steer/expired Runは新しいrequestを送信しない | 中止後の追加request0 | SC-07 |
| FETCH-012 | MUST | Worker crashで送信済/保存済を照合、UNKNOWNは誤報しない | lost ACKが0円/更新なしと誤認されない | SC-07/SC-12 |
| FETCH-013 | MUST | HTTPレスポンスを非信頼データとして格納・抽出へ渡す | prompt注入にTool権限を奪われない | SC-08 |
| FETCH-014 | SHOULD | robots.txtとサイト制約をsite policyに記録 | robots拒否に従う | SC-05 |
| FETCH-015 | SHOULD | HEAD→GET最適化はサイトサポートを検証して利用 | HEAD非対応時適切に戻す | SC-09 |

## 5. 内部I/O型・Version / Validation

```typescript
type FetchRequest = { schemaVersion: 1; url: string; method: "GET"|"HEAD"; allowRedirects: boolean; maxRedirects: number; acceptMime: string[]; maxDecodedBytes: number; maxCompressedBytes: number; deadlineAt: string; authenticatedScopeRef?: string; cachePolicy: "bypass"|"conditional"|"allow-stale-for-display" };
type FetchObservation = { initialUrl: string; finalUrl?: string; chain: Array<{from:string;to:string;status:number}>; httpStatus?:number; contentType?:string; charset?:string; headersSubset:Record<string,string>; observedAt:string; sourceLastModified?:string; etag?:string; bytesDecoded:number; sha256?:string; artifactRef?:string; freshness:"observed"|"not-modified"|"stale"|"unknown"; status:"ok"|"not_modified"|"not_found"|"blocked"|"partial"|"failed" };
interface PolicyTransport { connectToPinnedAddress(hostname:string, port:number, ips:string[], tlsServerName:string, grantRef:unknown):Promise<unknown> }
```

- これは**CHAT_04内のwire proposal**。Task/Run/ToolEnvelopeの正本の代替でない。DTOの schemaVersion はmajorで互換性を検査し、不明majorや余剰危険fieldをreject、optionalのundefinedとunknownを混同しない。
- すべての呼出しにserver-authoritative `tenantId, ownerUserId, taskId, runId, goalRevision, leaseEpoch`の**検証済み参照**を要求する。これらをLLMやpage dataから生成しない。Epochは安全なdecimal stringまたはbigintとし、JS numberへの精度喪失変換を禁止。[PROVISIONAL: CHAT_01/02/10]
- `recordVersion`と`goalRevision`と`leaseEpoch`は別概念。CASやfencingは最新永続行から判定し、期限切れまたはlost leaseのコマンドは拒否。TaskがConversationを持たない場合もTask ownerを認可する。
- `maxRedirects<=5`、`maxDecodedBytes<=8 MiB`、`maxCompressedBytes<=4 MiB`、`connect <=5s`・total <=20s（初期ハード上限、Configで厳格化）。URLに制御文字、encoded-userinfo、IP decimal/octal/hex等を含む場合はauthority parserとPolicyで厳格確認。DNS結果が不明ならfail-closed。

## 6. 時系列・状態遷移・Sequence

```text
PLANNED → POLICY_CHECK → RESOLVED_AND_PINNED → REQUESTING → HEADERS_RECEIVED → STREAMING → VERIFIED_METADATA → SAVED
POLICY_CHECK/STREAMING → DENIED|CANCELLED|PARTIAL|FAILED
3xx → POLICY_CHECK(next hop); 304 → CACHE_VALIDATION(only same scoped key)
```
1. Gateway Intent保存+許可→2. RedirectごとにURLと解決IP候補をCHAT_09で承認→3. transportは自動redirect禁止、接続先IP pin/SNIを一致→4. header受信・MIME/Content-Length事前拒否→5. streaming byte cap/timeout→6. safe metadata・raw artifact保存→7. conditional/304なら旧artifact参照と元観測日を返す。

## 7. 手続き・Pseudo code

```typescript
async function safeFetch(req, ctx) {
  const visited = new Set<string>(); let url = parseUrlStrict(req.url);
  for (let hop=0; hop <= Math.min(req.maxRedirects, 5); hop++) {
    ctx.assertLiveFence(); ctx.assertDeadline();
    if (visited.has(url.href)) return failed("REDIRECT_LOOP");
    visited.add(url.href);
    const grant = await gatewayReserve("fetch", ctx);
    const resolved = await egressPolicy.authorizeAndPin(url, ctx.owner, grant); // A + AAAA/IPv6
    const res = await transport.request({url, method:req.method, pinnedIps:resolved.ips,
      tlsServerName:url.hostname, redirect:"manual", signal:ctx.signal, timeoutMs:ctx.remainingMs});
    if (isRedirect(res.status)) { await res.body.cancel(); url = resolveLocationValidated(res.headers.location,url); continue; }
    const stream = limitCompressedAndDecodedBytes(res.body,req,ctx.signal);
    const artifact = await stageArtifact(await stream.consume(),ctx);
    await persistObservationWithFence(artifact, ctx); return result(res,artifact,visited);
  }
  return failed("REDIRECT_LIMIT");
}
```

アルゴリズムの `assertAuthorized`, `assertLiveFence`, `gatewayReserve`, `persistObservation` は**外部Ownerが提供する契約上の呼び先**であって、このMDが独自のPolicy/DDLを実装する意味ではない。次の物理操作は最新lease/goalRevisionでfenceを必須にする。

## 8. 異常・競合・再開・Lost ACK

| 注入/障害 | 検出 | Retry可否と収束 | 画面/監査 |
|---|---|---|---|
| 302 private-IP | Location→Policy | 再試行禁止 | EGRESS_DENIED |
| DNS rebinding | connect IP不一致 | 接続前中断 | SECURITY_BLOCKED |
| gzip bomb | decompressed cap | body破棄 | PARTIAL |
| 429 | Retry-Afterとglobal budget | deadline内に最大1回 | RATE_LIMITED |
| worker kill | intentはあるがresultなし | readだけbudget再承認 | UNKNOWN |
| 304 cache miss | Vary/etag不一致 | fresh GET再試行は別grant | CACHE_INVALID |
| cancel while streaming | Abort + staged artifact cleanup | 新規送信なし | CANCELLED |
Workerが返したACKだけでは外部操作のexactly-onceは成立しない。永続Ledger/Checkpointと観測状態に基づいて `DONE / SAFE_TO_RETRY / UNKNOWN` の区別を維持し、UNKNOWNの非冪等操作は自動再送しない。Cancel/Steer後の実行中read結果は歴史的証拠にのみ格納し、現行Goalを満たすと扱わない。

## 9. Security / Privacy / Trust Boundary

DNS A/AAAA、CNAME、IPv4 mapped IPv6、redirect先、proxy経路、接続先IP、TLS SNI/Hostを同時に検証する。ネットワークネームスペースのdeny egressも必須。CookieはBrowserWorker管理でありFetcherへ無断複製しない。robotsはポリシーを弱めない。scriptやHTMLを実行しない。
- Web/HTML/HTTP response, tool output, Cookie-protected contentは全てuntrusted data。ページ内の指示がRule・Role・tenant/owner・Tool Gatewayのpolicyを上書きしない。
- Host Shell/SSH/Docker socketやprivileged containerへのAgent権限付与は禁止。承認なき購入・予約・投稿・送金・外部メッセージ送信は禁止。
- Raw Artifact/Traceは秘密・Cookie・Auth header・PIIを削除/マスクし、アクセスscope・暗号化・期限はCHAT_09が定義。

## 10. Budget / Trace / Benchmark

初期提案: 同時Fetch 4/Task、1 hostに最大1 req/2s（より厳格なサイト規約が優先）、redirect 5、decode 8MiB/response、20s/call、総100MiB/Task。すべてServer config + Gateway Budgetのminを採用。
- Trace: `taskId, runId, toolAttempt, goalRevision, leaseEpoch, tenant-scoped correlation, adapterVersion, operation, durationMs, bytes, retryCount, errorClass, costEstimate, observedAt, artifactRef`。URLのquery secretとbodyはログへ書かない。
- Benchmark: 同じURL/資格/地域/ネットワーク/fixture/冷温起動条件でp50/p95、memory RSS、成功/partial率、per-step API費・token費を比較。実測前に「4倍」「90%削減」などの速度/成功保証はしない。測定基準と閾値はCHAT_11へ合意提案。

## 11. Persistence・Transaction・Migration（CHAT_10への提案）

FetchAttemptと每hop network grant、status/headers subset/ETag/cache metadata、content hash、artifactRef、lastObservedAt、originalCapturedAtを永続論理情報として提案。originをcross-tenant再利用しない。物理DDL・cleanup所有権はCHAT_10。
- **atomic boundary**: Operation intentとlease/fence snapshotの永続化 → Gateway実行 → Result/Artifact metadata＋Tool outcomeをトランザクションで記録 → outbox。ネットワーク呼出しをDB transactionと原子的にすることは不可能。送信後未保存は`UNKNOWN`。
- 公開ID UUIDv7の生成、nullable Conversation、recordVersion初期値、leaseEpochの幅/Run列/Task activeRunIdのCAS、Tenant↔Workspace mappingはCHAT_01/02/09/10へ一括契約試験で要合意。勝手な修正はしない。

## 12. UI/REST/APIへの提案（CHAT_08）

取得中・部分取得・403/429/robots・最新性未知・保存済み旧キャッシュなど区別。オリジナルURLと安全な最終URL、観測日時を表示。private IPやsecret値を生ログへ流さない。
ステータスの `partial / uncertain / waiting_input / cancelled` はユーザーに明示し、「操作完了」の捏造を防止。公開APIは内部BrowserPage/Socket/CDP handle・Credentialを直返ししない。

## 13. 段階的実装PR

1. **04-FETCH-01** strict URL/egress Policy fake transport + redirection tests
2. **04-FETCH-02** HTTP streaming caps/content decoders/timeout+cancel
3. **04-FETCH-03** conditional cache and artifact persistence+version CAS
4. **04-FETCH-04** search→fetch→extract E2E, security/benchmark gates

## 14. TEST設計・実行区分

**全件未実行（設計のみ）**。原典OSSのUnit Testは読解したが、元Repoでも当Harnessでもテストrunnerを実行していない。fixture対象はローカルHTTPサーバー、偽Browser/Provider、仮想ClockとCrash injectionを使用し、実サイト副作用を起こさない。

| Test ID | レイヤー・Setup → Action | Expected | Req | SC |
|---|---|---|---|---|
| FETCH-T01 | Unit: javascript/data/embedded credentials URL | 不正scheme/credentials URLの送信0 | FETCH-001 | SC-08 |
| FETCH-T02 | Fake DNS: public→127.0.0.1, AAAA ::1 | 内部/loopback/linklocal/multicast/metadata IP到達0 | FETCH-002 | SC-08 |
| FETCH-T03 | Mock 302→169.254.169.254 and hop6 | redirectによるprivate宛て到達0 | FETCH-003 | SC-08/SC-07 |
| FETCH-T04 | Proxy bypass/Alt-Svc path | 別経路も同一Policy拒否 | FETCH-004 | SC-08 |
| FETCH-T05 | Header test: Authorization/Cookie blocked | unsafe method/headerなし | FETCH-005 | SC-08/SC-06 |
| FETCH-T06 | gzip bomb 10MiB from 8KiB | zip bomb/無限stream遮断 | FETCH-006 | SC-08/SC-09 |
| FETCH-T07 | PDF/HTML/malformed charset fixtures | HTML以外をHTMLとして扱わない | FETCH-007 | SC-05 |
| FETCH-T08 | Tenant A/B identical URL with authenticated state | 別Tenantのcache誤利用0 | FETCH-008 | SC-06/SC-08/SC-12 |
| FETCH-T09 | 304 with prior ETag and changed page | 304を新価格観測と断定しない | FETCH-009 | SC-02/SC-12 |
| FETCH-T10 | 429 Retry-After 60s vs 5s deadline | 指定上限超過call0 | FETCH-010 | SC-07/SC-09 |
| FETCH-T11 | Cancel after 1st chunk | 中止後の追加request0 | FETCH-011 | SC-07 |
| FETCH-T12 | Kill after response before DB commit | lost ACKが0円/更新なしと誤認されない | FETCH-012 | SC-07/SC-12 |
| FETCH-T13 | HTML contains role-instruction | prompt注入にTool権限を奪われない | FETCH-013 | SC-08 |
| FETCH-T14 | robots deny fixture | robots拒否に従う | FETCH-014 | SC-05 |
| FETCH-T15 | HEAD returns 405 then GET | HEAD非対応時適切に戻す | FETCH-015 | SC-09 |

## 15. Acceptance Traceability

全MUST要件について上記表が **REQ-ID → TEST-ID → SC-ID** の1:1参照を持つ。合格はExpectedの全条件に対して機械的なassertを通した場合のみであり、仕様提出は合格を意味しない。SHOULD/LATERも試験設計は残すが正式導入前のGateを分離する。

## 16. 未決・Cross-team proposals

- **[PROVISIONAL: CHAT_09] P0 egress resolver:** DNS pinned TCP, proxy/IPv6 and redirect recheck API。合格試験: FETCH-T02/03/04。
- **[PROVISIONAL: CHAT_03] P0 Tool Ledger:** query body GETのみとcharge reservationを統合。合格試験: FETCH-T10/12。
- **[PROVISIONAL: CHAT_10] P1 Cache/Artifact:** scope key, 304 and original timestamp, staged artifact cleanup。合格試験: FETCH-T08/09。
- **[PROVISIONAL: CHAT_13] P1 diff snapshots:** immutable snapshotsをHTTP statusと独立保存。合格試験: FETCH-T09/12。
- **CHAT_11**: SC-05/06/07/08/09と、新しいSC-10〜16で本領域に関係するE2Eへ拡張し、CIの非認証・認証・悪意あるサイトFixtureを分離。
- **未確定の優先度P0**: Tenant/Workspace対応、BrowserSession/Run fencingのCAS・epoch serialize・ACLは他のDraft PRとの合意が必要。解消までDraft維持。

## 17. Sources / Evidence grade

[S1] **undici** `1b7b18e59f6c1e9d59af188bb0488890166a0c84` / MIT, Code verified 2026-10-30: [`lib/interceptor/redirect.js — createRedirectInterceptor`](https://github.com/nodejs/undici/blob/1b7b18e59f6c1e9d59af188bb0488890166a0c84/lib/interceptor/redirect.js); [`lib/handler/redirect-handler.js — RedirectHandler`](https://github.com/nodejs/undici/blob/1b7b18e59f6c1e9d59af188bb0488890166a0c84/lib/handler/redirect-handler.js); [`test/redirect-request.js — HTTP 300/redirect chain and origin tests`](https://github.com/nodejs/undici/blob/1b7b18e59f6c1e9d59af188bb0488890166a0c84/test/redirect-request.js).
[S2] **Scrapling** `26dc85a4fa307257fb5c669c80aa44b15acb84c4` / BSD-3-Clause（固定SHAのLICENSE確認済み）, Code verified 2026-10-30: [`scrapling/fetchers/requests.py — Fetcher/AsyncFetcher`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/fetchers/requests.py); [`scrapling/engines/static.py — FetcherSession`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/engines/static.py); [`tests/fetchers/sync/test_requests_session.py — session/retry/proxy/header tests`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/fetchers/sync/test_requests_session.py).
- **[Design hypothesis]** 本書のstate machine、API、上限値、timeout、transaction/fencing提案は本プロジェクト固有で実装・実測は未検証。
- **[Docs verified]** Playwright: https://playwright.dev/docs/browser-contexts 、https://playwright.dev/docs/auth 、https://playwright.dev/docs/actionability （参照版は上記SHAで固定）。
- **[Unverified]** 外部サイトごとのTerms/API契約/認証画面相性は本検証とは別。導入時に再確認。

## Review gate

Owned pathのみ。Sourceコードとテストを参照したがHarness test未実施、依存共有契約未Lockedにつき**Draft**。Cross-team合意がない部分を確定APIとして配布してはならない。


### Round 2 Source audit: upstream Issue（2026-10-30）

- **[Issue report / not reproduced]** Undici [#1462](https://github.com/nodejs/undici/issues/1462) はContent-Encodingが付いたHTTP応答でraw compressed bytesとtransparent decompressionの区別が重要であることを報告。本仕様の `maxCompressedBytes` と `maxDecodedBytes` は別カウンタとして必ず計測し、`Content-Length`（転送表現）をuncompressed sizeとして扱わない。T-FETCH-006でテストする。

