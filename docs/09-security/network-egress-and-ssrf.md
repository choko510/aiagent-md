# Network Egress and SSRF — Detailed Implementation Specification

> Owner **CHAT_09**, ID **09-04** | Path `docs/09-security/network-egress-and-ssrf.md` | **Draft / [PROVISIONAL]**, v0.1 | Research **2026-10-30 JST** | main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Depends CHAT_03/04/05/08/09/10/11 and PR #9.
>
> This specification defines an application-side **non-bypassable egress policy**, not an implementation of an external fetch provider or network administrator tool. All tests below are **designed, not executed**; OSS code was read at pin SHA.

## 0. Conclusion

All untrusted-origin network activity of the Agent (HTTP/API fetch, redirect, Chromium navigation + subresources/WebSocket, uploaded-content URL ingestion, Python Sidecar, and remote MCP client) must flow through a centrally controlled **Egress Authority**. Its default is **deny**. The authority rejects local/private/special/metadata addresses, forbidden hostname forms, redirects/downgrades and DNS changes, even when the Tool Registry grants a read capability. **A preflight URL check alone is NOT sufficient**. Bind transport to a validated DNS result or an enforced egress proxy that performs resolution+validation, deny direct network escape at container firewall/network namespace, and re-evaluate every redirect/hop.

- LLM/Web/MCP never gains `allowPrivateNetwork`, proxy settings, arbitrary CONNECT destinations or service discovery authority. Operator-certified internal integrations are a different, out-of-band allowlist with separate adapter & trace (future ADR only), **not a general user-accessible bypass**.
- Browser Worker does not trust URL allowlist watchdog alone: service worker, iframe, preconnect, WebSocket, DNS and redirects can bypass page-navigation checks; network namespace and proxy must guard every socket. Browser User takeover inherits the same network restrictions.
- Credentials may be attached only after the destination and certificate/SNI have been evaluated by this authority. Redirect to different origin drops all secrets unless explicitly reauthorized by the Credential Broker.
- Enforce finite hops, bytes, DNS answers, wall time, concurrency, hostname length and per-Task/network quota. Unknown DNS / TLS / proxy configuration means **no request**.

## 1. Scope and threat model

Attack surfaces: user-submitted URL, crawler link from HTML or X, HTML redirect, JavaScript browser navigation, iframe/image/CSS fetch, HTTP/2 push-like behavior, websocket upgrade, automatic proxies, MCP-returned URL, PDF/Office embedded URL, credentialed connector, browser remote debugging connection. Targets: `127.0.0.1`, `::1`, private subnet, IPv6 ULA/link-local, mapped v4, noncanonical numeric IPv4, cloud metadata (including `169.254.169.254`), DNS rebinding, internal hostname/search suffix, unix socket/pipe, FTP/file/gopher/data/javascript, raw IP and alternate ports. Aim: no Agent-accessible internal network/host system control; no guaranteed bot bypass. SC-08 main, SC-06 Human Handoff, SC-07 recovery, SC-03/05 research, SC-11 external triggers, SC-13 learned actions, SC-15 document ingestion.

## 2. Actual OSS code/test comparison

