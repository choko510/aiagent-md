# Error Taxonomy — Detailed Implementation Specification

> Owner CHAT_01 | Canonical docs/01-contracts/error-taxonomy.md | Draft / PROVISIONAL | Revision 0.1 | 2026-10-30 JST | Main at branch creation 3044c046b1caeea70c5ef6edfea7c57387c6c6da | Depends on 01-01 through 01-05, CHAT_02/03/05/08/09/10.

## 0. Design

**原因、再試行可能性、外部副作用の確定性、ユーザーに求める行動は別々の軸。** CancelやTimeoutでは外部書込が実行されなかったと証明できない。外部書込後のACK喪失ではindeterminate outcomeを永続化し、調査/承認なしの再実行を禁止する。Task/Runのterminalは01-03、Tool ledgerはCHAT_03、AuthはCHAT_09、HTTPはCHAT_08が所有し、本書は共通エラーの唯一の正本。

## 1. Responsibilities and scenarios

Scope: error code registry、error envelope、provider error mapping、transport phase、effect certainty、retry disposition、privacy safe messaging、cause lineage、versioning。Non-scope: actual Tool executor/Gateway、state transitions、physical DB、Web API route、login/cookie、price extraction。Supports SC-01..09 including 02/06/07/08/09 and expansion SC-10..16 acquisition failures, event source errors, PDF/XLSX parsing and notification issues.

## 2. OSS source/test comparison — inspected 2026-10-30, tests NOT executed

| OSS / license / pinned SHA | Actually inspected code / test | Adopt / limitation |
|---|---|---|
| OpenAI Agents SDK Python MIT, cd26bbbc52847845fde73b9952b51e62e2bcbdc0 | src/agents/exceptions.py: AgentsException, RunErrorDetails, ModelTimeoutError, ToolTimeoutError, MaxTurnsExceeded, redaction boundary; tests/test_run_error_details.py [S1] | typed error and run diagnostics; SDK exception alone cannot prove external effect absence |
| PydanticAI MIT, 3809c1b861036197de4a06ae3ee38e27e5455100 | pydantic_ai_slim/pydantic_ai/exceptions.py: ModelRetry, ToolFailed, ApprovalRequired, ModelConnectionError.phase, ModelHTTPError.retry_after; tests/test_exceptions.py: retry-after and serialization [S2] | retry vs terminal failure, connect/write/read phase; a read failure may occur after remote write |
| MCP TS SDK 39ae8e6192cf43273aaaf2de4169dfa9f7216e28, core-internal package.json Apache-2.0; root legacy MIT/Apache-2.0 transition | packages/core-internal/src/types/spec.types.2025-11-25.ts: JSONRPCErrorResponse, numeric codes including INVALID_PARAMS/INTERNAL_ERROR; test/e2e/scenarios/errors.test.ts [S3] | transport codes belong to adapter, not canonical business error code |

Alternative A raw provider exception as API payload: rejected for secrets and semantic drift. B retryable boolean alone: rejected because uncertainty and policy are independent. C structured error/phase/effect/disposition/visibility: **adopt**. D automatic HTTP status→business state mapping: reject; same 504 can be harmless read or uncertain external write.

## 3. Canonical authority and references

Only trusted adapter/classifier (not LLM or source HTML or untrusted MCP data) creates canonical ErrorEnvelope. Provider text is untrusted, sanitized into operator-only detail. Side-effect ledger must be checked before deriving outcome certainty. Owner-tenant scope comes from core-domain and CHAT_09. Event 01-02 carries safe error reference, not stack. RunState 01-03 consumes code/disposition; external observation 01-05 uses failure acquisition status, never invented numeric value.

## 4. Requirements

