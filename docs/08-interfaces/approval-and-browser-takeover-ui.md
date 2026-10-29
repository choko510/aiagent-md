# Approval & Browser Takeover UI — Human-In-The-Loop Security Protocol

> Owner CHAT_08 | docs/08-interfaces/approval-and-browser-takeover-ui.md | Draft / [PROVISIONAL: CHAT_01,02,03,04,09,10] | Research 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-04,06,07,08,09,11

## 0. 結論

**承認とログイン介入は異なる安全境界**。承認は「特定の不可逆/外部操作への一回限りの決定」であり、Browser Takeoverは「隔離された正規BrowserSessionで人間がログイン等を行う短命・排他的な操作権」。一方の権限を得ても他方の許可は与えられない。画面のボタンはサーバーが保存したpending recordへ意思表示するだけで、Tool実行やBrowserの直接所有を決定しない。URLに資格情報/Cookie/CDP endpointを載せない。期限切れ、同時承認、GoalRevision変更、権限失効、Worker再起動、Owner切替は必ずfail-closed。

実装候補: Next.js UI / Fastify REST API / Browser Worker。ライブラリの採否とブラウザ接続方式はCHAT_04/09に委譲。HITLは正規ログインをユーザー自身が行う方法であり、CAPTCHAやCloudflareの突破保証はしない。

## 1. Scope / non-scope / owners

Scope: pending approval一覧、read-only詳細、action digest/費用/出典/期限、approve/deny UI、CAS failure UX、human takeover開始/終了/期限/再開、clipboard安全、mobile+keyboard accessibility、audit display。Non-scope: Tool policy、approval records/state machine（CHAT_03）、BrowserContext/lease control（CHAT_04）、AuthN/session/token policies（CHAT_09）、DB DDL（CHAT_10）、Run pause/wait semantics（CHAT_02）。