| Source, ref, license | Inspected functions/tests | What it demonstrates; decision |
|---|---|---|
| OpenClaw `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, MIT | `src/infra/net/ssrf.ts` `isPrivateIpAddress`, `isBlockedHostnameOrIp`, `createPinnedLookup`, `resolvePinnedHostnameWithPolicy`; `src/infra/net/ssrf.test.ts` private/IPv6/IPv4 classification, pin; `fetch-guard.ssrf.test.ts` redirects, cross-origin header stripping, private DNS rebinding, proxy selection | **Code verified**: policy and pinned dispatcher pattern; tests include redirect to another private port, cross-origin secret removal. Some policy structures allow configured private exceptions, which are **prohibited here** unless an ADR-approved operator connector. [S1-S3] |
| browser-use `2cc71890e65056b67fdb29d98bb407749c15bb08`, license to be checked at adoption | `browser_use/browser/watchdogs/security_watchdog.py`: `SecurityWatchdog.on_NavigateToUrlEvent`, `on_NavigationCompleteEvent`, `_is_url_allowed`; `tests/ci/security/test_domain_filtering.py`, `test_ip_blocking.py` (including default disabled check, noncanonical numeric addresses) | **Code verified**: navigation-layer allowlist + IP blocking are useful defense-in-depth. **Not sufficient** as a network security boundary; test code explicitly shows IP blocking is opt-in by default, so require network-level enforcement independent of library config [S4-S6] |
| Docker network resource docs | bridge/internal networks, namespace isolation and limit capabilities | **Docs verified**, but direct container-policy implementation, remote proxy setup and DNS behavior require product deployment tests [S7] |
| OWASP SSRF Prevention | reject private addresses and protocols, URL resolution/allowlist, redirects | **Docs verified** and threat matrix, not code [S8] |

Alternatives: (A) regex URL validation then ordinary fetch => **reject** (DNS race/redirect/subrequests). (B) browser navigate watchdog only => **reject**. (C) network-mediated proxy + request-scoped DNS pinning + egress firewall + deterministic admission => **adopt**. (D) allow private network for all if operator has VPN => **reject** (VPN does not replace SSRF trust). (E) block all HTTP, accept HTTPS only => optional strict deployment policy; HTTP read may be needed for some public resources, but credentials never cross plaintext and redirect policy remains strict.

## 3. Trust boundary and canonical ownership

```text
LLM/External HTML/Doc/MCP -> Tool proposal (untrusted URL)
 -> CHAT_03 Tool Gateway (schema/policy/budget/approval)
 -> CHAT_09 EgressAuthority (URL parse + hostname/IP policy + target binding)
 -> Secured outbound transport / proxy (DNS resolution + pin + firewall)
 -> [HTTP adapter CHAT_04 | Connector CHAT_05 | Browser Worker CHAT_04 | Remote MCP CHAT_03]
 -> Content-scoped extraction with untrusted labels (CHAT_09 injection) -> Evidence store
```

`tenantId` and actual `workspaceId` never come from URL. Ownership resolver per PR #9 Round2 unresolved, `[PROVISIONAL: CHAT_01/10]`. Tool Gateway alone cannot permit arbitrary network or cloud metadata endpoint. Authentication via Credential Broker only AFTER egress admission; egress grant is not an auth session. Browser worker cannot access host network via different interface, Chromium WebSocket or startup network from container. Physical Docker network/iptables/nftables/host routing policy is CHAT_10; HTTP adapter is CHAT_04; MCP Tool adapter CHAT_03; Provider adapter CHAT_05.

## 4. Requirements

| REQ | Priority | Verification target | SC |
|---|---|---|---|
| EGR-001 | MUST | HTTP/Browser/MCP/Compute traffic uses one non-bypassable egress enforcement point | 06,08 |
| EGR-002 | MUST | hostname/IP policy denies loopback, RFC1918, ULA, link-local, metadata, multicast, unspecified, special & mapped address | 08 |
| EGR-003 | MUST | weird IPv4 representations and IDN/Unicode tricks normalized before match; malformed URLs fail closed | 08 |
| EGR-004 | MUST | resolve all A/AAAA and verify allowed addresses; pin actual socket or proxy resolution per hop; rebind blocked | 08 |
| EGR-005 | MUST | each HTTP redirect gets new URL+DNS+scheme+credentials policy; finite 5 redirects default | 05,08 |
| EGR-006 | MUST | Browser subresources/iframe/WebSocket/service-worker/downloads are subject to network-level blocking | 06,08,13 |
| EGR-007 | MUST | credential forwarding across origins/protocol downgrade prohibited unless separately scoped/authorized | 06,08 |
| EGR-008 | MUST | file://, gopher://, ftp://, data://, javascript:, unix:// and arbitrary raw sockets denied for network adapter | 08,15 |
| EGR-009 | MUST | user/LLM/MCP cannot alter allowlist/proxy/DNS/transport or introduce private-network opt-in | 08 |
| EGR-010 | MUST | tenant/task/intent/grant and destination+scheme are bound and rechecked on retries/restart | 07,08 |
| EGR-011 | MUST | connection count/bytes/redirects/DNS answers/timeouts/cost bounded, no infinite fetch | 07,09 |
| EGR-012 | MUST | DNS errors, proxy missing, TLS invalid, denied special address are distinguishable safe errors | 08 |
| EGR-013 | MUST | internal hostname/IP/query/secret information omitted from audit and user-facing errors | 08 |
| EGR-014 | MUST | attempted egress via direct container socket or host proxy escape fails without data transmission | 08 |
| EGR-015 | MUST | provider-specific allowlist and site policy evaluated on every hop, not only original request | 03,05,08 |
| EGR-016 | SHOULD | domain/IP failure ratios and policy versions measured without high-cardinality URL logging | 09 |
| EGR-017 | EXPERIMENT | independent proxy vs OS firewall vs pinned Undici plus namespace compared for compatibility and latency | 09 |

## 5. Contract and validation

```ts
type EgressKind = "http_read"|"connector"|"browser"|"mcp_remote"|"compute_fetch";
type EgressRequest = Readonly<{taskId:string;runId:string;intentId:string;ownerUserId:string;tenantId:string;
 goalRevision:number;runLeaseEpochDecimal:string;purpose:EgressKind;canonicalUrl:string;method:"GET"|"HEAD"|"POST";
  expectedOrigin?:string;maxBytes:number;timeoutMs:number;maxRedirects:number}>;
