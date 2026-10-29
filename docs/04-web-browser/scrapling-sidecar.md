# Scrapling Extraction Sidecar — Detailed Implementation Specification

> Owner CHAT_04 | Canonical path `docs/04-web-browser/scrapling-sidecar.md` | **Draft/PROVISIONAL** | Rev 0.1 | 2026-10-30 JST | main `b45d4e989998663a00ef09673e69ae1f4191f00d`  
> Tests executed in this project: **0**. Source tests **read, not run**.

## 0. 結論と採否
ScraplingはPythonでStatic HTTPとChromium系のStealthyFetcher、Selector、MCP Serverを提供する。ただし`ScraplingMCPServer`が管理する`_sessions`はプロセス内の辞書で、そこに渡した独自session_idはHarnessのBrowserSession/Run UUIDv7と異なる。既存MCPを直接Agentへ公開せず、**Gateway認可済みの静的抽出用Python Sidecar RPC**を第一候補にする。Browser操作は原則本体BrowserWorkerが所有する。

- **採用案**: 制約済み内部RPC（Unix socketまたは認証済み内部network）＋独立Python環境。CSS/XPath解析・静的HTML抽出を優先。MCP modeは権限・セッション・取消・容量のAdapter完成後の実験のみ。
- **却下案**: MCPツールをそのままモデルへ全面公開、Scrapling内部Session UUIDを公開BrowserSessionとして利用、StealthyFetcherでCAPTCHA突破を保証、Sidecarへhost file/process権限を渡す。
- **非目標**: 認証を迂回した取得や無断スクレイピング、全サイトのCloudflare突破保証、専用X/ホテルAPIとの同一性判定、Sidecarでの承認/ユーザー認証。

## 1. 目的・Input/Output・Owner / シナリオ
側車はHTML/ResponseArtifact参照からのCSS/XPath/metadata抽出を受け、元HTMLと抽出値のoffset/path/selectorを保持して返す。既存HTTP Fetchと重複して同一URLを二重取得しない。SidecarへURL fetchを委譲する場合もネットワークPolicyが全URLを再検証する。
本件はBrowser WorkerまたはPython Sidecar Adapterの動作条件だけを所有。Tool execution/approvalはCHAT_03、公開Task/Run/Evidence/Event/ErrorはCHAT_01、Run lease/goalRevisionはCHAT_02、egress/secret/認可はCHAT_09、物理テーブル/ComposeはCHAT_10、UIはCHAT_08。
関連: SC-08, SC-05, SC-06, SC-07, SC-12, SC-01, SC-09。

## 2. OSS調査（固定commit、コードとテスト）
[S1] **Scrapling** `26dc85a4fa307257fb5c669c80aa44b15acb84c4`, BSD 3-Clause, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`scrapling/core/ai/server.py (ScraplingMCPServer and _sessions)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/core/ai/server.py) / [`scrapling/fetchers/requests.py (Fetcher/AsyncFetcher)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/fetchers/requests.py) / [`scrapling/fetchers/stealth_chrome.py (StealthyFetcher)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/fetchers/stealth_chrome.py) / [`scrapling/parser.py (Selector CSS/XPath)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/parser.py) / [`tests/fetchers/sync/test_requests_session.py (session isolation/retry tests)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/fetchers/sync/test_requests_session.py) / [`tests/parser/test_general.py (CSS/XPath tests)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/parser/test_general.py) / [`LICENSE`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE).
[S2] **Playwright** `62d25ac221370f6fc5e26c600823792b83c67f1e`, Apache-2.0, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`packages/playwright-core/src/server/browserContext.ts (storageState/newPage)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) / [`tests/library/browsercontext-storage-state.spec.ts (context state tests)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).

