# System Architecture — Runtime and Trust Boundaries
> Status: **Draft**. Owner CHAT_00. Every component below is a proposed logical responsibility, not a deployed service. Physical tables, protocol message types and approval logic belong to other Owner specs.
## 1. Process topology — separated VM trust domains (2026-11-07 P0 resolution)

**Production baseline:** Ubuntu Operator Host (KVM/QEMU) manages two **independently kernel-isolated** guests: **Trusted Control VM** and **Untrusted Execution VM**. Agent Orchestrator, policy/approval, PostgreSQL, Sentinel, Credential Vault and Broker are in the Trusted Control VM; **generated code executes only in the Untrusted Execution VM**. The guest kernel running arbitrary code is never the kernel running the Vault or DB. An optional separate Browser Worker sandbox/VM has its own identity and is not a Vault tenant. Operator host/VM-admin sockets are not accessible to any Agent.

~~~mermaid
flowchart TB
  OP[Ubuntu Operator Host / KVM manager]
  U[Authenticated Web / Telegram]
  subgraph T[Trusted Control VM - separate guest kernel]
    API[API / Auth] --> DB[(Task Run Ledger DB)]
    DB --> Q[Durable Queue / Scheduler]
    Q --> AR[Agent Orchestrator / Model Loop - no arbitrary code]
    AR --> GW[Tool Gateway / Intent / Approval / Effect Ledger]
    GW --> SENT[Sentinel final authorization]
    GW --> EXECADMIT[Execution Admission / Scoped RPC]
    GW --> BCTRL[Browser Broker controller - CDP policy owner]
    GW --> CONN[Privsep Connector / Notification Worker]
    GW --> FETCH[Trusted HTTP Fetch / Package Broker]
    CRED[Credential Vault / Broker] --> CONN
    CRED --> BCTRL
    SENT --> NET[Enforced Egress Transport / Network Gateway]
    CONN --> NET
    FETCH --> NET
  end
  subgraph X[Untrusted Execution VM - distinct guest kernel]
    SUP[Execution Supervisor / Kernel cgroups]
    CELL[Generated Shell / Python / Node / CLI]
    WS[Task Workspace overlay]
    SUP --> CELL
    CELL --> WS
  end
  subgraph B[Isolated Browser Worker domain]
    BW[Chromium sandbox / Contexts]
  end
  OP --> T
  OP --> X
  OP --> B
  U --> API
  EXECADMIT -->|authenticated execution grant| SUP
  CELL -->|only scoped broker RPC; no public network| GW
  BCTRL --> BW
  BW -->|forced mediated web transport| NET
  NET --> WEB[Approved external destinations]
~~~

**No** `AUTH --> INTERNET`, `Privsep --> INTERNET`, `Browser --> INTERNET`, `Cell --> INTERNET` or dual-homed egress bypass exists. Egress transport is the only component with an operator-controlled permitted Internet route; all others are firewall/namespace-denied direct route. Sentinel decisions and exact transport recipient/action are bound and checked by the same enforcement boundary. Trusting a token or proxy environment variable alone is insufficient. Credential Broker provides **recipient-bound just-in-time injection inside trusted Workers**, not secrets/surrogates to guest.

Local `sandbox.exec` is **offline** by default. Generated code may request a narrow `broker_rpc_only` IPC to trusted HTTP Fetch / Typed Connector / Package Broker through Gateway; **each concrete operation** creates its own durable Tool Intent / Side-effect Ledger entry, Sentinel permit/approval and a Transport receipt before actual network I/O. Arbitrary raw Internet sockets, HTTP CONNECT and opaque TLS tunnels from the execution guest are always denied. VM vsock/virtio serial/other IPC is restricted to a per-Execution authenticated endpoint, verified by server-side Run/Lease/Capability; source VM identity alone does not convey tool authority. See [Exec](../02-runtime/sandbox-execution.md), [Cell](../09-security/runtime-cell-isolation.md), [Sentinel](../09-security/sentinel-authority.md).

