# Round 5 — All 61 Draft PRs / 85 MD integration gate and execution audit
**Date:** 2026-10-30 JST  
**Repo:** [choko510/aiagent-md](https://github.com/choko510/aiagent-md)  
**Audit branch:** `audit/round4-runtime-validation`  
**Decision:** **SPEC-COVERAGE GATE PASSED; GLOBAL LOCK / AUTOMATIC MERGE BLOCKED**. This repository contains specification documents, not a running Agent Harness.

## Executive decision
- 61 open Draft PRs, numbers #1–#61, all targeting `main`; no PRs merged at the verified snapshot. Every one of the 85 planned module documents is present in precisely one Draft PR. Five CHAT_00 Foundation docs entered through PR #59–#61.
- The live-HEAD scanner fetched every document, recorded file/PR SHAs, found zero owner-path violations, no unsafe `number` version fields in its heuristics, **11/11 named cross-owner contract presence checks** passed, and no uncovered mandatory TEST keyword among the 85 docs. One 19-line Foundation README lacks a failure section by design; this is not a module-spec failure.
- Pinned upstream-source URL occurrence count is **909**, not 909 independently audited upstream code locations. Full semantic consistency and individual Owner acceptance are **NOT PROVEN** by a lexical scanner; 45 documents have historical contract phrases that must be read in context. No automatically detected current active historic conflict assertion in the Round 5 heuristic.
- The latest executed CI has **four green jobs** at [Actions run 40959257985](https://github.com/choko510/aiagent-md/actions/runs/40959257985): `postgres-ddl`, `docker-browser-fixtures`, `parser-negative-fixtures`, `full-spec-matrix`. Previous failed runs were fixed and are retained as audit history, not erased. This report records what was genuinely tested and what remains designed-only.

## Concrete changes delivered in owner PRs
| Scope | Owner PR(s) | Round 5 result |
|---|---|---|
| Foundation 5 docs / PRD / architecture / glossary / ADR | [#59](https://github.com/choko510/aiagent-md/pull/59), [#60](https://github.com/choko510/aiagent-md/pull/60), [#61](https://github.com/choko510/aiagent-md/pull/61) | All planned docs now drafted; no assertion of Owner signoff |
| X event subscription, generation ledger, receipt/cursor and Task | [#50](https://github.com/choko510/aiagent-md/pull/50), [#10](https://github.com/choko510/aiagent-md/pull/10) | Cross-transport stable event uniqueness, immutable generation FK, receipt→Task+GoalRevision synthetic concurrency |
| Entity distinction and retail exclusion | [#51](https://github.com/choko510/aiagent-md/pull/51), [#58](https://github.com/choko510/aiagent-md/pull/58), [#10](https://github.com/choko510/aiagent-md/pull/10) | `organization` versus `retailer_storefront`; ambiguous target remains review-only |
| Notification digest and source intent scope | [#32](https://github.com/choko510/aiagent-md/pull/32), [#57](https://github.com/choko510/aiagent-md/pull/57), [#10](https://github.com/choko510/aiagent-md/pull/10) | Per-intent owner+destination binding, matching composite FKs, partial active-member unique index, UNKNOWN prevents blind resend |
| Raw/derived provenance and parser jobs | [#33](https://github.com/choko510/aiagent-md/pull/33), [#54](https://github.com/choko510/aiagent-md/pull/54), [#10](https://github.com/choko510/aiagent-md/pull/10) | Typed locator/transform proposal, provenance FKs, resource/sandbox requirements; real restricted parser **reference** fixture added |
| Browser safe cached actions | [#52](https://github.com/choko510/aiagent-md/pull/52), [#19](https://github.com/choko510/aiagent-md/pull/19), [#20](https://github.com/choko510/aiagent-md/pull/20), [#41](https://github.com/choko510/aiagent-md/pull/41) | Unverified click/fill automatic replay disabled; browser POST/autosave trap reproduced |
| Change/Watch/Source/Radar/Risk/Trend/Memory | [#53](https://github.com/choko510/aiagent-md/pull/53), [#16](https://github.com/choko510/aiagent-md/pull/16), [#55](https://github.com/choko510/aiagent-md/pull/55), [#51](https://github.com/choko510/aiagent-md/pull/51), [#56](https://github.com/choko510/aiagent-md/pull/56), [#15](https://github.com/choko510/aiagent-md/pull/15), [#10](https://github.com/choko510/aiagent-md/pull/10) | Forward migration 0003 proposals: scoped baseline, source attempt, candidate/risk, trends, feedback mutation history |
| QA cross-module acceptance | [#13](https://github.com/choko510/aiagent-md/pull/13) | Distinguish executed SQL/reference fixtures from unexecuted Harness tests |

## Executed technical evidence — exact limits
| ID | Ran in CI | Verified claim | **Not** established |
|---|---|---|---|
| DB-R5-01 | real PostgreSQL 16 | 0001→0002→0003 SQL, FK/uniques, checksum lookup and skip/diff decision, 32 parallel Event sequence writers | production migration runner, rolling deploy, recovery from actual power loss, network isolation |
| DB-R5-02 | real PostgreSQL 16 | 24 concurrent synthetic external post deliveries create **one receipt + one Task + immutable Task Goal Revision**. Forced exception rolls back; revoked generation no new Task | full real Event Router→DomainEvent→Outbox→Watch→Notification, X API eligibility/signature |
| DB-R5-03 | real PostgreSQL 16 | same-Tenant different-user digest and wrong destination FK rejected; released member can re-reserve, UNKNOWN member prevents reroute | legitimate `owner_user_id` and `destination_ref` were established by authenticated human consent |
| BROW-R5-01 | real local Chromium | click on a visually read-only button and input auto-save each cause an actual POST to local fixture | Harness Browser egress firewall/SSRF denial. This is a **threat reproduction**, not prevention |
| PAR-R5-01 | actual restricted nonroot Docker | tiny ZIP/Office metadata reference checker rejects decompression bomb, path traversal, macro marker, external link; formula flagged as untrusted; network call fails | production Python Parser, actual PDF/OCR/XLSX engine, hostile PDF fonts, all parser CVEs |
| DOC-R5-01 | GitHub REST fetched live PR HEADs | 61 Drafts/85 MDs, unique owner path, 11 cross-contract reference gates, static patterns | Owner acceptance, implementation correctness or all 909 upstream refs semantically verified |

**Reference test files:** `audit/round4/run_ddl.py`, `audit/round4/expansion_integration.sql`, `audit/round4/expansion_v3_integration.sql`, `audit/round4/full_spec_audit.py`, `audit/round4/browser_post_trap.py`, `audit/round5/run_ingress_concurrency.py`, `audit/round5/parser_sandbox_fixture.py`. All tests used fake data, local servers and synthetic provider messages; no purchase/post/booking, real provider credentials or uncontrolled network crawling.

## Outstanding cross-owner approval gates (cannot self-certify)
| Gate | Severity | Canonical Owners | Exact acceptance before Locked |
|---|---|---|---|
| G-R5-01 Event to outbox consistency | P0 integration | CHAT_01/02/05/07/09/10/13 + QA11 | Real EventRouter signed/polled message → receipt → Task + GoalRev + DomainEvent + Outbox atomic transaction. Duplicate/race/revoke/ACK-loss/partial cursor test against an implemented worker |
| G-R5-02 Digest sender authority | P0 integration | CHAT_07/09/10/13 + API08 | Real Outbox sender establishes **authentic** owner+destination from Watch/intent consent; same-tenant cross-user negative, opt-out/send race and UNKNOWN reconciliation; populate ownership only by trusted writer, backfill old v1 rows or refuse digest |
| G-R5-03 Browser Tool Gateway egress | P0 runtime security | CHAT_03/04/09/14 | Real Browser container transport denies unauthorized POST/WS/ServiceWorker/DNS rebinding/redirect, no side-channel egress; interactive learned recipe stays feature-flag OFF before this proof |
| G-R5-04 Multimodal production parser | P0 runtime security | CHAT_01/03/09/10/15 | Actual pinned parser image processing malicious PDFs, ZIP/OLE/Office, OCR and source geometry; no-network, no embedded code, bounded extraction, cancelled job staging GC |
| G-R5-05 Memory Feedback Writer | P1 integration | CHAT_06/09/10/12/14 | Single authoritative active-preference projection + reversible tombstone and scope/CAS; actual cross-tenant user auth and stale mapping split/undo negative test |
| G-R5-06 Change/Trend/Source/Profit | P1 integration | CHAT_01/05/07/10/12/13 | True Evidence/Observation lineage and source versions, stale/partial sampling, full risk scenario exact currency/buyback conditions; no unverified profit or fabricated trend confidence |
| G-R5-07 Production migration/recovery | P0 release | CHAT_10/11 | Real upgrade runner with advisory lock/checksums, immutable history, controlled backfill/read-writer compatibility, failure injection, restart and rollback on applied version; synthetic checker not enough |
| G-R5-08 Provider entitlements and costs | P1 rollout | CHAT_05/09/12/13 | Authorized X/Keepa/Amazon/hotel provider credentials, API quota/ToS/privacy, current pricing, bounded cost; unavailable provider transparent fallback |
| G-R5-09 Full multiuser/API authorization | P0 runtime security | CHAT_01/03/08/09/10/11 | Browser/Tool/Artifact/Message/SSE/Watch ownership and API token isolation under real identity/session change, forbidden data never visible |
| G-R5-10 Owner review signatures | P0 spec-lock governance | CHAT_00–15 + coordinator | Review actual latest PR HEAD, approve canonical Type/Event/DDL/API/Policy, resolve incompatible Provisional contracts or document rejected alternative, require independent review; do not self-assign cross-owner acceptance |

P0 here denotes **merge or release gate**; it is not proof of an exploitable deployed defect (the app has not been built).

## Verified inventory: all open Draft PRs by Owner
| Owner | Exact PRs | MDs |
|---|---|---:|
| CHAT_00 | #59, #60, #61 | 5 |
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
| **Total** | **61 Draft PRs** | **85** |

See the [per-PR 61-row merge-gate matrix](pr_merge_gate_matrix_2026-10-30.md) for each exact HEAD SHA, owner and specific outstanding condition.

## Merge recommendation and implementation transition
**HOLD all 61 Draft PRs, no automatic main merge.** This is not a statement all specs are defective. Detailed sequencing after Owner signoff:
1. Foundation #59–61. Product boundary and ADR become reviewed authority, not just an author's opinion.
2. Logical shared contracts #2/#23/#33/#43 and authentication #9/#18/#41/#47, then physical PostgreSQL #10/#22/#26/#36/#45 after confirming schema and write compatibility. Rebase each against actual main; no overlapping contradictory branches.
3. Runtime/Checkpoint/Agent Loop CHAT_02, Tool Gateway/SideEffect CHAT_03 and Browser/Providers CHAT_04/05, with exact versioned contract E2E tests.
4. Watch/Notification CHAT_07, API/SSE CHAT_08, Memory CHAT_06, then intelligence/event/browser-learning/multimodal CHAT_12–15.
5. QA CHAT_11 independent integration and fault testing; must retain a portable fake-provider suite. Specific **real implementation repository** or deployable source needs to exist before naming anything `Implemented`/`Verified`.
A single file can be a merge candidate only after the individual owner reviewed it, its accepted dependencies are compatible on main, unresolved in-scope P0s are eliminated, and maintainer approves. Structural green checks are not merge approval.

## Outcome statement
Round 5 **documentation coverage + cross-owner presence-gate + SQL constraint/concurrency + reference browser/parser smoke** milestone is complete at Actions run [40959257985](https://github.com/choko510/aiagent-md/actions/runs/40959257985). The remaining work is **runtime feature implementation and signed contract reconciliation**, not an excuse to describe 85 Draft specs as production verified. Next milestone is a maintainers' spec-LOCK review (accepted contracts and individual PR readiness) followed by a separate Agent Runtime implementation test harness.
