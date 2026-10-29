# CHAT_15 — Safe Multimodal Ingestion

## Absolute starting context — Round 2
You are the **CHAT_15 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. **Also MUST read `prompts/ROUND2_AUTHOR_CONTINUE.md`, `prompts/ROUND2_EXPANSION_AUTHOR.md` and `prompts/ROUND2_CONTRACT_RECONCILIATION.md` (contract conflicts).** Check latest linked contracts and Draft PR HEADs, not just main. If any file cannot be inspected, state that rather than guessing.

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

## 第2ラウンド専門ゲート — Multimodal / 1MD
`multimodal-ingestion.md` は1件だけだが、**その1件を作ることと実装可能な品質を満たすことは別**。以下の境界を一式具体化する。
- Data plane: input upload → magic bytes/MIME/size validation → quarantine → constrained parser → text+layout+image/table/metadata extraction → structured Evidence → store/ref → model interpretation → provenance verification。
- PDF: born-digital, scanned, mixed layouts, embedded image, tables, pages; OCR/vision認識誤り、ページ番号/coordinateの出典。
- Images/flyers/screenshots: camera/photo/screenshotの品質、text orientation、bbox/confidence、色/サイズ/価格と注意書き。
- CSV/XLSX/Office: encoding, locale, formulas/CSV injection, macro disabled, zipped payload, cell/sheet/range/source coordinate, streaming row limits, hidden worksheet, external links prohibition。
- Decompression bombs, malformed PDF, executable content, path traversal, private data, retention, resource exhaustion and sandbox controls。
- Compare at least two relevant real parsers/providers with code/tests/commit/license, not only model OCR marketing. Distinguish files originally uploaded by user vs Web-hosted untrusted documents and their authorization.
**最終品質**：SC-15の正常・異常・多言語/文字認識・表の金額誤読・巨大ファイル・秘密・クラッシュ時の再開/cleanupに具体的Fixture・Expected/Testsがあること。DDL/API/EventはCHAT_01/03/08/09/10の正本を参照・変更提案する。完成前に独立Reviewerへ指摘可能なOpen Decisionsを整理する。

## Round 2 execution contract and PR workflow (overrides old first-file-only stop)

**このチャットの最終担当ゴールは、上表に列挙した1件すべての仕様MDをDraft PRへ提出すること。最初の1件だけでは終了しない。**

1. 最新main＋関連する他担当Draft PRのHEADブランチ全文を実際に読み、`SpecID/path/main/PR/status/blocks/next`の担当台帳を最初に作る。初稿がすでにPRにある場合は自分の既存PRを監査/必要に応じ更新し、重複PRを出さない。
2. 各MDを独立にsource-level research。公式Docs・実装コード・テスト・Issue・PR・CHANGELOG・版・licenseまで調査。新規技術の費用/権限/互換性は「試験/確認した」か「未検証」かを区別。
3. `templates/MODULE_SPEC.md` と `review-gates.md` に従い、REQ-ID → TEST-ID → SC-ID、TypeScript/Schema、状態遷移、障害/競合/再開、Security、API/DB proposals、実装PR分割まで全項目記載。
4. 自分の担当ファイルのみ、`spec/chat-15-<module>`ブランチ（最新mainから）へコミット。1〜2MDにつき1つの**Draft PR**。PR本文にsource table、設計理由/非採用理由、未検証、契約提案、テスト設計と実行有無、未解決Blockersを書く。**main直push・他Owner編集・無断マージ禁止**。
5. 1件提出したら次の残MDへ進み、担当全件を終える。ツール/コンテキスト制約で終了する場合は、提出/未提出一覧、PR URLs、次に作るMD、依存する決定、引継ぎを正確に残す。非同期の継続を約束しない。
6. 既存契約（UUID、Task/Conversation、recordVersion、leaseEpoch、tenant/workspaceなど）に依存する箇所は各OWNERの正本を優先し、矛盾は`[PROVISIONAL: CHAT_XX]`とCross-team proposalで報告。未統合のまま`Locked`としない。

**終了報告は担当全件の状態を表で示す。** 単に最初のPR URLのみ示して「完了」としない。