## 2. Logical components and ownership
| Component | Canonical owner | Exposes (proposal) | Must not do |
|---|---|---|---|
| Web/Telegram/API | CHAT_08 | Task/Watch/create/read/approve/stream DTO, auth-scoped cursor | directly mutate Run internals or contact Tool without Gateway |
| Auth/Secrets / Sentinel | CHAT_09 | authenticated principal, protected Vault, final network/connector permission and egress enforcement | raw secrets in Cell/prompt or LLM-editable policies |
| Agent Orchestrator (trusted) / Sandbox Exec (untrusted VM) | CHAT_02 | claim/recover/steer/cancel in Control VM; bounded code inside distinct Execution VM | untrusted code in trusted Orchestrator, privileged shell or direct Internet |
| Tool Gateway | CHAT_03 | manifest validate→risk/approval→durable attempt→dispatch | tool self-authorization, blind effect retry |
| Browser Broker | CHAT_04 | exclusive CDP/session lease, bounded page operations and screenshot evidence | direct CDP to Cell, concurrent cross-owner Page control |
| Provider layer | CHAT_05 | read/adapt Amazon/Keepa/X/hotel data with provenance | raw provider text as trusted instructions |
| Memory/Skills | CHAT_06 | scoped reversible Memory Writer and gated Skill promotion | mutate source Evidence or grant new Tool power |
| Scheduler/Watch/Outbox | CHAT_07 | occurrence, observation compare, delivery intent | new Agent runtime or provider-specific duplicate queue |
| DB/Queue/Deploy | CHAT_10 | migrations, row CAS, counters, transactional outbox | silently define contradictory business semantics |
| QA | CHAT_11 | Contract/Fault/Benchmark fixtures | mark unrun checks as PASS |
| Intelligence | CHAT_12 | entity matching, discovery, risk, trends, reliability | claim confidence equals factual guarantee |
| Event adapters | CHAT_13 | normalize external subscription and event routing, change diff, notification ranking | spawn hidden Task without owner auth |
| Learned replay | CHAT_14 | bounded recipe / feedback routing | replay side effects based on read_only label |
| Document parser | CHAT_15 | sandboxed extract and provenance | executing macros/network requests |
## 3. Durable execution flow
```mermaid
sequenceDiagram
 participant User
 participant API
 participant DB
 participant Worker
 participant Gateway
 participant External
 User->>API: Task command + Idempotency + Auth
 API->>DB: TX: Task + accepted event + outbox
 DB-->>User: TaskId + status
 DB->>Worker: Durable job hint
 Worker->>DB: Fence-safe claim Run
 Worker->>DB: Immutable checkpoint MODEL
 Worker->>Gateway: bounded Tool Intent
 Gateway->>DB: Intent + approval/attempt record
 Gateway->>External: guarded request AFTER preflight
 External-->>Gateway: receipt or UNKNOWN
 Gateway->>DB: transaction: result ledger/event/outbox
 Worker->>DB: checkpoint VERIFY and Goal result
 DB-->>User: Signed snapshot/SSE stream
```
A model can propose tools, including local Shell/Exec; the trust boundaries are the Gateway's persisted intent/side-effect ledger plus the independently enforcing Sentinel and Runtime Cell isolation. If provider sends a response but crashes before ACK, permanent external effect is **UNKNOWN**, not retried automatically.
## 4. Main state and ownership
- `Conversation` optional on Task (Watch/External Event can create Task without Conversation). `Task` holds user goal and its terminal state; `Run` is one attempt and its lease epoch; Worker recovery reclaims the same Run with new leaseEpoch; terminal Task retry creates a new Task linked to previous Task but never mutates history.
- `DomainEvent` aggregate sequence is per aggregate, `deliveryOffset` per tenant; durable event/outbox state and consistent projection snapshot precede a signed SSE cursor. Provider-originated Event and user-visible Domain Event are **different identities**.
- Same Step may require Model/Intent/Approval/Receipt checkpoints: immutable checkpointRevision with Run-head compare-and-swap. Only the current Run lease can commit a new head.
- `Observation` is measured fact; `Evidence` is the traceable extracted assertion; `Artifact` is raw/derived binary; `Memory` is editable user/context preference; `Skill` is a reviewed procedure. They may reference but never impersonate one another.
## 5. External event (X, email, webhook) pipeline
Provider signed delivery / authenticated poll → Connector `ExternalEvent` (untrusted payload + stable actor/provider IDs) → `ExternalEventReceipt` dedup + subscription generation/authoritative cursor in same DB TX → durable eligible Task dispatch intent → Actor/Policy/Source validation → Task and DomainEvent + outbox → Watch/Notification conditional send. Disabled/revoked subscriptions must produce zero *new* tasks after revocation; already-inflight external side effects remain in ledger. **PROVISIONAL owner acceptance:** CHAT_01, CHAT_05, CHAT_07, CHAT_09, CHAT_10, CHAT_13.
## 6. Entity/feedback/multimodal integration
Entity resolver never fuzzy-merges distinct SKU variants or storefronts; store/merchant identity and branch identity separate. Feedback about retailer exclusion is an explicit user preference with `scope`, canonical target, version and Undo tombstone, not a hard fact. Multimodal raw bytes undergo bounded isolated parsing, preserving transformation history and input byte/page/cell coordinates. Parser output never becomes trusted code or secret.
## 7. Threat boundaries & resource budgets
- Host/control plane: Agent has no arbitrary shell, SSH, Docker socket or unrestricted host mount. Worker container read-only, non-root, resource quota, explicit egress proxy and no internet sidecar bypass.
- Network: DNS and redirect re-evaluation, IP ranges blocked, browser resources/WebSockets/multipart and provider credentials scoped. Test full route (not just URL prevalidator).
- Provider: OAuth/scope, quota, rate, consent expiry, policy and price recency validated. Unauthorized API returns transparent limitations, not scraped bypass guarantees.
- Actions: irreversible purchase/reservation/post/payment require verified fresh approval bound to frozen parameters and current permissions; cache cannot bypass.
- Limits: finite Run steps, child Task count, token/currency budget, active browser tabs, file decompression ratio, external events/minute; automatic stop with inspectable error and retained evidence.
## 8. Data and transport contracts
HTTP endpoints and SSE are owned by CHAT_08, physical SQL by CHAT_10, versioned envelopes by CHAT_01. Sequence and event ordering proof require consistent MVCC projection and committed offset. Everything over wire for signed int64 uses canonical decimal strings; internal arithmetic BigInt; external amount in minor units BigInt/decimal.
## 9. Deployment and upgrade
Proposed TS packages: `api, runtime, gateway, browser-worker, connectors, automation, domain, memory, web, qa`; optional separate Python parser process, PostgreSQL. Docker Compose with network-guarded egress (CHAT_09) and non-root workers. No exact images/versions declared locked until license/API/health/sandbox tests pass. DB migrations expand→backfill→switch→contract with immutable history; no destructive overwrite of already applied migrations.
## 10. Failure protocol
| Fault | Detection / recovery | Mandatory invariant |
|---|---|---|
| API returns before Worker starts | durable Job/Task DB state | no lost accepted Task |
| Worker kill while model/Tool pending | latest immutable checkpoint + fence re-claim | zero double effect |
| Browser crashes/login expires | BrowserSession expired; wait for human/restart scoped context | no credential cross-use |
| Provider outage / 429 | finite backoff, budget accounted | unknown ≠ zero/current |
| Webhook replay / two pollers | unique subscription+provider event identity and generation | at most one accepted Task |
| Outbox relay ACK loss | persistent event ID and reconciliation | no false delivered state |
| DB restore/resume | migration checksum, checkpoint hash, tenant FK and owner gate | no stale writes |
| Parser malicious ZIP/Office | resource jail; fail closed and clean intermediate files | zero execution or uncontrolled network |
## 11. Release gates
Unit schema/codecs, real PostgreSQL FK/concurrent CAS/migration upgrade, Browser POST trap, cross-tenant authorization, Event snapshot→SSE gap, event trigger duplicate/revoke, external provider entitlements, isolated malicious file parser, Watch/Outbox UNKNOWN and budget enforcement. No `Verified` until code and tests exist.
## 12. ADR / cross-team decision pointers
[ADR decisions](architecture-decisions.md), [Glossary](domain-glossary.md), [Spec Index](spec-index.md). Owner-specific proposals remain Draft until their corresponding PR HEAD documents the resolved schema and cross-owner reviewer accepts.

## 13. Muse migration: ownership invariant and compatibility (2026-11-07)

- Existing Task/Run/Conversation/Watch, Evidence, Actor/tenant, Scheduler and approval identities remain canonical; Execution/Workspace are child resources and must not create duplicate Task state.
- For sandbox.exec: Agent proposal -> trusted Gateway persistent Intent -> execution Admission (server-owned identity/policy) -> offline Execution VM process -> scoped Tool RPC for each external action -> Gateway concrete Intent/Ledger -> Sentinel -> enforced Egress Transport -> recorded effect receipt. No shared guest kernel with Vault/DB and no direct Internet.
- Any current direct Gateway→External arrows represent a **logical invocation**, not a direct unfiltered socket route. Actual network/connector authority is Sentinel. All legacy no-arbitrary-code statements must be understood as host/protected-service prohibition, not Runtime Cell limitation.
- The shared transaction/approval contracts are Draft; no explicit implicit lock or VM implementation is implied by this revision. Full implementation requires OWNER reconciliation + TEST SC-17..22.