type EgressDecision = Readonly<{grantId:string; policyVersion:string; origin:string; hostname:string;
  allowedAddresses:ReadonlyArray<{address:string;family:4|6}>; expiresAt:string; bindingHash:string}>;
interface EgressAuthority {
  evaluate(req:EgressRequest, serverResolvedContext:ValidatedOperationContext):Promise<EgressDecision>;
  fetchBound(grant:EgressDecision, immutableRequest:SanitizedRequest):Promise<RedactedResponse>;
  revoke(grantId:string):Promise<void>;
}
```

**URL canonicalization steps (deterministic)**: parse with standards-compliant WHATWG URL (no string substring regexp as sole parser) → only `http:`/`https:` → no `username:password@` or fragment → reject embedded control chars, invalid Unicode/percent escapes, noncanonical IPv4 and ambiguous host encodings (or canonicalize and compare to block list before any DNS) → IDNA ASCII/punycode hostname normalization with trailing dot removed consistently → enforce max hostname and label lengths → detect host as IP or resolve A+AAAA under network policy → block ANY forbidden resolved address (do not select a public answer while ignoring blocked answer) → compare provider origin policy -> bind to actual TCP destination and TLS SNI/Host name → start limited request. Bypass via raw `localhost.`, `0x7f000001`, IPv4-mapped IPv6, zone IDs or DNS rebinding must fail.

**IP policy**: loopback, private, link-local, ULA, unspecified, multicast, cloud metadata, benchmark/reserved/special use and non-global addresses rejected. Default rule: **globally routable addresses only**; IPv6 scope/zone/embedded v4 must normalize and re-classify. DNS CNAME chain may contain benign names but final all-address set must be globally routable. Resolver must not trust system proxy `HTTP_PROXY`/`NO_PROXY` from Agent environment.

**Transport guarantee**: if IP-pinning cannot be achieved with a particular HTTP/TLS library/proxy, deny that execution mode rather than falling back to unpinned fetch; in proxied operation the proxy itself must perform DNS lookup, IP checks and SNI validation. For browser, controlled proxy + container firewall is required so browser-level DNS/QUIC/WebSocket/subresource cannot bypass. QUIC/UDP and WebRTC external routes must be blocked unless independently mediated and reviewed. DNS query path (including DoH) also constrained.

**Provider access**: configurable public internet origin allowlist for OAuth/SMTP/API endpoints, independent from user-entered URLs. All credential-bearing outbound requests require exact expected origin & TLS. Inbound webhooks may originate from public hosts but **must not** create outbound private-network permissions. Special operator-certified on-prem connectors are LATER separate service with explicit audited policy, and not exposed as arbitrary URL fetch.

## 6. State machine / sequence

```text
REQUESTED -> URL_VALIDATED -> DNS_RESOLVED -> POLICY_PINNED -> DISPATCHED -> RESPONDED
                  |                |                |               |
                  v                v                v               v
              DENIED_URL       DENIED_IP        DENIED_POLICY     TIMEOUT/UNKNOWN
