# Product Requirements — Universal Personal Agent Harness
> Status: **Draft / owner CHAT_00 / 2026-10-30**. Canonical user intent: [Project Brief](project-brief.md), verification: [Acceptance Scenarios](acceptance-scenarios.md). These are design requirements, NOT evidence of implementation.
## 1. Mission and boundaries
A self-hosted Ubuntu/Docker **general-purpose research Agent Harness** which accepts a natural-language goal, plans, discovers tools, reads authorized sources, reasons, calculates, verifies evidence, and reports results. Supports unattended bounded background execution, condition monitoring and notifications. Not an agent for server administration, code execution on host, scraping as sole purpose, or autonomous buying, posting, reservation or transfers.
### Personas and scope
Owner is authenticated human; additional users are strictly isolated by tenant, workspace, resource ownership and capability. Operators have separate explicit auditable capabilities. All external/provider content, websites, emails, social posts, PDFs, images and Excel rows are **untrusted data**, never system instruction or capability.
### Non-goals
No arbitrary SSH/host shell or Docker socket/tool install; no evasion of site protection guarantees; no unlicensed data acquisition, unlimited spending or unapproved purchases/reservations/messages; no universal exactly-once external side effects. No implicit declaration of a price/stock/booking as real-time.
## 2. Verifiable priority-ordered requirements
| REQ | Level | Contract-level expected outcome | Tests / SC |
|---|---|---|---|
| PRD-001 | MUST | Accept a goal with constraints, budget/deadline, source/citation preference and idempotency key; persist Task before runtime dispatch | PRD-T01 / SC-01,04,05 |
| PRD-002 | MUST | Search/API/HTTP/Browser selection is automatic, capability-gated and cost-limited; Browser Session owned by Browser Worker, never by model | PRD-T02 / SC-05,06,09 |
| PRD-003 | MUST | Separate Conversation, Task, Run, ToolCall, Evidence, Artifact, Watch, Notification and Memory; no implicit coercion of Task-less event into dummy Task | PRD-T03 / SC-02,07,11 |
| PRD-004 | MUST | Every nontrivial conclusion preserves source, observedAt, gatheredAt, precision/conditions/unknown and falsifiable provenance; distinguish evidence from model opinion | PRD-T04 / SC-01,03,04,15 |
| PRD-005 | MUST | State and checkpoint survive process kill; uncertain external effect never blindly retried; pending approval/stop visible | PRD-T05 / SC-07 |
| PRD-006 | MUST | All tool calls pass Tool Registry → schema → policy → budget → approval → dispatcher, with monotonic version/fence/side-effect ledger | PRD-T06 / SC-07,08 |
| PRD-007 | MUST | Host OS/SSH/Docker socket/privileged/LAN/metadata endpoints denied; strict network egress for Browser/HTTP/MCP, no secrets to model/context/log | PRD-T07 / SC-06,08 |
| PRD-008 | MUST | Purchase, reservation, posting, payment or other irreversible side effects require explicit fresh user approval bound to immutable intent | PRD-T08 / SC-01,04,07 |
| PRD-009 | MUST | Watch has durable condition, time zone, next due time, authorization and active/paused/retired state; missing observation never treated as zero | PRD-T09 / SC-02,12 |
| PRD-010 | MUST | Notifications include evidence and event identity, avoid duplicate blind external sends and report UNKNOWN, enable opt-out and quiet hours | PRD-T10 / SC-02,16 |
| PRD-011 | MUST | X and other external events require authorized connector, stable actor/source ID, dedupe/cursor, disabled subscription guards, bounded Task dispatch | PRD-T11 / SC-03,11 |
| PRD-012 | MUST | Discovery explores previously unknown candidates, checks exact entity/variant, deterministic money and risk before claimed profit | PRD-T12 / SC-01,10,14 |
| PRD-013 | MUST | Cached browser actions replay only after fresh policy/effect validation; labeling a click or fill read-only does not certify absence of POST | PRD-T13 / SC-06,13 |
| PRD-014 | MUST | Ingest untrusted PDF, image, XLSX, CSV safely; map derived claims to raw SHA/page/cell/bbox and prevent macros/external links | PRD-T14 / SC-15 |
| PRD-015 | MUST | User feedback is explicit, reversible, scoped to owner/Task/Watch, and cannot rewrite evidence or change execution authority | PRD-T15 / SC-16 |
| PRD-016 | MUST | User can Pause/Resume/Cancel/Steer; revocation/Cancel wins before a new external dispatch boundary, already sent effects remain auditable | PRD-T16 / SC-07 |
| PRD-017 | MUST | Enforce finite model tokens, browser tabs/time, provider calls, external events/minute, job retries and daily monetary ceilings; expose usage | PRD-T17 / SC-09,11 |
| PRD-018 | SHOULD | Telegram supports a restricted subset of authenticated Web UI functions and shares the same persistent state | PRD-T18 / SC-02,05 |
| PRD-019 | MUST | Every module has testable REQ→TEST→SC, uncertainty/operational evidence and version-specific provider limitations | PRD-T19 / SC-01..16 |
## 3. User-visible results and definitions
- **Succeeded**: Goal Verifier evaluates all mandatory user success criteria against current evidence and recorded side-effect outcomes. A successful LLM response alone does not meet it.
- **Partial**: Some criteria met, other constraints explicitly failed or unverified; include blockers and known gaps.
- **Failed**: Goal was not achieved and safe retry or alternatives may be proposed, no invented facts.
- **UNKNOWN / reconciliation**: A delivery/tool effect may have occurred but no authoritative receipt exists; do not map to Failed, Succeeded or automatically re-send.
- **Waiting Approval/Input**: explicit human action required; activity must not silently proceed until revalidated.
- **Cancelled**: no new dispatch allowed after durable cancel guard. Already sent effects cannot be guaranteed reversed.
Output requires provenance URL/source ID, observedAt, evidence grade, condition, local TZ and precise uncertainty wherever applicable.
## 4. Feature acceptance matrix
| Journey | Source→decision→user observable result | Negative fixture |
|---|---|---|
| Product discovery | Search/Keepa/merchant source→identity→risk/amount→candidate list with timestamp | SKU capacity/color mismatch, unavailable voucher or outdated price never positive profit |
| Watch price | Durable Watch→measurement→condition→outbox→Telegram | failed scrape != price zero; unknown provider delivery != delivered |
| X actor event | Authorized poll/stream→receipt/cursor→Task once→report/notification | duplicate/repost, disabled account, stale cursor, changing handle |
| Hotel search | Provider/Web evidence→date/person/room/tax/cancellation→comparison | unknown availability isn't a confirmed room; booking never autonomous |
| Browser Login | isolated context→human take-over→resume→source | changed account scope, prompt injection and replay auto-POST denied |
| Document research | isolated parse→positioned facts→Evidence→Verifier | malicious ZIP, macro, formula-as-code, hidden sheet and unknown OCR handled |
## 5. Controls and measurable performance criteria (not fabricated targets)
Performance is measured on pinned hardware, model, provider quota, scenario dataset and browser mode; record median/p95 time, token spend, peak RSS, provider cost, evidence precision, Task completion and unintended effect count. **Do not invent current success percentages or guaranteed times**. Set budgets in user config with finite tested defaults before release, never unbounded exploration.
## 6. Implementation dependency and responsibility
Canonical types and events: CHAT_01. Runtime/checkpoint: 02. Tool and effect gate: 03. Browser: 04. Providers: 05. Memory: 06. Watch/Outbox: 07. UX/API: 08. Security: 09. DB/ops: 10. QA: 11. Expansion 12–15 supply domain algorithms but do not create parallel foundational owner. Product requirements in this document do not override security policy or a reviewed canonical contract.
## 7. Test and signoff gate
For every row PRD-Txx produce: setup/fixture→action→DB/HTTP/event/browser assertions→negative variant; pin source revisions and actor auth. Review Gates must pass before `Locked`, runtime E2E must execute before `Verified`. E.g. PRD-T11 simulates stream+poll same post and requires exactly one Task; PRD-T14 injects malicious Office formula and requires zero network/exec; PRD-T17 attempts to exceed provider budget and requires a no-dispatch error.
## 8. Open decisions
- Production provider entitlements (X/Amazon/Keepa/Hotels) cannot be confirmed without explicitly authorized credentials. Owner CHAT_05 / CHAT_09; show `provider_unavailable` fallback.
- Historical merge/refinement of old drafts needs governance; revision of these product requirements requires ADR and cross-team impact, not an individual tool author.
- Quantitative SLOs remain `NOT_SET` until benchmark baseline. Reviewer CHAT_11.
## Sources
Project Brief, Architecture Rules, SC-01..16 and extension file at main; representative OSS prior art to independently verify at implementation stage: [LangGraph durable execution](https://docs.langchain.com/oss/javascript/langgraph/durable-execution), [OpenAI Agents SDK](https://github.com/openai/openai-agents-python), [Playwright](https://playwright.dev/). These are architecture references, not evidence of our code running.
