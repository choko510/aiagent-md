# Browser Session Manager — Detailed Implementation Specification

> Owner: CHAT_04 | Canonical path: `docs/04-web-browser/browser-session-manager.md` | Status **Draft / PROVISIONAL** | Revision 0.1 | Research date 2026-10-30 JST | Source main `b45d4e989998663a00ef09673e69ae1f4191f00d`  
> Dependencies: CHAT_01 Task/Run/BrowserSessionIdentity, CHAT_02 Run lease, CHAT_03 Tool ledger, CHAT_08 takeover API, CHAT_09 Auth/Secrets, CHAT_10 DDL. **Implementation and application tests not run.**

## 0. 結論と採用案

Browser WorkerだけがChromium/BrowserContext/Pageを所有する。エージェントへ渡すのはopaque BrowserSessionIdと操作命令だけである。各Sessionをtenant+owner+taskに固定し、Tab操作は一時に1 leaseを取得。Run再開時に同一BrowserSessionを再利用しても**古いWorkerの同時制御を遮断**する。Cookie storageStateは別の暗号化資源であり、復元後のログイン成功は保証しない。

**採用案:** Browser Workerに単一のBrowserContext ownerを置くSession service、DB-backed lease epoch + in-process per-page mutex、context isolation、復旧時の明示的state revalidation。
**却下案:** Agent LoopがCDP WebSocket URLを持って直接接続、複数Taskが同一Chrome user data dir共有、TTLだけの排他、秘密を含むstorageStateをArtifact/LLMへ保存。
**明確な非目標:** DOM操作の選択（04-04）、モデル判断（04-05）、Human ticket認可（04-09/CHAT_09）、Toolを自動承認すること。

## 1. 責務・利用者GoalとScenario

明示的な1 BrowserSessionはTaskに束縛され、複数Runが順次所有可能。同一ownerであっても別TaskへCookieを暗黙再利用しない。ブラウザプロセスが落ちた場合はcookieの非完全保存と再認証を扱う。
受入 Scenario: SC-06, SC-08, SC-07, SC-09。認証・費用・観測を持たない検索結果を実測事実と断定しない。

## 2. 実ソース横断調査・代替比較

[S1] **Playwright** `62d25ac221370f6fc5e26c600823792b83c67f1e` / Apache-2.0, Code verified 2026-10-30: [`packages/playwright-core/src/server/browserContext.ts — newPage/close/storageState`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts); [`tests/library/browsercontext-storage-state.spec.ts — storageState/cookie tests`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts); [`packages/playwright-core/src/client/locator.ts — Locator.click/fill`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts).
[S2] **Browser Use** `2cc71890e65056b67fdb29d98bb407749c15bb08` / MIT（LICENSEの導入時再確認）, Code verified 2026-10-30: [`browser_use/browser/session.py — BrowserSession.start/stop`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py); [`tests/ci/test_event_bus_resilience.py — resumed keep_alive event bus behavior`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_event_bus_resilience.py); [`browser_use/tools/service.py — tab switch/click/close`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py).
**Playwright**のBrowserContext.storageState, setStorageState, newPage, closeとstorage state testsを参照。**Browser Use**のBrowserSession.start/stop/keep_alive event busとresilience testsを参照。どちらも本Harnessのmulti-tenant DB fencingやpermissionを提供するものではないため別の状態機械を定義する。
これらのソースとテストは**読解したが実行していない**。参照元の性能やサイト成功率を本Harnessへ流用しない。コードコピーせず、公開APIまたは独立したAdapterで利用する。対象OSSのlicenseと導入時のupstream変更は運用前再確認。Issue/PRの網羅調査は未完であり、未確認の事項をCode verifiedと呼ばない。

## 3. 契約境界・依存図・Owner

```text
API/Runtime → Gateway authorized operation → Browser Session Service (CHAT_04) → Browser Worker -> BrowserContext -> Page/Tab; DB lease&epoch (CHAT_10) ↔ Session owner; Auth/Secret Store (CHAT_09) → encrypted storageState only
```

