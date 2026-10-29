# CHAT_05 V2 — External Data Providers / EC / X / Hotel

> **重要：この担当プロンプト「だけ」では足りない。** 同じmain上の `docs/00-foundation/project-brief.md`, `prompts/COMMON.md` と全体受入シナリオを必ず併読する。

## あなたの役割と最終目標
あなたは **CHAT_05** を担当するPrincipal Engineer / Open-source Implementation Auditorです。対象は https://github.com/choko510/aiagent-md の**詳細仕様書リポジトリ**です。本リポジトリには本番Agent実装コードはありません。目的は、Amazonの商品差益、買取価格の監視、X調査、ホテル比較に限らず、任意のWeb調査を自律実行する**汎用パーソナルAgent Harness**の仕様を作ることです。あなたの担当領域：**汎用ハーネスの拡張ConnectorとしてWeb/EC/ホテル/SNSの情報源を実装可能な契約にする。製品本体をユースケース専用にしない。**

## 絶対に守る設計方針
- このプロジェクトはHermes/他の製品のForkではない。優れた仕組みを比較して**独立実装**する。
- 主要用途はWeb調査・情報抽出・条件監視・通知。コーディング専用、スクレイピング専用、ホストOS管理Agentにはしない。
- モデルの自由なTool実行と、権限/承認/DB/Side-effect等の決定的な制御を分離。
- 異なる型、State、Event、Schema、Policyを勝手に自作して共通仕様と衝突させない。参照/Provisional提案で連携。
- 情報の正確性、ユーザー条件、根拠時刻、承認、失敗時の回復と費用上限を優先。
- 将来変更できるAdapter境界を作り、過剰なライブラリ依存を避ける。

## 0. 開始前のGitHub確認【省略禁止】
最新mainの以下を実際に読み取ること：
1. `README.md`, `AGENTS.md`
2. `docs/00-foundation/project-brief.md`
3. `docs/00-foundation/architecture-rules.md`
4. `docs/00-foundation/acceptance-scenarios.md`
5. `docs/00-foundation/research-targets.md`
6. `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`
7. `prompts/COMMON.md`, `templates/MODULE_SPEC.md` とこの `prompts/CHAT_05.md`
8. 対象MDがすでにあれば最新版。他担当がPRで提出した共通契約/依存仕様も確認。
読み取れない文書・ソース・PRは、読んだと偽らず障害を報告する。

## 1. あなたの書き込み可能ディレクトリ
**`docs/05-connectors/`の計5件のみ**。他担当のファイル、README、AGENTS、prompts、全体インデックスを変更禁止。必要な契約修正は `Cross-team proposal` としてDraft PR本文に提案する。

## 2. 担当MDと、必ず解決する調査質問
| 順 | 作成対象 | 具体的に決めるべき内容 |
|---|---|---|
| 01 | `docs/05-connectors/connector-framework.md` | Provider capability discovery / versioning / auth / rate limiting / egress; 認可・ToS・共通Evidence契約 |
| 02 | `docs/05-connectors/amazon-keepa.md` | Amazon価格履歴/商品取得/検索の公式提供範囲; 商品IDマッピング; クーポン/最終実質価格; API資格/費用 |
| 03 | `docs/05-connectors/buyback-price-providers.md` | 買取店の価格・減額条件・日付・JAN; 商品一致; 取得ルート、変更耐性、規約・robots |
| 04 | `docs/05-connectors/x-research.md` | 過去24h検索の実現性とProvider別制限; 検索結果の原投稿/転載/時刻; 公式確認と費用 |
| 05 | `docs/05-connectors/hotel-research.md` | 予約サイトAPI提携資格と代替; 空室/価格の鮮度; 税・手数料・駐車場・キャンセル; 予約は承認制 |

ファイル一覧は全部が**将来の成果物**であり、現在実装済み・仕様確定済みではない。一度のチャットで全部を浅く埋めることはしない。まず **`connector-framework.md`** の1件を徹底的に研究し、Draft PRを出す。その後、未完成の担当MDを優先順と依存関係に沿って進める。

## 3. ソースコード調査で参考にするもの
優先候補：**Keepa, Amazon product APIs, X/xAI Search, hotel partner APIs, Scrapling**。
ただしブランド名の羅列では不可。実際の該当`file`/`class`/`function`/`test`とtags/commitsを確認し、どの処理が参考になり、どこが今回の条件と合わないか比較する。最低2件の関連OSSを比較することを基本とし、独自の最適解があれば設計として採用してよい。重要な未検証APIを確定として書かない。

## 4. 各MDに含めるべき成果物
- Requirement表（REQ-ID、MUST/SHOULD/LATER、達成条件、参照SC-ID）
- 採用設計と却下設計（最低2案）・理由・ライセンス/保守/安全
- 依存関係図、I/O型/Schema、API/イベント契約、ファイル・クラス/関数配置の提案
- 正常/異常系シーケンス、状態遷移、**実際に実装可能な疑似コード**
- Race、二重配信、途中停止、Recovery、Timeout、Cancel、Steer、Cost budgetの関連がある部分
- 具体的エラー分類、ユーザー可視のPartial/Uncertain/Needs approval
- 資格情報/権限/外部文章のPrompt injection対策、ログマスキング
- DB/HTTP/UI/他MDへの変更提案（所有者の正本を無断複製しない）
- 実装PRの順番と分割理由
- Unit/Contract/Integration/E2E/Fault injection/Security/Benchmarkの適切なテスト
- REQ-ID→TEST-ID→SC-ID追跡表、未解決・仮説、参考ソース
対象となる全体受入シナリオ：**SC-01, SC-03, SC-04**。他のSCにも影響する場合は明記。

## 5. 並列作業中の依存ルール
CHAT_00が製品/ADR、CHAT_01が共有型・状態、CHAT_03がTool Gateway、CHAT_09がセキュリティ方針、CHAT_10が物理DBの正本。あなたの担当がこれらと競合する場合は **`[PROVISIONAL: CHAT_XX]`** とし、（提案対象・差分・理由・移行/試験）をPRへ書く。外部要件が未確定でも**担当領域の調査を停止せず**、仮定を明示してDraftを進める。

## 6. GitHub納品
- 最新mainから自分専用の`spec/chat-05-<module>`ブランチ作成。
- 完全な担当MD 1件（必要なら強く関連するもう1件）だけコミットし、main宛て**Draft PR**提出。
- PR題名：`spec(05): <module> research & implementation contract`。
- PR本文：要約、Source table（URL/確認日/tag/commit/コード位置）、採用/不採用、他担当Proposals、未解決、設計したテスト/実際に実施したテスト、レビュー観点。
- **main直接push/自動マージ禁止。他人の変更を上書きしない。**
- 完了報告：PR URL、作成MD、調査したコード位置、決定と不確実性、次に作る担当MD。
- Write tool不可なら、未Pushと明示し、完成MD本文をユーザーへ提示する。成果物の存在を捏造しない。

## 7. 品質の自分用チェック
完了前に `docs/00-foundation/review-gates.md` の全項目を自己レビュー。未達があればDraftに残す。リリース・実装・テスト済みと誤認させる文章を作らない。**「膨大な文字数」よりも、既存コードで裏付けた仕様の具体性と、実装した後の検証可能性を徹底する。**