| REQ | Level | Pass condition | SC |
|---|---|---|---|
| ERR-001 | MUST | registered stable code + schema version for all failures | 07 |
| ERR-002 | MUST | provider status/code and canonical code separated | 05 |
| ERR-003 | MUST | retry recommendation distinct from external effect certainty | 07 |
| ERR-004 | MUST | maybe-sent irreversible action + ACK loss never blindly retried | 07 |
| ERR-005 | MUST | Cancel/timeout race retains uncertainty | 07 |
| ERR-006 | MUST | no secret, raw stack or private cross-tenant reference in public errors | 08 |
| ERR-007 | MUST | failed price fetch does not become 0 or unchanged observation | 02,12 |
| ERR-008 | MUST | expired approval and denied authorization distinguishable internally | 06,07 |
| ERR-009 | MUST | old RunEpoch/GoalRevision/RecordVersion refuse stale state updates | 07 |
| ERR-010 | MUST | budget/deadline/rate limits bounded and distinguishable | 09 |
| ERR-011 | MUST | Retry-After normalized, bounded, malformed rejected | 09 |
| ERR-012 | MUST | unknown major schema fails closed | 07 |
| ERR-013 | MUST | operator diagnostic and public projection separated | 08 |
| ERR-014 | MUST | required user action compatible with Task state | 04,06 |
| ERR-015 | MUST | error+state+Event+Outbox committed atomically | 07 |
| ERR-016 | SHOULD | unknown transport phase classified conservatively | 07 |
| ERR-017 | SHOULD | messages keyed for localization not provider string passthrough | 05 |
| ERR-018 | LATER | nested multi-errors and sophisticated operator diagnostic UI | 15 |

## 5. TypeScript logical canonical types

~~~ts
type ErrorCodeV1 =
  | "VALIDATION.INVALID_INPUT" | "VALIDATION.SCHEMA_UNSUPPORTED"
  | "IDENTITY.UNAUTHENTICATED" | "IDENTITY.FORBIDDEN" | "IDENTITY.OWNER_UNKNOWN"
  | "PERMISSION.APPROVAL_REQUIRED" | "PERMISSION.APPROVAL_EXPIRED"
  | "PERMISSION.CAPABILITY_DENIED" | "PERMISSION.EGRESS_BLOCKED"
  | "RESOURCE.NOT_FOUND" | "RESOURCE.DELETED" | "RESOURCE.CURSOR_EXPIRED"
  | "CONFLICT.RECORD_VERSION" | "CONFLICT.RUN_FENCE" | "CONFLICT.GOAL_REVISION"
  | "CONFLICT.DUPLICATE_KEY" | "CONFLICT.ACTIVE_RUN"
  | "BUDGET.COST_LIMIT" | "BUDGET.TOKEN_LIMIT" | "BUDGET.TIME_LIMIT"
  | "RATE.THROTTLED" | "RATE.PROVIDER_QUOTA"
  | "SOURCE.ACCESS_RESTRICTED" | "SOURCE.UNAVAILABLE" | "SOURCE.STALE"
  | "SOURCE.EXTRACTION_FAILED" | "SOURCE.OBSERVATION_MISSING"
  | "MODEL.INVALID_OUTPUT" | "MODEL.REFUSED" | "MODEL.UNAVAILABLE"
  | "MODEL.CONTEXT_OVERFLOW" | "MODEL.TIMEOUT"
  | "TOOL.SCHEMA_MISMATCH" | "TOOL.EXECUTION_FAILED"
  | "TOOL.ACK_LOST" | "TOOL.EFFECT_OUTCOME_UNKNOWN" | "TOOL.CANCELLED"
  | "BROWSER.SESSION_EXPIRED" | "BROWSER.NEEDS_HUMAN"
  | "STORE.CONFLICT" | "STORE.UNAVAILABLE" | "STORE.ARTIFACT_INTEGRITY"
  | "QUEUE.DELIVERY_UNCERTAIN" | "EVENT.REPLAY_WINDOW_EXPIRED"
  | "INTERNAL.UNEXPECTED";
type TransportPhase = "not_started"|"connect"|"write"|"read"|"after_response"|"unknown";
type EffectCertainty =
  | "not_applicable"|"not_dispatched"|"confirmed_applied"
  | "confirmed_not_applied"|"indeterminate";
type RecoveryDisposition =
  | "safe_retry_with_same_key"|"wait_until"|"reconcile_external"
  | "requires_user_action"|"permanent_failure"|"replan";
interface ErrorEnvelopeV1 {
  schemaVersion:"1.0"; errorId:string; code:ErrorCodeV1;
  category:string; publicMessageKey:string; safeSummary:string;
  actorAction:"none"|"retry"|"provide_input"|"approve"|"contact_admin"|"inspect_uncertain";
  disposition:RecoveryDisposition; effectCertainty:EffectCertainty;
  transportPhase:TransportPhase|null; retryAfterAt:UtcInstant|null;
  occurredAt:UtcInstant; taskId:TaskId|null; runId:RunId|null;
  logicalCallId:string|null; correlationId:string; causeErrorId:string|null;
  operatorDetailRef:string|null;
  providerInfo:{system:string;httpStatus:number|null;code:string|null}|null;
  transient:boolean; // advisory only, never executable permission
}
~~~