[PROVISIONAL: CHAT_01] 外部公開Task/Run/BrowserSession/Artifact/Evidence/Event/Error IDはcanonical UUIDv7を要求するDraft #2と整合するまで正式型を定義しない。内部ステップIDは公開IDと混同しない。TenantId/OwnerUserId/WorkspaceIdは等値と仮定しない。[PROVISIONAL: CHAT_10] DB physical schemaを本書へ導入しない。共通Taskのnullable Conversation、recordVersion(1開始) vs DB revision(0)、Run leaseEpochのnumber vs bigintおよび欠落しているDB CAS/fencingは #2/#1/#10の未確定契約として明示する。

Session/Context/Pageの寿命とtab mutexはCHAT_04が所有。Run leaseはCHAT_02/01、DB transaction/lease columnはCHAT_10、Authorization/Tenant/TakeoverはCHAT_09、REST projectionはCHAT_08。

## 4. 安定要件・MUST/SHOULD/LATER

| ID | 強度 | 仕様上の不変条件 | 測定可能なExpected | Scenario |
|---|---|---|---|---|
| BSESS-001 | MUST | BrowserSession UUIDv7参照とtenant/owner/taskを同一境界に束縛 | 他Task/TenantのContext取得0 | SC-06/SC-08 |
| BSESS-002 | MUST | BrowserWorkerがContext/Pageを単独所有 | Agent/LLMへCDP handle返却0 | SC-06 |
| BSESS-003 | MUST | Session leaseEpochはDB CASで単調増加し旧Owner失効 | 旧epochのaction/commit 0 | SC-07 |
| BSESS-004 | MUST | 同一PageはPage lockでwrite/read競合を制御 | 同時操作が混在せず順序確定 | SC-06/SC-07 |
| BSESS-005 | MUST | 各Taskに隔離されたBrowserContextとCredential scope | A CookieがBのContextへ0 | SC-06/SC-08 |
| BSESS-006 | MUST | storageStateは暗号化・owner束縛・TTL付き、モデル不可視 | secret出力0 | SC-06/SC-08 |
| BSESS-007 | MUST | 復旧時にContext/Tab validityを再検証してからaction | 未復元タブ操作0 | SC-07 |
| BSESS-008 | MUST | Human Handoff中はAgent action leaseを停止 | takeover中agent click 0 | SC-06 |
| BSESS-009 | MUST | Task cancel/logout/revokeでlease/grant失効とclose | 取消後新規action 0 | SC-07/SC-08 |
| BSESS-010 | MUST | navigationとtab spawn時にnetwork egressを適用 | privateへの接続0 | SC-08 |
| BSESS-011 | MUST | Browser crash後はUNKNOWNとNeedsLoginを区別 | 再認証の不確定状態を成功としない | SC-06/SC-07 |
| BSESS-012 | MUST | 租借競争で1勝者、stale resultをcurrentに混入しない | two-worker同時claim成功は最大1 | SC-07 |
| BSESS-013 | MUST | Session/TabのTTL、最大数、資源上限を強制 | 上限超過で新規Context0 | SC-09 |
| BSESS-014 | SHOULD | ストレージ復元のbrowser versionを監査 | 相違時に再ログイン要求 | SC-06 |
| BSESS-015 | LATER | 多Worker Browser ownership handoffをデータ移管なしに拡張 | 実測と安全試験後のみ有効 | SC-09 |

## 5. 内部I/O型・Version / Validation

```typescript
type BrowserSessionRef = { schemaVersion:1; browserSessionId: string; taskId:string; ownerUserId:string; tenantId:string; state:"NEW"|"STARTING"|"ACTIVE"|"PAUSED"|"WAITING_INPUT"|"RECOVERING"|"EXPIRED"|"CLOSED"; activeRunId?:string; leaseEpoch:string; expiresAt:string };
type TabRef = { tabId:string; browserSessionId:string; openerTabId?:string; createdAt:string; generation:string };
type BrowserCommand = { sessionId:string; tabId?:string; expectedEpoch:string; expectedGoalRevision:string; operationId:string; maxDurationMs:number };
type BrowserLease = { sessionId:string; holderRunId:string; epoch:string; expiresAt:string; mode:"AGENT"|"HUMAN" };
```

