# Lightpanda Compatibility-Gated Adapter — Detailed Implementation Specification

> Owner CHAT_04 | Canonical path `docs/04-web-browser/lightpanda-adapter.md` | **Draft/PROVISIONAL** | Rev 0.1 | 2026-10-30 JST | main `b45d4e989998663a00ef09673e69ae1f4191f00d`  
> Tests executed in this project: **0**. Source tests **read, not run**.

## 0. 結論と採否
LightpandaはZig製のCDP互換を目指すブラウザで、READMEが速度/メモリ改善を主張する。ただしCDPプロトコルの名前だけではChromiumと同一挙動を保証できない。現行ソース `Page.captureScreenshot` は**PNGを実装し非PNGを明示拒否**、`Runtime.evaluate`はInspectorへ委譲し、Page state testsも存在。デフォルトの本番ブラウザはPlaywright Chromium、Lightpandaはfeature flag付きの**read-only実験用**とする。

- **採用案**: CDP Capability Probe + fixture-based compatibility matrix + per-task safe fallback to Chromium with new session and reauthorization; output provenance includes engine.
- **却下案**: CDP接続しただけで全Playwright API互換とみなす、README 16x/9x数値を本Harness実測として採用、ログインCookieを異種エンジンに無検証移送。
- **非目標**: Webのアンチボット対策の自動突破、Chromium置換の既定化、BrowserSessionの所有権再実装、Lightpandaへ無制限の任意Scriptを許す。

## 1. 目的・Input/Output・Owner / シナリオ
軽量な公開read-onlyページを対象に実験。Javascriptを実行できる場合でもextensive DOM/iframe/Shadow DOM/スクリーンショット/認証保存/Downloads/Service Workersは別能力としてチェックする。複数CDP接続はCH04 Browser Worker管理下でのみ許す。
本件はBrowser WorkerまたはPython Sidecar Adapterの動作条件だけを所有。Tool execution/approvalはCHAT_03、公開Task/Run/Evidence/Event/ErrorはCHAT_01、Run lease/goalRevisionはCHAT_02、egress/secret/認可はCHAT_09、物理テーブル/ComposeはCHAT_10、UIはCHAT_08。
関連: SC-09, SC-06, SC-08, SC-05, SC-07, SC-13。