Scrapling `requests.py`は`Fetcher`/`AsyncFetcher`、`stealth_chrome.py`はChromium-basedブラウザのFetcher、`parser.py`はSelector CSS/XPath。 `server.py` は`ScraplingMCPServer`と`_sessions: Dict[str, _SessionEntry]`、HTTP token settings、SessionType別扱いを持つ。静的セッションのtestsはretry/proxy/header条件、parser test_general.pyはCSS/XPath selectionを確認。**ローカルエンジンが安全な多Tenant永続Sessionを提供することは確認できない**。BSD 3-Clauseライセンスは同SHAのLICENSEで確認。
**一次ソースの検証レベル:** 実コード/テスト閲覧をCode verified、README記載値をDocs verified、我々の統合成功率・性能/負荷はHypothesis/Unverifiedとする。upstream ISSUE/PR網羅調査は未完で、これを済んだと主張しない。対象ライセンスに従う個別サービス・プロセス境界を推奨し、ソースを無許可コピーしない。

## 3. Architecture / Trust Boundary / 契約所有
```text
Runtime→Tool Gateway→HTTP Fetch/ArtifactStore→Python Sidecar RPC (Selector parsing, bounded extraction)→normalized evidence
                                   BrowserWorker Playwright owns tabs; Scrapling MCP dynamic session is NOT canonical
```

[PROVISIONAL: CHAT_01/02/09/10] 公開IDのUUIDv7、`Task.conversationId: null`、`recordVersion`初期値1とDB `revision`初期値0、Run `leaseEpoch` number vs bigint/DB未保存、`tenantId/ownerUserId` vs `workspace_id`の不一致を解消する。独自のDB Schema・共有Error/Eventを再定義しない。認証済みTask ownerと有効なRun/BrowserSession fenceは**各外部呼出し前後**に確認。Session IDは偶然似た文字列でも別領域で区別する。

## 4. Requirements

| REQ-ID | level | MUST/SHOULDの意味 | 合否を判断できるExpected | SC |
|---|---|---|---|---|
| SCR-001 | MUST | Sidecarの呼出しはGateway認可後の内部RPCのみ | モデルから直接MCP Session制御0 | SC-08 |
| SCR-002 | MUST | Python Sidecarは特権host resource/任意pathへのアクセスなし | host shell/file権限0 | SC-08 |
| SCR-003 | MUST | InputはArtifactRef優先、URL送信時はFetcher Policy介在 | ネットワーク二重取得0/拒否送信0 | SC-05/SC-08 |
| SCR-004 | MUST | Sidecar sessionIdをBrowserSessionIdと混同しない | 公開IDに内部UUIDを返さない | SC-06/SC-07 |
| SCR-005 | MUST | RPCはschema/version、tenant、task/run/fence、operationIdを照合 | 越境/旧epoch受入0 | SC-07/SC-08 |
| SCR-006 | MUST | 抽出結果には元URL、時刻、selector/path/値を保持 | 検証可能なEvidenceを返す | SC-05/SC-12 |
| SCR-007 | MUST | fetch-onlyとbrowser-dynamicモードを厳密分離 | 動的modeは承認なし起動0 | SC-06/SC-08 |
| SCR-008 | MUST | MCP/Sidecarの資格情報をモデル/ログへ出さない | Authorization header非出力 | SC-08 |
| SCR-009 | MUST | CSS/XPathで空値/複数値/parse errorを区別 | unknownを0円として返さない | SC-01/SC-12 |
| SCR-010 | MUST | レスポンス/解析容量・session数・CPU/RSS・timeoutを制限 | Resource DoSなし | SC-09 |
| SCR-011 | MUST | Worker crash/retryは新generationになり旧sessionをinvalidate | 死んだsessionを成功としない | SC-07 |
| SCR-012 | MUST | Cloudflare/challengeは未解決状態として明示し、人間へ引継ぎ | 突破成功の保証なし | SC-06 |
| SCR-013 | MUST | 画像/HTML内の指示はデータとして扱いPolicy変更しない | prompt injection実行0 | SC-08 |
| SCR-014 | SHOULD | 内部RPCとMCP modeを互換テストで比較 | 評価合格後のみMCP検討 | SC-09 |
| SCR-015 | LATER | 共有ブラウザのPage injection連携を研究 | Ownership保証が取れた場合だけ | SC-06 |