- これは**CHAT_04内のwire proposal**。Task/Run/ToolEnvelopeの正本の代替でない。DTOの schemaVersion はmajorで互換性を検査し、不明majorや余剰危険fieldをreject、optionalのundefinedとunknownを混同しない。
- すべての呼出しにserver-authoritative `tenantId, ownerUserId, taskId, runId, goalRevision, leaseEpoch`の**検証済み参照**を要求する。これらをLLMやpage dataから生成しない。Epochは安全なdecimal stringまたはbigintとし、JS numberへの精度喪失変換を禁止。[PROVISIONAL: CHAT_01/02/10]
- `recordVersion`と`goalRevision`と`leaseEpoch`は別概念。CASやfencingは最新永続行から判定し、期限切れまたはlost leaseのコマンドは拒否。TaskがConversationを持たない場合もTask ownerを認可する。
- `BrowserSessionId`はCHAT_01正本ID、BrowserContext ID/Playwright internal target IDはprivate ephemeral。maxTabs<=6、maxConcurrentSessions<=2 per Task、idle TTL<=30m（初期提案）。異なるBrowserTypeやstorage schemaの復旧は互換テスト後。

## 6. 時系列・状態遷移・Sequence

```text
NEW → STARTING → ACTIVE ⇄ PAUSED → WAITING_INPUT → ACTIVE
ACTIVE → RECOVERING → ACTIVE|WAITING_INPUT|CLOSED
ANY → EXPIRED|CLOSED; CLOSED terminal
ownerEpoch old → REJECTED (never implicit resurrect)
```
1. DBでowner/task検証→2. Browser lease acquire CASでepoch更新→3. Worker起動と専有Context生成→4. Session ID+Tab refs保存→5. Gateway許可ActionごとにSession fence+Tab mutex獲得→6. 事後検証/commit→7. 正常解放。Human takeoverではAgent lease解放後Human modeをCAS claim。

## 7. 手続き・Pseudo code

```typescript
async function withSessionCommand(cmd, principal) {
  const session = await sessionRepo.getByTenant(cmd.sessionId,principal.tenantId);
  assertOwnerTaskRun(session,principal); assertLiveFence(session,cmd);
  return await pageLock(session.id, cmd.tabId).runExclusive(async () => {
    // ここでDB行ロックを握りながらWeb/ブラウザの外部待機をしない。
    const reread = await sessionRepo.readCurrentWithOwnerAndFence(session.id,principal);
    assertLiveFence(reread,cmd); assertMode(reread,"AGENT");
    const page = workerPrivatePages.resolve(cmd.tabId, reread.leaseEpoch);
    if (!page || page.isClosed()) return "SESSION_REQUIRES_RECOVERY";
    await toolLedger.recordIntentBeforeDispatch(cmd,principal);
    const outcome = await executeBoundedPageAction(page,cmd);
    const committed = await sessionRepo.commitIfEpochAndGoalMatch(
      cmd.sessionId,cmd.expectedEpoch,cmd.expectedGoalRevision,outcome);
    return committed ? outcome : "STALE_RESULT_NOT_CURRENT";
  });
}
```

アルゴリズムの `assertAuthorized`, `assertLiveFence`, `gatewayReserve`, `persistObservation` は**外部Ownerが提供する契約上の呼び先**であって、このMDが独自のPolicy/DDLを実装する意味ではない。次の物理操作は最新lease/goalRevisionでfenceを必須にする。

## 8. 異常・競合・再開・Lost ACK

