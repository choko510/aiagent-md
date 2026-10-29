# Side-effect Ledger — Durable External-effect Specification (03-04)

> CHAT_03 / path `docs/03-tools/side-effect-ledger.md` / Draft [PROVISIONAL] / rev 0.2 / research 2026-10-30 JST / main `b45d4e989998663a00ef09673e69ae1f4191f00d`.
>
> Owner: Tool external-effect semantics and reconciliation = CHAT_03. Dependencies: 03-02 Gateway, 03-03 Approval Policy, CHAT_01 Entity UUID/Run/State/Error/Event, CHAT_02 Run lease checkpoint, CHAT_09 identity/egress/credentials, CHAT_10 SQL & outbox, CHAT_11 crash test.

## 0. 結論
**実際の外部副作用を、ローカルDBと原子的にcommitすることは一般には不可能**。したがって「一度しか実行しない」ことをLLMやQueueやRedisのdedupe機能だけで保証する設計を却下する。Ledgerは *logical intent identity*、*attempt claim/fence*、*may-have-sent bit*、*response evidence*、*provider status inquiry*、*manual reconciliation* を区別する。

最重要ルール：

1. 外部effectを開始する前に、DBに不変の`Intent`と、**少なくとも1つの開始試行の保守的な送信可能性**をdurableに記録。
2. 同じlogicalCallの再配信、Worker kill/reclaim、Approval repeated callback、Queue再投入では**既存Intentの同じ結果か`UNCERTAIN`を返し、別の外部writeを起動しない**。
3. 送信後のTimeout、TCP切断、HTTP 500、SDK異常、Worker kill、DB結果保存失敗、no ACKを「失敗確定」と仮定しない。Providerが公開する状態照会、同一provider-defined idempotency key、明示的な人間判断により収束させる。
4. Providerがidempotency keyとeffect identity/status queryを**契約・実動作で保証**する場合のみ、限定条件付きsafe retryを許す。未サポート/不明なら自動再write不可。
5. 外部で本当に一回だけ処理されること（physical exactly-once）と、内部論理Intentを一回だけ記録しUIへ重複通知しないこと（logical at-most-once commit）を分けて表示。
6. 実行が`UNCERTAIN`のとき、確実な支払い/投稿/予約が成立したとユーザーに伝えない。同時に「操作なし」と断言もしない。

本Ledgerの正本はデータの意味論。物理DDLはCHAT_10、Event EnvelopeはCHAT_01、Task/Run停止・再開はCHAT_02。Gateway以外のConnector/MCP/Browserの独自ledgerは拒否する。

## 1. 使用範囲、非対象、SC
- 対象：外部APIのcreate/update/delete、ホテル予約送信、メール/投稿/購入、Watch登録変更、通知送信、Browser内のsubmit/confirm（CHAT_04 Browser Workerを所有）、MCP remote tool call、ある種のread API（metadata取得以外の費用/副作用可能性）。
- Read fetchも外部サイトへのアクセスや費用を生むので始動記録と予算上限を持つ。ただし外部可観測な**非冪等mutation**と同列にretryしない。
- 非対象：BrowserSession lease自身の正本、Watch notification exact delivery（CHAT_07 outbox）、Postgres DDL、利用者のチャットによる単なる「やっていい」の解釈、決済provider固有のstatus API。
- SC-07を主対象。SC-01/04は未承認購入/予約禁止、SC-02/11/16は通知重複排除連携、SC-06/13はBrowser effect再生、SC-08は悪意あるMCP、SC-09はコスト計測、SC-10/12/14/15は観測と副作用区別。

