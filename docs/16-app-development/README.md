# Autonomous Software Development — 仕様索引
> Status: **Draft / PROVISIONAL**, 2026-11-07。開発機能は既存の汎用Agent Harnessの**任意の利用形態**。設計のみ、実装・実機試験・セキュリティ検証は未実施。以下の型/状態/API/DDLは共同契約所有者のレビュー後にLockedとする。

## 目的・境界
自然言語からWeb/API/Android/desktop/iOSアプリの設計・実装・デバッグ・テスト・レビュー・PR/デプロイまで扱う。既存の検索、旅行、比較、イベント監視を削除・置換しない。**初期提供優先度: Webと既存GitHubリポジトリ改修 → Android/desktop → iOS（macOS runner必須）**。全工程を一つのモデル会話で保有せず、永続Project/Campaignと有限Task/Run/Execution/Artifactへ分割する。

## 仕様一覧
| ID | 正本案（新規） | 目的 |
|---|---|---|
| DEV-01 | [project-workspace-and-git.md](project-workspace-and-git.md) | Project/Workspace/Git/コミット競合 |
| DEV-02 | [multi-agent-development.md](multi-agent-development.md) | 3案→1統合→1実装→3独立レビュー |
| DEV-03 | [visual-design-and-demo.md](visual-design-and-demo.md) | UI見た目選択・動くDEMO・ユーザー判断 |
| DEV-04 | [expert-advisor.md](expert-advisor.md) | 難所で高性能モデルに相談 |
| DEV-05 | [build-preview-release.md](build-preview-release.md) | Toolchain/Preview/GitHub/ビルド/公開 |
| DEV-06 | [autonomous-browser-debugger.md](autonomous-browser-debugger.md) | Browser Workerによる自律デバッグ |
| DEV-07 | [review-and-repair-gates.md](review-and-repair-gates.md) | P0/P1修正ループ・独立検証 |
| DEV-08 | [feature-factory.md](feature-factory.md) | デモ承認を伴う機能追加の継続 |
| DEV-09 | [development-api-and-ui.md](development-api-and-ui.md) | 実行画面、typed read DTO、API/イベント |
| DEV-10 | [acceptance-and-rollout.md](acceptance-and-rollout.md) | REQ/TEST/SC/失敗時復旧/導入順序 |
| CAM-01 | [../07-automation/endless-run-campaign.md](../07-automation/endless-run-campaign.md) | 開発以外にも使えるEndless Run |

## 正本所有権（新領域は既存のDomainを再定義しない）
- CHAT_01: ID, aggregate, state, typed event, Artifact; CHAT_02: Task/Run, planner, subagent, checkpoint, model routing, budget; CHAT_03: tool intent, approval, ledger; CHAT_04: Browser Worker/Session/Action; CHAT_05: Git/CI/Package/Deployment provider adapter; CHAT_07: campaign wake/continuation; CHAT_08: UI/REST/SSE; CHAT_09: identity, isolation, secret, Sentinel; CHAT_10: DB physical DDL/queue; CHAT_11: QA and release gates。
- 本章の `Project`, `DevelopmentCycle`, `DesignDecision`, `DemoBuild`, `ReviewFinding`, `Campaign` は**論理契約案**。Task/Run/Approvalに同名の別正本を作らず、ownerとtenantを必ずサーバー認証から解決する。各資源はsource/decision/approval/commitのimmutable lineageを保持する。
- 全てのゲスト実行は独立 Untrusted Execution VM 内の Runtime Cell。Trusted Control VMにはAgent生成コードを置かず、host Shell/SSH/Docker socket/CDPへのアクセスを許さない。外部操作はGateway→Sentinel→Enforced Transportと個別ledgerを通す。[Sandbox Exec](../02-runtime/sandbox-execution.md)、[Runtime Cell](../09-security/runtime-cell-isolation.md)、[Sentinel](../09-security/sentinel-authority.md)を優先する。
- 開発者用Previewと外部一般Browser操作を混同しない。認可された対象プロジェクトの実行サーバーだけへ明示的に接続し、任意LAN/管理ホスト宛て接続の抜け道を作らない。
- [PR #71](https://github.com/choko510/aiagent-md/pull/71) のBrowser Live Observationは進行中の**別PR**。利用可能ならObserve-only UIとして連携するが、そのPRをマージ済みと仮定せず、CDPや実映像の公開を前提条件にしない。

## 共通の完了定義
Modelが「成功」と発言しただけで成功にしない。Goalに結び付いたコードのdiff、Git commit SHA、依存/ビルド環境digest、実行したテスト、ブラウザtrace、独立レビュー3件、P0/P1未解決0、ユーザー合意したDemo版と差分をVerifierで照合。未実行・不確定はPartial/Blockedと明示。機能追加数は**承認済み・実装・必須試験・レビューを通過した**distinct feature IDのみ数える。

## 実装の最短縦切り
1. Project/Workspaceとgit commit CAS/Artifact復旧。
2. 3→1→1→3 Task DAG、run fencing、予算予約、独立レビュー。
3. UI選択と承認済みデモ、Preview Broker、ブラウザE2E。
4. P0/P1修正ループ・Expert Advisor・Git/PR。
5. Campaign/Feature Factoryを複数有限サイクルで継続、Watch/Monitor/通知と連携。

本章は仕様の原案であり、既存88モジュールの本文がLocked化されたという意味ではない。