**Validation:** errorId UUIDv7 trusted-server generated. Code from registry, category from code prefix not raw input. Schema major recognized, cause lineage acyclic and bounded. Time UTC, Retry-After must be positive and finite and policy-limited. safeSummary max 256 chars, no raw HTML/stack/secret/URL credential; public projection excludes operatorDetailRef and provider raw data unless explicitly sanitized. Tenant+owner scope applies to every reference. Error code in provider response is not itself authoritative and does not mint Retry approval. **effectCertainty overrides the retry hint**: indeterminate implies reconcile_external, regardless of HTTP 500/timeout/transient.

**Policy mapping**:
- VALIDATION / PERMISSION / IDENTITY: reject; unauthorized resource may be public 404 even if internal code is FORBIDDEN, per CHAT_09.
- CONFLICT: re-fetch state/If-Match, never overwrite stale version or lease.
- BUDGET / RATE: finite stop or wait, budget extension requires explicit approval; Retry-After clamped.
- SOURCE: failed/restricted/stale acquisition cannot prove value zero or unchanged. Evidence remains unknown.
- MODEL: malformed output may receive bounded corrective feedback, refused output is not success; model timeouts are not always whole-task failures.
- TOOL: write/read phase of remote effect may have happened. If ledger cannot prove outcome, mark TOOL.EFFECT_OUTCOME_UNKNOWN; do not resend.
- BROWSER: human challenge WAITING_INPUT, no CAPTCHA bypass.
- EVENT / QUEUE: delivery uncertainty does not prove rollback; recover from persisted outbox.

## 6. Normal sequence

~~~text
Provider exception
 -> adapter captures transport phase / safe source metadata
 -> lookup durable Tool intent and side-effect ledger
 -> canonical classifier determines code and effectCertainty
 -> deterministic recovery policy decides disposition
 -> DB TX: record error, guarded Task/Run transition, Event+Outbox
 -> public safe UI projection / private operator details
 -> either bounded retry, authorized user input, reconciliation or terminal
~~~

Old Run worker cannot commit a late state or terminal Event; effect receipt may be saved separately in quarantined reconciliation ledger without inheriting old Run authority. If cancellation happens after an external operation was sent, do not erase the original uncertain record. An outbox retransmission is separate from reexecuting the external tool.

## 7. Pseudocode

~~~ts
function classify(ctx, exception, ledger, policy): ErrorEnvelopeV1 {
  const transport = adapter.normalizeTransport(exception);
  const certainty = ledger.knownOutcome ??
    (ledger.intentNeverDispatched ? "not_dispatched" :
     ledger.mayHaveBeenSent ? "indeterminate" : "not_applicable");
  const code = certainty === "indeterminate"
    ? "TOOL.EFFECT_OUTCOME_UNKNOWN"
    : canonicalCodeFromTrustedAdapter(exception,transport);
  const disposition = certainty === "indeterminate"
    ? "reconcile_external"
    : policy.chooseBoundedRecovery(code,ctx,transport);
  return validateAndRedactError({
    code, effectCertainty:certainty, disposition,
    transportPhase:transport.phase, operatorDetailRef:restrictedCauseRef(exception),
    taskId:ctx.taskId, runId:ctx.runId
  });
}
async function persistError(ctx, error) {
  return database.transaction(async tx => {
    await tx.assertTenantOwnerCurrentEpochAndVersion(ctx);
    await tx.insertImmutableError(error);
    await tx.applyCanonicalStateTransition(error);
    await tx.appendEventAndOutboxSameCommit(error.errorId);
  });
}
~~~

The above is a proposed implementation boundary, not an existing SDK function. Bounded retry requires Tool Gateway side-effect classification, durable same-key intent, user authorization, current RunEpoch, GoalRevision, budgets and external provider idempotency agreement; no error field can bypass them.

## 8. Race, fault and recovery matrix

