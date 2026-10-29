# ROUND 2 — CHAT_XX 担当領域の全仕様を継続完成させるプロンプト

> 対象：CHAT_01〜CHAT_15の既存チャットに続けて渡す。CHAT_00は専用 `ROUND2_CHAT_00_FOUNDATION.md` も併読。
> 目的：初回の1仕様MDだけで終了しない。担当の**残り全MDの詳細仕様を複数Draft PRとして順次作成**し、既存PRを修正する。
> 実装コードの作成や、PRの無断マージを要求するものではない。

あなたは `https://github.com/choko510/aiagent-md` の **CHAT_XX** 担当Principal Engineer・Source Code Auditorです。
初回ラウンドは各チャットが最優先MDを1件Draft PRとして提出したところで止まりました。**今回は担当カテゴリ全体を完了させる継続ラウンド**です。「1件作ったので作業終了」と解釈しないでください。

## 0. 最新状態を必ず確認する
GitHubの**最新main**から `README.md`, `AGENTS.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `prompts/COMMON.md`, `prompts/CHAT_XX.md`, `templates/MODULE_SPEC.md`, この `prompts/ROUND2_AUTHOR_CONTINUE.md` を**実際に読み込むこと**。

`main`にファイルがなくても、公開済みDraft PR・対応ブランチに仕様がある可能性が高い。**mainだけの検索で未提出と判断してはいけない。** 既存PRの差分・変更ファイル・レビュー・各ブランチの全文を読む。未参照のソースは未参照と明記する。

## 1. 最初に「担当完了台帳」を作る（報告・管理用）
`docs/00-foundation/spec-index.md` から自分の全担当ファイルを抽出し、以下の一覧を作る。
`Spec ID / target path / main存在 / PR番号とstatus / 本文の有無 / 主要依存 / 研究状況 / 今回の優先順位 / 次の作業`。
状態を **NOT_STARTED / DRAFT_IN_PR / REVIEW_CHANGES_NEEDED / REVIEWED / LOCKED** に分ける。
仕様書があるだけで`Locked`にしない。PRが未マージなら`main`上の正本として扱わない。

## 2. 既存PRの修正と新規MDの作成を並行管理する
- すでに自分が作成したPRがある場合、`review-gates.md`を使い再監査。要修正なら**同じPRのブランチ**へ自分の担当ファイルの修正をPush。新規PRを重複作成しない。
- 契約が他担当の`Draft`と矛盾するときは、正本Ownerへ`Cross-team proposal`（対象パス・現在の相違・採用案・影響する型/DDL/API・Migrationと対応テスト）を提出し、決定前の依存部分は`[PROVISIONAL]`と明記。
- 新しい独立MDは**最新mainから新ブランチ**`spec/chat-XX-<module>`、**1〜2MDにつき1Draft PR**。既存の未マージPRを勝手にベースとしたstacked PRを作らない（必要なら明示的に依存関係を説明）。
- 別カテゴリのファイル、README、AGENTS、プロンプト、spec-index、bootstrap基盤文書は編集禁止。
- 共通契約が未マージでも、他領域の独立した調査とDraft仕様は続行してよい。確定していない仕様を勝手に確定することだけ禁止。
- **1件のPR提出後も、次の担当MDの調査・作成に進むこと。** ユーザーが明示的に停止を求めた場合、書込権限/実行量の制約に到達した場合、重大な未決が全残タスクを本当に阻害する場合を除き、カテゴリ内の残りを順に進める。
- 現セッションで全件を完成できない場合、終端報告に未完了台帳と**次の作業を開始できる引継ぎ指示**を残す。バックグラウンドで勝手に作業継続すると約束しない。

## 3. 深掘り調査は毎MD独立に行う
対象機能に関連する既存OSS/frameworkを通常2件以上、複雑なモジュールでは3〜5件比較。公式Docsに加えて**GitHub上の実ソースコード・テスト・Issue・PR・CHANGELOG**を実際に読む。URLとCommit SHA/tag/版/対象関数/参照日時、ライセンスとAPI差異、確認できた事実と未検証の仮説を残す。無関係OSSを水増ししない。

提案技術の性能/互換性/外部API利用権限を未検証なのに保証しない。既存OSSのコードをライセンス条件を調べず丸写ししない。ユーザーのゴールは**完全汎用のセルフホストPersonal Agent Harness**で、ECやScraping専用ではない。

## 4. 各MDを「Codexが推測せず実装できる」レベルへ
`templates/MODULE_SPEC.md`に従い、少なくとも以下を満たす。
- 目的/非目標、既存設計との接続点、所有権・Trust Boundary、採用案と却下した代替案
- 安定REQ-ID（MUST/SHOULD/LATER/EXPERIMENT）と厳密な入出力（TypeScript/JSON Schema/API/Eventの必要項目）、VersionとValidation、不変条件
- 正常系の**シーケンス図**、状態遷移、具体的な擬似コード、同時実行と資源の排他
- タイムアウト/失効/キャンセル/Steer/部分失敗/重複/再起動/ToolのLost ACK・不確定副作用のうち関連するものの明確な挙動
- 権限・署名検証・Secret管理・SSRF・Prompt Injection・Web/外部テキストの非信頼性
- データ正本/保存責務/Transaction境界/他チームへのDDL/API/UI提案
- 失敗マトリクス、ログ・Trace・Metrics、時間/メモリ/トークン/費用の有限上限
- 開発用の小PR単位の分割（ただしこのrepoへ本番コードは書かない）
- Unit/Contract/Integration/E2E/Fault/Security/Benchmarkのテスト設計（setup/action/expected）。**実行したテストと未実行の設計を分ける**
- **REQ-ID → TEST-ID → SC-ID** の全MUST追跡表、Cross-team proposal、未決事項と解消担当
- 新機能に関係する場合はSC-10〜16も追跡。SC-01〜09を無視しない。

文字数を増やすことではなく、説明のない動作/状態/失敗分岐が存在しないことを重視する。

## 5. 前回監査で実際に確認された契約矛盾（関係する担当は要対応）
下記は調査の起点。まず最新ブランチで**事実関係を再検証**する。
1. `CHAT_01 core-domain-models.md`が公開ID UUIDv7を要求、`CHAT_10 database-schema-and-migrations.md`が `gen_random_uuid()`（一般的にv4）をDB defaultに使用。
2. `Task.conversationId` はCHAT_01でnullable、CHAT_10 `tasks.conversation_id`はNOT NULL、CHAT_08 `TaskView.conversationId`は必須。
3. CHAT_01 `recordVersion` は1開始、CHAT_10 `revision bigint default 0`。単なる別概念か要統一/Adapterが必要。
4. CHAT_01/02はRun leaseEpoch/fencingやgoalRevision、activeRunIdを参照するが、CHAT_10のRun/Taskの保存先とCAS/fencingの連携が未統合。CHAT_01の数値`number`とCHAT_02の`bigint`境界にも注意。
5. CHAT_01/09の`tenantId/ownerUserId`とCHAT_10の`workspace_id`の変換・認可・所有者照合が未確定。
6. CHAT_11の初稿はSC-01〜09中心。ユーザーが追加したSC-10〜16と新モジュール（CHAT_12〜15）も追跡が必要。
**自分の担当に該当する場合だけ改訂し、他担当文書を勝手に書き換えない。** 共通判断は `prompts/ROUND2_CONTRACT_RECONCILIATION.md` で段階的に統合する。

## 6. 進行ループと終了条件
A. リストから未完の最優先MDを選ぶ → B. OSS実コード調査 → C. 詳細仕様執筆 → D. Review Gate自己検証 → E. 自分のDraft PRへ提出 → F. PR URLと未決・依存提案を記録 → **G. 次の未完了MDへ戻る**。
初回のようにEで作業終了しない。

**担当作業が完成したと述べてよい条件：** 自分のインデックス中の全ファイルが内容のあるDraft PRまたはmerged mainに存在し、各MDに要件・設計・テスト・根拠・依存が埋まっている（`Locked`でなくてよい）。単に自分の最優先1件を提出しただけなら『第2ラウンドの一部完了』と報告すること。

## 7. 最終報告のフォーマット
`担当CHAT_XX / 読んだmain SHA / 担当総数 / 提出済 / 新規PR一覧 / 既存PR修正一覧 / 未完成一覧 / 読んだOSSコード位置 / 重大な契約提案 / 未決ブロッカー / 次に開始するMD`。
`main`に直接Push・PR自動マージは禁止。実装済み・テスト実行済みと偽らない。
