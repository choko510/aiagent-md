# DEV-03 — Visual Design Picker & Executable Demo Approval
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/04/08/09/10/11。Demoは未実装。

## 1. 意図
デザイン名称（Fluent Design 2、Material Design 3、Minimal等）**だけ**でなく、AIが同じ要件・同じコンテンツ・同じviewportを用いて生成した複数の外観を**サムネイル/画像/動くデモで直感的に選べる**ようにする。選定パターン数は既定3・有限上限あり。名称はデザインの参考スタイルであり、他社のロゴ/アセット/完全な著作物複製を意味しない。最終採否はユーザーの好みと機能要件を分離して記録。

## 2. UI 操作
比較画面は候補カード（light/dark、desktop/mobile、same fixture content、キーボードアクセス、比較の同期スクロール）、拡大、選択、採用/見送り/部分統合、自由テキスト変更・要素クリックによるフィードバック、再生成を提供。未提供/破損プレビューは画像なしの候補として明示し、存在しないデモを表示しない。候補の座標はsnapshot+viewport+DOM generationへbindし、別revのクリック座標を流用しない。
- Design reviewの選択肢は `AUTO_RECOMMEND`(A4推奨案を提示)、`DECIDE`(ユーザー比較選択)、`CUSTOMIZE`(特定部品/技術/要件だけ手動選択)。
- Demo reviewの選択肢は `REQUIRE_USER_APPROVAL`、`PREAUTHORIZED_BOUNDED_AUTO`、`REQUEST_REVISIONS`。Demo採用は既定でユーザー承認必須。事前に限定した委任が存在する場合だけ自動採用を許す。選択しない場合の既定は**DEMO確認後に本実装**。
- UI previewは画像だけでなく、画面遷移/ボタン/フォームを操作できるプログラム。未接続API・ダミーデータ・未実装操作を視覚的に明示。実決済や外部書き込みをデモで実行しない。

## 3. 凍結する契約
```ts
type DesignChoiceV1 = { cycleId:string; candidateIds:string[]; comparedFixtureDigest:string;
 viewportProfiles:string[]; selectedCandidateId?:string; selectedParts?:string[];
 acceptedRequirements:string[]; rejectedReasons?:string[]; userFeedback?:string;
 designRevision:string; expectedRecordVersion:string };
type DemoManifestV1 = { projectId:string; cycleId:string; designRevision:string;
 repoCommitSha:string; runtimeImageDigest:string; fixtureDigest:string; mockCapabilities:string[];
 routeList:string[]; previewSessionRef:string; screenshotArtifactRefs:string[];
 declaredUnimplemented:string[]; checksRef?:string };
```
ProducerのdemoManifestは`proposed`のみ、ユーザーが権限付きUIから受理するとDesign/Demo approval receiptが不可変になる。Tool副作用承認/ログインhandoffとは**別**。Approveはserver-side authenticated principal、CSRF、If-Match、Idempotency-Keyを必須とし、正確なmanifest digest、GoalRevision、対象projectにbindする。古いDemo、失効/別owner/競合で合意しない。変更依頼は新Revisionを発行し、再作成して再確認。

## 4. デモ品質と本実装の一致
Demoは本実装に近いコンポーネント構成、ルート・操作・主要レスポンシブ設計を実装。モック接続を実装済みAPIと誤認しない。承認済みDemoをscreenshot golden baseline/Interaction contractとして保存し、実装後にviewport別visual regression・アクセシビリティ・ユーザーフローとの一致を確認。微細な差分（font rendering等）と機能破壊差分を区別するthresholdは初期仮値で、false-positive fixtureで校正し、LLMによる「似ている」だけで合格にしない。