## 2. Source Code / Test / Issue audit
| OSS / pinned commit / license | Observed code and tests (read 2026-10-30) | Practical conclusion |
|---|---|---|
| pg-boss `71ea068b144c75b014a1eb90327aaa4a92da1cf2` MIT | [`src/plans.ts` `completeJobsUpdate`, `failJobsById`, `touchJobs`](https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/plans.ts#L2883-L2940)、[`test/attemptFenceTest.ts`](https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/attemptFenceTest.ts#L69-L127) は再取得されたjob attemptに旧workerがcomplete/fail/touchを誤適用しないケース。 | **Code/test inspected, not executed**。attempt fenceは採用。ただしqueue job fence≠外部side-effect idempotency。 |
| LangGraph `ffc3336367e8139668d11e5f0b0e6def57e7c762` MIT | [`libs/langgraph/langgraph/pregel/_loop.py`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py#L461-L514) `put_writes`、[L559-L615](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py#L559-L615) pending writes/checkpoint。 | **Code inspected**。実行結果とcheckpointのcommit境界は参考。ただしcheckpointが成功しても外部writeのatomicityなし。 |
| Temporal TypeScript SDK `6c837b2a92b5b8acb333ead32657fb1d8e1efc77` MIT | [`packages/worker/src/activity.ts`](https://github.com/temporalio/sdk-typescript/blob/6c837b2a92b5b8acb333ead32657fb1d8e1efc77/packages/worker/src/activity.ts#L73-L144) Activity execute/heartbeat/cancel、[`packages/common/src/retry-policy.ts`](https://github.com/temporalio/sdk-typescript/blob/6c837b2a92b5b8acb333ead32657fb1d8e1efc77/packages/common/src/retry-policy.ts#L7-L64) retry settings including unlimited options。 | **Code inspected**。durable retry/heartbeat API design参考だが、非冪等 Toolにdefault retryを移植しない。 |
| OpenAI Agents SDK `cd26bbbc52847845fde73b9952b51e62e2bcbdc0` MIT | [`tests/test_tool_approval_call_id_reuse.py`](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py#L333-L435) repeated/changed tool call identity test | **Test inspected**。provider call_idのみでde-dupやapprovalを認定しない。 |
| Issues | [pg-boss #925](https://github.com/timgit/pg-boss/issues/925) stale worker completion report（closed, fixed state not inferred）、[PydanticAI #10926](https://github.com/pydantic/pydantic-ai/issues/10926) approved payment twice after crash、[LangGraph #9397](https://github.com/langchain-ai/langgraph/issues/9397) checkpoint/replay report。 | **Issue report, not reproduced**。実際のexactly-onceを示す証拠ではない。 |

### 比較と決定
- A **Outboxだけで exactly-once**：internal enqueue重複には効くが外部provider処理が成功してDB保存前に落ちる穴を塞げない。**却下**。
- B **Redis SETNX/memory mutex**：短いTTL/partition/worker killで失効、外部効果追跡が消える。**補助のみ**。
- C **DB intent+attempt+effect state+opaque idempotency mapping+status inquiry+manual decision**：実行不確実性を正確に表せる。**採用**。
- D **Provider固有のdedupe keyだけに全面依存**：すべての外部API/MCP/Browserでサポートされるとは限らない。**条件付き**。

## 3. Canonical identity / ownership and contracts
CHAT_01 [PR #2](https://github.com/choko510/aiagent-md/pull/2) defines public UUIDv7 Entity IDs、`Run.taskId`、`Task.conversationId|null`、`Run.leaseEpoch` monotonic、`Task.goalRevision`、`recordVersion` starts 1。CHAT_02 [PR #1](https://github.com/choko510/aiagent-md/pull/1) defines stable `logicalCallId` based on `runId+stepOrdinal+proposalOrdinal` and Run recovery. CHAT_03 03-02/03-03 define prepare/approval/dispatch. CHAT_10 [PR #10](https://github.com/choko510/aiagent-md/pull/10) currently proposes `tool_intents/approval_records/tool_attempts` but `workspace_id, gen_random_uuid(), revision 0` need reconciliation. CHAT_09 [PR #9](https://github.com/choko510/aiagent-md/pull/9) owns principal and tenant.

**Intent identity invariant**：
`(tenantId, runId, stepOrdinal, proposalOrdinal)` uniquely identifies one *logical* call. `fingerprint` is `canonicalSchemaVersion | toolId | toolRevision | argsSHA256 | targetDigest | risk | goalRevision | credentialBindingVersion`. Repeating same key and fingerprint returns **existing** intent/receipt, not new attempt; repeating same key with different fingerprint gives `INTENT_CONFLICT`. Provider call_id is diagnostic only. Intent ID is newly server-generated UUIDv7 public Entity; `toolRef`/secret handle are opaque strings, not external authority.

**External provider idempotency key**：
Derived by secure opaque derivation over `providerId, tenantId, intentId, immutable operation target`; never use raw personally identifying IDs in provider-visible key if avoidable. Each provider adapter declares (admin-reviewed) scope: `NONE | CREATE_ONLY | ALL_WRITES | UNKNOWN`, provider accepted key TTL, supported operations, status endpoint, consistency horizon, permission scope, max possible duplicates. The key is unchanged across a safe retry of **the same immutable intent**, but changes for genuinely new intent. When provider cannot verify key continuity or TTL expired → not safe to retry.

## 4. Requirements
| REQ | Level | Observable result | SC |
|---|---|---|---|
| LED-001 | MUST | 先に不変Intent+fingerprintを永続化してから外部送信 | 07 |
| LED-002 | MUST | logicalCall unique・同一入力再送は同receipt、異なる入力は409 | 07,08 |
| LED-003 | MUST | Dispatch attemptにRun leaseEpoch/owner/fenceとmayHaveSentを保存してから送信 | 07 |
| LED-004 | MUST | 外部のeffect successとDB commit successを別扱い、lost ACKはUNCERTAIN | 07 |
| LED-005 | MUST | non-idempotent/unknown effectにはblind retry禁止、status inquiry/人間再照合 | 04,07 |
| LED-006 | MUST | provider idempotency key適用範囲・有効期限・status inquiryを契約で検証 | 07,08 |
| LED-007 | MUST | Worker crash/reclaim後current lease fenceを確認、旧workerの新send/settleを拒否 | 07 |
| LED-008 | MUST | cancellation/revocation後の新dispatch禁止、in-flightはuncertainを維持 | 07 |
| LED-009 | MUST | 結果の確定にはstatus/source/observedAt/output evidence、proof strengthを保存 | 01,04,07 |
| LED-010 | MUST | UNKNOWN effectを価格0/空室無し/送信失敗確定と読み替えない | 01,02,04 |
| LED-011 | MUST | Manual reconciliationは認証ownerの権限と監査、recovery reason、二重入力抑止 | 07,08 |
| LED-012 | MUST | Budget reservation/settlementとoutboxは同一Intentに1回だけ紐付ける | 07,09 |
| LED-013 | MUST | Secret/PIIをledger/outbox/modelに格納せずopaque artifactsで参照 | 06,08,15 |
| LED-014 | MUST | Provider/Browser/MCP adapterは独自retryで外部writeを再送しない | 07,08,13 |
| LED-015 | MUST | 監視/イベント起動/Recipe再生でも同一ledgerで重複効果を記録 | 02,11,13,16 |
| LED-016 | SHOULD | 適切なステータス照会を非同期reconcilerで一定回数行い、それでも不明ならmanual attentionへ | 07 |
| LED-017 | SHOULD | 原本Artifactと監査レコードをretention期間・tenant scopeで保存 | 09 |
| LED-018 | EXPERIMENT | provider別status APIのreconcile成功率と確認費用を測る | 09 |

## 5. Local contract and schemas [PROVISIONAL]
~~~ts
type IntentId = string;        // UUIDv7 Entity ID, CHAT_01
type OpaqueRef = string;       // registry/credential/artifact refs, NOT IDs/authority
type DecimalFence = string;    // wire codec proposal for DB bigint, CHAT_01/02 to decide
type EffectClass = "PURE_READ"|"IDEMPOTENT_WRITE"|"NON_IDEMPOTENT"|"UNKNOWN";
type IntentState = "PREPARED"|"WAITING_APPROVAL"|"READY"|"DISPATCH_CLAIMED"|
 "MAY_HAVE_SENT"|"CONFIRMED_SUCCEEDED"|"CONFIRMED_FAILED"|"UNCERTAIN"|"RECONCILING"|
 "MANUAL_REVIEW"|"DENIED"|"CANCELLED_BEFORE_DISPATCH";
type EvidenceGrade = "PROVIDER_STATUS_CONFIRMATION"|"SIGNED_RECEIPT"|
 "RESPONSE_ACK"|"UNVERIFIED_RESPONSE";
interface FrozenEffectIntent {
  id: IntentId; tenantId: string; ownerUserId: string; taskId: string; runId: string;
  stepOrdinal: number; proposalOrdinal: number; logicalCallId: string;
  goalRevision: string; // canonical int64 decimal
  toolId: string; toolRevision: string;
  canonicalArgsDigest: string; targetDigest: string; operationCode: string;
  effectClass: EffectClass; credentialBindingVersion?: string;
  createdAt: string; intentRecordVersion: string; // canonical int64 decimal approvalRef?: IntentId;
}
interface EffectAttempt {
  attemptId: IntentId; intentId: IntentId; ordinal: number;
  runLeaseEpoch: DecimalFence; dispatchFence: DecimalFence;
  providerId: string; providerIdempotencyKeyRef?: OpaqueRef;
  mayHaveSent: boolean; claimedAt: string; deadlineAt: string;
  responseReceivedAt?: string; completedAt?: string;
  outcomeArtifactRef?: OpaqueRef; reasonCode?: string;
}
interface EffectObservation {
  intentId: IntentId; classification: IntentState;
  proof: EvidenceGrade; providerOperationId?: OpaqueRef;
  effectiveAt?: string; observedAt: string; sourceRef: OpaqueRef;
  outputArtifactRef?: OpaqueRef; confidence?: string;  // scalar confidence not proof of effect
}
interface ReconcileCommand {
  intentId: IntentId; commandId: string; expectedRecordVersion: number;
  decision: "RETRY_VERIFIED_SAFE"|"MARK_CONFIRMED_SUCCESS"|"MARK_CONFIRMED_FAILURE"|
            "KEEP_UNCERTAIN"|"ABANDON_FUTURE_ATTEMPTS";
  observationRef?: OpaqueRef;
}
~~~

Types above are **CHAT_03-local projections only**, not CHAT_01 shared Event/Evidence shapes. `IntentState` is Ledger-local; Task/Run states are owned by CHAT_01. All public Entity IDs must conform to canonical UUIDv7, cannot use DB default `gen_random_uuid()` without compatible explicit ID generation. `DecimalFence` ↔ CHAT_01 number / CHAT_02 bigint / DB bigint mapping must reject overflow, negatives and fractional JS numbers. Never use HTTP/LLM `tenantId` to select rows; derive from authenticated WorkerTask lineage.

**Proof policy**：
- `PROVIDER_STATUS_CONFIRMATION` = authenticated read of same provider operation id under same account, with definitive business status.
- `SIGNED_RECEIPT` = verifiable provider receipt matched target/request digest.
- `RESPONSE_ACK` = network response successfully observed, may or may not certify external commit. Operation adapter documents what is confirmed.
- `UNVERIFIED_RESPONSE` = content untrusted; cannot settle an irreversible action by itself.
The absence of an item in eventually consistent status listing is **not** proof of failed effect. Only provider contract with known status semantics can yield CONFIRMED_FAILED. Human “mark confirmed” changes internal ledger's administrative belief with audit/evidence; it is not falsely labeled provider-confirmed.

## 6. State machine / sequence
~~~mermaid
stateDiagram-v2
  [*] --> PREPARED: immutable intent TX
  PREPARED --> WAITING_APPROVAL: policy
  PREPARED --> READY: authorized
  WAITING_APPROVAL --> READY: approved
  WAITING_APPROVAL --> DENIED: denial/expiry
  READY --> DISPATCH_CLAIMED: fence+budget+approval CAS
  DISPATCH_CLAIMED --> MAY_HAVE_SENT: conservative marker before I/O
  MAY_HAVE_SENT --> CONFIRMED_SUCCEEDED: verified outcome+DB TX
  MAY_HAVE_SENT --> CONFIRMED_FAILED: verified no effect+DB TX
  MAY_HAVE_SENT --> UNCERTAIN: timeout/lost ACK/DB failure
  UNCERTAIN --> RECONCILING: query status
  RECONCILING --> CONFIRMED_SUCCEEDED: positive proof
  RECONCILING --> CONFIRMED_FAILED: authoritative negative proof
  RECONCILING --> MANUAL_REVIEW: no reliable proof
  MANUAL_REVIEW --> UNCERTAIN: unresolved
  READY --> CANCELLED_BEFORE_DISPATCH: cancel
~~~

~~~mermaid
sequenceDiagram
  participant A as Agent Loop
  participant G as Tool Gateway
  participant D as Postgres
  participant E as External Provider
  participant R as Reconciler
  A->>G: execute(intent)
  G->>D: TX validate run fence+approval+budget; insert attempt and MAY_HAVE_SENT
  D-->>G: committed dispatch claim
  G->>E: invoke original request + provider idempotency key if supported
  alt definitive response and evidence saved
    E-->>G: response + operation ID
    G->>D: TX verified status + usage + outbox
    D-->>A: committed receipt
  else ACK lost, timeout, worker killed, DB commit failed
    G-->>A: UNKNOWN where response possible
    R->>D: load UNCERTAIN/MAY_HAVE_SENT
    R->>E: read-only authenticated status inquiry
    E-->>R: proof / still unknown
    R->>D: TX CAS reconcile state/evidence
  end
~~~

**Conservative send marker**：在`DISPATCH_CLAIMED`開始時に同一DB transaction内で`mayHaveSent=true`へ上げることも可。外部呼出の直前の別commitがcrashした場合は「実際に未送信かもしれない」が**安全に自動再送できる保証がない**のでUNCERTAINへ。逆に「send後にflagを立てる」実装は禁止（send成功→crash→DB未記録の穴）。Write before external I/O, never after.

## 7. Deterministic algorithms
~~~ts
async function beginEffect(intentRef: string, runRef: string) {
  // Inputs are opaque; server rehydrates authenticated identity from Task/Run.
  const ctx = await runAuth.loadCurrent(runRef);
  const record = await store.intentForTenant(intentRef, ctx.tenantId);
  return store.tx(async tx => {
    await tx.assertCurrentTaskRunGoalFence(ctx);       // Task.activeRunId + Run.leaseEpoch
    await tx.assertCurrentToolRevisionPolicyBudget(ctx, record);
    await tx.consumeApprovalIfNeeded(record);          // 03-03 single use
    const existing = await tx.lockIntent(record.id);
    if (existing.terminal) return existing.resultReceipt;
    if (existing.mayHaveSent) return {kind:"NEEDS_RECONCILIATION", id:record.id};
    // Shared invariant: exactly one claimant. CAS if version differs returns conflict.
    const claim = await tx.claimAttemptAndMarkMayHaveSent({
      intent:record, leaseEpoch:ctx.leaseEpoch, providerKeyRef:deriveProviderKey(record)
    });
    await tx.insertOutboxEvent("TOOL_DISPATCH_CLAIMED", record.id);
    return claim;
  });
}
async function settleEffect(attempt, received) {
  const normalized = await outputValidator.normalizeAndSaveArtifact(received);
  return store.tx(async tx => {
    const intent = await tx.lockIntent(attempt.intentId);
    if (intent.terminal && sameEvidenceFingerprint(intent, normalized))
      return intent.savedReceipt;
    if (intent.terminal) throw RecordedEffectConflict();
    // A stale worker cannot overwrite a newer result. Nevertheless evidence about
    // a successful external effect must be preserved for reconciliation.
    if (!await tx.isCurrentDispatchFence(attempt)) {
      await tx.appendLateEvidenceQuarantine(attempt, normalized);
      return {kind:"NEEDS_RECONCILIATION", id:intent.id};
    }
    if (!normalized.certifiesBusinessOutcome) {
      await tx.markUncertain(intent.id, "UNVERIFIED_RESPONSE");
      return {kind:"UNCERTAIN", id:intent.id};
    }
    await tx.markConfirmed(intent.id, normalized);
    await tx.settleReservedBudgetOnce(intent.id, normalized.usage);
    await tx.insertOutboxEvent("TOOL_OUTCOME_CONFIRMED", intent.id);
    return tx.getReceipt(intent.id);
  });
}
async function reconciliationLoop(intentId: string) {
  const i = await store.loadUncertain(intentId);
  if (!i || !i.mayHaveSent) return;                 // never initiate write here
  if (i.reconcileAttempts >= 3 || now() > i.reconcileDeadline)
    return store.markManualReview(i.id);            // bounded budget/latency
  const provider = adapterRegistry.getVerifiedProvider(i.providerId);
  if (!provider.supportsAuthoritativeReadStatus) return store.markManualReview(i.id);
  const result = await provider.readStatus(i.providerOperationId, i.credentialHandle);
  if (!result.definitive || !sameImmutableTarget(result, i))
    return store.recordUnresolvedObservation(i, result);
  return store.compareAndSetVerifiedResult(i, result);  // persist proof+outbox+cost once
}
async function manualReconcile(cmd: ReconcileCommand, principal) {
  await authz.assertFreshOwnerAndScope(principal,cmd.intentId);
  const i = await store.loadWithExpectedVersion(cmd.intentId,cmd.expectedRecordVersion);
  await audit.saveDecisionCommandIdempotently(cmd.commandId,i,cmd);
  if (cmd.decision==="RETRY_VERIFIED_SAFE")
    return requireProviderIdempotencyContractAndFreshApproval(i); // not an immediate write
  return store.recordHumanReconciliation(i,cmd); // app belief, evidence provenance preserved
}
~~~

**Retry classifier**：
- Safe read-only GET: bounded retries under server restrictions; if priced/side effect provider mark billability uncertain.
- Effect `NON_IDEMPOTENT`/`UNKNOWN`: **0 automated write retries** after MAY_HAVE_SENT unless hard provider guarantee.
- Effect `IDEMPOTENT_WRITE`: may retry only when **same immutable request/key/owner/target**, server contract explicitly guarantees idempotency for operation class, TTL not expired, strong fresh Policy and approval binding, no conflicting successful status, and operator-approved retry rule. No stack's general HTTP retry middleware may silently replay POST.
- `429` response on an operation that could have been accepted is still ambiguous unless provider contract guarantees rejection without effect.
- Manual `MARK_CONFIRMED_FAILURE` is not license to silently retry; require separate new logical intent/approval and warning if prior effect remains unresolved.

## 8. Failure matrix
| Event at boundary | Persisted evidence available | Action / resulting status |
|---|---|---|
| Before T1 intent insert | none | No execution permitted, prepare same logical ID |
| After T1 before dispatch | READY | New owner may begin after lease/approval recheck |
| After approval consume+send marker T2 before actual socket | MAY_HAVE_SENT | UNCERTAIN (conservative) on resume |
| After actual write before ACK | MAY_HAVE_SENT | status inquiry; no blind replay |
| HTTP 2xx ACK before Artifact write | pending attempt | capture response in memory only; DB failure -> UNCERTAIN |
| Artifact written before DB outcome commit | orphan object | hash-ref reconcile or cleanup; no resend |
| DB commit success, worker response lost | confirmed terminal | reread and return committed receipt; no duplicate outbox |
| Two worker claims same intent | unique+CAS+fence | one claim; loser reads existing status |
| Old worker late definitive success | invalid fence + evidence | quarantine evidence, reconcile with current attempt; do not erase genuine effect |
| Provider status read eventual missing | weak negative | retain UNCERTAIN/MANUAL_REVIEW, never mark failed |
| Provider cancelled after remote acceptance | cancelled local but remote may run | UNCERTAIN; cancellation not rollback |
| Policy or tenant revoked after send | cannot undo remote operation | no fresh attempts, record pending reconciliation |
| Reconcile poll quota exhausted | bounded attempts | MANUAL_REVIEW with explicit lastCheckedAt |
| Terminal result and outbox publish crash | terminal + committed outbox | outbox replay with dedupe key, no Tool resend |
| DB restore from old backup | external reality may be newer | suspend outbound effects; reconciliation required |
| Provider idempotency TTL expired | no safe retry proof | MANUAL_REVIEW; no automated replay |

## 9. Security/Privacy
Trust boundary: model → Gateway untrusted; Gateway→adapter restricted trusted code; provider results/status report untrusted until validated and target/account/operation ID matched. Provider cannot assert `runId`, `tenantId`, `approval` from payload. Secret keys, private hostnames, Authorization, Cookies, PII must not be serialized in ToolIntent/ledger/trace/log; store encrypted secret binding in CHAT_09 vault and fingerprint/hmac in ledger. When idempotency key is sent externally, use random/opaque handle, never expose actual task/user identity. Egress SSRF and redirect checks apply status inquiry too; status endpoint cannot become arbitrary user-controlled URL.

Public status read requires tenant+owner check; admin manual reconcile requires *fresh* auth and explicit reason/evidence link. Any synthetic duplicate effect (e.g. X event text asks to retry transfer) is rejected and audited as injection. For Browser forms, user approval does not authorize unrelated actions after navigation; changed origin or target changes fingerprint.

## 10. Observability & limits
Bounded initial defaults (review/measurement required): max 1 active dispatch attempt/intent, max 3 read-status inquiries with backoff and jitter, each inquiry 5s deadline, reconciliation horizon 24h then manual; max 16KiB redacted response for model, external raw evidence artifact bounded 2MiB; per Task external effect limit configured and finite (default not assumed unlimited), provider-specific budget hard cap. `reconciliation` does not use LLM endless polling. Watch/notification retry is entrusted to CHAT_07 with own dedupe.

Metrics: `ledger_intents_created_total`, `ledger_dispatch_claim_denied_total`, `ledger_uncertain_total`, `ledger_reconcile_confirmed_total`, `ledger_manual_review_total`, `ledger_duplicate_prevented_total`, `effect_provider_status_latency_ms`, `ledger_record_age_ms`, `lease_fence_rejections_total`。High cardinality IDs in protected trace not metric labels. Record `effectAttemptId, logicalCallId hash, runLeaseEpoch, policyEpoch hash, targetDigest, provider operation ID ref, evidenceAt, observedAt` without raw secrets. Effective cost/charge timestamp and currency are not conflated with estimated budget.

Bench design: fixture providers with (a) accepted then connection dropped, (b) no acceptance, (c) eventual status, (d) idempotency-key guaranteed, (e) no status API; 1/2/8 worker takeover and crash after each T1/T2/T3/T4 boundary. Record actual remote write counter and false success/missing/retry. Target zero unauthorized starts and zero duplicate writes in controlled fixture, **not** general production promise.

## 11. Persistence & transaction proposals [PROVISIONAL: CHAT_10]
New logical tables/fields: `effect_intent` (immutable ref/args/target/risk/status/version/owner), `effect_attempt` (per-attempt UUIDv7, current runLeaseEpoch, dispatchFence, mayHaveSent, start time, external idempotency key binding), `effect_observation` (provider status/receipt/evidence/ref), `effect_reconcile_action` (decisionActor, reason, idempotencyKey, timestamp, provenance), `budget_reservation` (intent-specific), `outbox` (event/notification transaction). Physical names and schemas are CHAT_10-owned; existing PR #10 `tool_intents` and `tool_attempts` may be extended rather than duplicated.

**Uniqueness and transaction invariants**：
- `UNIQUE(tenant_scope,run_id,stepOrdinal,proposalOrdinal)`; `UNIQUE(intent_id, attemptNo)`; `UNIQUE(intent_id, providerIdempotencyKey)` where supported; `UNIQUE(tenant,commandId)`; terminal outcome CAS.
- T1 transaction: `intent insert+immutable payload digest+budget reservation+approval request+outbox`.
- T2 transaction: `Run/Task/goal/fence/policy/approval verification+approval consume+attempt claim+mayHaveSent=true+outbox`. Durable before any network request.
- T3 Artifact Store IO separate; T4 DB transaction: `definitive result/evidence pointer+usage settle+outbox`. If T4 fails, external effect remains UNCERTAIN.
- Replica stale write prevented by `WHERE lease_epoch=:expected AND task.active_run_id=:runId AND status=...` with consistency model agreed by CHAT_02/10. Late callback may attach evidence to a quarantine append-only log but cannot overwrite result. DB isolation/locks and indexes in CHAT_10; never hold transaction open across remote call.
- Backup restore cannot rewind idempotency truth about external provider; dispatch disabled on restored scope until uncertainty sweep.

**Contract collision audit**：(1) CHAT_01 UUIDv7 vs CHAT_10 `gen_random_uuid` mismatch; (2) CHAT_01 `recordVersion=1` vs `revision default 0`, define migration/mapping; (3) Task conversation nullable vs DB NOT NULL; (4) `leaseEpoch:number` vs Runtime bigint and DB absence, loss of precision; (5) `tenantId/ownerUserId` vs `workspace_id`. All are P0, **not silently resolved here**. These affect ledger PK/FK, CAS, revision checks, approval scope. Pending result should not be called production-safe until reconciled.

## 12. UI/API / operational manual reconcile [PROVISIONAL: CHAT_08]
- `GET /tool-intents/{id}` response: `intentStatus`, `effectOutcome`, `providerConfirmation` (only if verified), `lastCheckedAt`, `mayHaveSent`, `userActionRequired`, `proofUrlRef`, `estimatedCost` vs `settledCost`. Do not show raw provider operation token or secret.
- `POST /tool-intents/{id}/reconcile` (admin/owner scoped with If-Match + Idempotency-Key + reason+evidence link) = **manual belief update only**; no direct external POST unless separate new approved intent.
- UI words: "完了（外部の確定証拠あり）", "失敗確定（providerの証拠あり）", "結果不確定—外部で処理済みの可能性", "手動確認が必要"。Never translate UNCERTAIN to FAILED or SUCCEEDED automatically.
- Recovery dashboard shows post-restart pending UNCERTAIN and delayed status inquiry, and explicit no-automatic-retry warning. Telegram alerts for uncertain states via notification outbox only (CHAT_07).

## 13. Implementation PR phases
L1 immutable identity/fingerprint/intent, unit/uniqueness and owner check. L2 attempt claim+dispatch marker before I/O+CAS/Run fence. L3 provider idempotency capabilities and status inquiry read API (CHAT_05), no blind retry. L4 result/evidence/budget/outbox atomic settlement and quarantine of late results. L5 human reconciliation UI/permissions and full crash-fuzz test; only enable under gated rollout after CHAT_01/09/10 P0 closure.

## 14. Tests (all designed, not run)
| Test ID | Setup → action | Expected |
|---|---|---|
| LED-T01 | 2 concurrent `prepare` same logical intent/fingerprint | one row/receipt, zero duplicate sends |
| LED-T02 | same logical ID, different argsHash/target/toolRev | explicit conflict, no overwrite |
| LED-T03 | kill before intent T1 | external calls 0; replay safe |
| LED-T04 | kill after T1/before T2 | READY not sent; claim safe with fresh policy |
| LED-T05 | kill after T2/before socket | MAY_HAVE_SENT; never auto retry nonidempotent |
| LED-T06 | remote accepts then drops ACK | UNCERTAIN; no duplicate write |
| LED-T07 | remote accepts, response saved, T4 DB fail | reconcile; no false success/failure |
| LED-T08 | stale worker A completes after B run lease takeover | A can't settle B; late evidence quarantined |
| LED-T09 | duplicate queue job during MAY_HAVE_SENT | same intent; no second write |
| LED-T10 | provider authoritative status says succeeded matching target | confirmed success with proof; notify once |
| LED-T11 | provider missing status in eventually consistent listing | stays uncertain/manual |
| LED-T12 | provider contract guarantees key and safe TTL, same request | bounded safe replay only, no double effect |
| LED-T13 | provider key TTL expired or scope differs | no replay, manual review |
| LED-T14 | two manual reconcile commands, same/different commandId | exactly one CAS decision; audit |
| LED-T15 | other tenant guesses intentId + status URL | denied; no private data leakage |
| LED-T16 | HTML/MCP response asks "retry charge" | untrusted text; ledger unchanged |
| LED-T17 | cancel after send | no new attempts, UNCERTAIN preserved |
| LED-T18 | restore old DB backup after provider success | outbound effects paused until recovery audit |
| LED-T19 | malformed JSON/outputSchema, huge Artifact | fail closed/partial, raw evidence bounded, no duplicate |
| LED-T20 | duplicate notification event from confirmed effect | one outbox dedupe key; CHAT_07 delivery rules |
| LED-T21 | task without conversation created by X post | valid Task linkage; same ledger |
| LED-T22 | UUIDv4 default/recordVersion0/lease overflow/tenant↔workspace mismatch | migration/validator rejects or converts explicitly, not silent |
| LED-T23 | 8 workers race and kill at all boundaries across 500 fixture intents | no unauthorized/duplicate physical effects, bounded UNCERTAIN |
| LED-T24 | benchmark inquiry costs and success over fixed provider semantics | p50/p95, cost, false confident rates measured |

## 15. Traceability
| REQ | Tests | SC | Pass |
|---|---|---|---|
| LED-001 | T03,T04,T05 | 07 | external send strictly after intent commit |
| LED-002 | T01,T02,T09 | 07,08 | one logical intent |
| LED-003 | T05,T08,T22 | 07 | unique current fence and marker |
| LED-004 | T06,T07 | 07 | UNCERTAIN correctly maintained |
| LED-005 | T05,T06,T11,T13 | 04,07 | unsafe retry count=0 |
| LED-006 | T12,T13 | 07,08 | provider guarantee checked |
| LED-007 | T08,T23 | 07 | stale settle prevented |
| LED-008 | T17,T18 | 07 | no new dispatch after cancel |
| LED-009 | T10,T11,T19 | 01,04,07 | proof+time saved |
| LED-010 | T06,T11,T21 | 01,02,04 | unknown never zero/none |
| LED-011 | T14,T15 | 07,08 | authorized/manual audit |
| LED-012 | T01,T07,T20 | 07,09 | one usage/outbox per effect |
| LED-013 | T15,T16,T19 | 06,08,15 | secret leakage=0 |
| LED-014 | T12,T13,T16 | 07,08,13 | no hidden adapter retries |
| LED-015 | T09,T20,T21 | 02,11,13,16 | same ledger across triggers |

## 16. Cross-team proposals / unresolved
| Proposal | Owner priority | Ask / affected contract / proof |
|---|---|---|
| LED-X01 | CHAT_01 P0 | logicalCallId, ToolInvocation vs Intent, UUIDv7, canonical errors, outcome `UNCERTAIN`, proof grade; T02/T10/T22 |
| LED-X02 | CHAT_02 P0 | re-claimed Run same runId with monotonic leaseEpoch; old worker fence and Goal revision; bigint conversion; T08/T23 |
| LED-X03 | CHAT_09 P0 | tenant↔workspace binding, scope-checked manual status, protected credential handles and revocation; T15/T16 |
| LED-X04 | CHAT_10 P0 | T1/T2/T4 transaction schema, mayHaveSent marker, CAS lease epoch, durable outbox/usage, migration of v4 UUID/version0; T05/T07/T22 |
| LED-X05 | CHAT_05 P1 | provider-specific status inquiry and idempotency contracts; unsupported providers marked UNKNOWN not safe; T10/T13 |
| LED-X06 | CHAT_08 P1 | human reconcile UI/REST and distinct uncertain display, 202 != effect success; T14 |
| LED-X07 | CHAT_07 P1 | outbox notification dedupe distinct from external effect dedupe; T20 |
| LED-X08 | CHAT_11 P1 | crash-after-every-DB-commit and remote-ACK fault harness, fixture external effect counter; T23 |
| LED-X09 | CHAT_13/14 P1 | event replay and browser action recipes must never bypass ledger; T21 |
| LED-X10 | CHAT_00 P2 | provider-specific preapproval grants and customer consent policy unresolved; no auto-write until ADR |

## 17. Sources, evidence grades, and limits
- [S1] [pg-boss `src/plans.ts`](https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/plans.ts#L2883-L2940), [`attemptFenceTest.ts`](https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/attemptFenceTest.ts#L69-L127); `71ea068b`, MIT, read 2026-10-30, **Code verified/test inspected, not executed**.
- [S2] [LangGraph `_loop.py`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py#L461-L615); `ffc3336`, MIT, **Code verified**.
- [S3] [Temporal TypeScript SDK `activity.ts`](https://github.com/temporalio/sdk-typescript/blob/6c837b2a92b5b8acb333ead32657fb1d8e1efc77/packages/worker/src/activity.ts#L73-L144), [`retry-policy.ts`](https://github.com/temporalio/sdk-typescript/blob/6c837b2a92b5b8acb333ead32657fb1d8e1efc77/packages/common/src/retry-policy.ts#L7-L64); `6c837b2`, **Code verified**, MIT LICENSE verified at pinned commit; no code copied.
- [S4] [OpenAI Agents SDK test](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py#L333-L435) MIT, **test inspected**.
- [S5] [pg-boss Issue #925](https://github.com/timgit/pg-boss/issues/925), [PydanticAI #10926](https://github.com/pydantic/pydantic-ai/issues/10926), [LangGraph #9397](https://github.com/langchain-ai/langgraph/issues/9397), 2026-10-30 **Issue report, not independently reproduced**.
- [S6] [Project Brief](../00-foundation/project-brief.md), [Architecture Rules](../00-foundation/architecture-rules.md), [SC](../00-foundation/acceptance-scenarios.md). Proposed internal Ledger semantics/limits = **Hypothesis** requiring architecture review + fault tests. **Runtime tests executed: 0**.


### Round 3 — lossless canonical version boundary (2026-10-30)
All cross-service `goalRevision`, `recordVersion`, `definitionRevision`, and `leaseEpoch` values MUST use CHAT_01's canonical signed-int64 decimal **string** codec (PostgreSQL BIGINT / internal BigInt), not JSON number. Each version comparison converts validated strings to BigInt and rejects leading zeros/exponents/unsafe numbers, preserving full 64-bit range. This module does not redefine the canonical type. **Contract test (designed, not executed):** roundtrip `9007199254740992` and `9223372036854775806`, reject an unsafe JS number, and reject stale version/goal approval without dispatching external effects.
