# DEV-06 — Autonomous Browser Debugger
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_02/03/04/08/09/10/11。実装・E2E未実施。

## Scope
AI自身が作成したアプリを操作し、エラーを証拠付きで発見・再現・ソースへ関連付け・自動修正・再検証する。exit0、DOMが存在すること、クリックAPI成功、LLMの「問題なし」を検証の代わりにしない。既存 [Browser Session](../04-web-browser/browser-session-manager.md)、[Action Engine](../04-web-browser/browser-action-engine.md)、Jev、Playwrightを交換可能adapterとして利用し、Session lease/Tool Gateway/Side effect判定は再実装しない。

## 7層のデバッグ
1. **操作**: routes, buttons, form, search, filters, modal, menu, scroll, keyboard, tab, focus, back/forward/reload。
2. **画面**: desktop/mobile/tablet、320pxなど狭幅、ダークモード、文字拡大、多言語、overflow、focus/contrast/アクセシビリティ。
3. **JS**: pageerror、Console、unhandled rejection、hydration/render、event loop。
4. **通信**: failed request、HTTP 4xx/5xx、timeout、WebSocket、API retry、競合、遅延。
5. **状態**: save/reload、複数タブ、セッション失効、空データ、連打、入力境界値。
6. **性能**: load/interaction、Web Vitals、CPU/RAM/long task、browser version固定のベンチ。
7. **異常系**: offline、遅い回線、API failure、server crash/reconnect、データ破損fixture。

## Exploration Pipeline
`DISCOVER_ROUTES → PLAN_SCENARIOS → EXECUTE (bounded) → COLLECT_EVIDENCE → DEDUPE_FINDINGS → REPRODUCE → REPAIR → REGRESSION → VERIFY`。
Browser Worker専有のContext/PageでARIA/DOM/Locatorを優先、必要時にvision/screenshot。操作前のURL/frame/DOM generation/Run leaseを固定、操作後のURL/DOM/HTTP/expected stateを再観測。自由なJS、raw CDP、あらゆるURLへの任意fetch、無限クリックは許さない。探索には最大route/アクション/CPU/費用/時間、停止条件を設定。新規画面と変更関連ルートを重点的に、確立されたE2Eの回帰を毎回実施。

## Evidence and correlator
```ts
type DebugFindingV1 = {
 findingId:string; projectId:string; cycleId:string; commitSha:string;
 designRevision:string; demoRevision?:string; scenarioId:string;
 browserVersion:string; viewport:string; fixtureDigest:string; imageDigest:string;
 severity:"P0"|"P1"|"P2"|"P3"; fingerprint:string;
 stepsArtifactRef:string; expected:string; observed:string;
 screenshotRefs:string[]; traceRefs:string[]; consoleRefs:string[];
 networkRefs:string[]; correlatedCodeRefs:string[];
 explanationStatus:"PROVEN"|"HYPOTHESIS"|"UNVERIFIED" };
```
証拠Artifactのraw screenshot/HAR/bodyは所有権・暗号化・保持期間・最大bytesを持ち、token/cookie/PII/Authorizationなどはredact。分類不能な通信内容はModel/一般UIに送らない。JSスタック/API request IDとserver log、source maps、commitShaを照合し、原因を確定できなければ仮説のまま保持。Trace/動画はopt-in/retention policyに従う。HAR replayがService Worker/時間のすべてを再現するという保証はしない。

## Visual Regression
承認済み [Demo Manifest](visual-design-and-demo.md) をbaselineに、同一viewport/theme/font/fixture/browser/runtime/animation状態を揃えたpixel+layout geometry+semantic assertions。差分しきい値/マスクは誤検知fixtureで校正し、意図しないUI変更を「改善」として自動承認しない。デザイン修正時だけ承認済みrevisionを更新。パフォーマンス改善も同一条件の前後実測が必要。

## Real-time debugging UX
ブラウザ映像(可能な場合)、before/after画像、操作タイムライン、Console、Network、trace、DOM/ARIA、source path、再現ボタン、修正コミットへジャンプ。実映像は別PR [Browser Live Observation #71](https://github.com/choko510/aiagent-md/pull/71) の適用/検証後のみ依存。未mergeの間は静止画/trace上で成立。Observe-onlyとhuman takeoverを分け、閲覧がbrowser operation権限とならない。

## Security & remediation
Preview Brokerは [DEV-05](build-preview-release.md) の専用認証routeだけ許す。本番DB/API/実支払/メール/削除/投稿に向けた破壊的操作は使い捨てfixture/mockで検査。Agent自動操作はBrowser Tool Gatewayと個別必要承認を通し、login handoff中は専有leaseを失う。診断はA8や専門サブAgentが担当可、修正・Git書込はA5 Builderに限定。修正前に再現シナリオを保存し、新SHAで不具合テスト+既存回帰を再実行。古いSHAのPASSでは解決としない。

## Tests
DEV-DBG-T01 click成功で状態変化なし→FAIL。
T02 狭幅のボタンはみ出し・重なり→DOM+画像で検出。
T03 HTTP500とConsole stack/server request ID/source map相関。
T04 offline/slow/reload/back/empty data/invalid inputのシナリオ実施。
T05 approved demoとの意図しない差分→FAIL。
T06 PII/secret入りHAR/Traceをmodel/UIへ生送信0。
T07 閲覧中/操作handoff中にagent操作が権限逸脱0。
T08 Browser Worker crash→未完scenarioはUNKNOWN、復旧して再確認。
T09 別tenant/CDP/private bridge/他port不可。
T10 新SHAでerror再現が消え回帰PASSした時だけFIXED_VERIFIED。