| 注入/障害 | 検出 | Retry可否と収束 | 画面/監査 |
|---|---|---|---|
| 2 worker claim | session CAS | 一方のみ所有 | STALE_OWNER |
| Browser kill | CDP connection lost | 最新epochで再起動、storageState可能なら復元 | RECOVERING |
| logout/session revoke | owner/credential invalid | 権限再取得まで停止 | WAITING_INPUT |
| Tab closed | Page.isClosed | 再snapshot/再計画 | STALE_TAB |
| Human takeover race | lease mode CAS | Agent/Human同時不可 | WAITING_INPUT |
| Run cancellation | revision/fence mismatch | 新アクション0 | CLOSED/PAUSED |
| Cookie restore incomplete | auth probe fails | 自動ログイン成功と主張せず再認証 | NEEDS_LOGIN |
Workerが返したACKだけでは外部操作のexactly-onceは成立しない。永続Ledger/Checkpointと観測状態に基づいて `DONE / SAFE_TO_RETRY / UNKNOWN` の区別を維持し、UNKNOWNの非冪等操作は自動再送しない。Cancel/Steer後の実行中read結果は歴史的証拠にのみ格納し、現行Goalを満たすと扱わない。

## 9. Security / Privacy / Trust Boundary

BrowserContextは個別のjar/storageで分離。永続Chrome profileをTask間で共有しない。storageStateはcookie/localStorage/IndexedDB等を含み得る秘密扱い、暗号化・限定scope保存。Browser Contextのnative permissions、downloads、file chooser、service workers、popup、network egressは保守的に禁止/制限。ページのiframe・subresourceに対してもネットワーク制御を適用。
- Web/HTML/HTTP response, tool output, Cookie-protected contentは全てuntrusted data。ページ内の指示がRule・Role・tenant/owner・Tool Gatewayのpolicyを上書きしない。
- Host Shell/SSH/Docker socketやprivileged containerへのAgent権限付与は禁止。承認なき購入・予約・投稿・送金・外部メッセージ送信は禁止。
- Raw Artifact/Traceは秘密・Cookie・Auth header・PIIを削除/マスクし、アクセスscope・暗号化・期限はCHAT_09が定義。

## 10. Budget / Trace / Benchmark

初期提案：同時Session <=2/Task、<=6 tabs/Session、idle TTL30min、1 tab action deadline 15s、browser memory worker hard capをCHAT_10で設定。Context再起動と失敗率を実測し運用値確定。
- Trace: `taskId, runId, toolAttempt, goalRevision, leaseEpoch, tenant-scoped correlation, adapterVersion, operation, durationMs, bytes, retryCount, errorClass, costEstimate, observedAt, artifactRef`。URLのquery secretとbodyはログへ書かない。
- Benchmark: 同じURL/資格/地域/ネットワーク/fixture/冷温起動条件でp50/p95、memory RSS、成功/partial率、per-step API費・token費を比較。実測前に「4倍」「90%削減」などの速度/成功保証はしない。測定基準と閾値はCHAT_11へ合意提案。

## 11. Persistence・Transaction・Migration（CHAT_10への提案）

BrowserSession resource ID、tenant/owner/task refs、activeRunId、leaseEpoch(bigint/decimal wire)、leaseOwner/expiry、state、current storage state encrypted ref、tab refs/generation、Browser version、revision CASを保存提案。Playwrightのraw page/context objectはDBに保存不可。
- **atomic boundary**: Operation intentとlease/fence snapshotの永続化 → Gateway実行 → Result/Artifact metadata＋Tool outcomeをトランザクションで記録 → outbox。ネットワーク呼出しをDB transactionと原子的にすることは不可能。送信後未保存は`UNKNOWN`。
- 公開ID UUIDv7の生成、nullable Conversation、recordVersion初期値、leaseEpochの幅/Run列/Task activeRunIdのCAS、Tenant↔Workspace mappingはCHAT_01/02/09/10へ一括契約試験で要合意。勝手な修正はしない。

## 12. UI/REST/APIへの提案（CHAT_08）

GET /browser-sessions/{id} はredacted stateとTakeover可能/期限だけ。session storage内容、CDP URL、cookieは表示不可。WAITING_INPUT→handoff-ticket処理はCHAT_09/08。
ステータスの `partial / uncertain / waiting_input / cancelled` はユーザーに明示し、「操作完了」の捏造を防止。公開APIは内部BrowserPage/Socket/CDP handle・Credentialを直返ししない。