## 5. Adapter I/O・Schema version・validation
```typescript
type ExtractRpc = {schemaVersion:1; opId:string; tenantScopeRef:string; taskId:string; runId:string; expectedEpoch:string; artifactRef:string; extraction:"css"|"xpath"|"text"|"metadata"; selector?:string; maxMatches:number; timeoutMs:number};
type ExtractRpcResult = {status:"ok"|"no_match"|"partial"|"invalid_selector"|"blocked"|"unknown"; values:Array<{value:string; sourceSelector?:string; domPath?:string; sourceOffset?:number}>; sourceUrl:string; observedAt:string; rawArtifactRef:string; sidecarVersion:string; errorCode?:string};
```
`maxMatches<=100`、input HTML<=4MiB、max extracted text <=256KiB、selectors<=500chars（仮値）。XPathは純解析のみ、文書を越えて外部doc()を引く機能は禁止。Session refのownershipはGatewayで検証。
Tool/Run IDは公開domain UUIDv7 Validatorを通し、opaque internal CDP/MCP/sidecar session IDをPublic IDとして使わない。Provider設定はserver-side allowlist、version=1以外はfail closed。owner/tenant/goalRevision/leaseEpochはRuntime/Gateway経由で渡し、Tool結果やHTMLでは変更不能。

## 6. ライフサイクル・正常Sequence / 状態遷移
```text
NEW→AUTHORIZED→ARTIFACT_RESOLVED→PARSING→NORMALIZED→PERSISTED
UNAUTHORIZED→DENIED; CRASH→UNKNOWN/RETRY_SAFE (only parse-only); CHALLENGE→NEEDS_HUMAN
```
1. GatewayがTask/Run/Artifact ACLを検証→2.短期Artifact handleを生成→3.内部RPCのtenant/epoch/tokenを検査→4.Selectorで値抽出（画像・Script実行しない）→5.値・locator・raw hashを正規化→6.DB fenced commit→7.Sidecar独自Sessionを破棄。

## 7. 実装可能な擬似コード
```typescript
async function extractViaSidecar(req, ctx) {
 assertLiveFence(ctx); await gatewayAuthorizeRead(req.artifactRef,ctx);
 const handle=await artifactStore.issueScopedReadHandle(req.artifactRef,ctx);
 try {
   const reply=await sidecarRpc.call({artifact:handle, mode:"parse-only",
      query:req.selector, maxMatches:Math.min(req.maxMatches,100),timeoutMs:req.timeoutMs,
      scope:ctx.tenantScope, opId:req.opId});
   const valid=validateReplyAndSourceHash(reply,req);
   await persistWithFence(valid,ctx); return valid;
 } finally { await artifactStore.revokeScopedHandle(handle); }
}
```
`authorize`/`persist`/`fence`等の共通契約は他オーナーの正式API確定前の擬似呼出し。自動fallbackをする場合も再認可・deadline/tenant/credential/観測の再検証を必須とする。

## 8. Failure/Race/Cancel/Restart
| ケース | 検出 | Recovery・Retry可否 | 状態と観測 |
|---|---|---|---|
| Sidecar process kill | RPC disconnect | parse-only artifactならfence確認し有限再実行 | PARTIAL |
| MCP no token | unauthorized endpoint | reject, no fallback bypass | DENIED |
| Selector invalid | CSS/XPath parse exception | not retry without new selector | INVALID_SELECTOR |
| empty page | 0 matches | UNKNOWN/NO_MATCH distinction | NO_MATCH |
| challenge page | content classifier | no bypass guarantee/human handoff | WAITING_INPUT |
| page/artifact race | source hash changed | stale reject | STALE |
| Resource over cap | RSS/CPU/response bytes | kill isolated worker, audit | RESOURCE_EXHAUSTED |
- 原子性: DB intent commit→送信→observation commitは別境界。送信済でACK喪失時の外部作用はUNKNOWNにし、不可逆操作の盲目的再実行禁止。read-onlyでも二重料金やサイト負荷を考慮。
- Worker A/Bの同時ClaimはDB fencingを使い一方だけ成功。失効したepochの遅延結果を現行Task成功として採用しない。SteerはgoalRevision一致後の安全境界からのみ適用。

