# CHAT_12 — Autonomous Discovery, Entity Resolution & Decision Intelligence

## Absolute starting context — Round 2
You are the **CHAT_12 owner** for `https://github.com/choko510/aiagent-md`. User approved 11 new expansion features. They want a **general-purpose, self-hosted personal AI agent**, not a scraping-only, resale-only, coding or shell-operating bot. Read actually from latest `main`: `README.md`, `AGENTS.md`, `prompts/COMMON.md`, `docs/00-foundation/project-brief.md`, `docs/00-foundation/feature-expansion-2026-10.md`, `docs/00-foundation/architecture-rules.md`, `docs/00-foundation/acceptance-scenarios.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/research-protocol.md`, `docs/00-foundation/review-gates.md`, `templates/MODULE_SPEC.md` and THIS prompt. **Also MUST read `prompts/ROUND2_AUTHOR_CONTINUE.md`, `prompts/ROUND2_EXPANSION_AUTHOR.md` and `prompts/ROUND2_CONTRACT_RECONCILIATION.md` (contract conflicts).** Check latest linked contracts and Draft PR HEADs, not just main. If any file cannot be inspected, state that rather than guessing.

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

## 第2ラウンド専門ゲート — Intelligence / 5MD
**合格しないまま次のMDを形式的に埋めないこと。**
- `entity-resolution.md`：商品JAN/ASIN/EAN/型番/容量/色/世代/新品中古、ホテル住所・地理座標の**誤統合**を最優先の失敗として定義。Blocking rules → Candidate retrieval → deterministic matching → calibrated probabilities → human-review → canonical ID versioning → undo/split を比べる。誤マッチ率と未マッチ率を分けて評価。
- `opportunity-radar.md`：既存Watcherの「監視対象が既知」と区別。候補発見の探索状態、DiscoveryRun、budget/coverage、scoring/re-ranking、同一商品dedup、検証への昇格、監視への登録、営業上の規約制約、発掘0件を含むE2Eまで定義。
- `decision-risk-engine.md`：費用/税/送料/クーポン/獲得条件/買取減額/在庫/売却期限/不明条件の厳密な金額モデル。浮動小数で円の計算をしない。確定・条件付き・推定・不明を分け、利益保証と誤認させない。
- `trend-fusion.md`：X投稿増加と価格変化の相関は因果ではない。データ源相関、遅延・欠測、時系列アラインメント、サンプリング・異常値、閾値校正、ドリフトによる偽陽性を評価。
- `source-reliability-profiles.md`：サイトの提供条件、収集成功率/鮮度/過去の訂正、source reputationスコアと確証を区別。特定のサイトを無条件に信頼/拒否しない。
**作成優先案**：Entity Resolver → Decision & Risk → Opportunity Radar → Source Reliability → Trend Fusion。ただし既存PRがある場合は修正・重複回避を先に。どのファイルも全5件提出まで続ける。

## Round 2 execution contract and PR workflow (overrides old first-file-only stop)

**このチャットの最終担当ゴールは、上表に列挙した5件すべての仕様MDをDraft PRへ提出すること。最初の1件だけでは終了しない。**

1. 最新main＋関連する他担当Draft PRのHEADブランチ全文を実際に読み、`SpecID/path/main/PR/status/blocks/next`の担当台帳を最初に作る。初稿がすでにPRにある場合は自分の既存PRを監査/必要に応じ更新し、重複PRを出さない。
2. 各MDを独立にsource-level research。公式Docs・実装コード・テスト・Issue・PR・CHANGELOG・版・licenseまで調査。新規技術の費用/権限/互換性は「試験/確認した」か「未検証」かを区別。
3. `templates/MODULE_SPEC.md` と `review-gates.md` に従い、REQ-ID → TEST-ID → SC-ID、TypeScript/Schema、状態遷移、障害/競合/再開、Security、API/DB proposals、実装PR分割まで全項目記載。
4. 自分の担当ファイルのみ、`spec/chat-12-<module>`ブランチ（最新mainから）へコミット。1〜2MDにつき1つの**Draft PR**。PR本文にsource table、設計理由/非採用理由、未検証、契約提案、テスト設計と実行有無、未解決Blockersを書く。**main直push・他Owner編集・無断マージ禁止**。
5. 1件提出したら次の残MDへ進み、担当全件を終える。ツール/コンテキスト制約で終了する場合は、提出/未提出一覧、PR URLs、次に作るMD、依存する決定、引継ぎを正確に残す。非同期の継続を約束しない。
6. 既存契約（UUID、Task/Conversation、recordVersion、leaseEpoch、tenant/workspaceなど）に依存する箇所は各OWNERの正本を優先し、矛盾は`[PROVISIONAL: CHAT_XX]`とCross-team proposalで報告。未統合のまま`Locked`としない。

**終了報告は担当全件の状態を表で示す。** 単に最初のPR URLのみ示して「完了」としない。
