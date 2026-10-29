# APPROVED EXPANSION CANDIDATES — Autonomous Discovery & Event-driven Intelligence

> 2026-10-30 user-selected **11 features**, to be researched and specified by CHAT_12–15. Adoption here means **product scope approval for investigation and design**, NOT an implementation-complete or evidence-verified design. Respect Project Brief and existing 74-module ownership. Version-sensitive facts require fresh independent verification in each spec.

## Goal
Make the universal personal agent not merely search on request, but (a) proactively discover relevant opportunities, (b) match and correlate entities across sources, (c) react to external events including named X accounts, (d) replay verified browser steps cheaply, (e) detect important changes, (f) learn preferences from explicit feedback, and (g) ingest non-HTML media. Do **not** replace generic task/runtime, tool gateway, watcher, notification outbox, model router, evidence store or base browser modules.

## Approved scope and canonical owners

| ID | Module | Path | What makes it distinct | Depends on / change proposal to |
|---|---|---|---|---|
| 12-01 | opportunity-radar | `docs/12-intelligence/opportunity-radar.md` | 候補の自律発掘、発掘戦略・探索範囲・重複抑制・収集コスト・候補評価・不確実性の扱い。特定の商品を登録しなくても新機会を見つける。 | 02, 05, 07, 12-02, 12-04 |
| 12-02 | entity-resolution | `docs/12-intelligence/entity-resolution.md` | 商品/ホテル/企業/人物のCross-source同一性照合。JAN・ASIN・EAN・型番・容量・住所など。誤統合を防ぐhuman reviewとversioned mapping。 | 01, 04, 05, 10 |
| 12-03 | trend-fusion | `docs/12-intelligence/trend-fusion.md` | X投稿・価格・買取相場・サイト変更などの複数signalを統合。時刻、サンプリング偏り、トレンドscore、偽陽性とsource correlation。 | 07, 12-02, 13, 05 |
| 12-04 | decision-risk-engine | `docs/12-intelligence/decision-risk-engine.md` | 利益見込み、クーポン、手数料、査定減額、在庫/価格変動、閾値、シナリオ分岐。計算はdeterministicで、不明値は推定として示す。 | 05, 12-02, 01 |
| 12-05 | source-reliability-profiles | `docs/12-intelligence/source-reliability-profiles.md` | 情報源ごとの可用性、更新頻度、取得成功率、根拠確認率、鮮度、著作権・利用規約、利用コストを履歴で評価。 | 04, 05, 11 |
| 13-01 | event-driven-agent | `docs/13-event-ingestion/event-driven-agent.md` | Webhooks/WebSocket/API streaming/pollingを共通Eventへ正規化、X特定アカウント投稿Trigger、署名/認可・重複・停止・バックプレッシャー・費用管理。 | 03, 05-x-research, 07, 09, 10, 08 |
| 13-02 | change-intelligence | `docs/13-event-ingestion/change-intelligence.md` | Webページのdiff、構造化フィールドの前後差分、ノイズ除外、snapshot retention、取得失敗/古い情報の区別。 | 04, 07, 01, 10 |
| 13-03 | notification-intelligence | `docs/13-event-ingestion/notification-intelligence.md` | 緊急度/価値に基づく通知優先順位とダイジェスト、静音時間、重複検知、ユーザー優先ルール、説明可能なランキング。 | 07, 08, 10 |
| 14-01 | learned-browser-actions | `docs/14-adaptive-experience/learned-browser-actions.md` | 成功したブラウザ操作を検証可能なRecipe/Selectorとして再利用し、DOM変化時は安全に無効化→再推論。セッション間の認証/権限漏洩禁止。 | 04, 03, 06, 09 |
| 14-02 | feedback-learning | `docs/14-adaptive-experience/feedback-learning.md` | 明示的ユーザーフィードバックの対象と範囲、好みの更新、記憶の誤推定・修正、ユーザールールと事実の分離、Undo・監査。 | 06, 01, 08, 09 |
| 15-01 | multimodal-ingestion | `docs/15-multimodal/multimodal-ingestion.md` | PDF/写真・チラシ・スクリーンショット/CSV/XLSX/Officeドキュメントの安全な解析・ページ/セル出典・構造化抽出・サイズ制限。 | 04, 01, 03, 09, 10 |