## 9. Security/Privacy/Resource Budget
SidecarはPython依存を分離した専用process/container、非特権UID、read-only filesystem、ネットワークnone（parse-only）、executable allowlist、file-system allowlist。MCP `_sessions`内部辞書はowner ACLの保証でない。HTTP Modeを試すときはPolicy egress proxy経由のみ。内部RPC token/Unix permissionsをWorkspace認可と混同しない。Chrome challenge solver文面から機能成功を推測しない。
- 外部HTML/CDP/抽出文字列はuntrusted。Proxy/Browser egressはCHAT_09の規則を経由。秘密をモデル、ログ、Artifactに出さない。
- AgentへHost shell/SSH/Docker socket/privileged権限を渡さない。無断購入/予約/投稿/送金をしない。
- **仮の有限hard cap:** 1 task最大5 sidecar calls、1 RPC 10s、HTML ≤4MiB、result ≤256KiB、並列2/Worker、Memory cap per process, retry max1 parse-only（仮値）。Task Gatewayのlimitが狭ければその値を優先。
- Trace: TaskId, RunId, owner-safe audit principal, adapter version SHA, engine kind, leaseEpoch, attemptId, observedAt, bytes, durationMs, selected fallback reason, total USD/token estimate, error code, artifact ref。生URL/PII/keyをログに吐かない。
- Benchmarkは同一サイトfixture・同一マシン/CPU/RSS/時間・cold/warm・JS/iframe/auth/page size別でChrome基準を比較。結果の正確さ・再現率が満たせなければ速度利点のみで本番採用しない。

## 10. Persistence/Transaction/Migration提案（CHAT_10）
Sidecar build/version・source artifact hash/URL/observedAt、query selector、extraction mode、result status、provenance values、retriesを格納。Python internalSessionを永続IDへ格納しない。MCP Session失効時はrecreateせずTaskを待機。
Physical tables、キー、Indexes、TTL、暗号化blob storeはCHAT_10/09へ提案。本書独自の正本を増やさない。

## 11. REST/UI/通知と他担当への接続
抽出元URL・最終観測時刻・selector/抽出件数・unknown理由を返し、Sidecar errorでWatchの価格を0にしない。認証ページはHuman Handoffに送る。

## 12. 実装PR分割
1. **04-SCR-1** Offline HTML→CSS/XPath RPC + JSON schema tests
2. **04-SCR-2** Scoped Artifact ACL+fenced calls, CPU/RSS limits
3. **04-SCR-3** Fail/retry/rollback and BrowserWorker boundary
4. **04-SCR-4** MCP experiment/quality comparison and license review

## 13. テスト設計（**未実行**）
| TEST-ID | Setup・action（fixture） | Expected assert | REQ→SC |
|---|---|---|---|
| SCR-T01 | Attempt unapproved MCP endpoint | モデルから直接MCP Session制御0 | SCR-001 → SC-08 |
| SCR-T02 | read /etc/shadow or host socket | host shell/file権限0 | SCR-002 → SC-08 |
| SCR-T03 | same URL fetched twice attempt | ネットワーク二重取得0/拒否送信0 | SCR-003 → SC-05/SC-08 |
| SCR-T04 | Use Scrapling internalSession as BrowserSession UUID | 公開IDに内部UUIDを返さない | SCR-004 → SC-06/SC-07 |
| SCR-T05 | Tenant A accesses Tenant B artifact | 越境/旧epoch受入0 | SCR-005 → SC-07/SC-08 |
| SCR-T06 | CSS price field with URL and timestamp | 検証可能なEvidenceを返す | SCR-006 → SC-05/SC-12 |
| SCR-T07 | Dynamic Stealth mode unauthorized | 動的modeは承認なし起動0 | SCR-007 → SC-06/SC-08 |
| SCR-T08 | Trace logs contain token | Authorization header非出力 | SCR-008 → SC-08 |
| SCR-T09 | no match vs $0 | unknownを0円として返さない | SCR-009 → SC-01/SC-12 |
| SCR-T10 | zip/HTML huge | Resource DoSなし | SCR-010 → SC-09 |
| SCR-T11 | kill sidecar during response | 死んだsessionを成功としない | SCR-011 → SC-07 |
| SCR-T12 | Turnstile challenge fixture | 突破成功の保証なし | SCR-012 → SC-06 |
| SCR-T13 | HTML prompt injection | prompt injection実行0 | SCR-013 → SC-08 |
| SCR-T14 | RPC vs MCP compare | 評価合格後のみMCP検討 | SCR-014 → SC-09 |
| SCR-T15 | Playwright Page injection experiment | Ownership保証が取れた場合だけ | SCR-015 → SC-06 |
実稼働API、課金Provider、認証サイトへCIで勝手にアクセスしない。

