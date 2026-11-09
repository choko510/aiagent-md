# Development Debugging & Verification — DEV-06 / DEV-07 / DEV-12

> Draft / PROVISIONAL。ブラウザデバッグ、独立レビューと修正ゲート、再現ラボの原文を仕様ID/テストIDを維持して同一の検証領域に集約。独立した責務・Ownerの承認は継続して必要。

## DEV-06

**DEV-06 — Autonomous Browser Debugger**

> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_02/03/04/08/09/10/11。実装・E2E未実施。

### Scope
AI自身が作成したアプリを操作し、エラーを証拠付きで発見・再現・ソースへ関連付け・自動修正・再検証する。exit0、DOMが存在すること、クリックAPI成功、LLMの「問題なし」を検証の代わりにしない。既存 [Browser Session](../04-web-browser/browser-session-manager.md)、[Action Engine](../04-web-browser/browser-action-engine.md)、Jev、Playwrightを交換可能adapterとして利用し、Session lease/Tool Gateway/Side effect判定は再実装しない。

### 7層のデバッグ
1. **操作**: routes, buttons, form, search, filters, modal, menu, scroll, keyboard, tab, focus, back/forward/reload。
2. **画面**: desktop/mobile/tablet、320pxなど狭幅、ダークモード、文字拡大、多言語、overflow、focus/contrast/アクセシビリティ。
3. **JS**: pageerror、Console、unhandled rejection、hydration/render、event loop。
4. **通信**: failed request、HTTP 4xx/5xx、timeout、WebSocket、API retry、競合、遅延。
5. **状態**: save/reload、複数タブ、セッション失効、空データ、連打、入力境界値。
6. **性能**: load/interaction、Web Vitals、CPU/RAM/long task、browser version固定のベンチ。
7. **異常系**: offline、遅い回線、API failure、server crash/reconnect、データ破損fixture。

### Exploration Pipeline
`DISCOVER_ROUTES → PLAN_SCENARIOS → EXECUTE (bounded) → COLLECT_EVIDENCE → DEDUPE_FINDINGS → REPRODUCE → REPAIR → REGRESSION → VERIFY`。
Browser Worker専有のContext/PageでARIA/DOM/Locatorを優先、必要時にvision/screenshot。操作前のURL/frame/DOM generation/Run leaseを固定、操作後のURL/DOM/HTTP/expected stateを再観測。自由なJS、raw CDP、あらゆるURLへの任意fetch、無限クリックは許さない。探索には最大route/アクション/CPU/費用/時間、停止条件を設定。新規画面と変更関連ルートを重点的に、確立されたE2Eの回帰を毎回実施。

### Evidence and correlator
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

