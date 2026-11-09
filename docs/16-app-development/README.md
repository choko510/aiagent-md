# Autonomous Software Development — 仕様索引
> Status: **Draft / PROVISIONAL**, 2026-11-07。開発機能は既存の汎用Agent Harnessの**任意の利用形態**。設計のみ、実装・実機試験・セキュリティ検証は未実施。以下の型/状態/API/DDLは共同契約所有者のレビュー後にLockedとする。

## 目的・境界
自然言語からWeb/API/Android/desktop/iOSアプリの設計・実装・デバッグ・テスト・レビュー・PR/デプロイまで扱う。既存の検索、旅行、比較、イベント監視を削除・置換しない。**初期提供優先度: Webと既存GitHubリポジトリ改修 → Android/desktop → iOS（macOS runner必須）**。全工程を一つのモデル会話で保有せず、永続Project/Campaignと有限Task/Run/Execution/Artifactへ分割する。

## 仕様一覧
| ID | 正本案（新規） | 目的 |
|---|---|---|
| DEV-01 | [development-orchestration.md#dev-01](development-orchestration.md#dev-01) | Project/Workspace/Git/コミット競合 |
| DEV-02 | [development-orchestration.md#dev-02](development-orchestration.md#dev-02) | 3案→1統合→1実装→3独立レビュー |
| DEV-03 | [visual-design-and-demo.md](visual-design-and-demo.md) | UI見た目選択・動くDEMO・ユーザー判断 |
| DEV-04 | [development-orchestration.md#dev-04](development-orchestration.md#dev-04) | 難所で高性能モデルに相談 |
| DEV-05 | [build-preview-release.md](build-preview-release.md) | Toolchain/Preview/GitHub/ビルド/公開 |
| DEV-06 | [debugging-and-verification.md#dev-06](debugging-and-verification.md#dev-06) | Browser Workerによる自律デバッグ |
| DEV-07 | [debugging-and-verification.md#dev-07](debugging-and-verification.md#dev-07) | P0/P1修正ループ・独立検証 |
| DEV-08 | [feature-factory.md](feature-factory.md) | デモ承認を伴う機能追加の継続 |
| DEV-09 | [development-api-and-ui.md](development-api-and-ui.md) | 実行画面、typed read DTO、API/イベント |
| DEV-10 | [acceptance-and-rollout.md](acceptance-and-rollout.md) | REQ/TEST/SC/失敗時復旧/導入順序 |
| DEV-11 | [pr-guardian.md](pr-guardian.md) | 既存PRを対象としたCI/レビュー/競合の継続監視・修復 |
| DEV-12 | [debugging-and-verification.md#dev-12](debugging-and-verification.md#dev-12) | 再現・bisect・flaky分類・修正前後の証明 |
| DEV-13 | [code-intelligence-and-adaptive-testing.md](code-intelligence-and-adaptive-testing.md) | symbol index/依存グラフ/文脈選択 |
| DEV-14 | [code-intelligence-and-adaptive-testing.md](code-intelligence-and-adaptive-testing.md) | 影響テストと必須テストの別Gate |
| DEV-15 | [advanced-software-factory.md](advanced-software-factory.md) | 複数修正候補の隔離実装・証拠比較 |
| DEV-16 | [advanced-software-factory.md](advanced-software-factory.md) | 複数repo間の互換性検証・部分配達 |
| DEV-17 | [advanced-software-factory.md](advanced-software-factory.md) | 根拠あるrepo固有のレビュー傾向学習 |
| DEV-18 | [advanced-software-factory.md](advanced-software-factory.md) | 安全な事前構築環境・immutable template |
| DEV-19 | [advanced-software-factory.md](advanced-software-factory.md) | 変更前後の統計的性能回帰検出 |
| DEV-20 | [acceptance-and-rollout.md#dev-20](acceptance-and-rollout.md#dev-20) | DEV-11〜19の要求・SC-32〜40・安全Gateと導入順 |
| CAM-01 | [../07-automation/endless-run-campaign.md](../07-automation/endless-run-campaign.md) | 開発以外にも使えるEndless Run |

## 正本所有権（新領域は既存のDomainを再定義しない）
- CHAT_01: ID, aggregate, state, typed event, Artifact; CHAT_02: Task/Run, planner, subagent, checkpoint, model routing, budget; CHAT_03: tool intent, approval, ledger; CHAT_04: Browser Worker/Session/Action; CHAT_05: Git/CI/Package/Deployment provider adapter; CHAT_07: campaign wake/continuation; CHAT_08: UI/REST/SSE; CHAT_09: identity, isolation, secret, Sentinel; CHAT_10: DB physical DDL/queue; CHAT_11: QA and release gates。
- 本章の `Project`, `DevelopmentCycle`, `DesignDecision`, `DemoBuild`, `ReviewFinding`, `Campaign` は**論理契約案**。Task/Run/Approvalに同名の別正本を作らず、ownerとtenantを必ずサーバー認証から解決する。各資源はsource/decision/approval/commitのimmutable lineageを保持する。
- 全てのゲスト実行は独立 Untrusted Execution VM 内の Runtime Cell。Trusted Control VMにはAgent生成コードを置かず、host Shell/SSH/Docker socket/CDPへのアクセスを許さない。外部操作はGateway→Sentinel→Enforced Transportと個別ledgerを通す。[Sandbox Exec](../02-runtime/sandbox-execution.md)、[Runtime Cell](../09-security/runtime-cell-isolation.md)、[Sentinel](../09-security/sentinel-authority.md)を優先する。
- 開発者用Previewと外部一般Browser操作を混同しない。認可された対象プロジェクトの実行サーバーだけへ明示的に接続し、任意LAN/管理ホスト宛て接続の抜け道を作らない。
- [PR #71](https://github.com/choko510/aiagent-md/pull/71) のBrowser Live Observationは**mainへマージ済みのDraft仕様**。Observe-only UIと連携するが、実装・検証済みの映像配信、CDPや操作権限の公開を意味しない。

## 共通の完了定義
Modelが「成功」と発言しただけで成功にしない。Goalに結び付いたコードのdiff、Git commit SHA、依存/ビルド環境digest、実行したテスト、ブラウザtrace、独立レビュー3件、P0/P1未解決0、ユーザー合意したDemo版と差分をVerifierで照合。未実行・不確定はPartial/Blockedと明示。機能追加数は**承認済み・実装・必須試験・レビューを通過した**distinct feature IDのみ数える。

## 実装の最短縦切り
1. Project/Workspaceとgit commit CAS/Artifact復旧。
2. 3→1→1→3 Task DAG、run fencing、予算予約、独立レビュー。
3. UI選択と承認済みデモ、Preview Broker、ブラウザE2E。
4. P0/P1修正ループ・Expert Advisor・Git/PR。
5. Campaign/Feature Factoryを複数有限サイクルで継続、Watch/Monitor/通知と連携。

本章は仕様の原案であり、既存88モジュールの本文がLocked化されたという意味ではない。

## PR #73 P0/P1 audit disposition (2026-11-08, Draft)
Review source: user-provided audit of HEAD `d54a3b8`. The P0/P1 issues are resolved as **specification proposals** only; they are NOT production verified. Implementation Owner sign-off remains pending. This chapter's ambiguity resolutions:
- P0-01: [Preview Capability](build-preview-release.md) binds trusted process, tenant/project/execution/port/origin/browser lease and revokes active sockets.
- P1-01/02: [Demo mode and approvals](visual-design-and-demo.md) distinguish observe, synthetic interact, human takeover, and explicit/bounded user acceptance.
- P1-03/04/10: [Feature Factory](feature-factory.md) and [Review Gates](debugging-and-verification.md#dev-07) require current integration HEAD and stable logicalWorkItem identity.
- P1-05: [Campaign](../07-automation/endless-run-campaign.md) specifies Stop/Quota/CAS fences and receipts.
- P1-06/07/08/09: DB/Auth/Event/QA and separate release gates are extended in their respective owner specifications and CI. The full app requirements are not implied by the initial 88-module static audit.
- PR #71（Live Observation）と #72（Secure Runtime再監査）はmainへマージ済み。両者の契約を維持して本章を統合し、ProvenanceとCIを更新する。Owner合意・実機検証は依然として未完了。

## 2026-11-09 Cloud Coding能力追加（Draft / NOT RUN）

[DEV-11〜19の9能力とDEV-20の受入マトリクス](acceptance-and-rollout.md#dev-20)を追加。**当初5つのMarkdownで追加された計10個の設計エントリ**を説明する。既存88基礎正本および2026-11-07の開発拡張11件の歴史的カウントは変更せず、合同owner契約の承認後にインデックス計数を正式移行する。各Proposalは新しいShared Task/Run/Tool/DBの正本ではない。PR #74との対象変更を避け、GitHub/CI/Context性能の横断変更は後続の所有者レビューとする。

## 2026-11-09 Python-first: 自律開発OSSとの統合方針（STEP 1、分類C / NOT RUN）

8AI開発チーム、Expert Advisor、Visual Design Picker、Autonomous Browser Debugger、Endless Runの仕様・Test IDは不変。Python-first（[ADR-036](../00-foundation/architecture-decisions.md)）で考慮するOSS:

| OSS | 想定用途 | 配置・制約 |
|---|---|---|
| OpenHands SDK（`openhands-sdk 1.60.2`、MIT、requires-python >=3.12） | Builder/Repair担当のCoder Agent候補 | **Untrusted Execution VMのCell内のみ**。LLM呼出しはscoped broker RPC→Model Gateway、Git push/PR等はTool Gateway Intent。Trusted VMで動かさない |
| PydanticAI / DeepAgents Python | Coordinatorの推論部（ADR-038の選定に従う） | Trusted Orchestrator内。生成コードの実行はしない |
| Playwright Python | Autonomous Browser Debugger、E2E | 独立Browser Worker（Preview Broker経由） |
| pytest / Ruff / mypy・pyright / Semgrep（LGPL-2.1-or-later） | 生成コードの検査・Review gate | Cell内で実行、結果はArtifact/Receipt。Semgrepのライセンスと規則セットの配布条件は実装時確認 |
| Dagger | 再現可能build（任意） | Cell内daemon/Engine権限の扱いをCHAT_09が審査するまで不採用 |

Agentが生成したコードは、言語がPythonであってもBackend本体と同じプロセス・権限・venv・FSで実行しない（[Architecture Rules §E-4](../00-foundation/architecture-rules.md)）。各OSSの統合方式（Cell内実行の可否、ネットワーク要件、Model Gateway経由化）はSTEP 2で検証する。
