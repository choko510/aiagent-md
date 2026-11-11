# Universal Personal Agent Harness — 実装向け仕様書

Ubuntu / Docker 上で動作する **セルフホスト型の汎用パーソナルAIエージェント**の設計資料です。自然言語からWeb調査、比較、ブラウザ操作、外部API、定期監視、通知などを安全に実行することを目標とします。特定の商品・SNS・旅行サービス専用でも、コーディングエージェント専用でもありません。

> **現在の状態（2026-11-05）:** 88件の詳細仕様MDはリポジトリに存在しますが、実装コードはまだありません。各仕様は `Draft / Provisional` で、契約の正式承認（Locked）や実機検証（Verified）を受けたことは意味しません。記載の外部API・OSSは実装時点で再検証してください。

## 実装開始時に読むもの

1. [AGENTS.md](AGENTS.md) — 実装担当AI向けの作業原則・禁止事項
2. [Project Brief](docs/00-foundation/project-brief.md) と [Product Requirements](docs/00-foundation/product-requirements.md) — 作るもの、作らないもの、合格条件
3. [System Architecture](docs/00-foundation/system-architecture.md) と [Architecture Rules](docs/00-foundation/architecture-rules.md) — コンポーネント境界・正本
4. [Acceptance Scenarios](docs/00-foundation/acceptance-scenarios.md) — ユーザーが求める動作
5. [Architecture Decisions](docs/00-foundation/architecture-decisions.md) — 採用案・保留案・判断根拠
6. [88仕様の索引](docs/00-foundation/spec-index.md) — 実装するモジュールの詳細仕様へのリンク
7. [未承認の共有契約 CL-01〜12](governance/round6/contract-lock-candidate.md) — 整合・承認を要する重要事項

各機能を実装するときは、索引からその機能の**正本MDと依存MD**（特に `01-contracts`、`03-tools`、`09-security`、`10-infrastructure`）を読んでください。88件を一括で読み込む必要はありませんが、関連する契約は省略しないでください。

## 開発言語の方針：Python-first（2026-11-09 確定、ADR-036）

| 言語 | 位置付け | 担当 |
|---|---|---|
| Python | 主開発言語 | FastAPI Backend、Agent Runtime、Model/Tool Gateway、公式MCP Python SDK、Playwright Python Browser Worker、Memory、Automation、自律コード開発 |
| TypeScript | UIの主開発言語 | React Web管理画面・ブラウザ側Client（API型はOpenAPI/JSON Schemaから生成） |
| SQL | データ永続化 | PostgreSQL DDL・Transaction・Migration（正本） |
| Rust | 任意 | 実測で必要と判断された局所最適化のみ。必須依存にしない |

Agent Runtime本体のOSSは**STEP 2の実測でDeepAgents Python＋LangGraphを選定**（ADR-038、NOT LOCKED）。Queueは自作PostgreSQL QueuePortを優先候補（ADR-039 PROVISIONAL）とし、適合試験は`conformance/agent_runtime/`。言語変更でTrusted/Untrusted VM、Sentinel、Tool Gateway、Credential Broker、Browser Worker、Trusted Egressの境界は変わりません。配置は [System Architecture §13.2](docs/00-foundation/system-architecture.md)、拘束規則は [Architecture Rules §E](docs/00-foundation/architecture-rules.md)、判断と移行監査台帳は [ADR-036〜040](docs/00-foundation/architecture-decisions.md)。既存仕様中の `ts` コードブロックは言語非依存の論理型記法です（実装はPydantic / 生成TS型）。

### 日本語文章の標準推敲（yomiyasu）