## 5. 分離/権限
開発Previewは固定Project/Worktree/Revision/portのPreview Broker経由。Browser WorkerがPage/CDPを単独保持する。閲覧専用とモック環境限定demo:interactを別Capabilityとして区別し、端末へのraw CDP/secret/localhost bridgeを出さない。機密Browser takeoverは別の既存専有leaseで管理。Preview contentは攻撃者が変更可能な低信頼Webコンテンツとして扱う。Browser Live Observation (#71) が未mergeでも通常の静止画プレビューと受入APIは成立する。

## 6. Tests
DEV-DES-T01: 3案が同一fixture/content/viewport、画像クリックで正しい候補を選択。
DEV-DES-T02: mobile/light/darkとkeyboard選択・読み上げラベルを検証。
DEV-DES-T03: stale manifest/old Goal/別tenant/二重承認/同時選択を拒否。
DEV-DES-T04: mock API動作をproduction接続と虚偽表示せず実側効果ゼロ。
DEV-DES-T05: user修正→新Demo生成→旧承認で本実装再開不可。
DEV-DES-T06: 本実装との差分とfunctional regressionをBrowser検査で判定。

## 2026-11-08 P1-01/P1-02 — Interactive Demo versus Human Takeover

**Viewer modes are separate capabilities** rather than a single viewer right:
- `preview:observe`: authenticated view-only snapshot/live observation; does not permit input or Browser Action.
- `demo:interact`: limited **synthetic/mock session** interaction (click, fill, scroll, navigate among enumerated in-demo routes, keyboard). Browser Worker remains CDP owner; an authenticated UI command is relayed through scoped Broker commands, not raw CDP, DOM arbitrary JS or local port forwarding. The controlling human must hold a single exclusive interactive lease; the AI's browser actions on that tab pause until returned to prevent races. No production credentials, real external network effects, backend writes or mutable persistent user data. Mode is explicitly marked 'synthetic interactive demo' and is bound to `tenant+project+demoManifestDigest+session+generation+identityVersion+TTL`.
- `browser:takeover`: existing sensitive Browser human login/verification takeover. It has separate risk policy, exclusive lease and fresh authorization; neither observe nor demo:interact implies takeover.
- `debug:synthetic`: AI test runner scoped to disposable fixture, protected against destructive real effects and not granted to ordinary viewers.
Server issues one capability per mode with mutually exclusive write control. Unknown action, route, network target, external send or stale preview generation => DENY. Invalidated mock state is reset at new demo session creation. Clickable preview iframe is isolated from Web Dashboard principal, cookies, storage and trusted cross-window messaging. No same-origin script interface into Web UI.

**Design selection vs demo acceptance vs external-effect approval are three distinct decisions**:
```ts
type DesignSelectionPolicyV2 = "AUTO_RECOMMEND" | "REQUIRE_USER_SELECTION";
type DemoAcceptancePolicyV2 = "REQUIRE_USER_APPROVAL" | "PREAUTHORIZED_BOUNDED_AUTO";
type DemoDecisionV2 = { projectId:string; cycleId:string; designRevision:string;
  demoManifestDigest:string; goalRevision:string; expectedRecordVersion:string;
  decision:"APPROVE"|"REJECT"|"REVISE"; principalRef:string; decidedAt:string };
```
`AUTO_RECOMMEND` selects a proposal to display, **not** approval for implementation. The default for Feature Factory is `REQUIRE_USER_APPROVAL`. Generic development also defaults to explicit approval unless the owner previously granted a narrow, revocable `PREAUTHORIZED_BOUNDED_AUTO` mandate identifying project, goal, allowed feature categories, permitted demo revisions/risk limits, expiry, maximum number of implementations, and no irreversible external effects. This mandate is checked server-side on **each** acceptance, and an auditable synthetic decision receipt records the grant ID/version rather than impersonating a human click. No implicit acceptance because a timer elapsed or user was offline. A change to design, goal, manifest, principal or grant invalidates the old decision. **Any feature carrying real external side effects requires separate Tool Approval regardless of demo mode**.

**DEV-DES-T07**: ordinary user can interact with buttons/forms in synthetic demo without privileged takeover, while access to real API/other origin fails.
**DEV-DES-T08**: two concurrent humans/AI competing for demo input → only one active lease, old-generation actions denied.
**DEV-DES-T09**: `AUTO_RECOMMEND` never causes implementation; Feature Factory requires a real decision or preauthorized bounded grant.
**DEV-DES-T10**: default generic development cannot silently interpret demoReady as approved; revoked/expired mandate invalidates future acceptances.
**DEV-DES-T11**: cross-origin script/cookie/iframe/storage attempt from malicious demo cannot access dashboard or another project.