## 14. Acceptance traceability
全MUSTは上表に1:1でREQ-ID→TEST-ID→SC-IDの行を持つ。SHOULD/EXPERIMENTも対応テストを設計済み。行は試験**実行**を保証しない。CHAT_11の統合E2EではSC-05/06/07/08/09および追加SCに必要なFault fixtureを含める。

## 15. Cross-team proposal / 未確定事項
- **[PROVISIONAL: CHAT_03/09] P0 RPC/secret/auth:** Sidecar egress and capability isolation、tenant scope and token contract。契約試験: SCR-T01/04/08。
- **[PROVISIONAL: CHAT_10] P1 data lineage:** source artifact/parse proof/sidecar version schema。契約試験: SCR-T06/11。
- **[PROVISIONAL: CHAT_05/12/13] P1 extraction handoff:** Source Parser resultは同一性/条件評価の正本にならない。契約試験: SCR-T09/12。
P0契約未LockedのためDraft維持。

## 16. Sources / Evidence grades
[S1] **Scrapling** `26dc85a4fa307257fb5c669c80aa44b15acb84c4`, BSD 3-Clause, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`scrapling/core/ai/server.py (ScraplingMCPServer and _sessions)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/core/ai/server.py) / [`scrapling/fetchers/requests.py (Fetcher/AsyncFetcher)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/fetchers/requests.py) / [`scrapling/fetchers/stealth_chrome.py (StealthyFetcher)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/fetchers/stealth_chrome.py) / [`scrapling/parser.py (Selector CSS/XPath)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/parser.py) / [`tests/fetchers/sync/test_requests_session.py (session isolation/retry tests)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/fetchers/sync/test_requests_session.py) / [`tests/parser/test_general.py (CSS/XPath tests)`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/parser/test_general.py) / [`LICENSE`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE).
[S2] **Playwright** `62d25ac221370f6fc5e26c600823792b83c67f1e`, Apache-2.0, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`packages/playwright-core/src/server/browserContext.ts (storageState/newPage)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) / [`tests/library/browsercontext-storage-state.spec.ts (context state tests)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).
**Hypothesis (unverified):** 上記新規State/timeout/limit/Bridge/rollout設計は我々の統合案。OSSのベンチマークは本Harness測定値ではない。

### Round 2 upstream security Issue/PR follow-up（2026-10-30）

- **[Issue report]** [Scrapling #413](https://github.com/D4Vinci/Scrapling/issues/413) はHTTP MCPの認証を既定強制にすべきという報告。[PR #414](https://github.com/D4Vinci/Scrapling/pull/414) は**merged**であり、現行固定commit `26dc85a` の `scrapling/core/ai/server.py::serve` では`http=true`/tokenなし/`allow_unauthenticated=false`の組合せでValueErrorを送出する。CLI `scrapling/cli.py` は`--no-auth`を明示した時のみ非認証で許す。**これはIssue報告時の「既定非認証」が現行でも残るという意味ではない。**
- **[Code verified: test read, not run]** `tests/ai/test_ai_mcp.py::test_http_without_a_token_refuses_to_serve` と `test_allowed_hosts_enable_dns_rebinding_protection` を確認。テストが存在することと我々のSidecarへの統合安全性は別。**SCR-T01/02/05/08** ではHTTP非認証起動試行を拒否し、`--no-auth` を本Harnessのdeploymentから不可にする。MCP内の`allowed_hosts`はSSRF/egressの正本に代わらず、CHAT_09の完全なNetwork Policy必須。
- **追加Source URL:** [server.py at pinned SHA](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/core/ai/server.py), [cli.py at pinned SHA](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/cli.py), [test_ai_mcp.py at pinned SHA](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/ai/test_ai_mcp.py).
