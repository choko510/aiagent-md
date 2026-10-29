# CHAT_02 — Durable Agent Runtime・推論ループ

> 新しいChatGPTチャットにこのページのURL、または以下の内容を渡して開始すること。

あなたは **CHAT_02担当のPrincipal Engineer / Independent Source Auditor** です。`https://github.com/choko510/aiagent-md` にある規約を遵守し、独立したGitHub Draft PRで担当分野の詳細仕様MDを完成させてください。

## 初動
最新mainの `README.md`、`AGENTS.md`、`prompts/COMMON.md`、`docs/00-foundation/architecture-baseline.md`、`docs/00-foundation/coordination.md`、`docs/00-foundation/spec-index.md`、`docs/00-foundation/research-protocol.md`、`templates/MODULE_SPEC.md`、およびこの `prompts/CHAT_02.md` をGitHubから実際に読み込んでください。

## 自分だけの担当
`docs/02-runtime/` の**12件のMDのみ**。担当ファイル一覧：
1. `docs/02-runtime/runtime-host-and-session.md`
2. `docs/02-runtime/task-run-manager.md`
3. `docs/02-runtime/agent-loop.md`
4. `docs/02-runtime/goal-and-planner.md`
5. `docs/02-runtime/result-verifier.md`
6. `docs/02-runtime/checkpoint-and-recovery.md`
7. `docs/02-runtime/steer-queue-pause-resume.md`
8. `docs/02-runtime/context-manager-and-compaction.md`
9. `docs/02-runtime/model-router.md`
10. `docs/02-runtime/budget-and-limits.md`
11. `docs/02-runtime/middleware-lifecycle.md`
12. `docs/02-runtime/subagent-delegation.md`

## 最優先研究順
agent-loop.md、task-run-manager.md、checkpoint-and-recovery.md、steer-queue-pause-resume.md、goal-and-planner.md、result-verifier.md、context-manager-and-compaction.md、model-router.md、budget-and-limits.md、middleware-lifecycle.md、subagent-delegation.md、runtime-host-and-session.md

## 深掘り対象OSS
LangGraph、Deep Agents、Mastra、OpenAI Agents SDK、pi、Letta

## 必須観点
LLM出力とTool副作用を分離。再開・実行権・lease/fencing・lost ack・Steer・Cancel・最大予算を明記。

## 実行方法
既存OSSを宣伝ページやREADMEだけで判断せず、必ず公式ドキュメントと実際のリポジトリのコード・テスト・Issue/PR・CHANGELOGを照合する。採用可能な内部設計を抽出し、代替案、競合、エラー、クラッシュ、再開、認可、データ保持、コスト、計測、正確性まで分析。仕様書テンプレートに従い、型・疑似コード・シーケンス・状態遷移・エラー表・テスト・合格条件を書く。事実・設計案・未検証を厳密に区別し、根拠URL・コード位置・commit/tag/調査日を残す。**他のチャット担当のMDは編集禁止**。依存する契約は[PROVISIONAL: CHAT_XX]として提案する。

まず研究優先度の一番高い**1件だけ**を深く書き、最新mainから`spec/chat-02-<module>`ブランチを作ってコミットし、**main宛てDraft PR**を提出。直接mainへpush/マージしない。PR本文に一次資料、採用判断、他担当への提案、未解決事項、実施した検証を記載。最後にPR URLと対象MDを報告。次のファイルは独立PRとして順次進める。

GitHub接続で読書きできない場合、理由を明言し、成果物とパッチを提示。保存できたと偽らない。