## 13. 段階的実装PR

1. **04-SESS-01** BrowserSession local state&identity + Browser Worker service
2. **04-SESS-02** DB lease fencing/Page lock + concurrent fault tests
3. **04-SESS-03** storageState encrypted scoped restore + crash recovery
4. **04-SESS-04** handoff/REST read projection and cross-tenant E2E

## 14. TEST設計・実行区分

**全件未実行（設計のみ）**。原典OSSのUnit Testは読解したが、元Repoでも当Harnessでもテストrunnerを実行していない。fixture対象はローカルHTTPサーバー、偽Browser/Provider、仮想ClockとCrash injectionを使用し、実サイト副作用を起こさない。

| Test ID | レイヤー・Setup → Action | Expected | Req | SC |
|---|---|---|---|---|
| BSESS-T01 | Security: tenant A/B cross task | 他Task/TenantのContext取得0 | BSESS-001 | SC-06/SC-08 |
| BSESS-T02 | Unit: agent tries raw websocket | Agent/LLMへCDP handle返却0 | BSESS-002 | SC-06 |
| BSESS-T03 | Concurrency: dual lease acquisition | 旧epochのaction/commit 0 | BSESS-003 | SC-07 |
| BSESS-T04 | Lock: simultaneous click/snapshot | 同時操作が混在せず順序確定 | BSESS-004 | SC-06/SC-07 |
| BSESS-T05 | Two contexts same site different cookies | A CookieがBのContextへ0 | BSESS-005 | SC-06/SC-08 |
| BSESS-T06 | storageState written under wrong Tenant | secret出力0 | BSESS-006 | SC-06/SC-08 |
| BSESS-T07 | Kill/restart with old tab generation | 未復元タブ操作0 | BSESS-007 | SC-07 |
| BSESS-T08 | Human agent action interleaving | takeover中agent click 0 | BSESS-008 | SC-06 |
| BSESS-T09 | revoke/cancel during goto | 取消後新規action 0 | BSESS-009 | SC-07/SC-08 |
| BSESS-T10 | redirect to metadata IP in subresource | privateへの接続0 | BSESS-010 | SC-08 |
| BSESS-T11 | Browser worker killed on login page | 再認証の不確定状態を成功としない | BSESS-011 | SC-06/SC-07 |
| BSESS-T12 | worker A returns after worker B claim | two-worker同時claim成功は最大1 | BSESS-012 | SC-07 |
| BSESS-T13 | resources exceed quota | 上限超過で新規Context0 | BSESS-013 | SC-09 |
| BSESS-T14 | browser version differs | 相違時に再ログイン要求 | BSESS-014 | SC-06 |
| BSESS-T15 | cross-worker handoff experiment | 実測と安全試験後のみ有効 | BSESS-015 | SC-09 |

## 15. Acceptance Traceability

全MUST要件について上記表が **REQ-ID → TEST-ID → SC-ID** の1:1参照を持つ。合格はExpectedの全条件に対して機械的なassertを通した場合のみであり、仕様提出は合格を意味しない。SHOULD/LATERも試験設計は残すが正式導入前のGateを分離する。

## 16. 未決・Cross-team proposals

- **[PROVISIONAL: CHAT_01/02] P0 session lease contract:** BrowserSessionIdentity.activeRunId/leaseEpochとRun leaseの差を明文化。合格試験: BSESS-T03/12。
- **[PROVISIONAL: CHAT_10] P0 persisted fencing:** BrowserSession lease+epoch+CASの実DB列/unique/expiry。合格試験: BSESS-T03/12。
- **[PROVISIONAL: CHAT_09] P0 owner/storage:** Tenant-Workspace mapping, encrypted state, origin/iframe egress。合格試験: BSESS-T05/06/10。
- **[PROVISIONAL: CHAT_08] P1 handoff projection:** Ticket fieldsとAgent/Human modeはopaqueで表現。合格試験: BSESS-T08。
- **CHAT_11**: SC-05/06/07/08/09と、新しいSC-10〜16で本領域に関係するE2Eへ拡張し、CIの非認証・認証・悪意あるサイトFixtureを分離。
- **未確定の優先度P0**: Tenant/Workspace対応、BrowserSession/Run fencingのCAS・epoch serialize・ACLは他のDraft PRとの合意が必要。解消までDraft維持。

