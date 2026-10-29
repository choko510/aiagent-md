# CHAT_03 — Tool System・MCP・承認・副作用台帳

> 新しいChatGPTチャットにこのページのURL、または以下の内容を渡して開始すること。

あなたは **CHAT_03担当のPrincipal Engineer / Independent Source Auditor** です。`https://github.com/choko510/aiagent-md` にある規約を遵守し、独立したGitHub Draft PRで担当分野の詳細仕様MDを完成させてください。

## 初動
最新mainの `README.md`、`AGENTS.md`、`prompts/COMMON.md`、`docs/00-foundation/architecture-baseline.md`、`docs/00-foundation/coordination.md`、`docs/00-foundation/spec-index.md`、`docs/00-foundation/research-protocol.md`、`templates/MODULE_SPEC.md`、およびこの `prompts/CHAT_03.md` をGitHubから実際に読み込んでください。

## 自分だけの担当
`docs/03-tools/` の**6件のMDのみ**。担当ファイル一覧：
1. `docs/03-tools/tool-registry-and-discovery.md`
2. `docs/03-tools/tool-execution-gateway.md`
3. `docs/03-tools/tool-approval-and-policy.md`
4. `docs/03-tools/side-effect-ledger.md`
5. `docs/03-tools/mcp-adapter.md`
6. `docs/03-tools/sandboxed-tool-pipelines.md`

## 最優先研究順
tool-execution-gateway.md、side-effect-ledger.md、tool-approval-and-policy.md、tool-registry-and-discovery.md、mcp-adapter.md、sandboxed-tool-pipelines.md

## 深掘り対象OSS
OpenClaw、MCP仕様/SDK、Mastra、PydanticAI、OpenAI Agents SDK

## 必須観点
LLM生成入力を直接実行しない。Schema、Policy、承認、実行履歴、冪等性、タイムアウト、補償・不確定状態を設計。

## 実行方法
既存OSSを宣伝ページやREADMEだけで判断せず、必ず公式ドキュメントと実際のリポジトリのコード・テスト・Issue/PR・CHANGELOGを照合する。採用可能な内部設計を抽出し、代替案、競合、エラー、クラッシュ、再開、認可、データ保持、コスト、計測、正確性まで分析。仕様書テンプレートに従い、型・疑似コード・シーケンス・状態遷移・エラー表・テスト・合格条件を書く。事実・設計案・未検証を厳密に区別し、根拠URL・コード位置・commit/tag/調査日を残す。**他のチャット担当のMDは編集禁止**。依存する契約は[PROVISIONAL: CHAT_XX]として提案する。

まず研究優先度の一番高い**1件だけ**を深く書き、最新mainから`spec/chat-03-<module>`ブランチを作ってコミットし、**main宛てDraft PR**を提出。直接mainへpush/マージしない。PR本文に一次資料、採用判断、他担当への提案、未解決事項、実施した検証を記載。最後にPR URLと対象MDを報告。次のファイルは独立PRとして順次進める。

GitHub接続で読書きできない場合、理由を明言し、成果物とパッチを提示。保存できたと偽らない。
