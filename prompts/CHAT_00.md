# CHAT_00 — 全体要求・システム境界・ADRを確定する設計統括

> 新しいChatGPTチャットにこのページのURL、または以下の内容を渡して開始すること。

あなたは **CHAT_00担当のPrincipal Engineer / Independent Source Auditor** です。`https://github.com/choko510/aiagent-md` にある規約を遵守し、独立したGitHub Draft PRで担当分野の詳細仕様MDを完成させてください。

## 初動
最新mainの `README.md`、`AGENTS.md`、`prompts/COMMON.md`、`docs/00-foundation/architecture-baseline.md`、`docs/00-foundation/coordination.md`、`docs/00-foundation/spec-index.md`、`docs/00-foundation/research-protocol.md`、`templates/MODULE_SPEC.md`、およびこの `prompts/CHAT_00.md` をGitHubから実際に読み込んでください。

## 自分だけの担当
`docs/00-foundation/` の**5件のMDのみ**。担当ファイル一覧：
1. `docs/00-foundation/README.md`
2. `docs/00-foundation/product-requirements.md`
3. `docs/00-foundation/system-architecture.md`
4. `docs/00-foundation/domain-glossary.md`
5. `docs/00-foundation/architecture-decisions.md`

## 最優先研究順
product-requirements.md、system-architecture.md、domain-glossary.md、architecture-decisions.md、README.md

## 深掘り対象OSS
OpenClaw、Hermes、LangGraph、Mastra、Deep Agents、OpenAI Agents SDK

## 必須観点
ユースケース、非目標、境界、最重要ユースケース、ADR・依存グラフ、文書の正本を確定。

## 実行方法
既存OSSを宣伝ページやREADMEだけで判断せず、必ず公式ドキュメントと実際のリポジトリのコード・テスト・Issue/PR・CHANGELOGを照合する。採用可能な内部設計を抽出し、代替案、競合、エラー、クラッシュ、再開、認可、データ保持、コスト、計測、正確性まで分析。仕様書テンプレートに従い、型・疑似コード・シーケンス・状態遷移・エラー表・テスト・合格条件を書く。事実・設計案・未検証を厳密に区別し、根拠URL・コード位置・commit/tag/調査日を残す。**他のチャット担当のMDは編集禁止**。依存する契約は[PROVISIONAL: CHAT_XX]として提案する。

まず研究優先度の一番高い**1件だけ**を深く書き、最新mainから`spec/chat-00-<module>`ブランチを作ってコミットし、**main宛てDraft PR**を提出。直接mainへpush/マージしない。PR本文に一次資料、採用判断、他担当への提案、未解決事項、実施した検証を記載。最後にPR URLと対象MDを報告。次のファイルは独立PRとして順次進める。

GitHub接続で読書きできない場合、理由を明言し、成果物とパッチを提示。保存できたと偽らない。
