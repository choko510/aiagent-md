# DEV-10 — REQ/TEST/SC Matrix, Security Gate & Delivery Phases
> Status Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_00/01/02/03/04/07/08/09/10/11。実装なし、試験は**NOT RUN**。

## 0. 責務と成果物の境界
Goalに対する独立8AI設計→デモ/ユーザー決定→実装→実Browser Debug→3Reviewer→修正→PR/公開を全体E2Eで検証。Endless Runは同一Taskを永続化せず有限Cycle群を復旧・継続する。テストの存在と実行済み検証を区別し、Owner契約がProvisionalな間はLockedと表示しない。

## 1. REQ → TEST → user scenario
| REQ | Priority | Acceptance | Primary tests | Scenario |
|---|---|---|---|---|
| DEV-REQ-01 | MUST | project/workspace/git CASがTask終了後も存続 | DEV-WS-T01..07 | SC-24 |
| DEV-REQ-02 | MUST | 3独立案→1比較統合→1Builder→3独立同SHA review | DEV-TEAM-T01..06 | SC-24 |
| DEV-REQ-03 | MUST | UI見た目で比較、同一content/viewport、選択をversion固定 | DEV-DES-T01..06 | SC-25 |
| DEV-REQ-04 | MUST | 動くDemoのApprove/Reject/Revision、未承認本実装拒否 | DEV-DES-T03..05, DEV-FF-T06 | SC-25 |
| DEV-REQ-05 | SHOULD | 高性能Advisorを有限費用/独立権限で相談 | DEV-EXP-T01..05 | SC-26 |
| DEV-REQ-06 | MUST | 実Browser探索/Console/Network/UI/Trace/E2E → 修正 | DEV-DBG-T01..10 | SC-27 |
| DEV-REQ-07 | MUST | 最新SHAで3レビュー、P0/P1未解決0、必須テストPASS | DEV-REV-T01..06 | SC-28 |
| DEV-REQ-08 | MUST | Build/toolchain/package/previewが隔離、Sentinel権限外0 | DEV-BLD-T01..08 | SC-29 |
| DEV-REQ-09 | MUST | approved limited scopeでGit commit/PR、未知effect再送なし | DEV-WS-T05, DEV-BLD-T06 | SC-29 |
| DEV-REQ-10 | MUST | Endlessly cycle work until stop/target/quota/safety limit | CAM-T01..11 | SC-30 |
| DEV-REQ-11 | MUST | Feature 10件はcurrent-INTEGRATED distinct countのみ | DEV-FF-T01..11 | SC-31 |
| DEV-REQ-12 | MUST | Approvals/ACL/data/egress/runtime isolationを緩めない | DEV-UI-T02..05, DEV-BLD-T04 | SC-29 |
| DEV-REQ-13 | MUST | restart/429/revoke/unknown effect/partialで正しい復帰 | CAM-T02/T05/T06, DEV-TEAM-T03 | SC-30 |

## 2. Cross-owner interfaces that MUST be reconciled before lock
- CHAT_01: Project/Design/Demo/DevCycle/ReviewFinding/Campaignのtyped IDs、events、GoalRevision/WorkspaceRevisionの関連、Artifact lineage、result statuses。
- CHAT_02: parent/child Task budget & DAG join、DesignApproval WAIT、review re-runのTask identity、Context縮小とAdvisor資料転送、Cycle Job fence。
- CHAT_03: Git/Package/Preview/CI/Deployment typed Tool Intent、外部副作用の分類、Design consentとTool Approvalの完全分離、UNKNOWN effect reconciliation。
- CHAT_04: Browser Context/CDP exclusive ownership、debug instrumentation、Preview Session virtual origin、PR#71 Live Observationとは条件付き連携。
- CHAT_05: Git Provider, package registry, CI provider, deploy/remote build runnerのversioned connectorの有無。
- CHAT_07: Campaign Goal/WorkItem/next-cycle scheduling、空tickを起こさない持続Run controller、quota waiting。
- CHAT_08: versioned API/REST/SSE DTO、Human decision, viewers/debug UI、キャンペーンのprogress。
- CHAT_09: 2 VM isolation, Browser domain, Preview Broker/SSRF/credential isolation, supply chain, secret redaction。
- CHAT_10: project/workspace/Campaign/receipt DB migrations, transactional outbox, dedupe, cross-tenant FK, retention/backup。
- CHAT_11: hostile concurrency, Browser SSRF/PII, review SHA correctness, infinite work bounded by hard envelopes。