| Fault | Canonical mapping | Required behavior |
|---|---|---|
| model malformed JSON | MODEL.INVALID_OUTPUT | feedback only within limits |
| tool wrong args | TOOL.SCHEMA_MISMATCH | no executor |
| rate 429 | RATE.THROTTLED | finite Retry-After |
| provider 401/403 | ACCESS_RESTRICTED or PERMISSION depending cause | no auto credential escalation |
| browser expired login | BROWSER.NEEDS_HUMAN | WAITING_INPUT |
| stale worker lease | CONFLICT.RUN_FENCE | old write/effect = 0 |
| old revision approval | CONFLICT.GOAL_REVISION | no new effect |
| unknown major schema | VALIDATION.SCHEMA_UNSUPPORTED | quarantine/migrate |
| API ACK lost after DB commit | consult command receipt, not treated as rolled back | idempotent same Task |
| failure before outbound bytes | not_dispatched | may bounded retry |
| write/read timeout remote irreversible Tool | TOOL.EFFECT_OUTCOME_UNKNOWN | reconciliation only |
| Cancel during external write | uncertain effect if no receipt | new effects blocked |
| price source fetch fail | SOURCE.UNAVAILABLE | no fabricated price or unchanged |
| artifact checksum mismatch | STORE.ARTIFACT_INTEGRITY | no verified Evidence |
| same error/outbox duplicate | stable error/event ID | no duplicate UI mutation |
| owner disabled while retry pending | IDENTITY.FORBIDDEN | no fresh Tool |
| context overflow | MODEL.CONTEXT_OVERFLOW | safe compaction/replan or fail |

## 9. Security and finite limits

Raw exception strings, payloads, HTML and tool provider errors are untrusted input and may contain prompt injection. Never echo verbatim into system prompt or public error; escape and mask URL query tokens, Cookie/Authorization, credential headers, raw PII, stack and provider messages. Private operator details stored encrypted/owner scoped with retention and audit. No unauthorized host Shell/SSH/Docker socket, no tool privilege from error content. Finite retries, deadline, backoff, cost/token caps, circuit breakers and provider rate policy are required (owners CHAT_02/03/05/09); never schedule infinite retry loops.

## 10. Observability, benchmark

Low cardinality metrics: error_total{code}, safe_retry_total, effect_unknown_total, expired_approval, rate_retry_after_invalid, stale_fence, redaction_fail_total, outbox_replay. Use errorId/correlationId/taskId/runId in restricted traces only; no raw credentials. Benchmark 1/4/16 workers × 1000 synthetic failures with read/write timeout, cancellation and ACK loss; record p50/p95/p99, memory, duplicate effect count, leaked bytes. No observed SLO asserted.

## 11. Persistence and migrations [PROVISIONAL CHAT_10]

Error records immutable by errorId/tenant/owner, canonical code/version/phase/certainty/disposition, safe summary/message key, private cause ref, task/run/call ref, Event/outbox. DB-side same-tenant foreign keys, version CAS, privacy retention and cause cycle/depth validation. Legacy free text errors not automatically declared safe-to-retry: migrate into quarantined INTERNAL.UNEXPECTED, uncertain if effect history missing. Outbox delivery at-least-once; handling event redelivery must not execute external Tool.

## 12. UI / REST / Telegram [PROVISIONAL CHAT_08/09]

Candidate HTTP mapping: 400/422 invalid input; 401 unauthenticated; 403 or privacy-preserving 404 denied; 409 conflict; 412 If-Match; 410 expired replay cursor; 429 rate/policy; 503 service unavailable. RFC9457 Problem Details with stable code, safe message key, errorId, required action; no raw cause, credential or foreign tenant ID. 202 command accepted does not mean action completed. Distinguish partial, unknown effect, Needs Input, rate limited, revoked, expired. Public 404 concealment is policy-owned.

## 13. Implementation PR split

ERR-PR1 codec/registry/safe serializer and tests; ERR-PR2 provider/mcp adapters and transport phase; ERR-PR3 effect certainty/ledger, state/Event transactional integration; ERR-PR4 REST/i18n, security/fault/benchmark and migration gate. No runtime code in this repository.

## 14. Tests designed (NOT executed)