## Trigger UX example — named X account posts
User request: 「Xで@shop_accountが『買取増額』を投稿したら、その型番を調べて、Amazonの購入価格と比較し、予想利益3,000円以上なら通知。無関係なポストは無視。」
1. `EventSubscription` accepts connector=X, author/actor USER ID (handle to ID lookup; handle may change), eventKind=post_created, includes/excludes replies/reposts/quotes, keywords optional, verification and polling/stream preference, monitor budget and notification approval.
2. X adapter chooses an **authorized supported** provider/event source. Where supported: filtered stream with from:user rules and suitable entitlement, possibly streaming/webhook. Fallback: `GET /2/users/:id/tweets` with persisted cursor/since_id and poll interval. **Do not promise instant updates**; permission, plan, limits and cost to be validated.
3. Normalize to `ExternalEvent`: provider, providerEventId/postId, user scope, source timestamp, receivedAt, event type, content/URL as untrusted data, checkpoint/cursor, dedup key, delivery attempt. Auth/payload signature validated for webhooks; polling response validated from authenticated API.
4. Event Router verifies subscription policy, actor match and deduplicates deliveries. Text/post is untrusted; never treat its content as instructions or expand tool privileges.
5. Condition engine checks keywords/regex/structured extraction. If matching, dispatch a new Task or approved, budget-bound child Run using Tool Gateway. Associate result with the originating subscription and event.
6. Extract product IDs, resolve cross-site match, evaluate costs and buyback constraints. Uncertain matches must be labeled and not auto-notified as confirmed profit.
7. Notification Intelligence suppresses duplicates and applies user priority/quiet hours. Delivery via existing `notification-outbox`, never a new independent notification sender.
8. A disabled subscription or revoked OAuth must stop subsequent events. Support retries, dead-letter/replay, idempotent event handling, API outage fallback and observability.

### Other event examples
- User-designated Gmail sender emails a sale ⇒ interpret campaign data only, check product prices and notify if verified.
- A retailer updates a particular page ⇒ differential extraction and relevant change classification.
- An approved external service posts signed webhook ⇒ check associated watch/goal and launch new bounded research run.
- On a certain account X post without keywords ⇒ summarize it with permalink, or run a user-specified research recipe.

## Spec boundaries / ownership (DO NOT DUPLICATE)
- `CHAT_07` owns Watch, Scheduler, trigger-evaluation and Notification Outbox. CHAT_13 owns generalized **external event adapters/dispatch** and notification **ranking/digest policy**; never rewrite delivery idempotency.
- `CHAT_05` owns external provider/Keepa/X access and legal terms. CHAT_13 owns event normalization/dispatch; X API provider implementation and query syntax require joint agreement.
- `CHAT_01` owns canonical Event/Evidence/Entity/Observation schemas; new types MUST be proposed to owner with [PROVISIONAL].
- `CHAT_04` owns Browser Session/Action Engine; CHAT_14 adds **optional learned action replay layer**, not a second browser owner.
- `CHAT_06` owns general Memory/Skills; CHAT_14 designs explicit feedback learning while CHAT_06 owns canonical memory mutation.
- `CHAT_03` owns approval and side-effect policy; external posts never gain execute privileges.
- `CHAT_09` owns auth/secret/prompt-injection/SSRF policy; new providers conform.
- `CHAT_10` owns physical DB/outbox/job schema; new authors submit schema proposals only.
- `CHAT_08` owns UI/Telegram; new authors submit interfaces and messages only.

## Required proof before marking a specification Locked
- Verify Stagehand currently documented v4 versus older v3 API differences; Browserbase server-side cache is separate from local self-host functionality. Compare with Playwright deterministic locators and our existing Jev adapter.
- Verify X post stream, user timeline polling, X Activity/Webhooks endpoint eligibility, price, rate limits and conditions **from contemporary X docs/account plan**; no assumptions that every account trigger can be near-instant.
- Verify Composio trigger kinds, signed delivery, user connections, polling delays, replay and credential expiry using actual code/docs; compare self-built routing and provider-specific adapters.
- Verify Firecrawl monitor/change tracking diff capabilities, cost, data-retention and legal terms; design a self-hostable normalized-diff equivalent if needed.
- Compare entity resolution deterministic matching vs fuzzy/embedding/LLM adjudication; design false-merge penalties and negative evidence.
- For every new REQ-ID define TEST-ID and relevant scenario (SC-10..SC-16).

## Additional safety and performance rules
Every external subscription has a user-approved purpose, a disabling mechanism, rate/cost/daily event limits, secret/network scoping and an audit trail. Untrusted source content is data, not agent instruction. Automated actions may only perform pre-approved read/research/notify operations; purchases/reservations/posts remain approval-gated. Cache hit never overrides authorization, browser session ownership or outcome verification. Unknown value is not zero. No unknown service silently impersonates realtime confirmed data.

## Prior art — verify, don't trust as guarantee
- X filtered stream: https://docs.x.com/x-api/posts/filtered-stream/introduction
- X timeline: https://docs.x.com/x-api/posts/timelines/introduction
- Stagehand: https://github.com/browserbase/stagehand and https://www.stagehand.dev
- Stagehand v4 caching in managed Browserbase: https://www.browserbase.com/changelog/caching-configurable
- Composio triggers/webhook signing: https://docs.composio.dev/docs/triggers and https://docs.composio.dev/reference/api-reference/webhook-subscriptions
- Firecrawl Monitor/diff: https://www.firecrawl.dev/monitor
- Entity resolution: review modern open source record linkage implementations, product matching benchmark sources and address normalization libraries; do not presume one library is universal.

## Research and delivery
Research each module independently with `templates/MODULE_SPEC.md`: code, tests, types, precise failure handling, expected output, human validation, benchmark and cross-team proposals. New docs are **not yet created**, only this expansion scope and prompts exist. All work is Draft PR, one or two docs at a time, branch per CHAT_12–15, no direct main push, no auto-merge.
