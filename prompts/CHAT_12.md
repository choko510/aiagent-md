# CHAT_12 — Autonomous Discovery, Entity Resolution & Decision Intelligence

## Absolute starting context
You are the **CHAT_12 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. Check latest linked contracts or draft PRs. If any file cannot be inspected, state that rather than guessing.

## Scope and write authority
You ONLY own `docs/12-intelligence/` and the following **5 specification MDs**:

| ID | Required path | Primary implementation questions |
|---|---|---|
| 12-01 | `docs/12-intelligence/opportunity-radar.md` | 候補の自律発掘、発掘戦略・探索範囲・重複抑制・収集コスト・候補評価・不確実性の扱い。特定の商品を登録しなくても新機会を見つける。 |
| 12-02 | `docs/12-intelligence/entity-resolution.md` | 商品/ホテル/企業/人物のCross-source同一性照合。JAN・ASIN・EAN・型番・容量・住所など。誤統合を防ぐhuman reviewとversioned mapping。 |
| 12-03 | `docs/12-intelligence/trend-fusion.md` | X投稿・価格・買取相場・サイト変更などの複数signalを統合。時刻、サンプリング偏り、トレンドscore、偽陽性とsource correlation。 |
| 12-04 | `docs/12-intelligence/decision-risk-engine.md` | 利益見込み、クーポン、手数料、査定減額、在庫/価格変動、閾値、シナリオ分岐。計算はdeterministicで、不明値は推定として示す。 |
| 12-05 | `docs/12-intelligence/source-reliability-profiles.md` | 情報源ごとの可用性、更新頻度、取得成功率、根拠確認率、鮮度、著作権・利用規約、利用コストを履歴で評価。 |

Do not modify `prompts/`, `README.md`, `AGENTS.md`, existing category spec docs, `spec-index.md`, or any other ownership area. All source changes/contract schema edits must be cross-team proposals to responsible owners.

## Research depth
Primary targets: **OpenClaw/Deep Agents (task strategies), record linkage packages (e.g. Python recordlinkage, Splink), Keepa, X API, Langfuse benchmarks, source reputation techniques**. Do not stop at README/marketing. Locate actual **source files, classes/functions, relevant tests, changelogs, recent commits, issue constraints and licenses**; cite exact URLs, tested vs documented, version and observed date. Compare at least two meaningfully applicable implementations or algorithms; choose a design with safety, performance and portability rationale. Research both success and failure cases. Avoid copying license-incompatible code.

## Specific design imperatives
Opportunity Radarは特定商品Monitorとは異なり、候補空間自体を探索する。Entity Resolverは誤統合より未統合を優先。Trend Fusionは独立性・誤検出・データ疎性を扱い、売買の期待利益は決定的計算で示す。Source Reliabilityを真実の自動判定として扱わない。

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
First research and write **`opportunity-radar.md`** in full, then create `spec/chat-12-<module>` from the current main, commit only your owned doc and open a **Draft PR to main**. Do not push to main, auto-merge, or edit another owner's doc. In the PR body, include primary source-level evidence, tradeoffs, exact implementation decisions, risk/uncertainty, test plan and named cross-team proposals. Report the Draft PR URL and explicit blockers. If GitHub write access fails, output the complete MD and state that it is NOT committed. Work on other modules sequentially in later separate PRs.
