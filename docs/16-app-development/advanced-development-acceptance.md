# DEV-20 — Cloud Coding Capabilities 統合受入・比較調査・導入計画

> 2026-11-09。**Draft / PROVISIONAL / Designed only / NOT RUN**。DEV-11〜19の要求/テスト/シナリオと横断正本の責務整理。これは仕様追加であり、実装・テスト・GitHub webhook/Token/実Git push・Benchmarkを実行した証拠ではない。

## 1. 重複回避と導入判断

[既存DEV-01〜10](README.md)にはWorkspace/Git、8AI DAG、Browser Debugger、3 Reviewer、Expert Advisor、Feature Factory、Preview、Endless Runが存在する。新機能の採用判定は**その上で何を追加するか**を明確にする。

| ID | 既存との差分 | 優先 | 実現価値 | 失敗時の扱い |
|---|---|---|---|---|
| DEV-11 [PR Guardian](pr-guardian.md) | PR作成後のopt-in継続監視と元PRへの修復 | P0 | 実用性・保守自動化 | CI/PR state不明→WAITING/BLOCKED |
| DEV-12 [Reproduction Lab](reproduction-lab.md) | CLI/CI/Browser再現、flaky、bisect | P0 | 原因不明修正の削減 | 再現不能→INCONCLUSIVE |
| DEV-13 [Code Intelligence](code-intelligence-and-adaptive-testing.md) | SHA pinシンボル/参照/影響グラフ | P0 | 大規模repoの精度・context削減 | incomplete→partial/text fallback |
| DEV-14 [Adaptive Testing](code-intelligence-and-adaptive-testing.md) | 開発途中の影響テスト選択 | P1 | テスト費用短縮 | graph不足→mandatory full/Blocked |
| DEV-15 [Patch Tournament](advanced-software-factory.md) | 2〜3本の実パッチを同じoracleで比較 | P1 | 難問の修復率 | 全候補FAIL→no auto choice |
| DEV-16 [Cross-Repo](advanced-software-factory.md) | repo別PR＋互換性検証とpartial delivery | P1 | フロント/バック等の同時開発 | partial PR状態のまま通知 |
| DEV-17 [Review Preference](advanced-software-factory.md) | repo固有の承認済み学習 | P2 | 繰返しレビュー削減 | 未承認→suggestionのみ |
| DEV-18 [Environment Templates](advanced-software-factory.md) | immutable sanitized env再利用 | P2 | 起動速度/環境再現 | trust不明→fresh/blocked |
| DEV-19 [Perf Guard](advanced-software-factory.md) | 同条件ベースライン/差分の統計 | P1 | 性能退行抑止 | 測定不十分→INCONCLUSIVE |

P0/P1/P2は**導入優先度**。P0/P1コード欠陥severityとは別軸。

## 2. 受入シナリオ（SC-32〜40：提案 / CHAT_11 approval待ち）

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

## 3. Requirement → Test → Gate（proposed）

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

## 4. 横断不変条件（全機能共通）

1. **権限**: tenant/owner/repoBindingは信頼済みサーバー認証からのみ解決。認可されていないRepo/branch/secret/CIへのアクセスを許さない。Issue/PR/logはすべて非信頼入力。
2. **Execution**: Trusted Control VMとUntrusted Execution VMのゲストカーネル隔離、独立Browser Worker、唯一のBroker RPC、Sentinel/Tool Gateway/Enforced Transportを保持。自前のhost Shell/SSH/Docker/CDP bypassを追加しない。
3. **状態**: Task/Run/Execution/Watch/PR監視/RepoSetは別のaggregate。state changeは現在の`recordVersion, goalRevision, leaseEpoch, policyEpoch`など適切なCAS、Outboxと整合性を確保。
4. **外部Effect**: push, PR create/comment/update, deployment, workflow, provider rerun等は操作ごとのtyped Intent+Approval+Ledgerが正本。timeout/unknown後の二重副作用を禁止。複数Repoの全体成功を原子的と偽らない。
5. **証拠**: Run/test/evaluationにはimmutable source sha、toolchain/runtime/fixture versions、test recipe、artifact provenanceを結びつける。旧HEAD・未実行のPASSやModelの自称成功を承認不可。
6. **Budget**: 全child/candidate/retry/webhook follow-up/index/build/bisect/perf testは親scopeから有限事前予約。費用不明を0として精算しない。
7. **安全**: Cache poisoning、Prompt injection、secret leak、SSRF、tenant越境、malicious postinstall/testを負例として必須検証。セキュリティ品質を速度・PR成功率と相殺しない。
8. **User control**: 監視・自動修復・並列候補・review learning・リリースは明確な個別設定。PRを監視する同意はmerge/本番deploy/secret可視化の同意ではない。

## 5. 故障・競合・継続回復マトリクス

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

## 6. 実装Slice・前提の検査順序

| Slice | 能力 | 解禁条件 |
|---|---|---|
| 0 | 現行DEV-01..10の正本整合、Runtime Cell/Tool Gateway/Artifact/Providerの土台 | CHAT_01/02/03/05/09/10/11によるOwner design sign-off。現時点未達 |
| A | PR Guardianのread-only monitor、Code Intelligenceのtext index、Reproのtest manifest | 同期/tenant/RBAC/immutable SHAの負例がPASS |
| B | PR自動修復（同じPR限定）、Repro before-after、精密symbol index | External effect UNKNOWN/cancel/recovery/secret negative testがPASS |
| C | Adaptive FAST_FEEDBACK、Cross-Repo read-only compatibility matrix、Perf測定 | full mandatory testsが確保、EnvのtrustとRepoSet ACL判定 |
| D | Patch Tournament、Cross-Repo staged PRs、Review Learning、Env reuse | 3AI integration review + P0/P1 gate、プロジェクト別opt-in |

各Sliceは実装リポジトリで小さく検証し、取得した実測のある成果のみ「Implemented/Verified」とする。既存仕様書の文書CIがPASSしてもSandbox VM/DB/browser/セキュリティ認証がPASSしたことにはならない。

## 7. 評価指標と比較実験設計

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

## 8. 出典と研究上の限界

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