# Browser Action Engine — Detailed Implementation Specification

> Owner CHAT_04 | Path `docs/04-web-browser/browser-action-engine.md` | **Draft / [PROVISIONAL]** | Revision 0.1 | Source main b45d4e989998663a00ef09673e69ae1f4191f00d | Research 2026-10-30 JST | Implementation/tests **not run**.

## 0. 結論・採用と却下
クリック操作の成功応答だけではTaskの達成を証明できない。DOM snapshotを取得した時点と実行直前のDOM/URL/Tab世代を分離し、認可済みBrowser Worker内で再照合してから一操作だけを行う。動作後は変化を観測し、成功・不確定・再計画を分ける。
- **採用**: DOM/ARIA Locator first + conditional screenshot fallback、opaque target fingerprint、短い単発Actionと事後観測、Session page mutex + epoch fence。
- **却下**: Modelに任意JSやページ内scriptの実行を許す、スクリーン座標のみで無条件click、古いelement indexを繰り返し再利用、Chromeを毎Step起動。
- **非目標**: Login challenge突破、キャプチャ文字認識の保証、BrowserSession管理/認可の再定義、不可逆送信の承認。

## 1. 責務 / Scenario
InputはTaskRunからのPlan（navigation/snapshot/click/fill/scrollなど）、Outputはread-only観測またはTool Ledgerに紐付く単一Action outcome。フィル入力中にもsubmit扱いになるサイトがあるためfillもpage effect観測対象。アイテム同定はURL/Frame/ARIA Role/text/test-idとDOM generationで構成し、ページ内の要素番号そのものは永続IDにしない。
対応: SC-07, SC-08, SC-06, SC-13, SC-05, SC-09。今回の新規SC-13 Learned browser actionはCHAT_14が所有し、本モジュールの実行Lease/Policyから迂回できない。