## 3. P0/P1 ship blockers
1. Reviewer SHAが一致しない or 3件未実施。
2. 上位承認済みDemoから大きく変化しているのに再決定なし。
3. P0/P1がOPEN/UNVERIFIED、あるいは必要fixture/E2Eが未実施。
4. 直 Internet/host/secret/CDPへの攻撃が1件でも成功。
5. 利用上限を越える予約/課金、停止後の新規dispatch、同じFeatureの二重計上。
6. 難所の`Expert Advisor`が認可なくtool変更。
7. git push/release/campaign stopのUNKNOWN効果を成功と断定。

## 4. Release / implementation slices
- Slice A: README/Project/CAS Git worktree/Artifact/ephemeral vs durable workspace、fixture test。
- Slice B: 8 role Task DAGと統合、横断report、P0/P1 review/repair loop。
- Slice C: Design Picker/可動DEMO/Preview BrokerとE2E（機密・SSRF検証を先に）。
- Slice D: Expert Advisor、browser debug Studio、Trace、Visual regression。
- Slice E: Package/toolchain/Android/desktop remote build、Git/CI、署名・デプロイbroker。
- Slice F: Campaign/Feature Factory continuous scheduling + 10-feature bounded demo approvals。
各Sliceは小さなPRで必要所有者が合意した契約から実装し、テストレシートを残す。実測のない費用/応答時間の保証を作らない。

## 5. Acceptance scenarios（独立ドメインの入力/期待/負例）
**SC-24 Multi-Agent Development:** 「Webアプリを設計→作成」→同一Goalから3独立案/A4比較統合、A5が実装、3 Reviewer同SHA、差分・最新テストと保存成果物。負例: 2 proposal、古いSHA review、テストなしの自称完了。
**SC-25 Visual Design/Demo:** 3つの画像/動くDemoからWindows11風等を視覚選択→Revisionと同一content保存→user Approve後だけ実装。負例: ダミーボタンを実APIと偽る、旧Approval受理、Previewがsecretを漏らす。
**SC-26 Expert:** 3回の同型失敗で高性能modelに限定質問→A5が実修正、成功/失敗を検査。負例: Advisor権限昇格、相談し続けて課金無限。
**SC-27 Browser Debug:** webUIがmobileでボタン見切れ、API500発生→Playwright/Browser Workerが再現・Trace・source相関・patch・E2Eを行う。負例: click API success=合格、HAR個人情報漏洩。
**SC-28 Repair Review:** P0/P1混在→新SHAで再試験/3AI reviewer、未解消0でverified。負例: reviewer1件未実行、P0の誤dismiss、旧SHA成功の流用。
**SC-29 Security+Build:** Cell build+Preview+Git PRの一連を検証。SSRF、raw direct TLS、CDP、署名鍵、host shell不可。外部push UNKNOWNは照会。負例: PreviewをLAN CONNECT proxy化。
**SC-30 Endless Campaign:** 「全体最適化をstop/上限まで」→有限Cycleを自動継続、停止、provider 429で安全待ち、復帰し計数/費用不変。負例: 空tick連続LLM、予算リセット、永続child Run。
**SC-31 Feature Factory:** 「デモ承認した10機能を実装」→複数候補、pending demoがあっても別feature進行、distinct verified 10で終了。負例: 10案作っただけ/重複/未レビューを数える。

