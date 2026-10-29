# Round 4 / CHAT_01–15 全58 Draft PR・80仕様MD 横断監査（2026-10-30）

> Status: **INTEGRATION BLOCKED / NOT MERGE-READY AS A WHOLE**.
> GitHub PRs #1–#58 を各PRの最新HEADから取得した監査。全件Draft/open、merged=0。mainは機能仕様の正本ではない。
> Report kind: static code/spec review + limited actual execution tests. **This repo contains specifications, not an implemented Agent Harness.** Test contracts marked designed are not executed.

## Executive finding

- **Total scope**: CHAT_01–11 = 49 Draft PR / 69 MD; CHAT_12–15 = 9 new Draft PR / 11 MD → **58 Draft PR / 80 MD / 15 owner chats**.
- **Remaining planned**: CHAT_00 owns 5 Foundation MD (product-requirements, system-architecture, domain-glossary, architecture-decisions, foundation README). These 5 are not in the 58 inspected PRs. 85 planned total.
- Full live PR/MD scanner: [Actions run 40949778345](https://github.com/choko510/aiagent-md/actions/runs/40949778345), head audit@aa4590f1, green for all 3 jobs. Artifact `round4-58prs-80specs-matrix` includes PR/MD-level lines, source pins, findings. **58 PRs / 80 unique MD paths / 909 pinned code-reference occurrences / no MD lacking an identifiable failure or test discussion / zero exact unsafe version-number field patterns**. Reference occurrences ≠ 909 independently verified sources, and structural tests are not semantic proof.
- Static scanner found **48 MDs with patterns mentioning historical version/contract conflicts**. This is a heuristic count and **NOT 48 distinct active P0 problems**. Many are historic Draft notes; latest PR #10 already fixed UUIDv4 default, nullable Conversation and version initial 1.
- No PR merged or marked Locked. Do not claim full test verification or production readiness.

## Actual execution evidence and limitations

### Verified on GitHub Actions runner (real tests, not just declarations)

- Postgres 16 in `postgres:16`, extracted SQL fenced block directly from PR #10's `database-schema-and-migrations.md`: `psql -v ON_ERROR_STOP=1` exited 0; **30 tables** created; `domain_events`, `run_checkpoints`, two sequence counters, retention metadata exist; old `task_events` absent.
- Actual row/FK fixtures: two checkpoint revisions within one Step committed; Task-less Watch Event + Outbox event FK inserted; **cross-Run checkpoint head FK** and **cross-tenant Watch owner FK** rejected.
- **32 parallel PostgreSQL writers** using row-serialized allocation: 33 total Watch Event entries incl initial seed, deliveryOffset/aggregateSequence each distinct and contiguous 1..33 (no failures).
- Docker restricted Alpine container smoke: non-root UID, read-only root, no-network, cap-drop ALL, no-new-privileges, no Docker socket. Actual Chromium headless local HTTP navigation passed.
- Reference Python URL/IP/redirect mock policy rejected 12 negative SSRF cases, including private/loopback, DNS rebinding simulation and redirect-to-metadata; local protected listener observed zero requests.

**Important limits**: these are execution of the proposed DDL, synthetic SQL transaction fixtures and independent Docker/Chromium/reference-SSRF programs. They do **not** validate the unimplemented Harness runtime, real Tool Gateway, browser-owned network access, X API subscription, real credentials, Webhook signing integration, multi-tenant HTTP auth, full migration upgrade, dead-letter/outbox delivery or backup restoration. Initial DDL success does not establish safe `0001→0002` upgrade, rolling deployment or every FK/check under every mutation. [Execution logs](https://github.com/choko510/aiagent-md/actions/runs/40949778345).

## Highest-priority blockers — consolidated with canonical owners

| ID | Sev | Exact affected PRs / MDs | Verified issue / missing decision | Owner action / acceptance |
|---|---|---|---|---|
| R4-P0-01 | P0 | #50 `event-driven-agent`; #34 X; #11/#16 Watch; #10 DB; #22 Outbox; #23 Event | ExternalEventSubscription, ingress receipt, provider event cursor, generation, multiworker idempotency, privacy scope and bounded Task dispatch exist as CHAT_13 **proposals**, not accepted canonical DB/Watch schema. A named X account post → Task pipeline is not executable with PR #10's current 30 tables alone. | CHAT_01/05/07/09/10 adopt versioned ExternalEvent ingress/receipt/cursor and cross-transport identity, and CHAT_08/11 update interface/tests. Assert 2 streams+poll same post→one Task; revoke/late delivery→0 Task; crash before/after Tx no double dispatch. |
| R4-P0-02 | P0 | #51 Entity; #58 Feedback; #55 Radar; #56 Trend; #53 Change; #10 DB | CanonicalEntity mapping/version, derived Observation source/price identity, invalidation and physical tables not ratified. **Concrete mismatch**: #58 `Feedback.Target.entity class=retailer` but #51 resolver domain supports product/hotel_property/hotel_rate/organization/public_person, no `retailer/storefront` identity. Ambiguous exclusions risk excluding another branch/merchant. | CHAT_12 + CHAT_01 + CHAT_06/10 decide merchant/storefront vs organization and stable ID; introduce mapping rev, split/undo/invalidation and related FK. Negative fixture same chain/different seller & capacity/color must not merge. |
| R4-P0-03 | P0 | #57 Notification Intelligence; #32 Outbox; #10 DB; #08 API; #58 Feedback | Digest membership, per-intent coverage/UNKNOWN status, opt-out/send barrier and single-outbox sender ownership are marked **unresolved** in #57 (§8 and §16). Without agreed linearization, a covered member may be resent or shown as delivered when aggregate delivery is uncertain. | CHAT_07/10/08/09 accept aggregate-to-member ledger or disable digest and use delayed individual sends. Multiworker/ACK-loss/quiet-hours/revoke tests; no false delivered state. |
| R4-P0-04 | P0 | #54 Multimodal; #33 Evidence/Tool schema; #20 Gateway; #18 Sandbox; #26 Docker; #10 DB | Provenance locator extensions (PDF transform/OCR vs source pixels, CSV byte offsets, Office positions), raw→derived artifact lineage, parser job/cleanup and no-network parser supervisor are **only proposals**. No actual malicious-file parser jail has been executed. | CHAT_01/03/09/10/11 sign off typed locator adapter, isolated parser lifecycle, exact source SHA, retention and external-Vision consent, then test malicious ZIP/PDF/XLSX/network/macro/killpoints on fixed images. |
| R4-P0-05 | P0 | CHAT_00 missing Foundation 5; all 80 Draft | No submitted canonical Product Requirements/System Architecture/Glossary/ADR review; provider selection, domain authority and conflict resolution still rely on Draft cross-owner proposals. | CHAT_00 submit 5 foundation MD; independent COORDINATOR ADR decision log, exact target version and owner-approved contract handoffs. No bulk merge without this gate. |
| R4-P1-01 | P1 | #52 Learned Browser Actions; #19 Browser; #20 Tool Gateway; #31 Ledger; #09 Security | #52 defines `read_only_click` and `read_only_fill`. **Operation labels alone cannot prove absence of server-side effects**: DOM clicks/fills may trigger POST/XHR/autosave or navigation. Pre/post UI checks are not an effect sandbox. | CHAT_03/04/09 establish explicit allowed network method/effect class and deny writes during cached read-only replay, or restrict automatic replay to snapshot/navigation/read locators. Test a click that secretly issues POST and a fill that auto-saves: no unauthorized mutation / cached reuse. |
| R4-P1-02 | P1 | #52 lines around 44, #58 around 57, #56 cross-team list, several older MDs | Text still says UUIDv7 vs DB `gen_random_uuid()`, Conversation NULL vs NOT NULL, revision1 vs 0 are *current P0*. Latest PR #10 actually removed these incompatibilities. Stale assertions are misleading and risk future Codex reintroducing obsolete fixes. | Rewrite as historical/resolved with exact #10 HEAD. Independently recheck live owner/schema mapping for remaining unknowns. No stale P0 promoted to a blocker without reconfirming HEAD. |
| R4-P1-03 | P1 | #53 Change; #16 Watch Trigger; #01 Evidence; #10 DB | Baseline acceptance, scope/locale/cookie variant, changed vs 304/failed fetch and output freshness are locally designed; storage/Watch trigger and CAS not ratified. | CHAT_07/10/01 approve one baseline/decision atomic transaction, scope key, snapshot retention and meaningful field-diff semantics. No false change notifications from rotating ads or failed crawl. |
| R4-P1-04 | P1 | #55 Radar; #51 Risk/Entity; #05 Connectors; #07 Watch | Initial candidate-discovery slots, source quality and confidence/identity/risk data dependencies are proposals. A verified profitability claim cannot be made before source authorization + exact identity + buyback condition + stock availability/freshness have been demonstrated. | CHAT_12/05/07/11 formalize accepted Evidence/identity/Risk Receipt and Watch promotion. Verify exact BigInt money, unknown tax/fees/condition and partial provider coverage; report conditional not guaranteed profit. |
| R4-P1-05 | P1 | #56 Trend; #55 Source Profiles; #34 X; #50 Event; #10 DB | Independent upstream count, repost/quote original root ID, source sampling completeness, sampling interval drift and source profile revisions are not shared canonical event/observation fields. | CHAT_01/05/10/12/13 agree immutable observation lineage; fake high-repost-rate and stale/partial feed fixture must not create fabricated trend confidence. |
| R4-P1-06 | P1 | #58 Feedback; #04/#15/#30 Memory; #57 Notifications; #52 LBA | Explicit user consent, scope (Task/Watch/Project/User), Undo/revoke tombstone, feedback-vs-fact dispute and Memory Writer interface require cross-owner acceptance. Avoid a second Memory writer or using soft feedback as Tool privilege. | CHAT_06/09/10 approve typed reversible Memory feedback Port, unique idempotency/CAS, tombstone restore fence and real policy scope. No auto-mutate hard Watch rules or shared truth from feedback. |
| R4-P1-07 | P1 | #50 X ingest; #34 X Connector; #54 external vision; #55 Keepa | **Actual credentials/entitlements/cost/latency/ToS are not live-verified** in user's account. An X filtered stream's existence or a provider pricing page does not imply access to arbitrary account stream, instant delivery or stable prices. | Before production, authorized account-level entitlement and live contract smoke, exact budget caps and provider denial fallback; no high-volume API calls in uncontrolled test. |
| R4-P1-08 | P1 | #11 QA plus #50–58 | SC-10..16 cases have been described in additional authors' MDs but have **not** been executed against an implemented Harness. Existing fault fixtures only cover baseline PostgreSQL synthetic state. | CHAT_11 add cross-feature End-to-end: X→Receipt→Task→Entity/Risk→Notification, Data changes→Watch, cached-browser POST trap, Feedback Undo and multimodal malicious parsers with full trace/provenance; run in isolated fake-provider environment. |
| R4-P1-09 | P1 | #10 DB & #26 Docker & #41 Security | Postgres initial DDL and prototype SSRF policy tests pass, but actual **migration upgrade/downgrade**, Dockerized Gateway/browser egress and real anti-rebinding at boundary are not tested in Harness. | Implement migration runner with persisted checksums/expand-backfill-contract and real EgressGuard tests inside Browser/HTTP/MCP; verify on same pinned production images. |

### Notes on severity
P0 here means a **hard integration/Locked gate**, not proof of exploitable defect in deployed product; there is no deployed product. P1 means necessary correction or experiment before release and may be a merge blocker when the corresponding feature is enabled. Count unique findings by this table, not text patterns.

## New 11 feature-by-feature verdict

| Feature / PR | Draft quality | Final specification merge verdict |
|---|---|---|
| Opportunity Radar #55 | Good bounded exploration, receipts, cost/coverage and idempotency | HOLD until source entitlement + Entity/Risk/Watch handoff / DB reviewed |
| Entity Resolver #51 | Good blocking/hard veto, negative evidence, undo/split | HOLD until canonical entity/mapping revision and retailer identity |
| Trend Fusion #56 | Good sampling/bias/unknown/correlation disclaimers | HOLD until source lineage and aggregation contracts |
| Decision & Risk Engine #51 | Good deterministic BigInt and condition/fee assumptions | HOLD until price/condition Evidence adapters + correct entity binding |
| Source Reliability #55 | Good reliability-vs-truth distinction | HOLD until authoritative source profile and retention contracts |
| Event-driven Agent #50 | Detailed stable X actor IDs, stream/poll, receipt/cursor, generation, bounded limits | HOLD — P0 ExternalEvent DB/Watcher/Event integration |
| Change Intelligence #53 | Good typed semantic diff vs noisy HTML, snapshot scope/304 distinction | HOLD — baseline CAS and Watch/DB contract |
| Notification Intelligence #57 | Detailed required-rule priority, DST/quiet and digest status | HOLD — P0 digest membership/UNKNOWN semantics |
| Learned Browser Actions #52 | Strong account/DOM binding, consent, fail-closed replay | HOLD — read-only click/fill side-effect protection and existing Browser/Tool ownership |
| Feedback Learning #58 | Strong explicit intent, Undo and no auto-privilege | HOLD — retailer target + Memory Writer Port + tombstone contracts |
| Multimodal Ingestion #54 | Strong file/coordinate provenance, size/ZIP/parser limits | HOLD — parser sandbox and Evidence locator contracts, no actual parser test |

## Existing 69 MD review context

All 49 earlier PRs (#1–#49) were scanned from latest PR HEAD in this round. The old P0 Event/DDL mismatch was addressed in PR #10/#23/#17 and the proposed SQL now **passes PostgreSQL 16 DDL/FK/concurrent writer fixtures**. Additional revisions normalized int64 types, Run checkpoint head CAS, terminal Task retry, and QA tests. This supports improvement of the **draft** contracts, not that AgentLoop/Tool Gateway/SSE/prod security are implemented. Cross-owner acceptance is still required, especially DB/Generic Event/SSRFFilter/approval/Notification/Skills.

The static full matrix shows every MD has both failure and test discussion. Some files remain short (~149 lines); line count alone does not determine adequacy. Source-level verification here sampled **Stagehand v4 cache service, Splink blocking, Composio Triggers, changedetection.io diff and Tweepy timeline functions** at pinned refs: files/symbols confirmed. One ExcelJS file inspected exists but targeted token not found; this is not proof of compatibility. All 909 source-link occurrences have **not** been independently code-audited.

## Merge decision: **HOLD**, do not auto-merge the 58 Draft PRs

- No PR can be declared `Locked` solely from the green matrix or syntax/fixture tests.
- Dependency order: CHAT_00 Foundation/ADR → CHAT_01 canonical Event/Entity/Evidence and CHAT_09 auth → CHAT_10 DB/Outbox + CHAT_02 Runtime + CHAT_03 Tool → CHAT_07 Watch/Notification and CHAT_08 API/SSE → CHAT_04/05/06 integration → CHAT_12–15 capability add-ons → CHAT_11 contract/Fault/E2E.
- These are review/merge dependency *gates*, not a prohibition on independent documentation improvements. Each PR must be rebased/updated against actual current main before merge to avoid stale cross-references; preserve audit evidence.
- An isolated spec could become Ready after owner reviews and no unresolved dependency **specific to that spec**. The present audit is insufficient to recommend any automated merge.
- Required next outputs: (1) explicit owner-signed shared contracts and ADR; (2) changed MDs updated to accepted fields and SQL; (3) full integration fixtures incl. new features; (4) independent second audit on current PR HEAD; (5) human merge approval.

## PR ownership / coverage (all read at latest HEAD)

| Owner | PRs | MD count |
|---|---|---:|
| CHAT_01 | #2, #23, #33, #43 | 6 |
| CHAT_02 | #1, #21, #27, #35, #42, #46, #48 | 12 |
| CHAT_03 | #6, #20, #31, #40, #49 | 6 |
| CHAT_04 | #3, #12, #19, #28, #37 | 9 |
| CHAT_05 | #5, #14, #25, #34, #44 | 5 |
| CHAT_06 | #4, #15, #30 | 5 |
| CHAT_07 | #11, #16, #32 | 5 |
| CHAT_08 | #8, #17, #29, #39 | 6 |
| CHAT_09 | #9, #18, #41, #47 | 6 |
| CHAT_10 | #10, #22, #26, #36, #45 | 5 |
| CHAT_11 | #7, #13, #24, #38 | 4 |
| CHAT_12 | #51, #55, #56 | 5 |
| CHAT_13 | #50, #53, #57 | 3 |
| CHAT_14 | #52, #58 | 2 |
| CHAT_15 | #54 | 1 |
| **Total** | **58 Draft PR** | **80 MD** |

### Evidence links

- [58 PR / 80 MD matrix + actual tests — GitHub Actions run 40949778345](https://github.com/choko510/aiagent-md/actions/runs/40949778345)
- [DB source PR #10](https://github.com/choko510/aiagent-md/pull/10), [Canonical Event #23](https://github.com/choko510/aiagent-md/pull/23), [SSE #17](https://github.com/choko510/aiagent-md/pull/17)
- [X event subscription #50](https://github.com/choko510/aiagent-md/pull/50), [Entity & Risk #51](https://github.com/choko510/aiagent-md/pull/51), [Learned Browser #52](https://github.com/choko510/aiagent-md/pull/52), [Semantic Change #53](https://github.com/choko510/aiagent-md/pull/53), [Multimodal #54](https://github.com/choko510/aiagent-md/pull/54), [Radar/Source #55](https://github.com/choko510/aiagent-md/pull/55), [Trend #56](https://github.com/choko510/aiagent-md/pull/56), [Notification #57](https://github.com/choko510/aiagent-md/pull/57), [Feedback #58](https://github.com/choko510/aiagent-md/pull/58)
