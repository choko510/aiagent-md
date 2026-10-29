# CHAT_14 — Learned Browser Actions & Explicit Feedback

## Absolute starting context
You are the **CHAT_14 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. Check latest linked contracts or draft PRs. If any file cannot be inspected, state that rather than guessing.

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

## Mandatory deliverable for each MD
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

## Submit
First research and write **`learned-browser-actions.md`** in full, then create `spec/chat-14-<module>` from the current main, commit only your owned doc and open a **Draft PR to main**. Do not push to main, auto-merge, or edit another owner's doc. In the PR body, include primary source-level evidence, tradeoffs, exact implementation decisions, risk/uncertainty, test plan and named cross-team proposals. Report the Draft PR URL and explicit blockers. If GitHub write access fails, output the complete MD and state that it is NOT committed. Work on other modules sequentially in later separate PRs.