## 2. OSS調査（固定commit、コードとテスト）
[S1] **Lightpanda** `bfb784fdfb7692db785acbe6d2af6a59d6fdff87`, AGPL-3.0, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`src/server/cdp/CDP.zig (dispatch and BrowserContext)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/CDP.zig) / [`src/server/cdp/domains/page.zig (captureScreenshot PNG only; Page tests)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/domains/page.zig) / [`src/server/cdp/domains/runtime.zig (Runtime.evaluate forwarding)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/domains/runtime.zig) / [`src/server/cdp/testing.zig (TestContext assertions)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/testing.zig) / [`README.md (performance claims, Docker/CDP guide)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/README.md) / [`LICENSE`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/LICENSE).
[S2] **Playwright** `62d25ac221370f6fc5e26c600823792b83c67f1e`, Apache-2.0, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`packages/playwright-core/src/server/browserContext.ts (storageState/newPage)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) / [`tests/library/browsercontext-storage-state.spec.ts (context state tests)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).

Lightpanda `CDP.zig`にはBrowserContext/Session bookkeepingがあり、`page.zig`の`captureScreenshot`はPNGのみ、`setBypassCSP`等いくつかのCDP呼出しはno-opと明記。`testing.zig` TestContextを用いたPage close/navigation testsを閲覧。PlaywrightのContext isolation/APIとそのstorageState testsを比較したが、LightpandaをPlaywright経由で起動した実機testは未実施。READMEの『16x lighter/9x faster』は外部環境の主張で測定条件を合わせない限り採否根拠にできない。AGPL-3.0適合の運用・変更ソース提供は別途法務確認。
**一次ソースの検証レベル:** 実コード/テスト閲覧をCode verified、README記載値をDocs verified、我々の統合成功率・性能/負荷はHypothesis/Unverifiedとする。upstream ISSUE/PR網羅調査は未完で、これを済んだと主張しない。対象ライセンスに従う個別サービス・プロセス境界を推奨し、ソースを無許可コピーしない。

## 3. Architecture / Trust Boundary / 契約所有
```text
Agent→ToolGateway→BrowserSessionManager(CH04)→EngineFactory→(Chromium baseline | Lightpanda experiment CDP)
                       ↘ CapabilityProbe+FeatureGate→ActionEngine read-only→Evidence
```

[PROVISIONAL: CHAT_01/02/09/10] 公開IDのUUIDv7、`Task.conversationId: null`、`recordVersion`初期値1とDB `revision`初期値0、Run `leaseEpoch` number vs bigint/DB未保存、`tenantId/ownerUserId` vs `workspace_id`の不一致を解消する。独自のDB Schema・共有Error/Eventを再定義しない。認証済みTask ownerと有効なRun/BrowserSession fenceは**各外部呼出し前後**に確認。Session IDは偶然似た文字列でも別領域で区別する。

## 4. Requirements

| REQ-ID | level | MUST/SHOULDの意味 | 合否を判断できるExpected | SC |
|---|---|---|---|---|
| LP-001 | MUST | Lightpanda実験はfeature flagが明示されたTaskのみ | 既定Chromium選択 | SC-09 |
| LP-002 | MUST | CDP接続エンドポイントはBrowser Worker private経路のみ | LLMにws URL非公開 | SC-06/SC-08 |
| LP-003 | MUST | Capability ProbeをVersion SHAとEngine config別に保存 | 未対応APIを成功としない | SC-05/SC-09 |
| LP-004 | MUST | Screenshot format PNG onlyを現行Code能力として明示 | JPEG要求時unsupported/fallback | SC-05/SC-09 |
| LP-005 | MUST | Page/iframe/Shadow DOM/JS/permission別の失敗分類 | ノーオペを成功と誤認しない | SC-05/SC-06 |
| LP-006 | MUST | Session/Tab/Run fenceはChromiumと同じものを使用 | 旧leaseでCDP操作0 | SC-07 |
| LP-007 | MUST | Lightpanda固有の設定/ネットワークegressを強制 | private IP接続0 | SC-08 |
| LP-008 | MUST | Engine fallbackは新Sessionへ再認可し、Secrets移送しない | 他engineにCookie誤流用0 | SC-06/SC-08 |
| LP-009 | MUST | 自動fallbackはread-onlyかつ未副作用時のみ | 購入/送信Actionが再実行されない | SC-07/SC-08 |
| LP-010 | MUST | 観測ArtifactへEngine/version/timeを添付 | Lightpandaの結果をChromium観測と区別 | SC-05/SC-09 |
| LP-011 | MUST | Worker crash/attach lost時はUNKNOWNと再計画 | 同一Sessionが復元済みと偽らない | SC-07 |
| LP-012 | MUST | 試行数/メモリ/時間/並列CDP connectionを制限 | 過負荷でもService継続 | SC-09 |
| LP-013 | SHOULD | 基準Browserと同一fixtureの抽出品質を比較 | 品質差が記録される | SC-09 |
| LP-014 | EXPERIMENT | 認証・full-actionサポートをケース別検証 | 人間承認後のみ実験 | SC-06/SC-13 |

## 5. Adapter I/O・Schema version・validation
```typescript
type EngineRequest = {schemaVersion:1; engine:"chromium"|"lightpanda"; requiredCapabilities:("navigate"|"dom"|"aria"|"screenshot_png"|"js"|"iframe"|"shadow"|"storage_state")[]; sessionId:string; expectedLeaseEpoch:string; taskId:string; runId:string; allowAuthenticated:boolean};
type EngineProbe = {engineVersion:string; manifestVersion:number; supported:string[]; unsupported:string[]; unknown:string[]; checkedAt:string; fixtureVersion:string};
type EngineObservation = {engine:"lightpanda"|"chromium"; engineSha:string; observedAt:string; artifactRef?:string; outcome:"ok"|"partial"|"unsupported"|"uncertain"; fallbackReason?:string};
```
Probeはステップごとではなくengine build SHA別の署名/設定済みmanifestと、Session開始時の疎通確認。CDP version-stringだけで能力をtrueにしない。ScreenshotでJPEG等を要求した場合は変換仕様を採用せずunsupportedを返す。
Tool/Run IDは公開domain UUIDv7 Validatorを通し、opaque internal CDP/MCP/sidecar session IDをPublic IDとして使わない。Provider設定はserver-side allowlist、version=1以外はfail closed。owner/tenant/goalRevision/leaseEpochはRuntime/Gateway経由で渡し、Tool結果やHTMLでは変更不能。

## 6. ライフサイクル・正常Sequence / 状態遷移
```text
DISABLED→PROBING→READY_READ_ONLY→EXECUTING→OBSERVED
PROBING→UNSUPPORTED→CHROMIUM_FALLBACK
EXECUTING→LOST_CONNECTION/UNCERTAIN→REPLAN
AUTHENTICATION_REQUIRED→WAITING_INPUT (no automatic cookie import)
```
1. Gatewayでread-only能力とTask scope確定→2. BrowserSessionがLightpanda instanceをleaseでclaim→3. capability probe→4.必要APIが欠落ならChromiumへ新Session構成を提案→5.有限Navigation/DOM snapshot→6. URL/内容/engine versionと観測時刻を保存→7.同条件のChromium baselineと品質照合。

## 7. 実装可能な擬似コード
```typescript
async function selectEngine(req,ctx) {
 if(!featureFlags.lightpanda || req.allowAuthenticated) return browserOwner.createChromium(ctx);
 await authorizeReadOnly(req,ctx); const probe=await probeCache.getOrRun(buildSha, ctx.deadline);
 if (!allSupported(probe,req.requiredCapabilities)) return browserOwner.createChromium(ctx);
 const s=await browserOwner.createIsolatedLightpandaSession(ctx);
 try {
   ctx.assertLiveFence(); const result=await boundedReadOnlyObserve(s,req,ctx);
   await persistEvidenceWithEngineAndFence(result,s,ctx); return result;
 } catch(e) {
   await s.close();
   if (isReadOnlyAndNoAmbiguousAction(e)) return await browserOwner.createChromium(ctx);
   return uncertain(e);
 }
}
```
`authorize`/`persist`/`fence`等の共通契約は他オーナーの正式API確定前の擬似呼出し。自動fallbackをする場合も再認可・deadline/tenant/credential/観測の再検証を必須とする。

## 8. Failure/Race/Cancel/Restart
| ケース | 検出 | Recovery・Retry可否 | 状態と観測 |
|---|---|---|---|
| CDP method unsupported | method error/observability mismatch | Chromiumへ新Session | UNSUPPORTED |
| Screenshot JPEG | explicit unsupported format | PNGのみ/新renderer | UNSUPPORTED |
| Iframe differs | same fixture coverage mismatch | Chromium, no silent success | PARTIAL |
| CDP disconnect | socket EOF | 旧lease失効→再観測 | UNCERTAIN |
| engine upgrade | build SHA differs | Probe cache invalid | REPROBE |
| fallback requires login | cookie transfer unverified | Human Handoff | WAITING_INPUT |
| egress denial | forbidden URL/subresource | fallbackでも禁止 | DENIED |
- 原子性: DB intent commit→送信→observation commitは別境界。送信済でACK喪失時の外部作用はUNKNOWNにし、不可逆操作の盲目的再実行禁止。read-onlyでも二重料金やサイト負荷を考慮。
- Worker A/Bの同時ClaimはDB fencingを使い一方だけ成功。失効したepochの遅延結果を現行Task成功として採用しない。SteerはgoalRevision一致後の安全境界からのみ適用。

## 9. Security/Privacy/Resource Budget
CDP WebSocketはloopback/Unix owner-only、公開port無し・TLS/secretの変更不要。Browser side effectsのInterceptとCSP挙動はChromium同等と仮定せず、出力が異なるなら停止。認証用storageStateはengine互換検証なしに移送しない。Lightpanda AGPLは組込み/再配布/ネットワーク提供時の義務を調査して運用判断。
- 外部HTML/CDP/抽出文字列はuntrusted。Proxy/Browser egressはCHAT_09の規則を経由。秘密をモデル、ログ、Artifactに出さない。
- AgentへHost shell/SSH/Docker socket/privileged権限を渡さない。無断購入/予約/投稿/送金をしない。
- **仮の有限hard cap:** Lightpanda実験同時1 Session、毎Task最大3ページ・10s/page、RSS上限はBrowser Workerに設定、fallback 1回のみ（仮値）。Task Gatewayのlimitが狭ければその値を優先。
- Trace: TaskId, RunId, owner-safe audit principal, adapter version SHA, engine kind, leaseEpoch, attemptId, observedAt, bytes, durationMs, selected fallback reason, total USD/token estimate, error code, artifact ref。生URL/PII/keyをログに吐かない。
- Benchmarkは同一サイトfixture・同一マシン/CPU/RSS/時間・cold/warm・JS/iframe/auth/page size別でChrome基準を比較。結果の正確さ・再現率が満たせなければ速度利点のみで本番採用しない。

## 10. Persistence/Transaction/Migration提案（CHAT_10）
EngineBuild SHA、manifest/probe fixture hash、Task scope、BrowserSession ref、失敗CDP method、Fallback reasonを論理保存。Provenanceにエンジン種類を残し、過去cacheが別エンジンに由来しても現在確認済へ昇格させない。
Physical tables、キー、Indexes、TTL、暗号化blob storeはCHAT_10/09へ提案。本書独自の正本を増やさない。

## 11. REST/UI/通知と他担当への接続
実験ブラウザからの結果にはEngine/観測日時/互換性制限を併記。認証画面対応が失敗した場合ログイン済みと表示しない。

## 12. 実装PR分割
1. **04-LP-1** CDP server fixed build + per-command capability probe
2. **04-LP-2** limited public read-only engine adapter + egress/security
3. **04-LP-3** Chromium fallback/Provenance/CAS bridge
4. **04-LP-4** same fixture cross-engine benchmark + license/security review

## 13. テスト設計（**未実行**）
| TEST-ID | Setup・action（fixture） | Expected assert | REQ→SC |
|---|---|---|---|
| LP-T01 | default flag off | 既定Chromium選択 | LP-001 → SC-09 |
| LP-T02 | CDP endpoint leaked attempt | LLMにws URL非公開 | LP-002 → SC-06/SC-08 |
| LP-T03 | upgrade manifest mismatch | 未対応APIを成功としない | LP-003 → SC-05/SC-09 |
| LP-T04 | JPEG unsupported command | JPEG要求時unsupported/fallback | LP-004 → SC-05/SC-09 |
| LP-T05 | iframe/Shadow root scenarios | ノーオペを成功と誤認しない | LP-005 → SC-05/SC-06 |
| LP-T06 | old lease executes | 旧leaseでCDP操作0 | LP-006 → SC-07 |
| LP-T07 | redirect private IP | private IP接続0 | LP-007 → SC-08 |
| LP-T08 | Cookie move to Chromium | 他engineにCookie誤流用0 | LP-008 → SC-06/SC-08 |
| LP-T09 | ambiguous external submit | 購入/送信Actionが再実行されない | LP-009 → SC-07/SC-08 |
| LP-T10 | compare engine provenance | Lightpandaの結果をChromium観測と区別 | LP-010 → SC-05/SC-09 |
| LP-T11 | kill CDP mid-page | 同一Sessionが復元済みと偽らない | LP-011 → SC-07 |
| LP-T12 | RSS cap & 100 parallel requests | 過負荷でもService継続 | LP-012 → SC-09 |
| LP-T13 | same 50 fixture pages | 品質差が記録される | LP-013 → SC-09 |
| LP-T14 | explicit auth experimental test | 人間承認後のみ実験 | LP-014 → SC-06/SC-13 |
実稼働API、課金Provider、認証サイトへCIで勝手にアクセスしない。

## 14. Acceptance traceability
全MUSTは上表に1:1でREQ-ID→TEST-ID→SC-IDの行を持つ。SHOULD/EXPERIMENTも対応テストを設計済み。行は試験**実行**を保証しない。CHAT_11の統合E2EではSC-05/06/07/08/09および追加SCに必要なFault fixtureを含める。

## 15. Cross-team proposal / 未確定事項
- **[PROVISIONAL: CHAT_09] P0 CDP isolation:** CDP loopback/Unix-only policy + egress all subresources。契約試験: LP-T02/07。
- **[PROVISIONAL: CHAT_10] P1 experimental resource budget:** Engine builds/probe cache/worker RSS service。契約試験: LP-T03/12。
- **[PROVISIONAL: CHAT_14] P1 learned action cache:** chromiumとLightpandaでrecipe互換を仮定しない。契約試験: LP-T05/13。
P0契約未LockedのためDraft維持。

## 16. Sources / Evidence grades
[S1] **Lightpanda** `bfb784fdfb7692db785acbe6d2af6a59d6fdff87`, AGPL-3.0, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`src/server/cdp/CDP.zig (dispatch and BrowserContext)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/CDP.zig) / [`src/server/cdp/domains/page.zig (captureScreenshot PNG only; Page tests)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/domains/page.zig) / [`src/server/cdp/domains/runtime.zig (Runtime.evaluate forwarding)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/domains/runtime.zig) / [`src/server/cdp/testing.zig (TestContext assertions)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/src/server/cdp/testing.zig) / [`README.md (performance claims, Docker/CDP guide)`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/README.md) / [`LICENSE`](https://github.com/lightpanda-io/browser/blob/bfb784fdfb7692db785acbe6d2af6a59d6fdff87/LICENSE).
[S2] **Playwright** `62d25ac221370f6fc5e26c600823792b83c67f1e`, Apache-2.0, inspected 2026-10-30, Code verified（ソースと該当テストを読解、実行なし）: [`packages/playwright-core/src/server/browserContext.ts (storageState/newPage)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts) / [`tests/library/browsercontext-storage-state.spec.ts (context state tests)`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts).
**Hypothesis (unverified):** 上記新規State/timeout/limit/Bridge/rollout設計は我々の統合案。OSSのベンチマークは本Harness測定値ではない。

### Round 2 upstream PR/Issue follow-up（2026-10-30）

- **[PR metadata inspected]** [Lightpanda PR #3231](https://github.com/lightpanda-io/browser/pull/3231) はScreenshot実装のmerged PR。現行 `Page.captureScreenshot` の**PNGのみ**という実コードを正本の挙動として評価。PR titleだけで他format対応は保証しない。
- **[Issue report / not reproduced]** [#3076](https://github.com/lightpanda-io/browser/issues/3076) はPlaywright Test over CDPの互換性改善要望。特にContext/Storage/Download/Network routeの同一動作を前提にしない。**LP-T03, LP-T05, LP-T08** ではAPIが成功応答を返しても状態隔離が失われていないか負のfixtureを追加。
- **[Issue report / not reproduced]** [#3747](https://github.com/lightpanda-io/browser/issues/3747) は日本語がPNG screenshot上で豆腐（四角）になるというv1.0.0報告。**LP-T13** の同条件比較に日本語/かな/漢字/混植fixtureを必須追加し、文字が判読不能なLightpanda visual outputを後続Visionに渡さない。HTML/Markdown抽出が成功していても画像側の品質は別判定。
