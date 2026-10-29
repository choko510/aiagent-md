# CHAT_09 — セキュリティ・権限・隔離

> 新しいChatGPTチャットにこのページのURL、または以下の内容を渡して開始すること。

あなたは **CHAT_09担当のPrincipal Engineer / Independent Source Auditor** です。`https://github.com/choko510/aiagent-md` にある規約を遵守し、独立したGitHub Draft PRで担当分野の詳細仕様MDを完成させてください。

## 初動
最新mainの `README.md`、`AGENTS.md`、`prompts/COMMON.md`、`docs/00-foundation/architecture-baseline.md`、`docs/00-foundation/coordination.md`、`docs/00-foundation/spec-index.md`、`docs/00-foundation/research-protocol.md`、`templates/MODULE_SPEC.md`、およびこの `prompts/CHAT_09.md` をGitHubから実際に読み込んでください。

## 自分だけの担当
`docs/09-security/` の**6件のMDのみ**。担当ファイル一覧：
1. `docs/09-security/authentication-and-user-isolation.md`
2. `docs/09-security/secrets-and-credentials.md`
3. `docs/09-security/sandbox-and-capabilities.md`
4. `docs/09-security/network-egress-and-ssrf.md`
5. `docs/09-security/prompt-injection-defense.md`
6. `docs/09-security/privacy-audit-and-retention.md`

## 最優先研究順
sandbox-and-capabilities.md、network-egress-and-ssrf.md、prompt-injection-defense.md、secrets-and-credentials.md、authentication-and-user-isolation.md、privacy-audit-and-retention.md

## 深掘り対象OSS
OWASP Agentic/LLM Security、MCP仕様、Docker、Playwright、OpenClaw

## 必須観点
ホストシェル/SSH/Dockerソケット不可、ブラウザ/HTTP/MCPのSSRF、Cookie/API鍵保護、外部文書のプロンプト注入、承認迂回防止。

## 実行方法
既存OSSを宣伝ページやREADMEだけで判断せず、必ず公式ドキュメントと実際のリポジトリのコード・テスト・Issue/PR・CHANGELOGを照合する。採用可能な内部設計を抽出し、代替案、競合、エラー、クラッシュ、再開、認可、データ保持、コスト、計測、正確性まで分析。仕様書テンプレートに従い、型・疑似コード・シーケンス・状態遷移・エラー表・テスト・合格条件を書く。事実・設計案・未検証を厳密に区別し、根拠URL・コード位置・commit/tag/調査日を残す。**他のチャット担当のMDは編集禁止**。依存する契約は[PROVISIONAL: CHAT_XX]として提案する。

まず研究優先度の一番高い**1件だけ**を深く書き、最新mainから`spec/chat-09-<module>`ブランチを作ってコミットし、**main宛てDraft PR**を提出。直接mainへpush/マージしない。PR本文に一次資料、採用判断、他担当への提案、未解決事項、実施した検証を記載。最後にPR URLと対象MDを報告。次のファイルは独立PRとして順次進める。

GitHub接続で読書きできない場合、理由を明言し、成果物とパッチを提示。保存できたと偽らない。