### Visual Regression
承認済み [Demo Manifest](feature-lifecycle-and-demo.md#dev-03) をbaselineに、同一viewport/theme/font/fixture/browser/runtime/animation状態を揃えたpixel+layout geometry+semantic assertions。差分しきい値/マスクは誤検知fixtureで校正し、意図しないUI変更を「改善」として自動承認しない。デザイン修正時だけ承認済みrevisionを更新。パフォーマンス改善も同一条件の前後実測が必要。

### Real-time debugging UX
ブラウザ映像(可能な場合)、before/after画像、操作タイムライン、Console、Network、trace、DOM/ARIA、source path、再現ボタン、修正コミットへジャンプ。実映像は別PR [Browser Live Observation #71](https://github.com/choko510/aiagent-md/pull/71) の適用/検証後のみ依存。実映像のRuntimeが未実装でも静止画/trace上で成立。Observe-onlyとhuman takeoverを分け、閲覧がbrowser operation権限とならない。

### Security & remediation
Preview Brokerは [DEV-05](build-preview-release.md) の専用認証routeだけ許す。本番DB/API/実支払/メール/削除/投稿に向けた破壊的操作は使い捨てfixture/mockで検査。Agent自動操作はBrowser Tool Gatewayと個別必要承認を通し、login handoff中は専有leaseを失う。診断はA8や専門サブAgentが担当可、修正・Git書込はA5 Builderに限定。修正前に再現シナリオを保存し、新SHAで不具合テスト+既存回帰を再実行。古いSHAのPASSでは解決としない。

### Tests
DEV-DBG-T01 click成功で状態変化なし→FAIL。
DEV-DBG-T02 狭幅のボタンはみ出し・重なり→DOM+画像で検出。
DEV-DBG-T03 HTTP500とConsole stack/server request ID/source map相関。
DEV-DBG-T04 offline/slow/reload/back/empty data/invalid inputのシナリオ実施。
DEV-DBG-T05 approved demoとの意図しない差分→FAIL。
DEV-DBG-T06 PII/secret入りHAR/Traceをmodel/UIへ生送信0。
DEV-DBG-T07 閲覧中/操作handoff中にagent操作が権限逸脱0。
DEV-DBG-T08 Browser Worker crash→未完scenarioはUNKNOWN、復旧して再確認。
DEV-DBG-T09 別tenant/CDP/private bridge/他port不可。
DEV-DBG-T10 新SHAでerror再現が消え回帰PASSした時だけFIXED_VERIFIED。

---

## DEV-07

**DEV-07 — Independent Triple Review & P0/P1 Repair Gate**

> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/03/08/10/11。試験未実施。

### Review roles / independent evidence
A6(機能・コード品質・回帰)、A7(セキュリティ・secret・依存・権限)、A8(UI/アクセシビリティ/Browser E2E)が**同一commit SHA/同一designRevision/同一fixture+build環境**をread-onlyで独立レビュー。初回レビュー時は他Reviewer/Builderの自己評価を混ぜない。必要なら再現コードやブラウザデバッグを実行し、Source line、再現条件、期待/観測、Trace/テスト結果を必須とする。1人でも未完なら「3AIが合格」と表示しない。

### Severity / status
- P0: user data loss、認証逸脱、secret漏洩、host/VM境界破壊、アプリ主要機能全滅など。
- P1: 主要な利用経路の破綻、重大な回帰、再現性ある深刻な不具合/セキュリティ問題。
- P2/P3: 限定条件の不具合/改善候補。P2もrelease policyで追加ブロック可。
Issue state: `OPEN`, `FIX_PROPOSED`, `FIXED_VERIFIED`, `DISMISSED_WITH_EVIDENCE`, `UNVERIFIED`。誤検知のdismissは反証を証拠として保存。モデルのseverityラベルや「解消済み」の文章だけで閉じない。

```ts
type ReviewFindingV1 = { findingId:string; reviewerRole:"functional"|"security"|"ui_e2e";
 cycleId:string; commitSha:string; designRevision:string; severity:"P0"|"P1"|"P2"|"P3";
 fingerprint:string; title:string; sourceRef?:string; evidenceRefs:string[];
 reproductionRef:string; state:"OPEN"|"FIX_PROPOSED"|"FIXED_VERIFIED"|"DISMISSED_WITH_EVIDENCE"|"UNVERIFIED";
 resolutionCommitSha?:string; verifyReceiptRef?:string };
type ReviewRoundV1 = { cycleId:string; round:number; codeSha:string;
 requiredReviewerReceipts:string[]; testManifestDigest:string;
 verdict:"REPAIR"|"APPROVED"|"BLOCKED"|"PARTIAL" };
```
shared IDs/events/stateはCHAT_01のcanonical契約レビューを要する。

### Repair Loop
`IMPLEMENT → TEST → COMMIT(SHA_n) → 3 REVIEWERS → MERGE FINDINGS → if P0/P1: REPAIR → TEST → COMMIT(SHA_n+1) → 3 NEW REVIEWS ...`。
- 3 reviewer receiptsとtest receiptsが最新SHAにbind。異SHA/old GoalRevision/old DemoVersionを使ったpassは無効。
- 重複findingはfingerprint+repro+根拠でdedupe。異なる根本原因を安易に同一扱いしない。
- 新SHAでaffected regression, whole required suite, Browser Debuggerを再実行し、見た目と機能の新規P0/P1も確認。
- 進捗なし/同じバグ再発/修正反転/レビュアー対立は [Expert Advisor](development-orchestration.md#dev-04) を費用上限付きで呼ぶ。一定回数(Operator/ユーザー設定の有限閾値)で収束しなければPARTIAL/BLOCKEDと残件を公開し、無駄な反復しない。
- PASS条件: latest SHA + approved design/demo、必要テスト**実施して合格**、独立3AIのvalid receipts、未解決P0/P1なし、security isolation OK、外部Effect状態確定。未実行/unknown/missing evidenceはPASS不可。
- 各修正を一貫した粒度でcommitし、PRにはSHAごとの修正・再現・テスト・未実施を記述。ユーザーが許可したcommit/PR scope外へ自動push/mergeしない。
- Endless Campaignでは同一CycleがBlockedでも他の独立Cycleを継続可。ただし対象ReleaseがP0/P1残存のまま「完了数」を増やすことは禁止。

### Tests
DEV-REV-T01: independent3件同SHAでPASS、2件なら拒否。
DEV-REV-T02: P0を修正してP1新規→再Round必須。
DEV-REV-T03: oldSHAレビュー、missing E2E、partialログ、未検証dismissでPASS不可。
DEV-REV-T04: reviewer間矛盾→追加証拠/Expert、自己評価で決めない。
DEV-REV-T05: 反復失敗→bounded Escalation、無制限ループなし。
DEV-REV-T06: cancel/revoke後に新PR・external tool起動0。

### 2026-11-08 P1-10 — Feature Review is not Integration Review

Individual branch verification of SHA_F and integration verification of SHA_I are distinct signed/evidence-bound receipts. A protected Git integration must identify an immutable expected parent SHA and resulting integration SHA. The merge/fast-forward operation is CAS-protected by project. A verifier MUST NOT reuse the original A6/A7/A8 first-pass for SHA_F as if they reviewed SHA_I. Any changed UI/component/API/schema/auth/permission flow receives integration regression/E2E and independently attributable A6/A7/A8 receipts against SHA_I. A reviewer with no affected files can return `NO_IMPACT_WITH_EVIDENCE`, not silent PASS; three receipts still required for the integration gate. An external protected-branch provider may reject CAS merge; status remains BLOCKED and requires rebase then new integrated-head tests. The Feature receipt hierarchy is `IMPLEMENTED→VERIFIED→INTEGRATED→RELEASED`; failure at a later stage never retroactively changes a historical commit receipt, but **current completion eligibility** depends on integration head freshness. See DEV-08 and CAM-01.

**DEV-REV-T07**: SHA_F 3-reviewed but SHA_I contains another feature; no integration pass until tests and SHA_I reviews.
**DEV-REV-T08**: merge occurs after integration receipt; prior receipt becomes stale and feature count current eligibility is recomputed.

---

## DEV-12

**DEV-12 — Reproduction Lab / 不具合の再現・原因特定・修正証明**

> **Draft / PROVISIONAL**, 2026-11-09。実装・実runner・実ブラウザ試験なし。DEV-06 Browser Debugger、DEV-07 Review/Repair、DEV-11 PR Guardianへ**証拠に基づく再現・履歴調査**を追加する設計案。型/状態はCHAT_01、ExecutionはCHAT_02/09、ArtifactはCHAT_01、Toolと外部アクセスはCHAT_03/04/05、QAはCHAT_11が正本。

### 1. 提案の核と比較

既存DEV-06はルート探索、Console/Network、Visual差分、Browser Workerの相関調査を持つ。新たに、ブラウザ以外のCLI/Unit/Integration/非決定的CI失敗も対象として、**「修正前に再現 → 修正後は同じ条件で解消 → 旧動作が退行していない」**を独立した判定にする。

採用の参考:
- [Git `bisect`公式](https://git-scm.com/docs/git-bisect/)：良いcommitと悪いcommit間の探索、`bisect run` の終了コード`0=good、1..127（125を除く）=bad、125=skip`。不確かなskipでは単一犯人commitを断言しない。
- [pytest Flaky Tests](https://docs.pytest.org/en/latest/explanation/flaky.html)：状態漏洩、順序依存、並列性、時間依存などが揺れを起こす。単純rerunは根本解決でない。
- [Open SWE](https://github.com/langchain-ai/open-swe)のCI分析・根拠がある場合だけのflaky rerun。自動再試行は証拠化が先。
- DEV-06 Browser Trace + DEV-07 independent findingsの共通Evidence/Workspace/Runをそのまま使用。独自証拠DB/別Browser権限は新設しない。

### 2. 実行条件と成果物

Input: `projectBinding`、`failureObservationRef`（CI/job log、exception、Issue、Browser trace、user steps）、`expectedBehavior`、`badSha`、任意`knownGoodSha`、`targetScope`、`reproductionBudget`。非信頼ログやIssueの文章だけでshell命令を生成し無条件実行しない。

Outputs: versioned `ReproCase` proposalと実行manifest、最小failure reproduction、原因候補とconfidence分類、`before`/`after`比較、必要ならbisect候補範囲、修正パッチと最新SHAに紐付くtests receipts。Fixtureにsecret、実個人情報、認証済みCookieを含めない。

論理型提案（canonical Entity/Error/Event/Artifactの変更は別owner決定）:

`ReproCaseProposal { caseId, projectId, logicalWorkItemId, goalRevision, originalFailureRef, expectedBehaviorRef, baseSha, failingSha, testRecipeDigest, runtimeImageDigest, dependencyLockDigest, fixtureDigest, seed, timeMode, networkMode, failureFingerprint, status, evidenceRefs, recordVersion }`

`ReproOutcomeProposal { caseId, evaluatedSha, runId, verdict: "REPRODUCED"|"NOT_REPRODUCED"|"FLAKY"|"INCONCLUSIVE"|"BLOCKED", attemptCount, actual/expectedRefs, testReceiptRefs, environmentDigest }`

- 再現`REPRODUCED`は該当SHAに対する同じrecipe/fixtureの失敗を意味し、根本原因確定とは別。
- `NOT_REPRODUCED`は成功ではない。環境依存/条件不足の可能性を明示。
- `FLAKY`は同一入力・同一SHAでPASS/FAILが混在した実測。
- `INCONCLUSIVE`はログ欠落・テスト不可・外部依存未制御・bisect skip過多。
- `BLOCKED`は認可、budget、安全なfixture、runtime欠如などで着手不能。

### 3. 実行パイプライン

`COLLECT → NORMALIZE → SELECT_RECIPE → BASELINE_RUN → [MINIMIZE] → [BISECT] → ROOT_CAUSE_HYPOTHESES → FIX → SAME_RECIPE_AFTER → REGRESSION → REVIEW`。各段階は有限Task/RunまたはExecutionに対応し、進行状態をcheckpoint/ledgerと接続する。`testRecipeDigest`や`goalRevision`変更時、以前の検証を再利用しない。

1. 症状を`source path/line, error signature, request ID, trace window, test name, platform, environment` に正規化。空のログを「問題なし」にしない。
2. データ・時計・乱数・タイムゾーン・Locale・feature flag・依存Lock・ブラウザviewport・OS/archを固定できる範囲で制御。ネットワークは固定fixture/mockが既定。LinuxとWindows等の差が疑われる場合は別cohortとして報告。
3. 既存テストで再現できればそのまま利用。できない場合は最小テストを**ゲスト内**で生成し、実行前に削除・決済・送信・本番接続を起こさないことをTool Policyで確認する。生成テストは不正なファイルやプロセスへのアクセスを試み得るため、Untrusted Execution VM内のみ。
4. 元の失敗SHAで**症状が出ることを実行確認**。必要に応じてdelta debuggingで入力/手順/依存を削り、最小再現へ縮小する。縮小によって症状が変わった場合は元の再現と同一視しない。
5. `knownGoodSha`の根拠と時系列/ancestorを検証できる場合に限り`git bisect`を隔離worktreeで実行。テスト不能なrevisionは`skip`で範囲に残す。全候補を検証できなければ`suspectRange`に留める。bisect用recipeは再現テストと独立した外部副作用なし。
6. Builderが修正後のSHAで**同一recipeを再実行**し、関連回帰・[DEV-14](code-intelligence-and-adaptive-testing.md)のimpact tests・DEV-06のBrowser E2Eを実行。失敗が消えたことだけで他要件を保証しない。
7. 根拠を`PROVEN`/`SUPPORTED_HYPOTHESIS`/`UNVERIFIED`に分け、ReviewFindingと紐付ける。推論だけで原因を`PROVEN`にしない。修正後だけテスト追加して旧SHAを走らせないケースは`REPRO_NOT_PROVEN`扱い。

### 4. Flaky Test と再試行の扱い

- まず同じSHA、入力、runner class、fixture digest、seedで許可された少数回を実行。結果の`passCount/failCount/attempts`を保存する。
- 一度でも成功したから安全と判定するのではなく、不安定なままなら`FLAKY/QUARANTINED_PROPOSED`として関連機能の完了Gateを独断で解除しない。
- 試験が一時的に成功しただけのCI rerunはDEV-11で`CI_RECOVERED_BY_RERUN`と区別。`BUG_FIXED`とは別。
- 時間・順序・資源競合などの原因を絞り込み、修正後は交互実行・ランダム順序・競合負荷といった**設定済み**検査を実施する。無限に繰り返さない。
- flaky quarantineは期間、責任者、解除条件を伴う明示の例外承認のみ。必須セキュリティ検査や要求されたE2Eを自動除外しない。

### 5. 安全性・競合・復旧

- 無関係な環境変数/secretをUser Issue、PR、CIログ経由で渡さない。失敗ログと脅威サンプルは不信任データ。試験結果はredact後にArtifactへ保存。
- 外部ネットワーク許可、環境準備、パッケージ取得、Git pushは既存Broker/Gateway/Sentinelに個別依頼。CLIやテストコードの直接Internetを禁止。
- 同一caseの複数workerは`caseId + evaluatedSha + recipeDigest + environmentDigest + attemptNo`を一意としてclaim。結果を二重加算しない。
- Worker deathや再起動後は、終了したRunの`test outcome`を引き継ぎつつ実行中未確定Attemptは`UNKNOWN`にする。新試行には新Attempt ID/予算予約が必要。
- 修正中にhead更新、designRev変更、Goal変更があれば旧結果は保持して新headのGateだけ無効化する。
- 相手サーバーへの負荷試験、破壊的データ作成、資格情報テストは事前承認のモック/使い捨て環境に限定。

### 6. UI/CLIと受入テスト（Designed / NOT RUN）

UI表示: 「現象」「再現条件」「修正前の実測」「原因の根拠」「対象コミット」「修正後の実測」「残る不確定性」の区別。再現用コマンド/動画・Traceを個人情報redact済みで提示。ユーザーが一部stepsを操作する場合はDEV-03のsynthetic demoまたはDEV-06のHuman takeover権限を使用。

| ID | シナリオ / 必須結果 |
|---|---|
| DEV-RPL-T01 | 再現テストが元SHAではFAIL、修正SHAではPASS→REPRODUCED/FIX_VALIDATEDの別receipt |
| DEV-RPL-T02 | 失敗が再現しない→成功扱い禁止、INCONCLUSIVEと追加条件 |
| DEV-RPL-T03 | 同一SHAでPASS/FAIL混在→FLAKY、単一rerunでFIXED不可 |
| DEV-RPL-T04 | Good/Badの間にbuild不能SHA→bisect skip、単一犯人を断定しない |
| DEV-RPL-T05 | 既存ブラウザTraceで再現→DEV-06同一Evidence refsを利用、重複収集を避ける |
| DEV-RPL-T06 | 生成テストがhost filesystem/secret/networkへアクセス→隔離・拒否 |
| DEV-RPL-T07 | same seed/fixture/recipeで2Run→比較可能、異digestを同一cohortとして集計しない |
| DEV-RPL-T08 | head SHA・依存Lock・GoalRevision変更→以前のPASSで最新検証を省略しない |
| DEV-RPL-T09 | cancel/lease expiry/予算超→新しいtest attempt 0、Partial/Blocked |
| DEV-RPL-T10 | 追加した再現テストが修正前にFAILしなかった→「修正証明済み」不可 |
| DEV-RPL-T11 | 外部支払い・メール・破壊的fixtureに対する自動再現→外部副作用0 |
| DEV-RPL-T12 | Worker killとrequeue→同一Attempt二重集計0、利用量・Evidence保持 |

### 7. 段階的導入

Slice A: 既存テスト/CIログとReproCase Manifest、before-after oracle（Linux・Web/CLI）。
Slice B: bisect/最小化/Flaky分類、Windows/Android等はrunner提供能力に応じて段階対応。
Slice C: GitHub PR Guardianの自動選択とCode Intelligence連携、root-cause evidenceを3 Reviewerへ転送。

合格の正本はCHAT_11であり、本稿は`DEV-RPL`の契約提案。`PROVEN`は実行証拠が揃った場合のみ。
