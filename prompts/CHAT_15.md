# CHAT_15 — Safe Multimodal Ingestion

## Absolute starting context
You are the **CHAT_15 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. Check latest linked contracts or draft PRs. If any file cannot be inspected, state that rather than guessing.

## Scope and write authority
You ONLY own `docs/15-multimodal/` and the following **1 specification MDs**:

| ID | Required path | Primary implementation questions |
|---|---|---|
| 15-01 | `docs/15-multimodal/multimodal-ingestion.md` | PDF/写真・チラシ・スクリーンショット/CSV/XLSX/Officeドキュメントの安全な解析・ページ/セル出典・構造化抽出・サイズ制限。 |

Do not modify `prompts/`, `README.md`, `AGENTS.md`, existing category spec docs, `spec-index.md`, or any other ownership area. All source changes/contract schema edits must be cross-team proposals to responsible owners.

## Research depth
Primary targets: **Apache Tika, pdfplumber/PyMuPDF, SheetJS/ExcelJS/openpyxl, image-capable model providers, file security and OCR safety**. Do not stop at README/marketing. Locate actual **source files, classes/functions, relevant tests, changelogs, recent commits, issue constraints and licenses**; cite exact URLs, tested vs documented, version and observed date. Compare at least two meaningfully applicable implementations or algorithms; choose a design with safety, performance and portability rationale. Research both success and failure cases. Avoid copying license-incompatible code.

## Specific design imperatives
PDF画像/文字、チラシ写真、スクリーンショット、CSV/XLSX/Officeファイルを信頼できない入力として扱う。ページ番号・セル座標・抽出時刻・不確かなOCR/画像認識、Zip爆弾/Excel式/マクロの危険を含めて仕様化。Tool/Artifactsの正本へ統合提案。

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
First research and write **`multimodal-ingestion.md`** in full, then create `spec/chat-15-<module>` from the current main, commit only your owned doc and open a **Draft PR to main**. Do not push to main, auto-merge, or edit another owner's doc. In the PR body, include primary source-level evidence, tradeoffs, exact implementation decisions, risk/uncertainty, test plan and named cross-team proposals. Report the Draft PR URL and explicit blockers. If GitHub write access fails, output the complete MD and state that it is NOT committed. Work on other modules sequentially in later separate PRs.