Run/Task and Events canonical CHAT_01; approval context must carry taskId,runId,goalRevision, tool intent digest, attempted side-effect, expiration, actor/tenant. Shared recordVersion initial=1 is provisional logical field, not DB revision=0. No \`leaseEpoch\` as user-provided token. UUIDv7 server IDs, Task.conversationId nullable, tenant/workspace mapping owner is CHAT_09/10.

## 2. OSS implementation audit 2026-10-30

| OSS pinned ref | Read source + test | Adopt / reject |
|---|---|---|
| **OpenClaw** 8b8c33ef [S1-S5] | \`src/gateway/approval-channel-custody.ts\` \`prepareApprovalChannelCustody\` re-check source delivery and reviewer capability; \`approval-channel-custody.test.ts\` role/actor and state change tests; \`approval-session-audience.ts\` scope/ancestor audience; \`approval-session-audience.test.ts\` bounded ancestry & closed session; \`approval-web-push.test.ts\` checks authorized visible delivery & generic approval notification | Adopt **authorization revalidation and audience-scoped visibility**; don't inherit broad operator shared-secret roles or host shell approvability. This product never exposes host command approval as allowed capability. |
| **Mastra** ec3ba11d [S6-S8] | \`client-sdks/react/src/agent/hooks.ts\` \`approveToolCall\`, \`declineToolCall\` and stream lifecycle; \`packages/core/src/agent/durable/durable-agent.ts\` \`approveToolCall\` delegates durable resume; \`packages/server/src/server/handlers/error.test.ts\` asserts losing concurrent approval/resume mapped 409 | Adopt resumed-run state re-read and claim conflict handling. Reject frontend optimistic "approved" as final authority; DB decision + Tool Gateway gate must precede effect. |
| **Browser takeover candidates** | No Browser Worker owned code was examined in the current harness because this repository contains specifications only; the exact CDP/interactive-control implementation is [Unverified: CHAT_04]. | UI issues short-lived intents, no direct BrowserContext access; request dependency tests prior to locking. |

OpenClaw MIT, Mastra standard modules Apache-2.0 with ee exception. No code copied, upstream tests **read but not run**. Source SHA/tag correspondence with release is not verified; no integration benchmark available.

**Alternatives:** (A) generic "approve everything" form with parameters editable by client—**reject**, rebinds effect and authority. (B) permit access to Browser session by returning CDP websocket URL—**reject**, credential/ownership leakage. (C) server-kept immutable action decision + short-lived fenced Browser Takeover backed by authoritative owner—**adopt**. (D) auto-approve after timeout—**reject**.

## 3. Responsibilities and dependency diagram

~~~text
Tool Gateway [CHAT_03] → PENDING_APPROVAL persisted [CHAT_10]
                                  │  public read projection [CHAT_08 REST]
Authenticated UI [CHAT_09] → ApprovalDetail (scope/digest/expiry)
                                  │ POST decision + idem + If-Match
                                  ▼
                         ApprovalService CHAT_03 (CAS, authorization,
                            goalRevision & actionDigest re-check)
                                  │ atomic receipt/outbox
                                  ├──> approve → Worker may resume via Gateway
                                  └──> deny/expire → no side effect

Browser Worker [CHAT_04] → WAITING_INPUT BrowserSession ref
                                  │
User UI → POST handoff request → Auth recheck/lease fence/ticket mint
                                  │
                   limited interactive view of isolated browser
                                  │ POST release or lease expiry
                                  ▼
                         Browser Worker regains ownership
                Task/Run Manager may resume at safe boundary [CHAT_02]
~~~

All UI views must come from authoritative GET \`/api/v1/approvals/{id}\` and \`/browser-sessions/{id}\`; SSE merely notifies. No separate owner state in frontend. Capturing raw Browser screenshots as chat Event without classification/redaction is forbidden.

## 4. Requirement IDs

| REQ | Level | Acceptance | SC |
|---|---|---|---|
| HITL-001 | MUST | Approval detail shows canonical action summary, immutable digest, run/task, risk/scope/cost, expiry and server-owned state | 04,06,07 |
| HITL-002 | MUST | Approve/deny authenticated, authorization rechecked right before atomic decision; If-Match + Idempotency-Key | 06,07,08 |
| HITL-003 | MUST | expiry, policy/user revoke, duplicate, stale GoalRevision or digest invalid → deny effect; no autoapprove | 07,08 |
| HITL-004 | MUST | browser handoff is short-lived, session-bound, task-bound, tenant-bound, exclusive and fenced | 06,08 |
| HITL-005 | MUST | raw cookie/password/token/storageState/CDP URL absent from UI app JSON, URLs, logs, SSE, model prompts | 06,08 |
| HITL-006 | MUST | Browser Worker blocks agent automation during human ownership; release/expiry triggers safe post-login reconciliation, not instant tool replay | 06,07 |
| HITL-007 | MUST | approval and browser rights are independent; takeover never implies external purchase/reservation/post approval | 04,06,08 |
| HITL-008 | MUST | pending, decision-ack, applied, expired, conflict, unknown-effect shown separately; browser crash and user close recover | 06,07 |
| HITL-009 | MUST | multi-channel decisions (Web/Telegram) CAS once and identify actual actor/channel; no replay from old message button | 07,08 |
| HITL-010 | MUST | keyboard/mobile/a11y and deliberate confirmation of irreversible actions, clear "deny" option, default safe | 04,06 |
| HITL-011 | SHOULD | short-lived take-over capability minted behind server-side one-time exchange + device/session binding | 06,08 |
| HITL-012 | SHOULD | audit history of decisions/lease release to authorized user (no secrets) | 07,08 |
| HITL-013 | LATER | multi-operator peer review / delegated approvals after ADR & tenancy migration | 07 |
| HITL-014 | EXPERIMENT | visual live preview masked, only after threat-model / privacy and resource test | 06,09 |

## 5. Public DTO proposals [PROVISIONAL CHAT_01/03/04/09]

~~~typescript
type PublicApprovalDetail = {
  approvalId:string; recordVersion:number;      // canonical CAS, initial 1
  taskId:string; runId:string; goalRevision:number;
  actionKind:"read"|"write"|"purchase"|"reservation"|"post"|"other";
  actionSummary:string;                       // server projected safe text
  actionDigest:string;                        // immutable server-side semantic digest
  destinationOrigin:string|null;              // sanitized, no credential URL
  maximumCharge:{amount:string;currency:string}|null;
  scopeSummary:string;                        // effect + affected account/object
  requestedAt:string; expiresAt:string;       // UTC + user-local display
  state:string; canDecide:boolean;             // advisory only
  requiresFreshAuth:boolean;                   // CHAT_09 owner
};
type ApprovalDecisionInput = {
  decision:"approve"|"deny";
  reason?:string;
}; // NO modified target, no "allow always", no tool args or policy flags
type PublicTakeoverDetail = {
  browserSessionId:string; taskId:string; runId:string|null;
  sessionOwner:"worker"|"human"|"none";         // CHAT_04-owned semantics
  recordVersion:number; handoffAvailable:boolean;
  expiresAt:string|null; warning:string|null;
  // no tenantId, credential, cookies, storageState, CDP/WebSocket endpoint
};
type TakeoverControlInput = { intent:"request"|"release"; reason?:string };
// One-time ticket exchange shape remains PRIVATE to CHAT_04/09 and is never
// a browser-consumable bearer token in URL or API logs.
~~~

**No confidence in client-calculated digest or boolean canDecide.** DB holds complete sanitized approval snapshot, goalRevision, effect args hash, policy/version; UI only displays server-projected explanation. User sees any external action exactly (what, destination, price and currency, item/capacity, account, irreversible impact). If critical fields unavailable, Approval cannot be completed: show \`INSUFFICIENT_CONTEXT\`, ask for a new request with server-built summary.

REST paths (from rest-api.md): GET /api/v1/approvals, GET /api/v1/approvals/{id}, POST /api/v1/approvals/{id}/decision, GET /api/v1/browser-sessions/{id}, POST /api/v1/browser-sessions/{id}/handoff, POST /api/v1/browser-sessions/{id}/release. Handoff endpoints use secure cookie/Origin/CSRF and 202 asynchronous ACK, then Browser Worker grants lease. Browser control itself uses a separate isolated channel/protocol defined by CHAT_04/09; v1 REST **does not proxy unrestricted arbitrary browser actions**.

## 6. Approval lifecycle

~~~text
Runtime Tool Gateway emits immutable pending intent
 → DB commits pending approval + event/outbox
 → UI receives SSE hint / GET approval detail
 → user reads action/scope/expiry/risks
 → UI confirms fresh authentication if required
 → POST decision with If-Match:"N" + Idempotency-Key
 → owner validates current principal + approval + goalRevision + actionDigest + expiry
 → atomic CAS first caller wins; second gets 409/412
 → Tool Gateway revalidates policy, budget and effect ledger at execution time
 → applied/denied/expired/unknown state projected to UI via GET/SSE
~~~

If expiry occurs **before** DB decision commit, refuse approval. Concurrent web and Telegram decision: only one persistent decision, loser sees already resolved; changing decision requires a **new approval record** issued by Tool Gateway, not toggling old record. Denial must never allow tool execute. If tool effect was already started before revoke, mark UNKNOWN / reconcile; user "Cancel" doesn't undo it. Steer changes GoalRevision, old approval is not reusable even if action superficially appears identical; new Tool Gateway validation required.

~~~ts
async function submitDecision(p, approvalId, expectedVersion, decision, key) {
  const detail = await api.getApproval(approvalId);
  ensureSameImmutableProposal(detail, expectedVersion); // UI check only
  if(isExpired(detail.expiresAt) || !detail.canDecide) return showBlockedState();
  const result = await api.decideApproval(approvalId,{decision},{
    "If-Match":quoteVersion(detail.recordVersion),
    "Idempotency-Key":key
  });
  return {commandId:result.commandId, uiState:"decision-accepted-not-applied"};
}
// Server (CHAT_03 canonical, not UI) under one DB CAS:
await tx.atomicConsumeApproval({
  approvalId, tenantId, actorId, recordVersion,
  goalRevision, actionDigest, notExpiredAt:tx.dbTime,
  identityVersion, policyVersion
});
~~~

**Use DB time** for expiry and decisions. Client countdown clock is advisory. Never open a redirect with approval proof in URL. Error map: 401 unauth, 404 not visible, 409 already resolved/invalid transition, 410 expired one-time takeover, 412 stale CAS, 428 missing ETag, 429 rate, 503 unavailable. No "automatic retry" with a new idempotency key after unknown ACK.

## 7. Browser Human Takeover / login handoff

1. Browser Worker detects human-needed challenge and publishes WAITING_INPUT with BrowserSessionRef, taskId, runId, leaseEpoch. Chat/UI only shows "認証操作が必要" with allowed host and safety notice; no screenshot containing secrets without explicit masked permission.
2. User navigates to session detail and reauths if required (CHAT_09). API verifies task owner and current BrowserSession owner/version. \`POST /handoff\` returns CommandAck; after worker accepts exclusive takeover, server exchanges a one-use, short-lived capability for a hardened same-origin interactive session. Ticket is **bound to user/session/browserSessionId/generation/lease/expiry/nonce and exact takeover purpose**.
3. When human takes control, Browser Worker pauses Agent Browser commands to that BrowserSession and fences old attempts. UI channel has no arbitrary host SSH/shell and cannot escape isolated BrowserContext.
4. User authenticates with site directly in controlled Browser. Password/OAuth/token never forwarded through LLM or Chat UI. Legitimate challenge is performed by human; no automated bypass. If user cancels, BrowserSession remains WAITING_INPUT or fails with meaningful state, never fake login.
5. On explicit \`POST /release\` (or timeout/revoke), Worker verifies current lease/generation and reclaims; session cookies remain encrypted scoped Browser storage governed CHAT_04/09, not app UI data. Run resumes only after CHECKPOINT/owner revalidation and normal Tool Gateway policy.
6. If Browser crashed while human controlled: fence old lease, mark \`TAKEOVER_LOST\` and notify user for a fresh session; never silently reconnect to default context/other tenant's session.

**Ticket protocol [PROVISIONAL]:** one-use exchange, 60-second issue→redeem, 10-minute idle, 30-minute absolute lease; configurable finite caps, unmeasured. The browser relay must be user-session-bound and should use separate origin/storage partition or sandboxed embed as reviewed by CHAT_09/04. If delegated remote viewing technology cannot guarantee bound session isolation, disable interactive takeover rather than return raw CDP websocket endpoint.

**Model/approval separation:** AI cannot read user passwords or browser cookies; human controls browser login only. If the next action is booking/purchase, Tool Gateway still generates separate immutable Approval and explicit user consent.

## 8. Race, failure and recovery matrix

| Failure | Detection | Outcome / recovery |
|---|---|---|
| Approval expired at submit time | DB clock CAS guard | 409/410; no effect; new approval from Tool Gateway |
| Two users/channels click approve/deny | CAS version and consumed flag | one wins; loser refreshes; zero duplicate side effects |
| User revoked after UI opened | identityVersion/owner check before commit & execute | deny; cache cleared; no old approval |
| Steer revision changed after request | goalRevision mismatch | old approval invalid; issue new proposal |
| Action digest differs from stored params | server immutable digest | deny; no retarget/argument substitution |
| Effect executed but ACK lost | side-effect ledger UNKNOWN | show reconcile; no reapproval automatically reused |
| Browser lease stale from expired tab | fenced leaseEpoch check | old browser control denied; fresh request necessary |
| Browser worker restart | browser session lease reconciliation | revoked old ticket, user notified |
| User closes takeover tab | TTL/explicit release | worker reclaims only after safe barrier |
| Two tabs request handoff | session lease CAS | one wins; other "already controlled" |
| Redirect to private host / external script prompt | CHAT_09 egress + CHAT_04 browser policy | fail closed, no privilege escalation |
| Notification link to an expired approval | reauth + server fetch | show expired/handled status; no executable old callback |

Browser Session status and approval status follow respective Owner state machines; this UI presents safe projected outcomes, not new canonical enum.

## 9. Security and privacy

- Auth+tenant+owner+resource checks **before view and again before decision**. Authorization cannot be inferred from possession of approvalId/taskId, signed cursor, Telegram callback or handoff token.
- No arbitrary Tool execute endpoint. No host shell/Docker socket/SSH. Irreversible purchases, bookings, posts and transfers require explicit bounded approval; human login alone never grants it.
- XSS defense: HTML source/article text escaped, sanitized links, no raw iframe from unknown site. CSP/frame policy reviewed for browser relay. No credential, cookie, secret, raw screen capture or arbitrary DOM in JSON/error/log/trace. UI screenshot rendering requires privacy review and mask rules.
- Browser takeover stream must authenticate each new connection; revocation closes live relay; reverse proxy websocket/stream headers cannot be forged. Clipboard/readback/drag/drop/file upload restricted to authorized browser actions with audit and permissions.
- Approval confirmation defaults deny, buttons disabled after submit until fetched state; assistive technology announces scope and countdown without auto-click. Showing a hidden approval screen from a Push notification requires fresh auth.
- Logs: audit safe actorId/taskId/approvalId/actionHash/decision/expiry/client type/changedVersion, no decision secret or action raw arguments. Store immutable audit with retention limit CHAT_09. API rate/parallel caps user/tenant and one human takeover per BrowserSession.
- Metrics: approval conflict, expired attempts, unauthorized attempts (target 0 permitted effects), staleGoalRejected, takeoverLeaseExpiry, takeoverCrossTenantRejected, consentToEffectLatency measured after integration only. No hypothetical percent or latency guarantee.

## 10. Persistence / API and external-team changes

No new CHAT_08 physical tables. [CHAT_03] Immutable tool intent digest, snapshot, expiry, decision actor, policyEpoch; side-effect result unknown. [CHAT_04] BrowserSession owner + leaseEpoch bigint fence, session generation, encrypted storage handles, exclusive bridge, safe release. [CHAT_09] user/tenant/workspace mapping and auth freshness/CSRF/revocation; client cannot mint ticket. [CHAT_10] atomic approval decision and outbox with idempotency record, lease/ticket consumed and expiry indexes; row lock. [CHAT_01] event and GoalRevision state and Task lineage. [CHAT_02] WAITING_INPUT, checkpoint and safe resume semantics. [CHAT_08 REST] GET projection/If-Match/Idempotency; [CHAT_08 SSE] authorization and replay of waiting/approval events.

Upgrade: old broad approval ids are not automatically mapped to new policy. Unknown active approvals are quarantined pending new consent, not auto-approved. On BrowserWorker upgrade, invalidate old control leases and require user re-handoff. Any migration altering token encoding must revoke or prevent reuse of existing tickets. Tests must verify no owner fallback.

## 11. Implementation phases

HITL-PR1 safe approval read DTO and keyboard/a11y details; HITL-PR2 authenticated decision service adapter with CAS/expiry/GoalRevision/ledger; HITL-PR3 BrowserSession read projection and lease service+short-lived ticket exchange; HITL-PR4 interactive isolated browser view with privacy controls; HITL-PR5 cross-channel race, crash/revoke/E2E and rollout review. No implementation in this documentation repo.

## 12. Verification plans (not executed)

| TEST | Setup/action | Expected | SC |
|---|---|---|---|
| HITL-T01 | pending reservation with amount, URL, expiry, digest, missing context | exact immutable summary visible; missing material facts blocks approval | 04,06 |
| HITL-T02 | approve/deny commands with valid/invalid actor and stale ETag | one accepted per expected version, other 412/403, no leak | 06,07,08 |
| HITL-T03 | expire/revoke/Steer old goalRevision then approve | all rejected, external effect count 0 | 07,08 |
| HITL-T04 | tenant A/B request same BrowserSession takeover; competing tabs | only owner one lease, stale fence denied | 06,08 |
| HITL-T05 | scrape rendered JSON/SSE/logs/URLs during takeover/login | 0 raw cookie/password/token/CDP endpoint exposed | 06,08 |
| HITL-T06 | BrowserWorker kill while human active then reconnect | old capability revoked, no Agent Browser action concurrently | 06,07 |
| HITL-T07 | user logs in then Agent attempts unapproved purchase | blocked by distinct approval gate; no order placed | 04,06,08 |
| HITL-T08 | unknown external side effect / ack lost on Cancel | UNKNOWN displayed, no repeated effect | 07 |
| HITL-T09 | Web and Telegram simultaneously approve vs deny | exactly one DB decision; no callback re-use | 07,08 |
| HITL-T10 | keyboard-only screenreader phone 320px & explicit confirm | no accidental irreversible consent, Deny accessible | 04,06 |
| HITL-T11 | reload/expired URL and 100 attempts rate limit | bounded, error clear, zero replayed authority | 06,09 |
| HITL-T12 | SQLite/PG? use production Postgres mock with DB clock + expiry boundary | expiry checked transactionally before side effects | 07 |

MUST map: 001→T01; 002→T02; 003→T03; 004→T04; 005→T05; 006→T06; 007→T07; 008→T08; 009→T09; 010→T10. All tests synthetic/isolated. No real purchase/reservation/post, no real credentials.

## 13. Blockers/cross-team proposals

| Proposal | Owner | Priority | Exit criteria |
|---|---|---|---|
| HITL-P01 | CHAT_03 | P0 | immutable approval identity+digest+expiry+GoalRevision+CAS/consumed semantics |
| HITL-P02 | CHAT_04 | P0 | browser owner/leaseEpoch/relay contract, safe takeover and crash recovery |
| HITL-P03 | CHAT_09 | P0 | fresh auth and short-lived ticket, CSP/relay isolation, tenant-workspace mapping |
| HITL-P04 | CHAT_02 | P0 | WAITING_INPUT, resume barrier and old effect UNKNOWN |
| HITL-P05 | CHAT_10 | P0 | atomic consume + outbox and one-use ticket store |
| HITL-P06 | CHAT_01 | P1 | approval event state and goal/run linkage |
| HITL-P07 | CHAT_11 | P1 | cross-tab/channel and crash/fencing E2E test harness |

## 14. Sources, evidence grade

[S1] **Code verified**, OpenClaw \`prepareApprovalChannelCustody\` pinned 8b8c33ef (MIT), 2026-10-30: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/approval-channel-custody.ts  
[S2] **Code verified test inspected (not run)**: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/approval-channel-custody.test.ts  
[S3] **Code verified**, approval audience: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/approval-session-audience.ts  
[S4] **Code verified test inspected** audience: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/approval-session-audience.test.ts  
[S5] **Code verified test inspected** push visibility: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/approval-web-push.test.ts  
[S6] **Code verified**, Mastra React hooks \`approveToolCall/declineToolCall\` pinned ec3ba11d (Apache-2.0 except ee): https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/client-sdks/react/src/agent/hooks.ts  
[S7] **Code verified**, durable approval: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/durable-agent.ts  
[S8] **Code verified test inspected** conflict 409: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/error.test.ts  
**Unverified:** Browser relay implementation/compatibility, token protocol, real AuthProvider, production browser isolation and human UI visual/a11y behavior. Numeric TTLs and file layout are design proposals. Source tests have not been executed and no application exists here.