## 17. Sources / Evidence grade

[S1] **Playwright** `62d25ac221370f6fc5e26c600823792b83c67f1e` / Apache-2.0, Code verified 2026-10-30: [`packages/playwright-core/src/server/browserContext.ts — newPage/close/storageState`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/server/browserContext.ts); [`tests/library/browsercontext-storage-state.spec.ts — storageState/cookie tests`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts); [`packages/playwright-core/src/client/locator.ts — Locator.click/fill`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts).
[S2] **Browser Use** `2cc71890e65056b67fdb29d98bb407749c15bb08` / MIT（LICENSEの導入時再確認）, Code verified 2026-10-30: [`browser_use/browser/session.py — BrowserSession.start/stop`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py); [`tests/ci/test_event_bus_resilience.py — resumed keep_alive event bus behavior`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_event_bus_resilience.py); [`browser_use/tools/service.py — tab switch/click/close`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py).
- **[Design hypothesis]** 本書のstate machine、API、上限値、timeout、transaction/fencing提案は本プロジェクト固有で実装・実測は未検証。
- **[Docs verified]** Playwright: https://playwright.dev/docs/browser-contexts 、https://playwright.dev/docs/auth 、https://playwright.dev/docs/actionability （参照版は上記SHAで固定）。
- **[Unverified]** 外部サイトごとのTerms/API契約/認証画面相性は本検証とは別。導入時に再確認。

## Review gate

Owned pathのみ。Sourceコードとテストを参照したがHarness test未実施、依存共有契約未Lockedにつき**Draft**。Cross-team合意がない部分を確定APIとして配布してはならない。


### Round 2 Upstream Issue/PR audit（2026-10-30）

- **[Issue report / not reproduced]** Playwright [#31108](https://github.com/microsoft/playwright/issues/31108) は `storageState` にsessionStorageの扱いを追加する機能要求。現行 `BrowserContext.storageState()` で**sessionStorageやあらゆるサイト認証状態まで完全に復元できるとは仮定しない**。T-BSESS-007ではsessionStorage依存のfixtureも追加し、復元できなければWAITING_INPUTに留める。
- **[Issue report / not reproduced]** Browser Use [#5912](https://github.com/browser-use/browser-use/issues/5912) は切断済みCDP clientへのStorageStateWatchdogアクセスでERRORを繰り返す報告。関連 [PR #5913](https://github.com/browser-use/browser-use/pull/5913) は確認時点でopen・未マージ（PR metadata inspected）。本HarnessはBrowser disconnect時にwatchdogを停止・破棄し、旧Session event listenerが復元後も走り続けないようにする。T-BSESS-011で旧listenerのログ/保存の発火0件を検証。


### Round 2 transaction-lock self-audit（2026-10-30）

Browser actionの間に `SELECT ... FOR UPDATE` 行ロックを保持すると、ページ待機や人間HandoffによってDB更新がブロックされ、Cancel/Handoff/lease takeoverのデッドロックや大幅遅延を起こし得る。**旧擬似コードの `getForUpdate` を削除した**。Action開始前にowner+lease mode/epoch/goalRevisionを読取り、Tool Ledgerにdispatch intentをcommit。外部操作中はDB transactionを保持せず、観測保存時に短いtransaction内で `WHERE lease_epoch=:expected AND goal_revision=:expectedGoal AND mode='AGENT'` 相当の条件付き更新/Insertでfenceする。CAS失敗後の遅延結果は現行成功にしない。**BSESS-T03/08/09/12**に、外部操作が長時間待機してもCancel/Human handoffのtransactionが完了し、古いWorkerのcommitだけが0行になる競合fixtureを追加する。物理SQLはCHAT_10の契約提案。
