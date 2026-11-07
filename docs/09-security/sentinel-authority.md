# Sentinel Authority — ネットワークと外部操作の唯一の許可主体

> Owner: CHAT_09 | Spec: 09-08 (proposed) | Status: Draft / PROVISIONAL | Updated: 2026-11-07 | Shared contracts CHAT_01/02/03/04/05/08/09/10/11 are not Locked. No product runtime or egress test has been executed.

## 0. Decision / trust boundary

**Sentinel is the independent final permission authority for all external connector actions and every concrete network egress** originating from Runtime Cell code, child processes, scheduled jobs, generated Skills, browser/network adapters, remote MCP and subagents. Unlike the Agent, Sentinel and policy store live outside the untrusted Cell. An Agent's request is a proposal, not an authorization. Sentinel cannot be replaced by an LLM instruction, MCP descriptor, generated program, model judge, Browser JS or operator-supplied user prompt.

Separation of duties:
- Tool Gateway (CHAT_03): freezes signed intent, schema, ownership, budget, current Run lease, approval ledger and side-effect bookkeeping. **Does not itself provide a network bypass**.
- Sentinel (CHAT_09): deterministic authorization, policy generation/epoch, egress target + network enforcement, final ALLOW / ASK / DENY for concrete operations, last-mile credential release authorization.
- Credential Broker (CHAT_09): alone holds actual secret material/keys and returns scoped surrogate or injects just-in-time after recipient verification. Agent, Cell, generated code, tool stdout and model receive no real secret.
- Connector Privsep Worker (CHAT_05/09): executes credential-capable integrations outside Cell, with per-service identity and credential allowlist; agent can request via Gateway only.
- Browser Broker (CHAT_04): sole CDP owner; screen/DOM/action API subject to Gateway/Sentinel and current owner lease; UI takeover is separate.
- Egress enforcement (CHAT_09/10): kernel/network namespace/firewall + compulsory proxy. Optional model classifiers help triage but are not policy authority.

Meta Muse's public security design is a reference model, not proof this self-hosted implementation is equivalent: https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse

## 1. Enforcement must cover actual sockets, not tool names

Untrusted runtime can create subprocesses and arbitrary user-space code; therefore instrumenting only HTTP client libraries or routing only registered Tools is insufficient. At admission: attach Cell identity (cgroup/UID/network namespace and signed task/lease metadata). At each outbound connection enforce allowlisted route through network controller; deny direct sockets, alternate namespaces, raw sockets, IPv4/IPv6, TCP/UDP/QUIC, DNS bypass, Unix/vsock escape, loopback, private/internal/meta ranges and CONNECT tunnels. Recheck DNS A/AAAA, SNI/cert, all redirects, proxy authentication, websocket/iframe/subresources, network interface changes and source attribution.

**TLS content limit**: Sentinel cannot claim to understand encrypted request paths, methods, bodies, exfiltrated content or side effects when it sees only L4 metadata. For such opaque traffic, do not mark it read-only. Require bounded trusted HTTP client/Connector that can inspect concrete destination+method+body, a client-side mediated grant with faithful final enforcement, or user approval for narrow explicit destination/operation. Unverifiable sensitive egress is denied; arbitrary public HTTPS is not a blanket safe allowlist. User-data taint classification is conservative: unless auditable enforcement exists, "clean" must not be inferred from a process's reported intent. eBPF LSM-based taint propagation is **experimental**, not a current capability.

A user-approved domain is not a blanket approval for POST, paid action, sending email, login, cookie exfiltration, or transferring user data. Every destination/method/action/scope is checked at dispatch and again before egress. No implicit approval from old Skill success, cron registrations, or Tool annotations.

## 2. Proposed permission model (provisional)

Every request has an immutable identity and at least: ownerUserId, tenantId, taskId, runId, goalRevision, leaseEpoch, executionId or trustedWorkerId, intentId, toolVersion, policyEpoch, actionClass, destination canonical origin, DNS/IP/port/protocol, method/path or explicit UNKNOWN, payloadDigest or UNKNOWN, dataSensitivity and credential scope, expiry, one-use/non-replay properties, user approval reference if applicable.

Expected state:
PROPOSED -> VALIDATED -> {ALLOW|ASK|DENY} -> GRANT_ISSUED -> STARTED -> {RECORDED|UNCERTAIN|DENIED_LATE}.
The final gateway/ledger owns authoritative external outcome and UNRECONCILED side effects. ALLOW means **permission**, not successful execution; a process may still fail or never run. An ASK must persist an owner-bound PendingApproval that user can inspect, approve or deny. Any policy/identity/clock/recipient uncertainty is DENY or WAIT, never relaxed AUTO_ALLOW. Grants bind action digest, owner/tenant/lease/version/TTL and cannot be re-used for another host, redirect, subprocess, session or body. Exception for idempotent read retries requires a new authorization check. Repeated safe reads may use narrowly specified policies with quotas and TTL.

Risk classes remain those from CHAT_03 Tool Approval & Policy: read, sensitive, write, irreversible. Shell/exec is **local compute** risk until its concrete operations reveal outbound or privileged effects. Actual GET with private user data, redirect, logged-in session, unsafe method, even browser fill/click can increase risk. Unknown side effects default to ASK or DENY, not "read". For approval: show exact target, method, class, data being shared at reasonable specificity, account, amount/limits, expiry, and data-source provenance. Post/transfer/payment/bookings require per-action explicit approval, with preserved Side-effect Ledger semantics.

