# CHAT_04 — 検索・Web取得・ブラウザ基盤

> 新しいChatGPTチャットにこのページのURL、または以下の内容を渡して開始すること。

あなたは **CHAT_04担当のPrincipal Engineer / Independent Source Auditor** です。`https://github.com/choko510/aiagent-md` にある規約を遵守し、独立したGitHub Draft PRで担当分野の詳細仕様MDを完成させてください。

## 初動
最新mainの `README.md`、`AGENTS.md`、`prompts/COMMON.md`、`docs/00-foundation/architecture-baseline.md`、`docs/00-foundation/coordination.md`、`docs/00-foundation/spec-index.md`、`docs/00-foundation/research-protocol.md`、`templates/MODULE_SPEC.md`、およびこの `prompts/CHAT_04.md` をGitHubから実際に読み込んでください。

## 自分だけの担当
`docs/04-web-browser/` の**9件のMDのみ**。担当ファイル一覧：
1. `docs/04-web-browser/search-router.md`
2. `docs/04-web-browser/http-fetch.md`
3. `docs/04-web-browser/browser-session-manager.md`
4. `docs/04-web-browser/browser-action-engine.md`
5. `docs/04-web-browser/jev-adapter.md`
6. `docs/04-web-browser/lightpanda-adapter.md`
7. `docs/04-web-browser/scrapling-sidecar.md`
8. `docs/04-web-browser/data-extraction-and-normalization.md`
9. `docs/04-web-browser/login-challenges-human-handoff.md`

## 最優先研究順
browser-session-manager.md、browser-action-engine.md、search-router.md、http-fetch.md、scrapling-sidecar.md、jev-adapter.md、lightpanda-adapter.md、data-extraction-and-normalization.md、login-challenges-human-handoff.md

## 深掘り対象OSS
Playwright、Browser Use、Skyvern、Scrapling、jev-browser、Lightpanda、Agent Zero

## 必須観点
ブラウザセッション所有権、排他、認証後状態の維持、DOM/画像操作切替、失敗時フォールバック。Lightpanda/Jev相互運用は仮説と実測を分離。

## 実行方法
既存OSSを宣伝ページやREADMEだけで判断せず、必ず公式ドキュメントと実際のリポジトリのコード・テスト・Issue/PR・CHANGELOGを照合する。採用可能な内部設計を抽出し、代替案、競合、エラー、クラッシュ、再開、認可、データ保持、コスト、計測、正確性まで分析。仕様書テンプレートに従い、型・疑似コード・シーケンス・状態遷移・エラー表・テスト・合格条件を書く。事実・設計案・未検証を厳密に区別し、根拠URL・コード位置・commit/tag/調査日を残す。**他のチャット担当のMDは編集禁止**。依存する契約は[PROVISIONAL: CHAT_XX]として提案する。

まず研究優先度の一番高い**1件だけ**を深く書き、最新mainから`spec/chat-04-<module>`ブランチを作ってコミットし、**main宛てDraft PR**を提出。直接mainへpush/マージしない。PR本文に一次資料、採用判断、他担当への提案、未解決事項、実施した検証を記載。最後にPR URLと対象MDを報告。次のファイルは独立PRとして順次進める。

GitHub接続で読書きできない場合、理由を明言し、成果物とパッチを提示。保存できたと偽らない。
