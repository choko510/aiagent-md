# Architecture Decision Register — Accepted Direction vs Proposed Contracts
> CHAT_00 **Draft** decision ledger. Status labels `PRODUCT_FIXED` mean the user-approved product boundary, not source-code verification. `PROPOSED` means individual owner PRs still require integration review. ADR entries here never change other owner's existing branch.
## Decision procedure and status
`PROPOSED→OWNER_REVIEW→CROSS_OWNER_ACCEPTED→LOCKED`; `EXPERIMENT` becomes accepted only after pinned tests. Each ADR has an owner, evidence, alternatives, rollback/trigger and test. Changing a `PRODUCT_FIXED` boundary requires explicit user agreement, not another Agent output.
## Decisions
| ADR | State | Decision and rationale | Alternative rejected/held | Owner / verification |
|---|---|---|---|---|
| ADR-001 | PRODUCT_FIXED | general research/monitoring Agent, no host server administration; extensible adapters | Hermes fork, code/OS admin Agent, scraping-only | CHAT_00/09; SC-05,08 |
| ADR-002 | PRODUCT_FIXED | Tool Gateway enforces schema, budget, authorization, approval, side effects | LLM direct system/Browser or arbitrary shell access | CHAT_03/09; SC-07,08 |
| ADR-003 | PROPOSED | durable Task/Run, immutable checkpoint, fence and outbox; atomic domain state/event append | stateless chat loops, blind side-effect retry | CHAT_01/02/10; PG/killpoint tests |
| ADR-004 | PROPOSED | one centrally owned Browser Worker; Playwright Chromium compatibility baseline, HTTP/API first | independent direct Page attachment from Agent, Lightpanda by default without compatibility | CHAT_04/09; SC-06,09,13 |
| ADR-005 | EXPERIMENT | Jev, Scrapling, Lightpanda as strictly scoped optional adapters based on feature/compat/license | mandatory untested third-party browser | CHAT_04/05; pinned source+benchmark |
| ADR-006 | PROPOSED | PostgreSQL as durable state/Event/Outbox; stream replay uses tenant delivery offset and scope signed cursor | task-local event counter for global stream, untrusted Redis offset | CHAT_01/08/10; SC-07 |
| ADR-007 | PROPOSED | same Run on crash + new lease; new Run only under nonterminal Task; terminal retry creates new Task | resurrect terminal Task or duplicate irreversible operation | CHAT_01/02/08; RETRY tests |
| ADR-008 | PROPOSED | wire full signed int64 via decimal string, in-process BigInt and DB BIGINT with validated overflow | JS Number for versions >2^53 | CHAT_01/10; boundary codec tests |
| ADR-009 | PRODUCT_FIXED | provider terms, credential authority, freshness and cost budget precede actual data assertions | CAPTCHA evasion or fabricated real-time availability | CHAT_05/09; SC-01,03,04 |
| ADR-010 | PROPOSED | source→entity/variant→risk/observation→Watch/Notification causal provenance; avoid false merges | merge by name alone or profit = sale − buy | CHAT_12/01/10; SC-01,10,14 |
| ADR-011 | PROPOSED | external named X actor triggers require stable provider ID, subscription generation, durable dedupe/cursor, disable barrier | cron-only with duplicate-run race or instant stream guarantee | CHAT_13/05/07/10; SC-11 |
| ADR-012 | PROPOSED | digest is decision layer over a single sender Outbox, unknown send remains unknown | dual senders, assume timeout means not sent | CHAT_07/13/10; SC-16 |
| ADR-013 | PROPOSED | cached Browser recipe is restricted to observed read/extraction and gateway effect guards | label read_only_click/fill as automatically safe | CHAT_03/04/09/14; SC-13 |
| ADR-014 | PROPOSED | user Feedback has scoped Memory Writer, undo/tombstone, cannot change authority | self-mutating runtime logic, overwriting facts | CHAT_06/14; SC-16 |
| ADR-015 | PROPOSED | untrusted file parser isolated no-network, raw→derived artifact + coordinates | model alone executing embedded PDF/Office content | CHAT_01/09/10/15; SC-15 |
| ADR-016 | PROPOSED | migration expand→backfill→switch→contract, pinned CI PostgreSQL+Browser+Docker+SSRF evidence | altering an already-applied migration or declaring smoke test production-safe | CHAT_10/11; SC-07,08 |
## Cross-team conflict register
| ID | Blocker | Required owner handshake | Closure evidence |
|---|---|---|---|
| DEC-P0-01 | X ExternalEvent receipt/Subscription/Cursor/Task dispatch | CHAT_01+05+07+09+10+13; API 08, QA11 | physical DDL, Event mapping, two-source duplicate/revoke/crash E2E |
| DEC-P0-02 | retailer/storefront identity absent across Entity/Feedback | CHAT_01+06+10+12+14 | canonical stable store identity, negative false-merge and Undo tests |
| DEC-P0-03 | digest membership/send UNKNOWN | CHAT_07+08+10+13 | single sender ledger + atomically claimed membership, ACK loss tests |
| DEC-P0-04 | multimodal locator/parser job & confinement | CHAT_01+03+09+10+15 | typed locator/shas/position, isolated parser cap/egress/cleanup tests |
| DEC-P1-01 | read-only labeled Browser actions can cause POST | CHAT_03+04+09+14 | evidence-backed side-effect guard or automatic replay limited to no-click read |
| DEC-P1-02 | Change baseline/provider source lineage not ratified | CHAT_01+07+10+12+13 | CAS baseline + source variant key + retention/error tests |
| DEC-P1-03 | direct Provider access entitlements/quotas | CHAT_05+09+12+13 | authorized provider contract smoke or explicit unsupported state |
| DEC-P1-04 | old Source conflict notes refer to superseded SQL | respective owner of each Draft PR | current HEAD reconciliation and superseded annotations |
## Locking and decision escalation
1. Core Owner publishes canonical proposed contract with schema version, precise examples, invariants, migration and negative tests.
2. Affected Owners review the HEAD and explicitly accept/reject or propose a compatible alternative; don't silently assume agreement.
3. DB Owner maps accepted model to FK/index/transaction and Migration; API/Tool/Watch Owners map interfaces; QA creates executable assertions.
4. Independent reviewer checks all latest HEADs; `LOCKED` only when P0/P1 in scope closed, with each unresolved external fact labeled; `VERIFIED` only after real code and tests.
5. No blanket merge of all 58PR; no automatic main push. If acceptance unavailable, mark `PROVISIONAL` rather than invent signer decisions.
## Sources and verification
Authoritative local: Project Brief, Architecture Rules, SC-01..16, full PR #1..58 audit report (audit branch). Prior art: [LangGraph checkpoints](https://docs.langchain.com/oss/javascript/langgraph/persistence), [Playwright locators](https://playwright.dev/docs/locators), [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html), [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html). Specific versions/source signatures still need owner review; citations here do not assert external implementations tested.