Agent Runtimeでは、日本語の説明文・技術文書・PR説明・メールなどの作成や推敲時に、[nanaism/yomiyasu](https://github.com/nanaism/yomiyasu) のSkillを**デフォルトで参照**します。ユーザーの明示的な不使用指定を優先し、コード・JSON・コマンド・引用文・短い通常会話を機械的に変更しません。数値、否定、条件、主体、断定度、根拠は保持します。

通常のソースチェックアウトには[簡易ガイド](backend/packages/domain/src/aiagent_domain/skills/yomiyasu/SKILL.md)を同梱し、**Dockerビルド時**に [固定コミット](https://github.com/nanaism/yomiyasu/commit/b9a69a3791dd7d3c063ed963bc4a408e515f6288) （v3.0.1）の公式Skill全体をダウンロードし、各ファイルのGit blob SHAを検証して置き換えます（[導入・更新用スクリプト](backend/tools/sync_yomiyasu.py)）。ビルド後のRunはネットワークからSkillを取得せず、読み取り専用で参照します。公式SkillはMITライセンスです。

**現時点の境界:** 標準適用とは*モデルの推敲指示とSkill参照*を意味します。出力後に独立したLLM検査パスを強制するものではなく、Skill付属のPythonチェックはTrusted Runtime内で自動実行しません（実行したと虚偽表示しません）。将来スクリプト検査を有効化する場合は、既存のTool Gateway承認と隔離Cellを経由する必要があります。

## 仕様のまとまり

| 領域 | 主な資料 |
|---|---|
| 基礎・契約 | [00-foundation](docs/00-foundation/README.md) / [01-contracts](docs/01-contracts/core-domain-models.md) |
| Agent Runtime / Tool Gateway | [02-runtime](docs/02-runtime/agent-loop.md) / [03-tools](docs/03-tools/tool-execution-gateway.md) |
| Web・連携・Memory | [04-web-browser](docs/04-web-browser/browser-session-manager.md) / [05-connectors](docs/05-connectors/connector-framework.md) / [06-memory-skills](docs/06-memory-skills/memory-data-model.md) |
| 監視・UI | [07-automation](docs/07-automation/watch-lifecycle.md) / [08-interfaces](docs/08-interfaces/rest-api.md) |
| 安全・運用・QA | [09-security](docs/09-security/sandbox-and-capabilities.md) / [10-infrastructure](docs/10-infrastructure/database-schema-and-migrations.md) / [11-qa](docs/11-qa/end-to-end-acceptance.md) |
| 拡張機能 | [12-intelligence](docs/12-intelligence/opportunity-radar.md) / [13-event-ingestion](docs/13-event-ingestion/event-driven-agent.md) / [14-adaptive-experience](docs/14-adaptive-experience/feedback-learning.md) / [15-multimodal](docs/15-multimodal/multimodal-ingestion.md) |

## 機能別ロードマップ

- [DeckCraft MCPスライド生成の調査・統合ロードマップ](docs/03-tools/deckcraft-mcp-roadmap.md) — DeckCraftの実装・ライセンス・既知リスク、隔離Worker/MCP接続、Phase 0〜5の実装順、受入テストをまとめたDraft計画（未実装）。

## 実装の進め方（提案）

1. **共有契約の整合と判断:** 未承認のCL項目と `PROVISIONAL` な型・イベント・DDLの矛盾を解消し、採用決定をADRへ記録する。
2. **基盤:** DBマイグレーション、認証・認可、Tenant分離、秘密情報保護、Tool Gateway/Policy/Egress を先に構築する。
3. **最小Agent実行経路:** Task/Run永続化 → モデル呼出し → 承認済みツール実行 → Evidence保存 → 検証可能な結果応答をつなぐ。
4. **障害耐性:** checkpoint、lease、冪等性、再試行、承認待ち、復旧、監視/通知を追加する。
5. **機能拡張:** ブラウザ・外部Provider・Memory・UIと、イベント駆動/自律発掘/マルチモーダルを段階的に統合する。
6. **合格判定:** [QA仕様](docs/11-qa/benchmark-and-release-gates.md) とSC/REQ/TESTを使い、実行済み試験と未実施試験を区別する。

この順序は実装計画案であり、未承認の仕様を無条件にLockedとするものではありません。各PRは、小さく動作確認できる単位に分け、失敗・安全性・互換性まで検証してください。

## Muse型 Secure Runtime（2026-11-07 設計改訂）

従来の「登録済みToolだけ」から、****Trusted Control VMとは別カーネルのUntrusted Execution VM**内のRuntime CellでShell・Python・Node.js・CLI・自己生成コードを実行できる汎用Agent**へ拡張する設計です。ホストUbuntu、VM管理プレーン、Vault、Sentinel、Browser CDP、保護サービスへのアクセスは引き続き禁止します。Agentには自由なローカル計算を与え、**あらゆる外部操作は独立Sentinelが最終認可し、Trusted Egress Transportだけが実送信**（生成コードの直接ネットワーク接続は禁止）します。Credential Brokerが秘密を代理使用し、Agentは実トークンを閲覧しません。実装/実証済みではなく、共有契約は Draft / Provisional のままです。

- [Runtime Cell Isolation](docs/09-security/runtime-cell-isolation.md) — VM/Cell分離、リソース制限、寿命、起動可否
- [Sentinel Authority](docs/09-security/sentinel-authority.md) — すべての具体的外部操作と通信出口の最終許可
- [Sandbox Execution](docs/02-runtime/sandbox-execution.md) — sandbox.exec、Execution/Workspace、停止、復旧
- [受入シナリオ SC-17〜22](docs/00-foundation/acceptance-scenarios.md) — 生成コード実行からVM/通信隔離まで

本番候補はKVM/QEMUによる**Trusted Control VMとUntrusted Execution VMの2ゲスト構成**（Agent LoopはTrusted側、生成コードはUntrusted側、Browser Workerは独立隔離）、Dockerはサービス管理と補助隔離に使用します。ハードウェア仮想化なしの開発用プロファイルは本番と同等ではありません。Sentinel障害・隔離不成立時に安全性を下げるフォールバックはありません。

## 安全性の基本原則

Agentに**Ubuntuホストの**任意Shell/SSH/Dockerソケットや権限昇格を与えない。支払い、予約確定、投稿、外部送信などの不可逆な副作用は明示承認を必須にする。CookieやAPIキーをLLM文脈へ露出させず、Web上の文言を高優先度の命令として扱わない。実装がない/試験を実行していない段階で成功を宣言しない。

## 調査資料の整理について

並列仕様執筆時に使った担当別プロンプトや作成テンプレートは整理済みです。旧プロンプトの一部は既存仕様からの相対参照を維持するため、`prompts/ROUND2_AUTHOR_CONTINUE.md` のみ**履歴用資料として残置**しています。実装手順ではありません。削除したファイルもGit履歴から確認できます。元の61件の仕様PRとのSHA対応や未承認事項を確認する `audit/`、`governance/`、`.premerge/pinned-prs.json` は保持しています。

## コンテキスト管理の実装判断（2026-11-07追記）

LLMへ毎回全履歴を渡さず、Policy/現行Goal/実行状態を型付き固定領域とし、Evidence原本のArtifact offload、関連度・鮮度で選択したWorking/Memory投影、根拠付きSummary、Checkpoint復旧/Model変更時の再構築を組み合わせる。[詳細: Context Manager 18節](docs/02-runtime/context-manager-and-compaction.md) / [ADR-023〜028](docs/00-foundation/architecture-decisions.md) / [QA ACM-T01〜20](docs/11-qa/benchmark-and-release-gates.md)。

Muse型Runtimeで発生するScript/stdout/外部Web/Memoryは非信頼Data。Context圧縮を経てもSentinel/Gateway、認可、秘密情報の境界を緩めない。2026-11-09にはDeepAgents JS・Mastra Observational Memory・OpenClawから採用する圧縮・監査・復旧の契約を[Context 19節](docs/02-runtime/context-manager-and-compaction.md)へ追加した（Draft）。原本保存失敗や品質不合格なら旧HEADを維持する。設計の追加であり、本体コードやRuntime実機テストを完了したことにはならない。既存88仕様MDの件数は維持。

## サーバーブラウザのライブ閲覧（追加設計、未実装）

[Browser Live Observation](docs/08-interfaces/browser-live-observation.md) は、Agentがサーバー上で操作するBrowser画面をWeb管理画面から閲覧するための補足仕様です。見るだけのObserveはAgentを停止せず、入力操作は従来のHuman Takeoverへ明示的に切り替えます。認証/セッション隔離、機密画面の全面遮断、映像非保存、WebRTCまたは認証済みWebSocket、SC-23/テスト案を含みます。既存88件の正本モジュール数は変えません。現在はDraftで本体コード・映像配信はありません。

## 自律アプリ開発・Endless Run（2026-11-07 新規仕様案）

本Harnessは汎用機能を維持したまま、**自然言語からのソフトウェア設計・実装・デバッグ・レビュー・GitHub PR**を扱う追加開発ワークスペースを提案します。[仕様索引](docs/16-app-development/README.md)。
- **8AI開発チーム**：3独立提案→1統合→1Builder（操作可能なDemo・本実装）→3独立レビュー→P0/P1修正ループ。相談先として限定予算の高性能 **Expert Advisor** を追加。
- **Visual Design Picker**：デザイン名だけでなく同一内容のサムネイル/実Demoを比較。任意の設計判断・Demo採用を経て本実装。未承認のFeatureは実装しない。
- **Autonomous Browser Debugger**：実ブラウザ操作・UI見切れ・Console/Network/Trace/視覚回帰・source相関・修正後再検証。開発Previewはprivate-network禁止を緩めず専用認証Brokerを使用。
- **Endless Run**：[Campaign正本案](docs/07-automation/endless-run-campaign.md)を通じ、改善/全体最適化/UIUX/Feature Factory/任意研究を有限Cycleで繰り返し、停止・目標件数・Quota/費用/安全制約まで継続。新機能の例は承認済み10件の実装。ユーザー判断待ちがあっても独立作業は進む。

[受入試験一覧 SC-24〜31](docs/16-app-development/acceptance-and-rollout.md)と[追加ADR-030〜034](docs/00-foundation/architecture-decisions.md)を参照。**初期88個の既存Draft正本とは別の新規11仕様（DEV-01〜10、CAM-01）**であり、計99件の設計案（別PR #71のBrowser Live Observationは未統合）。本体は未実装、仕様・テスト・脅威モデル・APIはDraft/PROVISIONAL、実行済みのテストはありません。


## Rust製ローカルDevice Client（追加設計・未実装）

[Rust製ローカルDevice Client / Cua Driver統合仕様](docs/16-app-development/local-device-rust-client.md) は、クラウドPython-firstを維持し、ユーザーPCのRust/TauriクライアントからCua MCP、Chrome拡張、Build/ADBを安全に利用するためのDevice Gateway設計です。**Draft / NOT RUN**（ADR-041）。