## 6. Test execution status
本PRはMarkdown仕様だけ。上記SC-24..31、DEV-xx、CAM-Txxは**すべて Designed / NOT RUN**。本体未実装、Providerバージョン/Entitlement/実機隔離/Preview起動/Android/macOS Runnerも未検証。契約のLocked/Verified宣言は禁止。

## 2026-11-08 P0/P1 audit closure matrix / contract readiness

| Finding | Correction now specified | Trace | Runtime owner status |
|---|---|---|---|
| P0-01 | Preview scoped Capability + origin isolation + live stream revoke / process attestation | DEV-BLD-T09..14 / QA-E2E-T29N/F | CHAT_03/04/09/10 review pending |
| P1-01 | `preview:observe` vs mock `demo:interact` vs sensitive takeover | DEV-DES-T07/08/11 / QA-E2E-T25P/N | CHAT_04/08/09 pending |
| P1-02 | AUTO_RECOMMEND never accepts Demo; user decision or bounded mandate | DEV-DES-T09/10, DEV-UI-T07 | CHAT_03/08 pending |
| P1-03 | Integrated current HEAD default count for 10 features | DEV-FF-T07/08, QA-E2E-T31P/N | CHAT_07/10 pending |
| P1-04 | stable logicalWorkItemId, criterion distinct from identity, unique count | DEV-FF-T10, CAM-T17 | CHAT_01/07/10 pending |
| P1-05 | STOP controlVersion per dispatch, QuotaWaitReceipt, resumed generation | CAM-T12..16 / QA-E2E-T30F | CHAT_02/07/10 pending |
| P1-06 | additive candidate physical SQL/FK/CAS/outbox/forward migration | DEV-DB-T01..12 | CHAT_01/09/10 pending |
| P1-07 | explicit Auth registry actions + typed Event kinds | AUTH-DEV-T01..06, EVT-DEV-T01..05 | CHAT_01/08/09 pending |
| P1-08 | SC-24..31 P/N/F fixtures and dedicated CI spec checks | QA-E2E-T24P..T31F | CHAT_11 pending |
| P1-09 | shared security gates vs opt-in Development/Campaign feature gates | GATE-DEV-T01..04 | CHAT_09/11 pending |
| P1-10 | feature SHA vs integration HEAD triple review + revalidation | DEV-REV-T07/08, QA-E2E-T28F | CHAT_02/11 pending |

**New requirements**:
- **DEV-REQ-14** MUST: preview capability isolation, live revoke and malicious script safe origin. DEV-BLD-T09..14 → SC-29.
- **DEV-REQ-15** MUST: Campaign stop/quota epochs and stable logicalWorkItem identity and count. CAM-T12..19 → SC-30.
- **DEV-REQ-16** MUST: integrated HEAD, triple integration review, counting only current eligible receipts. DEV-FF-T07..12 + DEV-REV-T07..08 → SC-28/31.
- **DEV-REQ-17** MUST: conditional feature gates with common security always enforced. GATE-DEV-T01..04 → SC-29/30.
- **DEV-REQ-18** MUST: real authenticated Demo choice and per-mode Preview interactions. DEV-DES-T07..11 + DEV-UI-T07..10 → SC-25.

`DEV-REQ-11` from the earlier table is now interpreted as **distinct current-INTEGRATED feature count by default**, not branch-only VERFIED count; this explicitly supersedes earlier wording. `DEV-REQ-07` additionally binds triple review and required tests to combined integration SHA when target head moves. No independent merge/production deployment is authorized by accepting a Demo.

**Integration with merged PRs #71/#72 (2026-11-08):** PR #71とPR #72はmainへ統合済み。本PRは両者のBrowser/REST/Sentinel/Sandbox/Provenanceの最新契約を保持してmergeし、88正本とCOMP-01のGit blob SHAを実際の統合treeに合わせる。ADR-029/SC-23のLive Observationは維持し、Preview/Live Viewer権限を混同しない。#72のSecure Runtime再監査で追加されたSentinel/Tool承認・フェンシング要件を優先し、信頼境界は緩和しない。