## 3. Credential surrogation & irreversible operations

Vault master key and plaintext secrets never enter Cell, even as environment variables or temporary files. CredentialRef is opaque to model. Broker may produce short-lived scope-limited surrogate token; only Sentinel/Privsep may redeem it at the verified endpoint after checking current owner/task/action/budget/approval, receiver, policy version and expiry. No raw Authorization header, Cookie, refresh token or browser storageState is returned to Cell or included in trace. Scrub before LLM and analytics; prevent credential transfer across redirect/downgrade/cross-origin and ensure no shadow network path can transmit the surrogate to arbitrary destinations. A surrogate is still sensitive: redact from stdout/logs and revoke on Run cancel/restart.

Browser manual login happens in Broker-owned context so password/OTP never passes through Cell. Browser display/screenshot/DOM can still reveal personal data; scope and redact accordingly. Some websites cannot safely be automated with this separation: **explicitly report unsupported/human handoff**, do not bypass.

For external POST or other non-idempotent requests, Gateway must durably persist the intent and idempotency material BEFORE final Sentinel authorization/dispatch. Timeout between external acceptance and local commit gives UNCERTAIN; no blind retry. An agent-created curl command cannot substitute for a trusted connector to bypass this ledger.

## 4. Compatibility & ownership

CHAT_09 network-egress-and-ssrf remains the canonical URL/DNS/transport rules; this file is cross-route permission semantics. secrets-and-credentials remains Vault/Broker lifecycle owner. CHAT_03 retains Tool Gateway and Side-effect Ledger. Browser Worker (CHAT_04) retains Page/Context/CDP lease. Changes to these must preserve the single authorization chain, not invent parallel registries or duplicate approvals. All model/Tool/Skill data untrusted (INJ rules). Install/user/tenant admin policies are operator-controlled; Agent must not update Sentinel, policy rules, firewall, certificates, DNS, trusted-worker allowlists, Connector code or audit records.

## 5. Requirements

| ID | Requirement |
|---|---|
| SENT-001 | Sentinel is the sole final external connector/egress permission authority independent from Runtime Cell |
| SENT-002 | All process-originating direct/indirect egress paths are forced through an enforcing network boundary |
| SENT-003 | Every grant binds authenticated user/tenant/Run fence/intent/policy epoch/action/destination/expiry |
| SENT-004 | Concrete target validation includes resolved/final IP, IPv4/IPv6, DNS changes, SNI/cert, redirects and scope |
| SENT-005 | No false promise of inspecting encrypted content; opaque non-readonly egress is blocked/approval-gated |
| SENT-006 | Sensitive/irreversible operations require bound approval; no Tool/Skill/eval shortcut |
| SENT-007 | Vault secrets never appear in Cell/model/logs; surrogate replacement permitted only after exact receiver/policy check |
| SENT-008 | Connector Privsep and Browser Broker identities cannot swap credential scopes or bypass Sentinel |
| SENT-009 | External operation intent and grant/receipt ledger committed durably with state/uncertainty preserved |
| SENT-010 | Revoke/Cancel/Steer/rotation/lease loss invalidates grants before new network dispatch |
| SENT-011 | Unknown policy service/egress proxy/protocol/attribution/clock fails closed |
| SENT-012 | eBPF taint/HTTP L7 inspection never assumed available without independent functional evidence |
| SENT-013 | Operator-only policy changes, tenant isolation, audit logs and reviewable explanation codes |
| SENT-014 | Side-effect classification includes browser click/fill and arbitrary sandbox code at last-mile enforcement |
| SENT-015 | Ensure no raw direct path via local LAN, metadata, DNS, UDP, DoH, WebSocket, QUIC or tunneling |
| SENT-016 | Budget, bandwidth and rate ceilings apply per task, tenant, destination and provider |

## 6. Required E2E/security evidence

- SENT-T01: generated Python curl/Node/CLI tries socket/DoH/IPv6/UDP/QUIC/WebSocket proxy bypass, direct DNS and private IP; zero successful prohibited packets.
- SENT-T02: public DNS rebinds to private IP after preflight; final socket rejected.
- SENT-T03: TLS opaque arbitrary POST attempt; denied (or approved trusted connector with exact payload/target bound).
- SENT-T04: prompt injection in web/PDF/email asks for exfiltration; no unauthorized egress, no secret/token in model or stdout.
- SENT-T05: approval grant is replayed with new body, changed domain, different Run, revoked owner, stale lease; denied.
- SENT-T06: privilege boundary from cell→Sentinel admin/Vault/Browser CDP; denied by kernel and application.
- SENT-T07: crash between connector dispatch and persist receipt; UNKNOWN, no duplicate write.
- SENT-T08: simulate proxy/SSRF engine failure, IPv6-only host, hostname aliases and chained redirects; fail closed.
- SENT-T09: authorized read-only public request succeeds; approved sensitive request only follows exact bound scope.
- SENT-T10: raw credentials absent from agent prompt, artifacts, telemetry and task history; non-authorized users cannot view approvals.

No implementation or test was run while preparing this spec. Deployment/CI gates must pin Ubuntu kernel, isolation profile and enforceable ingress/egress configuration. Test failures block production readiness.