## 2. OSS調査 / 実装場所 / ライセンス
[S1] **Playwright**, Apache-2.0, **Code verified: source inspected, tests read not executed**, commit `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30): [`packages/playwright-core/src/client/locator.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts) (Locator.click/fill); [`packages/injected/src/ariaSnapshot.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/injected/src/ariaSnapshot.ts) (generateAriaTree/renderAriaTreeAsJSON); [`tests/library/browsercontext-storage-state.spec.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts) (context isolation tests).
[S2] **Browser Use**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `2cc71890e65056b67fdb29d98bb407749c15bb08` (2026-10-30): [`browser_use/tools/service.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py) (Tools._click_by_index/input/switch/scroll); [`browser_use/browser/session.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py) (BrowserSession); [`tests/ci/test_tools.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_tools.py) (fixture HTTP server + action tests).
[S3] **jev-browser**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30): [`src/navigate.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts) (NavigateOptions.page, navigate/abort/budget); [`src/provider.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/provider.ts) (askJev / InvalidJevAnswer); [`test/unit.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) (injected Page life-cycle, redaction, action space); [`test/e2e.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/e2e.test.mjs) (key-gated fixture navigation).
Playwright Locator.click/fillはlocator-based actionを提供、ariaSnapshotのツリー生成は観測対象の候補抽出の根拠。Browser Use tools/service.pyはindex action/scroll/switchをdispatchし、tests/ci/test_tools.pyはローカルfixtureを用いる。JevのnavigateはPage挿入可能だが判断器と実行者の責務を混同させない。
ベンチマーク数値と互換性は**実証していない**。Issue/PRの網羅調査は未完。Sourceと自作提案を分離する。

## 3. 所有権・依存関係
```text
Task/Run→CHAT_03 Gateway permit→SessionManager.acquire(page lease)→Snapshot(ARIA/DOM + optional screenshot)→Validate target+policy→Playwright action→Observe URL/navigation/DOM→fenced ledger + evidence
```
- BrowserContext/Page/Tab/Session lockは**CHAT_04/browser-session-manager**のみ保持。Agent LoopにPage handle、CDP URL、Cookieを返さない。
- Task/Run/BrowserSession IDはCHAT_01のUUIDv7 Draftに従う前提（未Locked）。[PROVISIONAL: CHAT_01/02/10] `BrowserSession.leaseEpoch`, `Run.leaseEpoch`, `recordVersion`, `goalRevision`の意味は別、opaque fenceを併用し互換契約合意までシリアライズ固定しない。
- [PROVISIONAL: CHAT_09] `tenantId/ownerUserId`の強制認可は`workspace_id`と無条件同値にしない。Task.conversationIdがnullでも実行可能。Tool execution/side-effect approvalはCHAT_03の正本。

## 4. MUST/SHOULD要件
| REQ | level | 不変条件 | 観測できる合格条件 | SC |
|---|---|---|---|---|
| BACT-001 | MUST | ActionはGateway承認を経由し、session leaseとRun fenceを検証 | 未認可/旧epoch action 0 | SC-07/SC-08 |
| BACT-002 | MUST | Page/Tab lockを1操作+結果観測まで占有 | 同Tab同時click不可 | SC-06/SC-07 |
| BACT-003 | MUST | Snapshotにframe/URL/generation/observedAtを付す | 旧snapshot由来要素拒否 | SC-06/SC-13 |
| BACT-004 | MUST | 操作直前にlocator cardinality=1とvisible/enabledを検証 | 2個一致やhiddenでclick 0 | SC-07 |
| BACT-005 | MUST | DOM/ARIA優先でcandidate選出、screenshot fallbackは一時観測 | 観測が曖昧なら確認待ち | SC-05/SC-06 |
| BACT-006 | MUST | click/fill/submitの副作用分類をGatewayに委譲 | 送信/購入/予約の無承認実行0 | SC-08 |
| BACT-007 | MUST | 操作後URL/DOM/target stateを再観測 | click API successだけでgoal completeにしない | SC-05/SC-07 |
| BACT-008 | MUST | Timeout/retryを有限にし状態不確定なら再clickしない | UNKNOWN送信の重複0 | SC-07 |
| BACT-009 | MUST | Popup/iframe/Shadow DOMでframe ownershipとoriginを照合 | 跨origin認可逸脱0 | SC-06/SC-08 |
| BACT-010 | MUST | cancel/steer/worker restartでold commandをfence | obsolete result current反映0 | SC-07 |
| BACT-011 | MUST | screenshot/DOM内容をtoken・byte/PII上限で縮小 | credentialをmodelへ出力0 | SC-08/SC-09 |
| BACT-012 | MUST | Action traceが証跡/試行/結果を遡れる | 各stepにbefore/after artifact ref | SC-05/SC-09 |
| BACT-013 | SHOULD | read-only actionを学習Recipeへ渡す際検証条件を添付 | DOM変更時replayを停止 | SC-13 |
| BACT-014 | LATER | pixel-only policyを隔離されたsandboxで実験 | 測定後のみON | SC-09 |

## 5. I/O / 型 / Validation（CHAT_04内の提案）
```typescript
type ActionCommand = { schemaVersion:1; sessionId:string; tabId:string; expectedEpoch:string; goalRevision:string; operationId:string; snapshotRef?:string; action:"snapshot"|"navigate"|"click"|"fill"|"scroll"|"select"|"press"|"screenshot"; target?:{frameRef:string; locatorKind:"role"|"label"|"testId"|"css"; expression:string; domGeneration:string}; valueRef?:string; timeoutMs:number };
type ActionOutcome = { status:"observed"|"acted"|"uncertain"|"needs_human"|"cancelled"|"stale"; beforeRef?:string; afterRef?:string; navigationUrl?:string; sideEffectClass:"read"|"ambiguous"|"external-write"; observedAt:string; errorClass?:string };
```
Target selectorはクライアント/LLMから自由なJS式を受けない。Locator種別はallowlist、Frameはsame session/Tab、valueRefはGateway側の秘匿管理、DOM generationは現在snapshotのものと一致、max action timeout 12s。
外部公開Command/Event/Error/Evidence/ArtifactはCHAT_01/03のschemaを参照。[PROVISIONAL]。バージョン不明のDTO、権限のないtab ID、古いgoalRevision、予算超過はaction開始前にrejectする。

## 6. State machine + sequence
```text
NEW→SNAPSHOTTED→TARGET_VALIDATED→POLICY_ALLOWED→ACTION_DISPATCHED→POST_OBSERVED→RECORDED
TARGET_VALIDATED→STALE→RESNAPSHOT; DISPATCHED→UNKNOWN (lost ACK); ANY→CANCELLED
```
1. 現行Task/Run fenced lease取得→2. read-only Snapshot→3. Candidate role/name/frame/URL/generationの確定→4. Gateway分類/承認→5. page lockでpre-action再検査→6. 1 action→7. state再観測→8. observation/fence commit→9.次StepをRuntimeに返す。

## 7. アルゴリズム・疑似コード（実行順）
```typescript
async function execute(cmd, ctx) {
 const owner = await sessionManager.acquire(cmd.sessionId,cmd.expectedEpoch,ctx.principal);
 return owner.withPageMutex(cmd.tabId, async page => {
   await assertLiveFence(ctx); const pre = await snapshotWithBudget(page);
   const requiresTarget = ["click","fill","select","press"].includes(cmd.action);
   const target = requiresTarget ? strictResolveLocator(page,cmd.target,pre) : null;
   if (requiresTarget && (!target || !exactlyOneVisible(target))) return stale("TARGET_AMBIGUOUS");
   const grant = await gatewayAuthorize(cmd,pre,ctx);
   if (!grant.allowed) return denied(grant);
   const receipt = await ledgerRecordIntent(grant,ctx);
   const action = await runSingleAction(page,cmd,receipt,ctx.signal);
   const post = await snapshotWithBudget(page).catch(()=>null);
   return persistWithFence(classifyOutcome(action,post),ctx);
 });
}
```
進行中TaskをSteer/Cancelした場合、新規Tool intentを投入せず、現行Actionの完了結果を新しいGoalへ暗黙適用しない。DOM再計画前にSession/Page generationを再取得。Agentに可逆操作と確認済の違いを伝える。

## 8. Failure matrix / race / recovery
| 障害 | 検出 | Retry / 回復 | 結果 |
|---|---|---|---|
| DOM replace | generation mismatch | 新snapshot後に再計画 | STALE |
| double match | locator.count!=1 | retry禁止、target再選択 | NEEDS_REPLAN |
| click timeout after network send | after state不明 | 自動reclick禁止 | UNKNOWN |
| human takeover | mode=HUMAN | Agent action拒否 | WAITING_INPUT |
| iframe origin changed | frame and egress mismatch | abort | DENIED |
| tab close/worker kill | Page closed/lease invalid | context検査→resnapshot | RECOVERING |
Network送信/外部クリックとDB commitはatomicではない。送信後lost ack・browser killはTool Ledgerへ`UNKNOWN`を報告し、不可逆操作の無条件再実行は禁止。

## 9. Security / privacy / Budget
Cross-origin iframeの混同とdocument.writeで偽ボタン挿入は非信頼。可逆なscroll/読み取りと、submit/checkbox/メッセージの外部作用を分離。入力フィールド値はsecret storeから安全にinjectしてモデルへ戻さない。スクリーンショットはSecret redaction不確実なら禁止。
- URL、iframe、popup、subresourcesのegressはCHAT_09/04-02に再検査を要求する。Web文面/DOMはuntrusted、system命令として実行しない。
- Cookie/API key/認証画面の秘密・スクリーンショットはmodel-visible Artifactに自動露出させない。記録ポリシーが秘匿できない場合はscreenshot禁止。
- 初期提案上限は1 action ≤12s、1 snapshot ≤256KiB DOM representation・スクリーンショット≤1MiB、連続action ≤20/Run（提案値）。**最終値はTool Gateway全体BudgetとサイトPolicyのmin**。時間/メモリ/トークン/外部API出費に必ず有限の上限。計測はtask,run,leaseEpoch,actionId,DOM generation,attempt,status,latencyMs,RSS,tokenCost,sideEffectClass,artifactRef。
- CI fixtureの同一環境で、p50/p95・token/step・成功率・ページ変化下の安全失敗率を測定して採用する。性能比較は未実施。

## 10. 永続化・Transaction / Migration
Before/After snapshot artifacts・page generation・action intent/receipt・fence・sideEffectClass・observedAt、failure categoryを論理保存提案。Raw DOMはArtifact Storeに置きLLM contextに全文を送らない。
[PROVISIONAL: CHAT_10] Browser Session/Task/Run UUIDv7とSession/Run leaseEpochのDB CAS、nullable Conversation、recordVersion初期値1とrevision0の差、tenant-to-workspace mappingの統合はP0。物理DDLを本書で勝手に作らない。操作意図commit→外部操作→Result/Artifact commit→outboxの境界を守る。

## 11. UI/REST/他モジュール
対象URL、Tab、前後の閲覧差分とステータス、不確定actionの再実行確認を表示。Click成功＝購入完了の表示は禁止。

## 12. 実装PR分割
1. **04-BACT-1** snapshot/ARIA locator resolver+unit
2. **04-BACT-2** Gateway/fence/tab mutex/action executor
3. **04-BACT-3** post-action evidence/UNKNOWN recovery
4. **04-BACT-4** human takeover & adversarial multi-site E2E

## 13. Test design（**全件設計のみ / 実行0件**）
| TEST | Setup/Action（Mock・fixture） | Expected | REQ→SC |
|---|---|---|---|
| BACT-T01 | Stale run tries click | 未認可/旧epoch action 0 | BACT-001→SC-07/SC-08 |
| BACT-T02 | Two concurrent click same tab | 同Tab同時click不可 | BACT-002→SC-06/SC-07 |
| BACT-T03 | Snapshot then DOM replaced | 旧snapshot由来要素拒否 | BACT-003→SC-06/SC-13 |
| BACT-T04 | Duplicate aria role/hidden button | 2個一致やhiddenでclick 0 | BACT-004→SC-07 |
| BACT-T05 | DOM vs screenshot disagree | 観測が曖昧なら確認待ち | BACT-005→SC-05/SC-06 |
| BACT-T06 | Unapproved submit target | 送信/購入/予約の無承認実行0 | BACT-006→SC-08 |
| BACT-T07 | Click returns but page unchanged | click API successだけでgoal completeにしない | BACT-007→SC-05/SC-07 |
| BACT-T08 | Timeout after submit side effect | UNKNOWN送信の重複0 | BACT-008→SC-07 |
| BACT-T09 | Popup and cross-origin frame | 跨origin認可逸脱0 | BACT-009→SC-06/SC-08 |
| BACT-T10 | Cancel/steer between observe and click | obsolete result current反映0 | BACT-010→SC-07 |
| BACT-T11 | PII/password in image | credentialをmodelへ出力0 | BACT-011→SC-08/SC-09 |
| BACT-T12 | Trace before/after retrieval | 各stepにbefore/after artifact ref | BACT-012→SC-05/SC-09 |
| BACT-T13 | Learned replay changed DOM | DOM変更時replayを停止 | BACT-013→SC-13 |
| BACT-T14 | Pixel-only benchmark | 測定後のみON | BACT-014→SC-09 |

## 14. 受入とTraceability
前表でMUSTごとにREQ → TEST → SCを網羅。実際の合格はMock test/Browser fixture/Crash injectionを実施しassertした後のみとする。テスト設計は実装を意味しない。

## 15. Cross-team proposal / 未決
- **[PROVISIONAL: CHAT_03] P0 external side-effects:** fill/submit/navigateの副作用とapproval gateをAction前に確認。合格: BACT-T06/08。
- **[PROVISIONAL: CHAT_01/02/10] P0 epoch/revision:** Tab actionのfenced read-after-writeと64-bit lease。合格: BACT-T01/10。
- **[PROVISIONAL: CHAT_14] P1 learned action replay:** snapshot fingerprintとAction gateを共通化。合格: BACT-T13。
- **[PROVISIONAL: CHAT_08/09] P1 takeover UI:** Agent/Human相互排他とsnapshot redaction。合格: BACT-T04。
共通契約P0が残る限りDraft維持。CHAT_14のLearned Browser Actionsに対しては同じSession lease/Target freshness/Approval Gateを利用する契約を提案する。

## 16. Sources（2026-10-30 JST）
[S1] **Playwright**, Apache-2.0, **Code verified: source inspected, tests read not executed**, commit `62d25ac221370f6fc5e26c600823792b83c67f1e` (2026-10-30): [`packages/playwright-core/src/client/locator.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts) (Locator.click/fill); [`packages/injected/src/ariaSnapshot.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/injected/src/ariaSnapshot.ts) (generateAriaTree/renderAriaTreeAsJSON); [`tests/library/browsercontext-storage-state.spec.ts`](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/tests/library/browsercontext-storage-state.spec.ts) (context isolation tests).
[S2] **Browser Use**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `2cc71890e65056b67fdb29d98bb407749c15bb08` (2026-10-30): [`browser_use/tools/service.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py) (Tools._click_by_index/input/switch/scroll); [`browser_use/browser/session.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py) (BrowserSession); [`tests/ci/test_tools.py`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_tools.py) (fixture HTTP server + action tests).
[S3] **jev-browser**, 導入前LICENSE再確認, **Code verified: source inspected, tests read not executed**, commit `817b09eea6e21aad836b5ed32a613c68e5644a08` (2026-10-30): [`src/navigate.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/navigate.ts) (NavigateOptions.page, navigate/abort/budget); [`src/provider.ts`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/src/provider.ts) (askJev / InvalidJevAnswer); [`test/unit.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/unit.test.mjs) (injected Page life-cycle, redaction, action space); [`test/e2e.test.mjs`](https://github.com/jkudish/jev-browser/blob/817b09eea6e21aad836b5ed32a613c68e5644a08/test/e2e.test.mjs) (key-gated fixture navigation).
**Design hypothesis:** 本書のType、state、limit、timeout、retry、fault protocolはCHAT_04独自の未検証提案。正式Shared ID/Event/Policy/DDLの正本は各Owner。


### Round 2 Action種別テスト補強（2026-10-30）

`navigate`/`scroll`/`snapshot`/`screenshot` は必ずしもDOM Locatorを持たない。旧擬似コードは `snapshot` 以外の全操作を `exactlyOneVisible(target)` に渡していたため、Locatorなしの正常操作が実行不能になる。現在は `requiresTarget` を明示し、`click/fill/select/press` のみ一意Locator必須。**T-BACT-004** は重複/非表示Locatorの拒否に加え `scroll` と `navigate` が適切なPolicy/egress審査を経て実行できることを試す。Navigationにも必ずGateway許可とHTTP/Browser egressを適用する。