| TEST | Setup/action | Expected |
|---|---|---|
| ERR-T01 | registered code serialization/unknown major | known only |
| ERR-T02 | MCP -32602 vs HTTP 422 | transport preserved, canonical normalized |
| ERR-T03 | HTTP500 in readonly vs irreversible Tool | independent safe dispositions |
| ERR-T04 | external Tool write then lose ACK | unknown effect, zero blind retry |
| ERR-T05 | Cancel/timeout and eventual external ACK | uncertainty preserved, reconciliation |
| ERR-T06 | secret/stack/HTML injection in provider error | zero public/log leaked bytes |
| ERR-T07 | price Watch 403/429 | observation error, no 0 |
| ERR-T08 | expired approval vs revoked auth | different internal code and action |
| ERR-T09 | stale RunEpoch/GoalRevision/version after worker replacement | zero stale mutation |
| ERR-T10 | cost/deadline/rate crossing | bounded stop |
| ERR-T11 | Retry-After negative/huge/date malformed | reject/clamp finite |
| ERR-T12 | unknown schema major / old migrated error | quarantine |
| ERR-T13 | cross-tenant operator detail guess | zero bytes |
| ERR-T14 | human Browser takeover | WAITING_INPUT not FAILED |
| ERR-T15 | kill before/after Error+Event commit | state+error+outbox atomic |
| ERR-T16 | unknown transport phase after maybe-send | conservative unknown |
| ERR-T17 | Japanese/English renderer | same error code, trusted strings |
| ERR-T18 | revoke while retry enqueued | no new external Tool |
| ERR-T19 | 1000 concurrent failed calls | bounded latency/RSS recorded |

## 15. REQ→TEST→SC

| REQ | TEST | SC |
|---|---|---|
| ERR-001 | T01 | 07 |
| ERR-002 | T02 | 05 |
| ERR-003 | T03,T16 | 07 |
| ERR-004 | T04 | 07 |
| ERR-005 | T05 | 07 |
| ERR-006 | T06,T13 | 08 |
| ERR-007 | T07 | 02,12 |
| ERR-008 | T08,T14 | 06,07 |
| ERR-009 | T09 | 07 |
| ERR-010 | T10 | 09 |
| ERR-011 | T11 | 09 |
| ERR-012 | T12 | 07 |
| ERR-013 | T13 | 08 |
| ERR-014 | T14 | 04,06 |
| ERR-015 | T15 | 07 |
| ERR-016 | T16 | 07 |
| ERR-017 | T17 | 05 |

## 16. Unresolved cross-team proposals

- ERR-P01 P0 CHAT_03: side-effect certainty from ledger and stable intent, no unsafe retry. T03-T05.
- ERR-P02 P0 CHAT_02: Task/Run states for uncertainty/cancel/budget/timeout. T05/T09/T10.
- ERR-P03 P0 CHAT_10: error records, encrypted detail/outbox atomic, migration and rollback. T12/T15.
- ERR-P04 P0 CHAT_09: public-safe vs private error, redaction, 404 concealment. T06/T13.
- ERR-P05 P1 CHAT_08: Problem Details and HTTP mapping, i18n. T17.
- ERR-P06 P1 CHAT_04/05/07: provider error mapping, bounded Retry-After, failed observation is not zero. T07/T11.
- ERR-P07 P1 CHAT_01 self: 01-04 ToolResult uses ErrorEnvelopeV1 and 01-03 uses these canonical error categories, no local duplicates.
- ERR-P08 P1 CHAT_13/15: corrupted external Event/file parsing errors vs empty result.
Keep Draft until owners approve and full contract tests pass.

## 17. Sources (2026-10-30)

[S1] Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/exceptions.py and https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_run_error_details.py; MIT LICENSE.  
[S2] Code verified: https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/exceptions.py and https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_exceptions.py; MIT LICENSE.  
[S3] Code verified: https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/src/types/spec.types.2025-11-25.ts and https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/errors.test.ts. Pinned core-internal package.json declares Apache-2.0, root LICENSE covers MIT/Apache-2.0 transition; contribution-specific provenance must be checked before copying.  
[S4] Hypothesis: our ErrorCode/EffectCertainty/RecoveryDisposition and safe message catalog, no runtime verification.

**Review gates:** P0 contracts remain, all tests are designs rather than execution. Not Locked.

**MCP license evidence (Docs verified, pinned 39ae8e6, 2026-10-30):** https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/LICENSE (MIT→Apache-2.0 transition); https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/package.json (license: Apache-2.0). This project copies no upstream source code.
