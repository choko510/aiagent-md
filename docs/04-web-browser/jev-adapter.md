# Jev Judgment Adapter — Detailed Implementation Specification

> Owner CHAT_04 | Path `docs/04-web-browser/jev-adapter.md` | **Draft / [PROVISIONAL]** | Revision 0.1 | Source main b45d4e989998663a00ef09673e69ae1f4191f00d | Research 2026-10-30 JST | Implementation/tests **not run**.

## 0. 結論・採用と却下
Jevはページの次の操作を判断する補助エンジンであり、BrowserSessionのOwnerではない。最新版ソースを確認すると`NavigateOptions.page?: Page`と`transport?: JevTransport`により既存Playwright Pageの注入が可能。これを使ってもJevのinternal navigate loopが勝手にTool approvalを通過しないよう、**読み取り判断と実行権限を分離する境界**を用意する。
- **採用**: Jevへの既存Page注入をBrowser Worker内に限定し、予算/モデルproviderは有界transportとして渡す。副作用のあるJev内アクションをGatewayで逐次承認できないならread-only/experimentモードのみに制限。
- **却下**: Jev CLI/MCPの独立Browser起動を同一Sessionとして扱う、injected Pageへcookie seed/録画を指定する、JevのconfidenceをPolicy承認として扱う。
- **非目標**: BrowserSessionリースや認可の独立実装、認証壁の自動突破、実測していない速度向上の保証。

## 1. 責務 / Scenario
既存Playwrightの`navigate`は内部ループで複数Actionを起こしうる。現行公開NavigateOptionsの文面からは各actionに外部Approval middlewareを挟める保証がないため、未検証のままfull-navigationをAgentの自由操作に使用しない。副作用を持つactionは04-04の明示コマンドとして再表現しGatewayで再承認する。
対応: SC-06, SC-09, SC-07, SC-08, SC-13, SC-05。今回の新規SC-13 Learned browser actionはCHAT_14が所有し、本モジュールの実行Lease/Policyから迂回できない。

