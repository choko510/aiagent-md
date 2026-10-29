# CHAT_14 — Learned Browser Actions & Explicit Feedback

## Absolute starting context — Round 2
You are the **CHAT_14 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. **Also MUST read `prompts/ROUND2_AUTHOR_CONTINUE.md`, `prompts/ROUND2_EXPANSION_AUTHOR.md` and `prompts/ROUND2_CONTRACT_RECONCILIATION.md` (contract conflicts).** Check latest linked contracts and Draft PR HEADs, not just main. If any file cannot be inspected, state that rather than guessing.

## Scope and write authority
You ONLY own `docs/14-adaptive-experience/` and the following **2 specification MDs**:

| ID | Required path | Primary implementation questions |
|---|---|---|
| 14-01 | `docs/14-adaptive-experience/learned-browser-actions.md` | 成功したブラウザ操作を検証可能なRecipe/Selectorとして再利用し、DOM変化時は安全に無効化→再推論。セッション間の認証/権限漏洩禁止。 |
| 14-02 | `docs/14-adaptive-experience/feedback-learning.md` | 明示的ユーザーフィードバックの対象と範囲、好みの更新、記憶の誤推定・修正、ユーザールールと事実の分離、Undo・監査。 |

Do not modify `prompts/`, `README.md`, `AGENTS.md`, existing category spec docs, `spec-index.md`, or any other ownership area. All source changes/contract schema edits must be cross-team proposals to responsible owners.

## Research depth
Primary targets: **Stagehand v4 and v3 API differences, Browserbase caching vs local mode, Playwright locators, Jev Browser, Letta/Hermes Skills and feedback**. Do not stop at README/marketing. Locate actual **source files, classes/functions, relevant tests, changelogs, recent commits, issue constraints and licenses**; cite exact URLs, tested vs documented, version and observed date. Compare at least two meaningfully applicable implementations or algorithms; choose a design with safety, performance and portability rationale. Research both success and failure cases. Avoid copying license-incompatible code.

## Specific design imperatives
Stagehand v4とv3の互換を分離し、Browserbaseのサーバーキャッシュ機能がローカル無料運用でも同じとは限らないことを検証。既存Playwright/Jevの同時操作禁止・セッション間漏洩・DOM変化・事後検証を優先。FeedbackはMemory担当の契約へ提案し、勝手に事実やポリシーを書き換えない。

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

## 第2ラウンド専門ゲート — Adaptive Experience / 2MD
- `learned-browser-actions.md`：Stagehandの版差（v3/v4）とBrowserbase管理サービスのキャッシュを、**ローカルPlaywright/Jevの自己ホスト実装と混同しない**。Cached actionのidentity (origin, page pattern, DOM fingerprint, selector, task intent, account scope, expected outcome, expiresAt) とconfidenceを明示。成功したread-only操作だけを自動再利用候補とする。可逆/不可逆なアクションの権限/再承認、DOM更新・並列tab・再ログイン・途中失敗・cache poisoning・復元試験。
- `feedback-learning.md`：ユーザーの明示的な「この店を除外」「利益が薄い」等をscope付きで保存する提案。好み・事実・ルール・技能を混同せず、履歴、撤回、誤学習防止、他Taskへの反映範囲、プライバシー。Memory/Skillの正本変更はCHAT_06への提案のみ。
**競合検証必須**：CHAT_04 browser execution/ownership、CHAT_06 memory/skills、CHAT_03 approval、CHAT_09 trust、CHAT_11 replay tests。
**作成順**：Learned Browser Actions → Feedback Learning。両方のMDが提出されて初めて担当一巡完了。

## Round 2 execution contract and PR workflow (overrides old first-file-only stop)

**このチャットの最終担当ゴールは、上表に列挙した2件すべての仕様MDをDraft PRへ提出すること。最初の1件だけでは終了しない。**

1. 最新main＋関連する他担当Draft PRのHEADブランチ全文を実際に読み、`SpecID/path/main/PR/status/blocks/next`の担当台帳を最初に作る。初稿がすでにPRにある場合は自分の既存PRを監査/必要に応じ更新し、重複PRを出さない。
2. 各MDを独立にsource-level research。公式Docs・実装コード・テスト・Issue・PR・CHANGELOG・版・licenseまで調査。新規技術の費用/権限/互換性は「試験/確認した」か「未検証」かを区別。
3. `templates/MODULE_SPEC.md` と `review-gates.md` に従い、REQ-ID → TEST-ID → SC-ID、TypeScript/Schema、状態遷移、障害/競合/再開、Security、API/DB proposals、実装PR分割まで全項目記載。
4. 自分の担当ファイルのみ、`spec/chat-14-<module>`ブランチ（最新mainから）へコミット。1〜2MDにつき1つの**Draft PR**。PR本文にsource table、設計理由/非採用理由、未検証、契約提案、テスト設計と実行有無、未解決Blockersを書く。**main直push・他Owner編集・無断マージ禁止**。
5. 1件提出したら次の残MDへ進み、担当全件を終える。ツール/コンテキスト制約で終了する場合は、提出/未提出一覧、PR URLs、次に作るMD、依存する決定、引継ぎを正確に残す。非同期の継続を約束しない。
6. 既存契約（UUID、Task/Conversation、recordVersion、leaseEpoch、tenant/workspaceなど）に依存する箇所は各OWNERの正本を優先し、矛盾は`[PROVISIONAL: CHAT_XX]`とCross-team proposalで報告。未統合のまま`Locked`としない。

**終了報告は担当全件の状態を表で示す。** 単に最初のPR URLのみ示して「完了」としない。
