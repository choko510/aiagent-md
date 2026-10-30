# Domain Glossary — Terms That Must Not Be Interchanged
> CHAT_00 Draft glossary. Definitions describe roles; canonical TypeScript types, enum values and validation live in CHAT_01. No glossary entry overrides a source-of-truth contract.
| Term | Exact domain meaning | Invariant / does not mean | Owner |
|---|---|---|---|
| Tenant | isolation and tenant Event ordering boundary | not a user; tenant offset may contain hidden owner events | CHAT_09 / 01 |
| User | authenticated human principal | not arbitrary model actor | CHAT_09 |
| Workspace | collaboration/resource grouping within Tenant, with explicit membership | not a substitute for Tenant or Owner authentication | CHAT_09 |
| Conversation | human-facing thread of messages | not required for externally triggered Task | CHAT_01 |
| Thread | continuation branch/interaction context | not a new Task or Run | CHAT_01 |
| Task | one durable, authorized goal with success criteria and budget | terminal Task cannot get another Run; retry creates new Task lineage | CHAT_01 / 02 |
| Run | execution attempt of Task, with leaseEpoch | crash recovery retains RunId; a new Run is only a permitted retry under nonterminal Task | CHAT_01 / 02 |
| Step | bounded logical think→act→verify cycle | one step can contain multiple checkpoint revisions | CHAT_02 |
| Checkpoint | persisted immutable phase snapshot within a Run | not a mere model text log; Run head CAS is authoritative | CHAT_02 / 10 |
| Tool Intent | frozen parameters, identity, approval, version/goal context | not an already-executed side effect | CHAT_03 |
| Tool Attempt | actual guarded dispatch attempt | may yield sent/unknown even without receipt | CHAT_03 |
| Side-effect Ledger | durable record of intents/attempt outcomes | DB exactly-once does not guarantee provider effect exactly-once | CHAT_03 |
| Approval | explicit human authorization for a frozen high-risk intent | cache/old approval never expands privilege | CHAT_03 / 09 |
| BrowserSession | authenticated, owner-scoped browser context lifetime | not raw reusable Cookie across tenants | CHAT_04 |
| Page/Tab lease | exclusive Browser Worker operation ownership | not an unlimited self-dispatch authorization | CHAT_04 |
| Provider Connector | policy-compliant adapter for external API/site | not a universal entitlement or guaranteed freshness | CHAT_05 |
| Raw Artifact | immutable obtained bytes or data plus SHA/context | not a verified model claim | CHAT_01 / 10 |
| Derived Artifact | transformed raw data with method/provenance lineage | cannot replace source SHA and coordinates | CHAT_01 / 15 |
| Evidence | specific sourced assertion/measurement/provenance | not a generalized Memory/preference nor a LLM guarantee | CHAT_01 |
| Observation | timestamped measured value and source quality | failed fetch, missing or uncertain != numeric zero | CHAT_07 |
| Watch | durable condition and trigger policy associated with user/tenant | not an Agent Run; may emit Event without Task | CHAT_07 |
| Schedule Occurrence | one due time with stable idempotency key | not one Task per duplicate scheduler wake-up | CHAT_07 |
| Domain Event | immutable committed internal aggregate mutation and audience | not an untrusted external provider message | CHAT_01 |
| Aggregate Sequence | per tenant+type+aggregate ID monotonic number | not global or delivery order | CHAT_01 / 10 |
| Delivery Offset | tenant-level committed Event replay position | not per Task and not permission to view hidden events | CHAT_01 / 10 |
| External Event | untrusted external post/mail/webhook with stable provider identity | not internal Domain Event and never instructions | CHAT_13 |
| Subscription | explicit user-approved external source/event filter with generation/cursor | disabled subscription cannot dispatch Task | CHAT_13 / 07 |
| External Receipt | durable dedupe and dispatch evidence for provider event | does not guarantee a notification was delivered | CHAT_13 / 10 |
| Notification Intent | approved eligible message queued for sending | not delivered status | CHAT_07 / 13 |
| Delivery UNKNOWN | sender may have sent but lacks reliable acknowledgment | must not be shown as confirmed delivered | CHAT_07 |
| Digest | grouping of independent notification intents with coverage statuses | not an alternative sender without Outbox ownership | CHAT_13 / 07 |
| Entity | canonical source-independent object (product, hotel, business...) | not a weakly matched alias | CHAT_12 / 01 |
| Storefront | distinct selling branch/channel tied to an Organization | not necessarily same store/price as other branch | CHAT_12 |
| Entity Mapping Revision | versioned decision linking source IDs to canonical entity | split/undo must invalidate dependent cached inference | CHAT_12 |
| Opportunity Candidate | newly discovered possible finding requiring validation | not a guaranteed price/profit | CHAT_12 |
| Source Reliability | empirical collection quality/freshness/correctness profile | not truth nor permission to access provider | CHAT_12 |
| Trend Score | evidence-weighted statistical signal | correlation not causation or a prediction guarantee | CHAT_12 |
| Feedback | explicit user preference or correction, scoped and reversible | cannot rewrite original Evidence or auto-approve actions | CHAT_14 / 06 |
| Learned Action | condition-bound validated Browser procedure | read_only_click label alone never proves no side effect | CHAT_14 / 04 / 03 |
| Multimodal Extract | parsed fields and page/cell/bbox coordinates | untrusted data, not executable code | CHAT_15 |
| Cursor | signed, scope-bound replay position | doesn't grant access; reauthorize on read | CHAT_08 / 09 |
| Int64 Wire | canonical positive/nonnegative signed int64 decimal string | never a lossy JS number / uint64 / floating point | CHAT_01 |
## Mandatory ambiguity rules
Avoid `Session` without qualifying Browser/Provider/User; `Event` without Domain/External/Notification; `Owner` without Tenant/User/Resource; `Version` without record/goal/lease/checkpoint/schema; `Result` without Tool receipt/Goal verification/Notification receipt. Treat a Source post's `createdAt`, provider observation `observedAt`, Event `committedAt`, and notification `sentAt` as distinct times.
## Review tests
GLOSS-T01: human reads 20 mixed scenarios and maps each ID to only one canonical type; GLOSS-T02: raw X post does not become Domain Event without durable validated ingestion; GLOSS-T03: Task-less Watch accepted; GLOSS-T04: read-only cached browser action that auto-POSTs is rejected; GLOSS-T05: missing price never becomes zero; GLOSS-T06: external effect UNKNOWN not labeled delivered. Links to SC-01,02,07,08,11,13,16. Tests designed, not executed.