## 2. OSS調査 / 実装場所 / ライセンス
[S1] **jev-browser**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30): [`src/navigate.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts) (NavigateOptions.page, navigate/abort/budget); [`src/provider.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/provider.ts) (askJev / InvalidJevAnswer); [`test/unit.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) (injected Page life-cycle, redaction, action space); [`test/e2e.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/e2e.test.mjs) (key-gated fixture navigation).
[S2] **Playwright**, Apache-2.0, **Code verified: source inspected, tests read not executed**, commit `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30): [`packages/playwright-core/src/client/locator.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts) (Locator.click/fill); [`packages/injected/src/ariaSnapshot.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/injected/src/ariaSnapshot.ts) (generateAriaTree/renderAriaTreeAsJSON); [`tests/library/browsercontext-storage-state.spec.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts) (context isolation tests).
[S3] **Browser Use**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `2cc71890e65056b67fdb29d98bb407749c15bb08` (2026-10-30): [`browser_use/tools/service.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py) (Tools._click_by_index/input/switch/scroll); [`browser_use/browser/session.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py) (BrowserSession); [`tests/ci/test_tools.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_tools.py) (fixture HTTP server + action tests).
実際に `src/navigate.ts` で `ownsBrowser=!options.page`、既存contextの再利用、recordDirとinjected Pageの制限が確認できる。unit.test.mjsには既存Pageのlifecycleとtimeout維持を検証するテストがある（読解のみ）。`src/provider.ts`は`askJev`/transport判定、InvalidJevAnswerを持つ。対応できないCDP/iframe/Shadow DOMや一意selectorの回復保証は読み取れていないため本Harness側で実験と明示。
ベンチマーク数値と互換性は**実証していない**。Issue/PRの網羅調査は未完。Sourceと自作提案を分離する。

## 3. 所有権・依存関係
```text
Runtime → Tool Gateway → Browser Session owner → Snapshot → Jev transport (judgment only / max deadline) → Action proposal → Gateway policy → 04-04 Action engine; optional injected page experimental path (read-only allowlist)
```
- BrowserContext/Page/Tab/Session lockは**CHAT_04/browser-session-manager**のみ保持。Agent LoopにPage handle、CDP URL、Cookieを返さない。
- Task/Run/BrowserSession IDはCHAT_01のUUIDv7 Draftに従う前提（未Locked）。[PROVISIONAL: CHAT_01/02/10] `BrowserSession.leaseEpoch`, `Run.leaseEpoch`, `recordVersion`, `goalRevision`の意味は別、opaque fenceを併用し互換契約合意までシリアライズ固定しない。
- [PROVISIONAL: CHAT_09] `tenantId/ownerUserId`の強制認可は`workspace_id`と無条件同値にしない。Task.conversationIdがnullでも実行可能。Tool execution/side-effect approvalはCHAT_03の正本。

## 4. MUST/SHOULD要件
| REQ | level | 不変条件 | 観測できる合格条件 | SC |
|---|---|---|---|---|
| JEV-001 | MUST | 既存Pageを利用する場合新規Browser launchしない | 2 browser processを作らない | SC-06/SC-09 |
| JEV-002 | MUST | JevはSession leaseを保持しない・Task/Tab ownership検査を迂回しない | 旧epochでのjudgment結果をactionに使用0 | SC-06/SC-07 |
| JEV-003 | MUST | Jev内部の複数step実行はPolicy middleware検証までfull control禁止 | hidden click/submitの実行0 | SC-08 |
| JEV-004 | MUST | transport provider API/versionを固定Manifestで検証 | unknown providerで実行0 | SC-09 |
| JEV-005 | MUST | abort deadlineをJudgment/typing/Browser timeoutへ伝播 | 期限切れLLM延長なし | SC-07/SC-09 |
| JEV-006 | MUST | typing/URL/selectorがsecret dataにアクセスしない | modelやtraceにpassword 0 | SC-06/SC-08 |
| JEV-007 | MUST | 候補Actionのconfidenceを権限に転用しない | high confidence harmful actionも拒否 | SC-08 |
| JEV-008 | MUST | Jev outputをschema validateしてIndex/Elementを現在snapshotで再照合 | stale target実行0 | SC-07/SC-13 |
| JEV-009 | MUST | iframe/Shadow DOM/SPAの未対応時はfallback/needs_humanへ | successを捏造しない | SC-05/SC-06 |
| JEV-010 | MUST | stop判定を操作前に行いmaxSteps/maxSecondsを強制 | 期限切れ後action0 | SC-07 |
| JEV-011 | MUST | injected Pageのclose/recording/default timeoutを呼出し元権利のまま保持 | 呼出し後Page open | SC-06 |
| JEV-012 | MUST | モデル利用量/step failureをTool Ledgerへ保存 | 予算超過で自動再試行なし | SC-09 |
| JEV-013 | SHOULD | read-only judgmentのfixturesをPlaywright baselineと比較 | 品質実測後に実験Flag有効化 | SC-09 |
| JEV-014 | LATER | Jev作者にper-action hooksの安定APIを提案 | Upstream実装確認後のみ統合 | SC-09 |

## 5. I/O / 型 / Validation（CHAT_04内の提案）
```typescript
type JevJudgmentRequest = { schemaVersion:1; sessionId:string; tabId:string; expectedEpoch:string; snapshotRef:string; intent:string; allowedActions:("observe"|"propose_click"|"propose_fill")[]; maxSteps:number; deadlineAt:string; providerId:string };
type JevJudgment = { kind:"candidate"|"no_action"|"blocked"|"uncertain"; proposedTarget?:{frameRef:string; role?:string; name?:string; selector?:string}; confidence?:number; usage?:{inputTokens:number;outputTokens:number}; observedAt:string; untrusted:true };
```
Jev `NavigateOptions.page`はブラウザWorker内部からしか設定しない。`recordDir`と`cookies`はinjected Pageで拒否されるソースの挙動を守る。秘密情報を含むプレーンな`password.value`の呼出しは禁止。
外部公開Command/Event/Error/Evidence/ArtifactはCHAT_01/03のschemaを参照。[PROVISIONAL]。バージョン不明のDTO、権限のないtab ID、古いgoalRevision、予算超過はaction開始前にrejectする。

## 6. State machine + sequence
```text
DISABLED→CAPABILITY_CHECKED→SNAPSHOT_READY→JUDGING→PROPOSAL_VALIDATED→(APPROVED_ACTION|NO_ACTION|FALLBACK)
ANY→CANCELLED; AUTH_WALL→WAITING_INPUT; stale epoch→DISCARD
```
1. BrowserOwnerがPage+lease確認→2. snapshot redaction→3. capped Jev transport→4. parse/target confirm→5. Action proposalだけ返却→6. Gatewayが許可Actionを新しいActionCommandとして渡す。Live loopはread-only fixtureで事前実験のみ。

## 7. アルゴリズム・疑似コード（実行順）
```typescript
async function jevSuggest(req, ctx) {
 ctx.assertFence(); const page = browserOwner.privatePage(req.sessionId,req.tabId);
 const snapshot = await boundedObserve(page,req.snapshotRef);
 const transport = resolvePinnedTransport(req.providerId,ctx.budget);
 const candidate = await askJevWithAbort(transport,snapshot,req.deadlineAt);
 if (!validateJudgment(candidate)) return fallback("INVALID_JEV_REPLY");
 ctx.assertFence(); if (snapshot.generation !== currentGeneration(page)) return fallback("STALE");
 const action = classifyProposedAction(candidate); // candidate.kindは結果種別。操作種別ではない
 if (action !== null && !req.allowedActions.includes(action)) return fallback("ACTION_NOT_ALLOWED");
 if (candidate.kind !== "candidate") return toNonActionOutcome(candidate);
 return toGatewayActionProposal(candidate); // browser action is NOT executed here
}
```
進行中TaskをSteer/Cancelした場合、新規Tool intentを投入せず、現行Actionの完了結果を新しいGoalへ暗黙適用しない。DOM再計画前にSession/Page generationを再取得。Agentに可逆操作と確認済の違いを伝える。

## 8. Failure matrix / race / recovery
| 障害 | 検出 | Retry / 回復 | 結果 |
|---|---|---|---|
| Jev provider unavailable | transport request_failed | alternate judgment or deterministic fallback | PARTIAL |
| invalid answer | InvalidJevAnswer | reject/replan once | INVALID_OUTPUT |
| DOM changed | generation diff | fresh snapshot | STALE |
| bot wall | page evidence + challenge signal | human handoff | WAITING_INPUT |
| injected page closed | Page.isClosed | owner recovery | NO_PAGE |
| limit expired | AbortSignal | no new action | CANCELLED |
Network送信/外部クリックとDB commitはatomicではない。送信後lost ack・browser killはTool Ledgerへ`UNKNOWN`を報告し、不可逆操作の無条件再実行は禁止。

## 9. Security / privacy / Budget
Jevリポジトリの`password.ts` redaction等は参考だが、本Harnessのsecret managerを置換しない。JevにTaskの全HTMLと資格情報を自由送信しない。サイトが要求するCAPTCHA/Turnstileは人間へ。外部typing generatorを投入する場合もGatewayがTool budgetを固定。
- URL、iframe、popup、subresourcesのegressはCHAT_09/04-02に再検査を要求する。Web文面/DOMはuntrusted、system命令として実行しない。
- Cookie/API key/認証画面の秘密・スクリーンショットはmodel-visible Artifactに自動露出させない。記録ポリシーが秘匿できない場合はscreenshot禁止。
- 初期提案上限はmaxSteps<=8、maxSeconds<=30、maxChars<=1500 per judgment、provider使用token/attemptはGateway Budget内、1 Run Jev judgment <=6（いずれも仮値）。**最終値はTool Gateway全体BudgetとサイトPolicyのmin**。時間/メモリ/トークン/外部API出費に必ず有限の上限。計測はtask,run,leaseEpoch,actionId,DOM generation,attempt,status,latencyMs,RSS,tokenCost,sideEffectClass,artifactRef。
- CI fixtureの同一環境で、p50/p95・token/step・成功率・ページ変化下の安全失敗率を測定して採用する。性能比較は未実施。

## 10. 永続化・Transaction / Migration
Jev provider/model version、usage tokens、judgment proposal hash、snapshot generationとJev result、action採否/Tool intentを異なるrecordとして保存。秘密の文章とCookieはログ対象外。
[PROVISIONAL: CHAT_10] Browser Session/Task/Run UUIDv7とSession/Run leaseEpochのDB CAS、nullable Conversation、recordVersion初期値1とrevision0の差、tenant-to-workspace mappingの統合はP0。物理DDLを本書で勝手に作らない。操作意図commit→外部操作→Result/Artifact commit→outboxの境界を守る。

## 11. UI/REST/他モジュール
Jevの判断候補と理由は提案として表示。ステップ実行/サイト障害/認証待ちを区別。結果確定は後続Action observationがある場合のみ。

## 12. 実装PR分割
1. **04-JEV-1** Jev injected Page compatibility smoke test
2. **04-JEV-2** judgment-only transport interface and schema
3. **04-JEV-3** Action Engine/Gateway bridge with policy-protected execution
4. **04-JEV-4** iframe/SPA/ShadowDOM and budget/compat benchmarks

## 13. Test design（**全件設計のみ / 実行0件**）
| TEST | Setup/Action（Mock・fixture） | Expected | REQ→SC |
|---|---|---|---|
| JEV-T01 | navigate injected Page, launch spy 0 | 2 browser processを作らない | JEV-001→SC-06/SC-09 |
| JEV-T02 | stale session/epoch request | 旧epochでのjudgment結果をactionに使用0 | JEV-002→SC-06/SC-07 |
| JEV-T03 | Jev proposes purchase submit | hidden click/submitの実行0 | JEV-003→SC-08 |
| JEV-T04 | unknown transport id | unknown providerで実行0 | JEV-004→SC-09 |
| JEV-T05 | provider timeout/Abort | 期限切れLLM延長なし | JEV-005→SC-07/SC-09 |
| JEV-T06 | password echo in result | modelやtraceにpassword 0 | JEV-006→SC-06/SC-08 |
| JEV-T07 | high confidence harmful proposal | high confidence harmful actionも拒否 | JEV-007→SC-08 |
| JEV-T08 | stale selector after DOM mutate | stale target実行0 | JEV-008→SC-07/SC-13 |
| JEV-T09 | iframe and shadow root absent | successを捏造しない | JEV-009→SC-05/SC-06 |
| JEV-T10 | maxSteps=0/deadline expired | 期限切れ後action0 | JEV-010→SC-07 |
| JEV-T11 | original Page timeout/default remains | 呼出し後Page open | JEV-011→SC-06 |
| JEV-T12 | usage > budget | 予算超過で自動再試行なし | JEV-012→SC-09 |
| JEV-T13 | benchmark same fixtures | 品質実測後に実験Flag有効化 | JEV-013→SC-09 |
| JEV-T14 | upstream hook investigation | Upstream実装確認後のみ統合 | JEV-014→SC-09 |

## 14. 受入とTraceability
前表でMUSTごとにREQ → TEST → SCを網羅。実際の合格はMock test/Browser fixture/Crash injectionを実施しassertした後のみとする。テスト設計は実装を意味しない。

## 15. Cross-team proposal / 未決
- **[PROVISIONAL: CHAT_03] P0 policy hooks:** Jev internal executionをGateway外へ出さない。per-action hook未確認。合格: JEV-T03/07。
- **[PROVISIONAL: CHAT_01/02] P0 epoch/Step:** Jev提案はTask/Run/goalRevisionに紐付くread-only Observation。合格: JEV-T02/08。
- **[PROVISIONAL: CHAT_09] P1 model/redaction:** 外部LLMへ出すstateのPII/secret検査。合格: JEV-T06。
- **[PROVISIONAL: CHAT_14] P1 recipe usage:** Jev judgment cacheは認可/DOM保証の代替にしない。合格: JEV-T08/13。
共通契約P0が残る限りDraft維持。CHAT_14のLearned Browser Actionsに対しては同じSession lease/Target freshness/Approval Gateを利用する契約を提案する。

## 16. Sources（2026-10-30 JST）
[S1] **jev-browser**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30): [`src/navigate.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts) (NavigateOptions.page, navigate/abort/budget); [`src/provider.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/provider.ts) (askJev / InvalidJevAnswer); [`test/unit.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) (injected Page life-cycle, redaction, action space); [`test/e2e.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/e2e.test.mjs) (key-gated fixture navigation).
[S2] **Playwright**, Apache-2.0, **Code verified: source inspected, tests read not executed**, commit `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30): [`packages/playwright-core/src/client/locator.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts) (Locator.click/fill); [`packages/injected/src/ariaSnapshot.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/injected/src/ariaSnapshot.ts) (generateAriaTree/renderAriaTreeAsJSON); [`tests/library/browsercontext-storage-state.spec.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts) (context isolation tests).
[S3] **Browser Use**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `2cc71890e65056b67fdb29d98bb407749c15bb08` (2026-10-30): [`browser_use/tools/service.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py) (Tools._click_by_index/input/switch/scroll); [`browser_use/browser/session.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py) (BrowserSession); [`tests/ci/test_tools.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_tools.py) (fixture HTTP server + action tests).
**Design hypothesis:** 本書のType、state、limit、timeout、retry、fault protocolはCHAT_04独自の未検証提案。正式Shared ID/Event/Policy/DDLの正本は各Owner。


### Round 2 擬似コード型監査（2026-10-30）

`JevJudgment.kind` は候補の**結果状態**（candidate/no_action/blocked/uncertain）であって、`allowedActions` が保持する**操作種別**（observe/propose_click/propose_fill）ではない。比較するのは `classifyProposedAction` によって明示的に復元したAction種別のみ。**T-JEV-003/007/008** は `candidate.kind='candidate', allowedActions=['propose_click']` の正常提案を許可済み操作として識別し、禁止候補の拒否・`no_action`非実行・old DOMの拒否をassertする。