**Verification label:** Static contract checks can be PASS while all VM, Browser, DB, concurrent execution and real feature tests remain **NOT RUN**. This addendum is an implementation plan and testable interface proposal, not a claim of Owner consensus or runtime correctness.

**Regression acceptance extension:** [DEV-DB-T11/T12](../10-infrastructure/database-schema-and-migrations.md), DEV-FF-T12, CAM-T19 require old/current integration HEAD comparison and immutable first-count identity. A previously reached Campaign must not advertise stale completion when current-head eligibility falls. Static schema checks cannot assert runtime transaction correctness.

## 2026-11-09 Advanced Cloud Coding proposal（DEV-11〜20 / NOT RUN）

追加された**9能力**（DEV-11 PR Guardian、DEV-12 Reproduction Lab、DEV-13 Code Intelligence、DEV-14 Adaptive Testing、DEV-15 Parallel Patch Tournament、DEV-16 Cross-Repo、DEV-17 Review Preference、DEV-18 Environment Templates、DEV-19 Performance Guard）の要求DEV-REQ-19..27、SC-32..40、設計テスト、P/N/F、導入順序は [DEV-20](acceptance-and-rollout.md#dev-20) の提案を参照する。既存DEV-REQ-01..18、SC-24..31、必須3AI Review、Preview/Broker/Gateway/Sentinel、Endless Runの安全Gateを変更しない。新シナリオはCHAT_11統合前の提案であり本実装E2E fixtureおよびQA Release Gateでの検証は**すべてNOT RUN**。

---

## DEV-20

**DEV-20 — Cloud Coding Capabilities 統合受入・比較調査・導入計画**

> 2026-11-09。**Draft / PROVISIONAL / Designed only / NOT RUN**。DEV-11〜19の要求/テスト/シナリオと横断正本の責務整理。これは仕様追加であり、実装・テスト・GitHub webhook/Token/実Git push・Benchmarkを実行した証拠ではない。

### 1. 重複回避と導入判断

[既存DEV-01〜10](README.md)にはWorkspace/Git、8AI DAG、Browser Debugger、3 Reviewer、Expert Advisor、Feature Factory、Preview、Endless Runが存在する。新機能の採用判定は**その上で何を追加するか**を明確にする。

| ID | 既存との差分 | 優先 | 実現価値 | 失敗時の扱い |
|---|---|---|---|---|
| DEV-11 [PR Guardian](pr-guardian.md) | PR作成後のopt-in継続監視と元PRへの修復 | P0 | 実用性・保守自動化 | CI/PR state不明→WAITING/BLOCKED |
| DEV-12 [Reproduction Lab](debugging-and-verification.md#dev-12) | CLI/CI/Browser再現、flaky、bisect | P0 | 原因不明修正の削減 | 再現不能→INCONCLUSIVE |
| DEV-13 [Code Intelligence](code-intelligence-and-adaptive-testing.md) | SHA pinシンボル/参照/影響グラフ | P0 | 大規模repoの精度・context削減 | incomplete→partial/text fallback |
| DEV-14 [Adaptive Testing](code-intelligence-and-adaptive-testing.md) | 開発途中の影響テスト選択 | P1 | テスト費用短縮 | graph不足→mandatory full/Blocked |
| DEV-15 [Patch Tournament](advanced-software-factory.md) | 2〜3本の実パッチを同じoracleで比較 | P1 | 難問の修復率 | 全候補FAIL→no auto choice |
| DEV-16 [Cross-Repo](advanced-software-factory.md) | repo別PR＋互換性検証とpartial delivery | P1 | フロント/バック等の同時開発 | partial PR状態のまま通知 |
| DEV-17 [Review Preference](advanced-software-factory.md) | repo固有の承認済み学習 | P2 | 繰返しレビュー削減 | 未承認→suggestionのみ |
| DEV-18 [Environment Templates](advanced-software-factory.md) | immutable sanitized env再利用 | P2 | 起動速度/環境再現 | trust不明→fresh/blocked |
| DEV-19 [Perf Guard](advanced-software-factory.md) | 同条件ベースライン/差分の統計 | P1 | 性能退行抑止 | 測定不十分→INCONCLUSIVE |

P0/P1/P2は**導入優先度**。P0/P1コード欠陥severityとは別軸。

### 2. 受入シナリオ（SC-32〜40：提案 / CHAT_11 approval待ち）

| SC | Given / When | 必須のThen（肯定） | 防止する負例 |
|---|---|---|---|
| SC-32 PR Guardian | PR headAでCI failure、後にheadBへ更新 | 同じPRで最新headBのCI・reviewを確認して修正、曖昧なら待機 | headA PASSを流用、別PR量産、unknown pushを再送 |
| SC-33 Repro Lab | 失敗するCLI/Browser case、既知Good Sha | Before FAIL / After PASS、bisect曖昧はrange提示 | 修正後だけPASSで原因証明、flaky rerun=fix |
| SC-34 Code Intelligence | 複数言語・一部dynamic importを含むrepo | 選別したrefsのsource SHA/coverageとmissing領域を表示 | text hitを全精密Ref扱い、別repo情報混入 |
| SC-35 Adaptive Testing | 小変更とlockfile/schema変更 | 開発中は選別、最終必須検査は元仕様の全Gate | skippedをPASS、cache汚染をtrustedに転用 |
| SC-36 Patch Tournament | 3実装候補、1候補がfastでもsecurity FAIL | 安全性gate後に良案選択し最新統合SHAで再review | 失敗候補を速さで選択、古いreceipt流用 |
| SC-37 Cross-Repo | schema repo と consumer repoが連動、B push失敗 | RepoSetManifest＋partial status、元PR継続 | A成功をall succeeded扱い、他repo勝手にpush |
| SC-38 Review Learning | 過去採用review、botコメント、悪意あるIssue混在 | owner承認後のみstyleに利用、security findings常時表示 | P0 rule解除や他repo漏洩 |
| SC-39 Environment Templates | lock+image+policy pin、untrusted PR | 安全なreuse / 失効時fresh / secretなし | cache poisoning、VM横断token残留 |
| SC-40 Perf Guard | baseline/candidateを異条件・同条件で測定 | 同条件時のみ比較可能、欠測はInconclusive | 外乱を改善扱い、性能でSecurityを相殺 |

### 3. Requirement → Test → Gate（proposed）

| REQ ID | MUSTとtrace | Release Gate |
|---|---|---|
| DEV-REQ-19 | PR tracking 1 logical intent, SHA CAS, status reconciliation → DEV-PRG-T01..12 / SC-32 | Post-PR opt-in gate |
| DEV-REQ-20 | Repro before/after + flaky/bisect honesty → DEV-RPL-T01..12 / SC-33 | Repair evidence gate |
| DEV-REQ-21 | Code index SHA/ACL/coverage exactness → DEV-CIN-T01..07 / SC-34 | Context correctness gate |
| DEV-REQ-22 | Test selection may not weaken required gates → DEV-ATS-T01..10 / SC-35 | Mandatory tests gate |
| DEV-REQ-23 | Candidate comparison security-first & integration SHA → DEV-PAT-T01..06 / SC-36 | Candidate selection gate |
| DEV-REQ-24 | Multi-repo scoped authorizations & partial external effects → DEV-XRP-T01..06 / SC-37 | Multi-Repo delivery gate |
| DEV-REQ-25 | Approved review learning cannot override P0/P1 → DEV-RPLR-T01..05 / SC-38 | Review policy gate |
| DEV-REQ-26 | Safe environment snapshot and cache trust → DEV-ENV-T01..06 / SC-39 | Template release gate |
| DEV-REQ-27 | Valid paired benchmark and evidence-backed thresholds → DEV-PERF-T01..06 / SC-40 | Performance gate |

テストID `DEV-PAT-T01`のようなID表記は親ドキュメントの`DEV-PAT T01`の省略記法と同一とする。新規状態/DTOの受理はCanonical Ownerの実装レビュー後。既存`DEV-REQ-01..18`/`SC-24..31`/`CAM`/`CHAT_09`に対する互換修正は別Owner PRか統合契約レビューで行う。

### 4. 横断不変条件（全機能共通）

1. **権限**: tenant/owner/repoBindingは信頼済みサーバー認証からのみ解決。認可されていないRepo/branch/secret/CIへのアクセスを許さない。Issue/PR/logはすべて非信頼入力。
2. **Execution**: Trusted Control VMとUntrusted Execution VMのゲストカーネル隔離、独立Browser Worker、唯一のBroker RPC、Sentinel/Tool Gateway/Enforced Transportを保持。自前のhost Shell/SSH/Docker/CDP bypassを追加しない。
3. **状態**: Task/Run/Execution/Watch/PR監視/RepoSetは別のaggregate。state changeは現在の`recordVersion, goalRevision, leaseEpoch, policyEpoch`など適切なCAS、Outboxと整合性を確保。
4. **外部Effect**: push, PR create/comment/update, deployment, workflow, provider rerun等は操作ごとのtyped Intent+Approval+Ledgerが正本。timeout/unknown後の二重副作用を禁止。複数Repoの全体成功を原子的と偽らない。
5. **証拠**: Run/test/evaluationにはimmutable source sha、toolchain/runtime/fixture versions、test recipe、artifact provenanceを結びつける。旧HEAD・未実行のPASSやModelの自称成功を承認不可。
6. **Budget**: 全child/candidate/retry/webhook follow-up/index/build/bisect/perf testは親scopeから有限事前予約。費用不明を0として精算しない。
7. **安全**: Cache poisoning、Prompt injection、secret leak、SSRF、tenant越境、malicious postinstall/testを負例として必須検証。セキュリティ品質を速度・PR成功率と相殺しない。
8. **User control**: 監視・自動修復・並列候補・review learning・リリースは明確な個別設定。PRを監視する同意はmerge/本番deploy/secret可視化の同意ではない。

### 5. 故障・競合・継続回復マトリクス

| Race / failure | 判定・復旧 |
|---|---|
| GitHub duplicate/out-of-order webhook | Durable event identity、current PR再照会、Task起動の重複0 |
| Worker kill after a possible push | Side-effect UNKNOWN、remote SHA status reconcile。無根拠再送0 |
| Repo HEAD moves during review | stale review invalidated、最新integration SHAに再審査 |
| Cancel/steer/quota/ACL revoke | Active fences invalidation、これ以降の新規外部effectを0 |
| Candidate branch conflict | 別worktree保全、CAS failure→修正計画を再評価 |
| Multi-repo partial success | PARTIALLY_DELIVERED、残りのTask/PRを監視、全部成功と言わない |
| Index/runner/cache poisoning | manifest invalidation、failed/blocked、trustedへ昇格不可 |
| Flaky check or unreachable sandbox | Inconclusive/Blocked、再現証拠なしの勝手な合格0 |
| Performance noisy sample | INCONCLUSIVE、統計条件を整えるかユーザー判断へ |
| Multiple overlapping feedback loops | repo/pr/workItem/shaの一意keyと親Campaign envelope、無限発火を抑制 |

### 6. 実装Slice・前提の検査順序

| Slice | 能力 | 解禁条件 |
|---|---|---|
| 0 | 現行DEV-01..10の正本整合、Runtime Cell/Tool Gateway/Artifact/Providerの土台 | CHAT_01/02/03/05/09/10/11によるOwner design sign-off。現時点未達 |
| A | PR Guardianのread-only monitor、Code Intelligenceのtext index、Reproのtest manifest | 同期/tenant/RBAC/immutable SHAの負例がPASS |
| B | PR自動修復（同じPR限定）、Repro before-after、精密symbol index | External effect UNKNOWN/cancel/recovery/secret negative testがPASS |
| C | Adaptive FAST_FEEDBACK、Cross-Repo read-only compatibility matrix、Perf測定 | full mandatory testsが確保、EnvのtrustとRepoSet ACL判定 |
| D | Patch Tournament、Cross-Repo staged PRs、Review Learning、Env reuse | 3AI integration review + P0/P1 gate、プロジェクト別opt-in |

各Sliceは実装リポジトリで小さく検証し、取得した実測のある成果のみ「Implemented/Verified」とする。既存仕様書の文書CIがPASSしてもSandbox VM/DB/browser/セキュリティ認証がPASSしたことにはならない。

### 7. 評価指標と比較実験設計

外部サービスのマーケティング数値を当システムの達成値に転用しない。固定Issue/test cohortとbaseline branch、同じmodel/hardware/limitsを使用し、以下をmatched comparisonで評価。

| 指標 | 定義 | 注意 |
|---|---|---|
| Repair verified rate | 最新SHA、必須テストとレビューを通った修復Task / 許可されたTask | partialとblockedを分母から隠さない |
| PR time-to-ready | PR作成→最新head required checks/reviewsが通るまで | pending/closed等を別status |
| Reproduction yield | 再現が旧SHAで観測されたcase / 対象case | flaky/inconclusiveを別集計 |
| Code navigation precision/recall | golden symbol referencesで評価 | Index coverageごとに集計 |
| Test runtime savings | FAST_FEEDBACKだけの時間/費用減 | mandatory full検査の費用は別集計 |
| Patch tournament lift | 1Builderと同条件比較の修正成功率差 | token/CPU/費用やSecurity Gateを併記 |
| Cross-repo integrity | RepoSetManifest単位の全互換テスト成功 | per-repo passだけのものは除外 |
| Env startup latency | cold/warm p50,p95とimage size/cost | trusted reuseに限定 |
| Perf false alarms | 不変scenarioで誤回帰判定した率 | 反復数、閾値、CIを表示 |

### 8. 出典と研究上の限界

2026-11-09、以下の公式/公開資料を閲覧。開発プラットフォームの実装内部や未提供API、Sourcegraph Enterpriseの利用権を保証しない。

- [Open SWE](https://github.com/langchain-ai/open-swe): durable thread、CI監視、review style、isolated sandbox。MITソース参照だがGitHub access scopeと実際のsandbox強度は構成依存。
- [Cursor Cloud Agent](https://prod.cursor.com/docs/cloud-agent)・[Environment Setup](https://prod.cursor.com/docs/cloud-agent/setup): multi-repo、事前構築VM。サービス挙動を自前VMの安全保証としない。
- [VS Code Agent Merge](https://code.visualstudio.com/docs/agents/reference/ai-settings): CI/review/conflict/mergeの別設定。
- [GitHub Webhooks best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)・[Signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries): secure ingest。
- [GitHub Checks](https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-checks): completion status/conclusion semantics。
- [Git bisect](https://git-scm.com/docs/git-bisect/)・[pytest flaky](https://docs.pytest.org/en/latest/explanation/flaky.html): causal regression and nondeterministic test diagnosis。
- [Sourcegraph Precise Code Navigation](https://sourcegraph.com/docs/code-navigation/precise-code-navigation)・[SCIP](https://github.com/scip-code/scip): source index protocol。
- [Nx affected](https://nx.dev/docs/features/ci-features/affected)・[Cache security](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching): test impactと低信頼cache境界。

調査だけでは未確定の事項: GitHub App install scopes / CI provider API limits、SCIP indexer/toolchain相性、同環境の性能データ、サンドボックス実機hardening、DB+outbox実装、GitHub branch protections・Ruleset、ライセンス依存。**実装前にOwner決裁とPoC実測を必須**。