DISPATCHED --redirect--> REQUESTED (same intent, hop+1, drop credentials and recalculate)
DISPATCHED --cancel/revoke--> ABORTED (provider may still observe bytes already sent)
```

Normal fetch sequence:
1. Gateway commits read intent and obtains authenticated owner-bound action grant.
2. Egress validates strict URL scheme, host, ports, method and configured provider allowlist; client-supplied owner/proxy headers ignored.
3. Resolve ALL DNS addresses, ensure every candidate is permitted, build pinned transport or authenticated egress proxy route with policyVersion/intentHash/expiry.
4. At connect and TLS negotiation validate actual remote peer IP, hostname certificate and SNI. Secret Broker injects only if exact bound origin authorized.
5. For redirect, cancel/consume response body safely within cap; clear Authorization, Cookie, Proxy-Authorization, credential-bearing custom headers; restart 1–4 for new URL; deny HTTPS→HTTP credential downgrade.
6. Streaming response enforces decompressed and compressed byte ceilings, content type, wall deadline, redirect cap. Record sanitized sourceURL/observedAt and policy decision. Source text remains untrusted.
7. Browser uses proxy for navigation and *all requests*; Worker must be provisioned in network namespace without alternative direct route.

## 7. Algorithms / failure & retry pseudocode

```ts
async function fetchWithPolicy(intent, originalUrl) {
  const ctx=await gateway.validateCommittedIntentCurrentOwnerRevisionLease(intent);
  let url=originalUrl, previousOrigin=null;
  for (let hop=0; hop<=POLICY.maxRedirects; hop++) {
    await gateway.assertNotCancelledOrRevoked(ctx);
    const safe=canonicalizeStrictHttpUrl(url); // block userinfo/unsafe schemes/private IP literals
    const addresses=await dns.lookupAllWithDeadline(safe.hostname);
    if (!addresses.length || addresses.some(a=>!isGloballyRoutable(a))) throw EgressDenied();
    const origin=canonicalOrigin(safe);
    if (!policy.permits(ctx,origin,safe.method)) throw EgressDenied();
    const conn=await transport.bindAndAttest({hostname:safe.hostname,allowedIps:addresses,
      tlsSni:safe.hostname, deadline:ctx.deadline,policyVersion:ctx.policyVersion});
    if (!conn.enforced) throw UnsafeTransport();
    const headers=previousOrigin && previousOrigin!==origin ? stripCredentials(intent.headers) : intent.headers;
    const response=await conn.sendBounded({url:safe,headers,maxBytes:ctx.maxBytes,redirect:"manual"});
    if (!isRedirect(response)) return projectSanitizedResult(response);
    if (hop===POLICY.maxRedirects) throw TooManyRedirects();
    previousOrigin=origin; url=resolveRedirectLocationStrict(safe,response.headers.location);
  }
}
```

No automatic retry after request may have caused side effect; GET/HEAD only may retry if policy and budget permit. `POST` allowed only for approved connector endpoint within CHAT_03 effect ledger and cannot retry blindly after ACK loss. If DNS returns both allowed+blocked, fail entire request; never silently fallback to a different family without a new approved binding. Expired grant: no transport; revalidate against Task/Run owner and lease before new grant.

## 8. Failure matrix

| Fault | Detection → result → recovery |
|---|---|
| DNS NXDOMAIN/timeout | `EGRESS_DNS_UNAVAILABLE`; max bounded retries; no fallback to cached unsafe IP |
| mixed public/private A/AAAA | `EGRESS_PRIVATE_ADDRESS`; no socket even if first result public |
| DNS changes after policy pin | peer mismatch/connection setup mismatch; deny & audit, not fallback |
| 301/307 to internal/metadata/localhost | new hop validation denies, no credential forwarding |
| HTTPS→HTTP redirect | deny credentialed request, optional uncredentialed public read only with new policy |
| proxy environment variable injection | proxy ignored/rejected, safe proxy config from operator only |
| Browser iframe/websocket/subresource goes local | network-level proxy/firewall denies; no internal data |
| Browser QUIC/DoH/UDP bypass attempt | egress namespace blocks alternative socket path |
| malware/hijacked MCP tool returns host URL | content remains data, no effective grant expansion |
| worker killed/replayed old authorization | old leaseEpoch/grant invalid; no external dispatch |
| TLS cert/hostname mismatch | deny without ignoring TLS errors |
| oversized gzip/page/chunked response | abort and persist explicit truncated/failed state, no fabricated facts |
| unknown post-effect HTTP result | UNKNOWN ledger; no automatic external mutation replay |
| proxy down / policy service unavailable | `EGRESS_POLICY_UNAVAILABLE` fail closed, may retry only safe read |
| quota exhausted | `EGRESS_BUDGET_EXHAUSTED` and partial state, stop new requests |

## 9. Security/privacy/research accuracy

- HTTP API and Browser, MCP remote server, URL preview/OEmbed, image downloader, PDF external references and connectors all use the same class of policy. If a connector uses custom SDK opening sockets, it cannot be used until proxied, wrapped or explicitly sandbox-reviewed; never assume SDK respects `fetch` wrappers.
- Authenticated browser page content may contain private information; owner and secret broker scope apply in addition to network controls. User Takeover is not an egress exception.
- Prompt injection cannot ask tool to access localhost or send API key externally. SSRF guard enforces at transport, not by LLM refusal.
- Logs store policyVersion, sanitized hostname or stable hash, decision code, intentId correlation, hop count and bytes only. Strip query, path where sensitive, username, cookies, signed URLs; audit cannot become another secret leak.
- Evidence source URL can be private and signed; present safe projection to user and secure original under owner-bound Artifact. Unsupported external URL state = UNKNOWN or BLOCKED, never record as confirmed fetch.

## 10. Observability & finite budgets

Per-host/tenant request count, DNS resolution histogram, denied/private attempts, redirect count, bytes compressed/uncompressed, response time p50/p95, connection pool count, browser proxy bypass attempts; no high-cardinality full URL metrics. Proposal defaults to be benchmarked: redirect limit 5, max 8 DNS answers/lookup and 2s DNS deadline, 15s HTTP deadline per hop, 10MiB decompressed body/read, 20MiB per task, 8 parallel read requests/owner, 2 Browser contexts/owner, 4 remote MCP connections/owner, 1000 egress attempts/day per tenant. These are **initial bounds, not measured SLO**. Any `0/unlimited` config rejected. Rate and terms from providers may impose tighter caps; Policy uses min of all limits.

## 11. Persistence / migration [PROVISIONAL: CHAT_10]

Store versioned EgressPolicy metadata and destination grants, expiration, per-tenant quota counters, audit decisions and job-level usage (not raw token). Do not persist DNS pin blindly across resume; renew policy+resolve for every new connection. Provider allowlist edits are operator-authenticated and versioned, immediately invalidate old grants. DB membership/tenant/workspace mapping from PR #9, P0. Migration from unguarded fetch must set default deny and fail startup if any adapter lacks configured egress path. No old internet allowlist automatically inherited without review.

## 12. UI / API integration [PROVISIONAL: CHAT_08]

Expose safe codes `EGRESS_DENIED_PRIVATE`, `EGRESS_DNS_FAILED`, `EGRESS_TLS_FAILED`, `EGRESS_RATE_LIMIT`, `EGRESS_POLICY_UNAVAILABLE`, `EGRESS_TRUNCATED`. User can edit permitted public research targets but cannot permit private network, raw IP or weaken infrastructure policy. Approval UI for side effect shows the actual canonical origin and redirect boundary; permission to browse one domain never authorizes login credentials on another. Report partial research and unavailable source instead of claiming complete verification.

## 13. Implementation PR sequence

1. **EGR-PR1** strict URL canonicalizer, IP/hostname classifier, all-negative fixtures.
2. **EGR-PR2** DNS all-answers check + pinned Undici/proxy transport, redirect cross-origin/auth handling, response limits.
3. **EGR-PR3** Browser Worker proxy/network isolation and QUIC/WebSocket/subresources egress tests.
4. **EGR-PR4** MCP/Sidecar/Connector SDK transport integration + security audit/tenant quota.
5. **EGR-PR5** chaos/fault/rebind/restart/bench, block release until no bypass paths.

## 14. Tests — only designed, not executed

| Test | Setup / Action | Expected |
|---|---|---|
| EGR-T01 | registry lists fetch, browser, MCP, compute adapters; attempt direct socket | all egress mediated, unauthorized socket bytes 0 |
| EGR-T02 | try loopback, RFC1918, 169.254.169.254, ::1, fc00::, link local, mapped IPv4 | rejected before connect |
| EGR-T03 | Unicode/IDN, trailing dot, userinfo, 0x7f000001, 2130706433, octal IPv4, zone ID | canonical safe or denied; no private peer |
| EGR-T04 | DNS public at validation then private on connect / AAAAA mixed answers | pinned peer only, mixed answers denied; no internal bytes |
| EGR-T05 | chain of 301/302/307 including 5th hop to private/HTTP downgrade | per-hop revalidate; no leaked headers; hop cap |
| EGR-T06 | Chromium page triggers iframe/CSS/image/WS/service worker/fetch to private address | network firewall/proxy blocks every request |
| EGR-T07 | redirect from allowed domain to attacker origin with Auth/Cookie | headers stripped, credential origin scope deny |
| EGR-T08 | non-HTTP schemes, malformed host or file path URL | no network or local filesystem access |
| EGR-T09 | MCP/content attempts set allowPrivateNetwork/proxy env/config | denied; policyVersion unchanged |
| EGR-T10 | Worker kill after grant, owner disabled, leaseEpoch changed | stale dispatch 0, new grant revalidates |
| EGR-T11 | extreme redirect/stream/gzip bomb/dangling DNS/timeout | request bounded, no unbounded loops |
| EGR-T12 | unavailable proxy, expired TLS, connection mismatch | explicit failure code with 0 unsafe fallback |
| EGR-T13 | secrets in query/path/headers during failure | audit/Trace secrets 0, only safe reason |
| EGR-T14 | root network namespace egress route and browser QUIC/DoH/UDP escape | direct route fails; only approved path succeeds |
| EGR-T15 | provider API redirects to other egress host; different tenant policy | denied independent of origin URL match |
| EGR-T16 | 2 tenant/owner load test + DNS latency/Connection reuse | finite throughput/budgets and no cross-tenant grants |
| EGR-T17 | POST mutating API loses ack after remote success | UNKNOWN and reconcile; no blind resend |
| EGR-T18 | same domain behind dualstack, one blocked special IPv6 | whole request denied even if IPv4 public |

All tests operate synthetic DNS/HTTP/Chromium/local private targets in isolated test network; no live SSRF of external systems. **Execution status: not run.**

## 15. Requirements traceability

| REQ | Tests | Scenarios |
|---|---|---|
| EGR-001 | T01,T06,T14 | 06,08 |
| EGR-002 | T02,T18 | 08 |
| EGR-003 | T03 | 08 |
| EGR-004 | T04,T18 | 08 |
| EGR-005 | T05,T07 | 05,08 |
| EGR-006 | T06,T14 | 06,08,13 |
| EGR-007 | T07 | 06,08 |
| EGR-008 | T08 | 08,15 |
| EGR-009 | T09 | 08 |
| EGR-010 | T10 | 07,08 |
| EGR-011 | T11,T16 | 07,09 |
| EGR-012 | T04,T12 | 08 |
| EGR-013 | T13 | 08 |
| EGR-014 | T01,T14 | 08 |
| EGR-015 | T05,T15 | 03,05,08 |

## 16. Unresolved/Cross-team proposals

| ID / owner | Priority | Request / acceptance |
|---|---|---|
| EGR-P1 / CHAT_04 | **P0** | Browser Worker proxy/Chrome network sandbox service worker/CDP/WebSocket; EGR-T06/T14 |
| EGR-P2 / CHAT_10 | **P0** | Compose network namespace + deny default outbound firewall, no direct DNS/QUIC/proxy bypass; EGR-T14 |
| EGR-P3 / CHAT_03 | **P0** | Tool/MCP connector admission & shared run/approval/cancel/UNKNOWN authority; EGR-T09/T17 |
| EGR-P4 / CHAT_05 | P0 | provider-specific origin/path allowlist, redirects, paid API rate limits; EGR-T15 |
| EGR-P5 / CHAT_01/02/09 | P0 | owner+tenant/workspace mapping, lossless leaseEpoch before dispatch; EGR-T10 |
| EGR-P6 / CHAT_08 | P1 | partial/unavailable user-visible error and secret-redacted citation; EGR-T13 |
| EGR-P7 / CHAT_11 | P1 | realistic DNS rebinding/private mock network matrix & benchmark (no public attack); EGR-T04/T18 |
| EGR-P8 / CHAT_00 | P2 | on-prem operator-certified private connector should require separate ADR, not wildcard bypass |

**P0 safety condition**: no release/Locked if Browser/MCP adapter can open unsupervised network sockets or if proxy cannot pin DNS+TLS remote peer. Exact OS proxy implementation and kernel features are unverified hypotheses.

## 17. Sources (2026-10-30 JST)

- [S1] **Code verified** OpenClaw MIT `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, `src/infra/net/ssrf.ts`, `isPrivateIpAddress`, `createPinnedLookup`, `resolvePinnedHostnameWithPolicy`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/infra/net/ssrf.ts
- [S2] **Code verified, tests not run** `src/infra/net/ssrf.test.ts`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/infra/net/ssrf.test.ts
- [S3] **Code verified, tests not run** `src/infra/net/fetch-guard.ssrf.test.ts` redirect/headers/private rebind: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/infra/net/fetch-guard.ssrf.test.ts
- [S4] **Code verified** browser-use `2cc71890e65056b67fdb29d98bb407749c15bb08`, `SecurityWatchdog`: https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/watchdogs/security_watchdog.py
- [S5] **Test source verified, not run** browser-use test_domain_filtering: https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/security/test_domain_filtering.py
- [S6] **Test source verified, not run** browser-use test_ip_blocking (default false at line 398): https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/security/test_ip_blocking.py#L395-L410
- [S7] **Docs verified** Docker networking: https://docs.docker.com/engine/network/
- [S8] **Docs verified** OWASP SSRF Prevention: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- [S9] **Hypothesis** Exact browser proxy/network namespace/deadlines/byte limits are not integrated or benchmarked. Not a claim of passing tests.
- [S10] **Issue/PR** No project-relevant upstream issue/PR was source-verified; source and tests are direct evidence. License verification of browser-use needed before implementation.

## Review gates
- [x] code/tests pinned; two OSS approaches; threat surface across adapters, resolver/redirect policy, transport-bound algorithm, failure/race, test traceability and cross-team ownership.
- [ ] egress proxy effective firewall/Chromium integration run, browser-use LICENSE checked, provider policy/owner contract approved. **Draft**.
