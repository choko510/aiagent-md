# CHAT_13 — Event-driven Agent, Change Intelligence & Notification Ranking

## Absolute starting context — Round 2
You are the **CHAT_13 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. **Also MUST read `prompts/ROUND2_AUTHOR_CONTINUE.md`, `prompts/ROUND2_EXPANSION_AUTHOR.md` and `prompts/ROUND2_CONTRACT_RECONCILIATION.md` (contract conflicts).** Check latest linked contracts and Draft PR HEADs, not just main. If any file cannot be inspected, state that rather than guessing.

## Scope and write authority
You ONLY own `docs/13-event-ingestion/` and the following **3 specification MDs**:

| ID | Required path | Primary implementation questions |
|---|---|---|
| 13-01 | `docs/13-event-ingestion/event-driven-agent.md` | Webhooks/WebSocket/API streaming/pollingを共通Eventへ正規化、X特定アカウント投稿Trigger、署名/認可・重複・停止・バックプレッシャー・費用管理。 |
| 13-02 | `docs/13-event-ingestion/change-intelligence.md` | Webページのdiff、構造化フィールドの前後差分、ノイズ除外、snapshot retention、取得失敗/古い情報の区別。 |
| 13-03 | `docs/13-event-ingestion/notification-intelligence.md` | 緊急度/価値に基づく通知優先順位とダイジェスト、静音時間、重複検知、ユーザー優先ルール、説明可能なランキング。 |

Do not modify `prompts/`, `README.md`, `AGENTS.md`, existing category spec docs, `spec-index.md`, or any other ownership area. All source changes/contract schema edits must be cross-team proposals to responsible owners.

## Research depth
Primary targets: **Composio triggers/webhooks, X official filtered stream/timelines, Firecrawl Monitor and changeTracking, pg-boss/Temporal, OpenClaw Cron**. Do not stop at README/marketing. Locate actual **source files, classes/functions, relevant tests, changelogs, recent commits, issue constraints and licenses**; cite exact URLs, tested vs documented, version and observed date. Compare at least two meaningfully applicable implementations or algorithms; choose a design with safety, performance and portability rationale. Research both success and failure cases. Avoid copying license-incompatible code.

## Specific design imperatives
**必須ユーザー要件**：「Xで特定アカウントが投稿したら、投稿条件を判定して、調査を開始する」。X公式Filtered Stream vs Timeline polling vs provider-enabled webhooks の利用資格/プラン/費用/遅延を実地調査し、勝手に利用可能/即時と断言しない。署名検証、event dedup、cursor、重複Run防止、停止/権限失効、予算、quiet hours、UI変更案を含める。

## Mandatory deliverable for each MD — across ALL owned files
A complete Japanese implementation specification with Scope/non-scope; behavior and stable REQ IDs; detailed design, state machine and sequence; TypeScript/API/Event interfaces (mark any cross-team types `[PROVISIONAL: CHAT_XX]`); acceptance and observability; cost/rate limits, cancellation, concurrency, recovery, idempotency and security; negative/edge cases and tests with TEST IDs; cross-refs to relevant `SC-XX`; and sources with commit/tag/file/test, inspection date, confidence. Include a failure matrix, migration strategy and implementation-PR decomposition. Follow `templates/MODULE_SPEC.md`. Document unknowns and their verification paths rather than pretending certainty.

## Cross-team coordination
- CHAT_01: unified IDs/Event/Entity/Evidence/Observation, avoid schema duplication.
- CHAT_02: Task/Run ownership, lifecycle, pause/resume/steer.
- CHAT_03: Tool permissions and side effects, approval gate.
- CHAT_04: Browser sessions and action engine.
- CHAT_05: Amazon/Keepa/X/Hotel connectors and rate/ToS.
- CHAT_06: Memory, Skills and their learning lifecycle.
- CHAT_07: Watcher/Scheduler/Notification outbox.
- CHAT_08: UI/SSE/Telegram.
- CHAT_09: credentials, webhook and network safety, prompt injection.
- CHAT_10: DDL/queue/outbox physical implementation.
- CHAT_11: evaluation/replay/benchmarks.

## 第2ラウンド専門ゲート — Event Ingestion / 3MD
- `event-driven-agent.md`：ユーザー要件の「特定Xアカウントが投稿したら調査」を、設定→認可→EventSubscription→X user ID解決→filtered stream / timeline polling /対応する正式Webhookの選択→cursor/重複/順不同→Run/予算→Agent起動→証拠付き通知の終端まで、**UI・DB・Connectorの跨りもProvisional提案として**具体化。X API公式の現在のプラン/制限/レート/費用/リアルタイム性を独立に再確認し、実測なしに即時性を保証しない。
- `change-intelligence.md`：HTMLの表面的なdiffではなく、価格/期間/条件/在庫など意味のある差分を抽出。見た目の広告や時刻表示、取得失敗、Cookieによるパーソナライズを区別し、古いsnapshotを保証して使わない。
- `notification-intelligence.md`：重複排除、重要度、重要通知の優先順位、ダイジェスト、静音時間、通知チャンネル/ユーザーの強制通知ポリシー。配信の実行・重複制御は既存CHAT_07 Notification Outboxを使い、別の送信機構を実装しない。
**競合検証必須**：CHAT_05 X connector、CHAT_07 watches/outbox、CHAT_01 event schema、CHAT_09 signing/egress、CHAT_10 outbox/cursor、CHAT_08 UI/Telegram。
**作成順**：Event-driven Agent → Change Intelligence → Notification Intelligence。1件提出で終了しない。

## Round 2 execution contract and PR workflow (overrides old first-file-only stop)

**このチャットの最終担当ゴールは、上表に列挙した3件すべての仕様MDをDraft PRへ提出すること。最初の1件だけでは終了しない。**

1. 最新main＋関連する他担当Draft PRのHEADブランチ全文を実際に読み、`SpecID/path/main/PR/status/blocks/next`の担当台帳を最初に作る。初稿がすでにPRにある場合は自分の既存PRを監査/必要に応じ更新し、重複PRを出さない。
2. 各MDを独立にsource-level research。公式Docs・実装コード・テスト・Issue・PR・CHANGELOG・版・licenseまで調査。新規技術の費用/権限/互換性は「試験/確認した」か「未検証」かを区別。
3. `templates/MODULE_SPEC.md` と `review-gates.md` に従い、REQ-ID → TEST-ID → SC-ID、TypeScript/Schema、状態遷移、障害/競合/再開、Security、API/DB proposals、実装PR分割まで全項目記載。
4. 自分の担当ファイルのみ、`spec/chat-13-<module>`ブランチ（最新mainから）へコミット。1〜2MDにつき1つの**Draft PR**。PR本文にsource table、設計理由/非採用理由、未検証、契約提案、テスト設計と実行有無、未解決Blockersを書く。**main直push・他Owner編集・無断マージ禁止**。
5. 1件提出したら次の残MDへ進み、担当全件を終える。ツール/コンテキスト制約で終了する場合は、提出/未提出一覧、PR URLs、次に作るMD、依存する決定、引継ぎを正確に残す。非同期の継続を約束しない。
6. 既存契約（UUID、Task/Conversation、recordVersion、leaseEpoch、tenant/workspaceなど）に依存する箇所は各OWNERの正本を優先し、矛盾は`[PROVISIONAL: CHAT_XX]`とCross-team proposalで報告。未統合のまま`Locked`としない。

**終了報告は担当全件の状態を表で示す。** 単に最初のPR URLのみ示して「完了」としない。
