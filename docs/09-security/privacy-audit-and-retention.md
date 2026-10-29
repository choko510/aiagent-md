# Privacy, Audit and Retention — Detailed Implementation Specification

> Owner: **CHAT_09**, module **09-06** | Canonical path: `docs/09-security/privacy-audit-and-retention.md` | **Draft / [PROVISIONAL]** | Version: 0.1 | Research: **2026-10-30 JST** | Branch base main: `3044c046b1caeea70c5ef6edfea7c57387c6c6da`
>
> Depends: CHAT_00 policy/ADR, CHAT_01 User/Tenant/Task/Artifact/Event contracts, CHAT_02 recovery, CHAT_03 Tool ledger/approval, CHAT_04 Browser, CHAT_06 Memory/Skills, CHAT_07 Watch/Outbox, CHAT_08 REST/Telegram, CHAT_09 Auth/Secrets/Prompt injection, CHAT_10 physical DB/object storage/backup, CHAT_11 E2E; CHAT_13/14/15 expanded ingest and learned state.
>
> **Scope verification**: External OSS implementation and test *sources* were read at pinned SHAs; they were not executed. This repository specifies a product, not an implementation. All proposed shared contracts, retention defaults and SQL structures are subject to owner review and remain NOT LOCKED.

## 0. Conclusion and adopt/reject decisions

**Adopt** a versioned per-resource **privacy inventory + durable deletion/export workflow + sealed redacted audit**, tightly integrated with canonical DB ownership and backup reconciliation. Data are not one blob: `Evidence`, `Raw Artifact`, `Working Context`, `Memory`, `Skill`, `Browser storageState`, `Watch Observations`, `Trace`, `Credential`, `Notification`, `Outbox`, `Cache/Search index`, `External provider data`, and `Backup` have different purpose, sensitivity, retention and owner. A user deletion request must prevent immediate use/reindexing, then sweep and verify **all derived copies**. A success toast is not proof of purging all copies.

**Choose**:
1. Every persisted resource carries a server-resolved owner (Tenant↔Workspace mapping and user-private ownership from PR #9), logical purpose, sensitivity, retention policy ID/version, origin and lifecycle. Evidence is not the same as a prompt or a secret.
2. Trace by default captures safe metadata only (step/state/tool alias/duration/token count/sourceRef), never raw prompt/secret/cookie/full URL query/HTTP headers. Detailed debug payload requires a dedicated short-lived, local-operator-only controlled setting and must still exclude secrets; no default full conversation replay.
3. User-initiated export/deletion uses fresh auth/CSRF, idempotency key and immutable request receipt; an owner-scoped **Deletion Manifest** enumerates DB rows, object blobs, browser states, memory index, derived cache, trace and pending notifications. `DELETION_REQUESTED` stops new reads/processing before asynchronous physical purge; partial progress is visible.
4. DB transaction and object storage are not one atomic system. Use idempotent step receipts, tombstones, retry backoff and bounded sweeper; on failure, show `PARTIAL / RETRY_REQUIRED`, never `COMPLETED`. Pending worker queue, late provider events and backup restore cannot resurrect deletion due to a durable suppression epoch/tombstone checked at every ingestion/use.
5. Backups have fixed maximum lifetime; inability to physically remove one subject from a snapshot must be disclosed and controlled by restore-time tombstones. Crypto-erasure can be used **only** if the per-record DEK and its wrapped copies are removed from all relevant stores and key backups. Otherwise do not call it immediate secure erasure.
6. Security audit records should not replicate private content; audit trail of deletions retains safe decision metadata according to explicit policy. User content and security events have separate retention classes. Any legal/regulatory hold is a separately authorized, bounded, logged exception, not an automatic indefinite hold.

**Reject**: logging every LLM prompt/HTTP payload; deletion via one SQL DELETE without object/index/browser cleanup; returning `success` after enqueue alone; unlimited raw traces; backup restores that bypass tombstones; globally shared cache without owner scope; per-user copies of credentials in artifacts; automatic export of tokens as part of user data download.

## 1. Purpose, scope, non-scope and acceptance scenarios

- Protect privacy of one initial owner and future several users in a self-hosted Ubuntu Docker deployment. Inputs: trusted user deletion/export/retention commands, domain data expiration, owner disable, operator key rotation, job/callback expiry; Outputs: redacted status, evidence of steps completed, tenant-scoped audit, safe export package, optional failure/UNCERTAIN report.
- Non-goals: guaranteeing physical deletion from someone else's public website or X/API provider; modifying third-party data without provider permission; claiming legal compliance without jurisdictional review; arbitrary logs/backup file deletion by Agent; retaining all raw evidence indefinitely; performing host administration through Agent.
- SC-06 Browser Cookie and Human Takeover isolation, SC-07 crash/cancel/retry/reconciliation, SC-08 secret and cross-user security, SC-01..05 provenance, SC-02 Watch/notification, expanded SC-11 event ingestion, SC-14 feedback and SC-15 multimodal file copies (use final acceptance-scenarios owner definitions).

### 1.1 Data classification and proposed policy matrix

The following periods are **hypothesized safe operational starting defaults**, not law, deployment settings or measured standards; the product owner must approve each by ADR. User preferences and resource-specific constraints may shorten them, never silently extend legal max.

| Class | Examples | Retention candidate | Constraints |
|---|---|---|---|
| C1 Sensitive identity/credentials | vault OAuth, model key, browser Cookie/storageState, Telegram binding | while active; revoke immediately, sealed record purged after 7 days reconciliation | No LLM/trace/raw export, key revocation on consent withdrawal |
| C2 Raw source artifacts | HTML, page screenshot, PDF upload, raw fetched X text, network response, DOM | 30 days after Task completion by default | user owner, linked Evidence, encrypted; only selected citations retained separately if needed |
| C3 Derived evidence/observations | claims, extracted prices, Watch observations, source times, provenance | 90 days by default, Watch data subject to explicit retention preference | stale observations never represented as current |
| C4 Task/conversation/working memory | Task/Run history, checkpoints, conversation thread, progress | until explicit deletion or user-defined period; inactive default 180 days proposed | snapshots/index and cache included; original goal constraints not leaked |
| C5 Operational telemetry | step durations, budget/use, model route, redacted errors | 14 days detailed, 90 days aggregated proposed | no content/token; minimize cardinality and owner tags |
| C6 Security audit / deletion receipts | login failures, owner denies, approval, grant revocation, data deletion manifest status | 90 days proposed, protect from user-level edits | safe metadata only, owner ref pseudonymized; legal hold policy separate |
| C7 Temporary browser/compute scratch | browser downloads, sidecar temp, HAR/screenshots during handoff | memory/scratch lifetime only, max 24h after lease expiry | secure temp scope, not in backups, explicit cleanup |
| C8 Backup snapshots | encrypted PG/object metadata/keyless backup | 30 days rolling proposed, verification and immutable manifests | user deletion pending until all live+restore conditions verified; no indefinite retention |
| C9 Cache/search indexes/outbox/notifications | search shards, embedding chunks, queued Telegram text, rendered thumbnails | shortest feasible, <= originating resource retention | tombstone checked before publish; no resurfacing on retry |

Avoid treating `retentionDays=0` as unlimited. Choose explicit policy enum (`KEEP_UNTIL_DELETED`, `EXPIRE_AT`, `EPHEMERAL`, `HOLD_WITH_EXPIRY`) and effective expiry; null/unconfigured is an error for new data. No assertion of compliance with a specific legal regime until the operator supplies applicable jurisdiction and policy.

## 2. OSS source and tests investigated

| OSS / ref / license | Code and test locations actually inspected | Mechanism / limits / adoption |
|---|---|---|
| **Langfuse** `71ddcb9094f53d19176a262f107871d8b2950fb0`, **mixed MIT (core) + Enterprise License (selected components)** | `worker/src/features/batch-data-retention-cleaner/index.ts` `BatchDataRetentionCleaner`: project cutoff and lease-bound batch delete; `worker/src/__tests__/batchDataRetentionCleaner.test.ts`: expired/nonexpired projects, multi-retention, lease loss, partial queries; `worker/src/features/media-retention-cleaner/index.ts`; `worker/src/queues/traceDelete.ts` `traceDeleteProcessor`; `worker/src/__tests__/traceDeleteQueue.test.ts` and `traceDeletion.test.ts` | **Code verified**, not run. Separate content/trace/media deletion and per-project cutoff are sound design ideas, not usable source code for this repo without license checks. Ingestion masking may be **Enterprise-only**. Source `LICENSE` explicitly points to `ee/LICENSE` for paths under `packages/shared/src/server/ee`. Use **independent implementation only**, no EE code copying [S1–S7]. |
| **Langfuse ingestion masking** same ref, Enterprise section | `packages/shared/src/server/ee/ingestionMasking/applyIngestionMasking.ts`: `applyIngestionMasking` with callback/retries and configurable failClosed; `worker/src/__tests__/ingestionMasking.test.ts` includes malformed/invalid masking responses and license behavior | **Code verified** but EE license! Warn: masking callbacks can transmit raw sensitive content to another service; this project prefers local structured allowlist redaction and **fail closed** for unsafe telemetry. Never send raw credentials to an external redaction provider [S8/S9]. |
| **OpenClaw** `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, MIT | `src/logging/redact.ts`: structured/form/url redaction primitives and bounded pattern masking; `src/security/audit-secrets.ts` | **Code verified** earlier redaction source, pattern protection useful in addition to default no-payload collection; regex masks are never sole guarantee [S10]. |
| **Playwright** `38fb53bf4c181abb91f1ca2a2ab741983c5d177c`, Apache-2.0 | `packages/playwright-core/src/server/browserContext.ts` `storageState` and `tests/library/browsercontext-storage-state.spec.ts` (storage captures Cookie/localStorage/IndexedDB) | **Code verified**: Browser state is sensitive derivative and must be included in deletion graph. Playwright does not implement tenant deletion guarantees [S11/S12]. |

**Alternatives**: A) One SQL cascade delete: **reject** (object blobs/SSE/queue/cache/backup unaffected). B) Synchronous delete all stores inside one HTTP request: **reject** (timeouts/crash/partial persistence). C) Per-object durable manifest with tombstone + idempotent workers + restore reconciliation: **adopt**. D) "Delete encryption key = deletion" unconditionally: **reject** if duplicate keys/backups remain. E) Copy Langfuse EE retention/masking implementation: **reject** due license restrictions and different data model; independently implement principles.

## 3. Ownership and trust graph

~~~text
Owner user (fresh authenticated) -> CHAT_08 Delete/Export API
 -> CHAT_09 PrivacyAdmission (tenant/user/target/audience/CSRF/policy validation)
 -> CHAT_10 canonical DB transaction: mark delete tombstone/epoch + manifest/outbox
 -> CHAT_02/07/13 stop new Run/Watch/Event intake for tombstoned scope
 -> CHAT_10 scheduled worker: DB rows -> Object store -> Search/Cache -> Trace -> Browser State
 -> CHAT_09 completion verifier: required storage receipts + backup horizon + restore suppression
 -> CHAT_08 safe state: PENDING/PARTIAL/COMPLETED
Operator -> approved retention policy/backup and hold exceptions (Agent cannot rewrite rules)
~~~

- CHAT_09 owns data classification, privacy/security admission, redaction/retention/deletion completion policy and safe audit requirements.
- CHAT_01 owns Event/Task/Artifact/Evidence shared object types; CHAT_10 physical DDL/queue/transaction/backup/sweeper; CHAT_08 API/UI; CHAT_04 Browser state; CHAT_06 Memory; CHAT_03 side-effects; CHAT_07 Notification outbox; CHAT_11 QA.
- **[PROVISIONAL: CHAT_01/10]** UserId, ownerUserId, TenantId and WorkspaceId mapping must not be conflated. Same-Tenant two users do not share private data automatically. See Round2 PR #9 proposals RC-09-01..08.

## 4. Requirements

| REQ | Level | Measurable outcome | SC |
|---|---|---|---|
| PRIV-001 | MUST | all persisted user data classified with owner/purpose/retention policy/version; unknown class quarantined | 06,08 |
| PRIV-002 | MUST | no raw credential/Cookie/secret/private prompt in normal logs, tracing, telemetry or export | 06,08 |
| PRIV-003 | MUST | owner export/delete requires fresh authenticated same-user action, CSRF and strict scope | 06,08 |
| PRIV-004 | MUST | deletion is a durable operation ID with immutable manifest, idempotency key, tombstone, status and progress | 07,08 |
| PRIV-005 | MUST | once tombstone commits, new model/tool/Watch/ingest/read/export/reindex cannot revive or expose deleted scope | 07,08,11 |
| PRIV-006 | MUST | delete sweeps DB, Blob, Browser storage, Memory, Evidence, Trace, search/cache, notifications/outbox safely | 06,07,08,15 |
| PRIV-007 | MUST | crash/lease expiry/repeated delete/delivery replays do not skip or misattribute deletion steps | 07 |
| PRIV-008 | MUST | deletion completion only after all required stores verified and backup restore suppression enforced | 07,08 |
| PRIV-009 | MUST | backup retention and restore replay honor durable deletion tombstones; no unreported resurrection | 07,08 |
| PRIV-010 | MUST | trace/event/audit record keeps only explicit safe metadata and no secrets, body, unsafe URL query | 08 |
| PRIV-011 | MUST | expired records swept by finite policy, with bounded batch, lease and per-owner concurrency | 07,09 |
| PRIV-012 | MUST | exported data package is access-controlled, encrypted, time-limited, with manifest and no raw secrets | 06,08 |
| PRIV-013 | MUST | user/Tenant ownership is verified at each step; no cross-user export, purge or trace projection | 06,08 |
| PRIV-014 | MUST | legal/operational hold is bounded, authorized, audited and visibly delays completion, not silent indefinite retention | 08 |
| PRIV-015 | MUST | redaction failure prevents raw sensitive content ingestion into telemetry, never fail open | 08 |
| PRIV-016 | MUST | removal/expiration cancels future notification with sensitive content and prevents queued stale event dispatch | 02,07,08 |
| PRIV-017 | MUST | privacy policy change/version migration cannot silently lengthen existing retention or change owners | 07,08 |
| PRIV-018 | SHOULD | user self-service dashboard shows data categories, expiry, export/delete progress and exceptions | 08 |
| PRIV-019 | EXPERIMENT | deploy per-resource DEK crypto-erasure, prove keys absent from all backup stores before declaring erasure | 09 |

## 5. Internal contracts [PROVISIONAL: CHAT_01/08/10]

These are CHAT_09's security-side interfaces, not duplicate canonical Domain Event types or physical SQL.

~~~ts
type DataClass = "IDENTITY_SECRET"|"RAW_ARTIFACT"|"EVIDENCE"|"TASK_HISTORY"|"TRACE"|"AUDIT"|
  "BROWSER_STATE"|"WATCH_OBSERVATION"|"CACHE_INDEX"|"NOTIFICATION"|"BACKUP";
type RetentionMode = "EXPIRE_AT"|"EPHEMERAL"|"KEEP_UNTIL_DELETED"|"HOLD_WITH_EXPIRY";
type DeletionState = "REQUESTED"|"SUPPRESSED"|"SWEEPING"|"VERIFYING"|"PARTIAL"|"COMPLETED"|"BLOCKED_HOLD";
type DeleteTarget = Readonly<{kind:"task"|"conversation"|"watch"|"artifact"|"account"; id:string}>;
type PrivacyCommand = Readonly<{target:DeleteTarget; requestKey:string; expectedRecordVersion:number;
  purpose:"USER_DELETE"|"EXPIRY"|"OPERATOR_APPROVED"; authenticatedSessionRef:string}>;
type DeletionManifest = Readonly<{operationId:string; ownerUserId:string; tenantId:string;
  workspaceRef:string; target:DeleteTarget; deletionEpochDecimal:string; policyVersion:string;
  createdAt:string; lastUpdatedAt:string; state:DeletionState; pendingStoreKinds:string[];
  completedReceiptRefs:string[]; blockedReasonCodes:string[]; backupSuppressionUntil:string|null}>;
interface PrivacyService {
  requestDelete(cmd:PrivacyCommand, verifiedActor:ServerPrincipal):Promise<DeletionManifest>;
  verifyDeletion(opId:string, executorIdentity:ServerOnlyWorkerIdentity):Promise<DeletionManifest>;
  requestExport(target:DeleteTarget, actor:ServerPrincipal):Promise<{operationId:string; state:string}>;
}
~~~

**Input rules:** canonical validated UUIDv7 identifiers are proposed by CHAT_01; never accept client ownerId/tenantId as authority; a resolved Tenant→Workspace mapping and ownerUserId ownership check is mandatory. Delete target is read by authenticated principal plus owner, no mass wildcard by default. `expectedRecordVersion` follows CHAT_01 CAS initial 1 (PR #10 revision default0 conflict pending). `deletionEpochDecimal` is monotonic DB-sourced bigint, serialized as lossless decimal. It is **not** Run.leaseEpoch or goalRevision; shared meaning to be proposed to CHAT_01/10.

**API example** [PROVISIONAL: CHAT_08]:
~~~json
{
  "operationId": "019a2fc0-4b21-7af0-9c22-34f78b6a1d04",
  "state": "PARTIAL",
  "requestedAt": "2026-10-30T00:00:00.000Z",
  "completedStores": ["db", "browser_state"],
  "pendingStores": ["artifact", "trace", "backup_retention"],
  "backupSuppressionUntil": "2026-11-29T00:00:00.000Z",
  "cause": "STORAGE_UNAVAILABLE"
}
~~~
The example values are illustrative only, not live operations. No raw URLs, Secret IDs or private source text in response.

## 6. State machine and event sequence

~~~text
ACTIVE_CONTENT --verified owner request / expiry--> REQUESTED
REQUESTED --DB tombstone + manifest + event commit--> SUPPRESSED
SUPPRESSED --worker claim + fencing--> SWEEPING
SWEEPING --all receipts collected--> VERIFYING
SWEEPING --store unavailable/partial--> PARTIAL --retry claim--> SWEEPING
VERIFYING --live stores clear and backup replay suppression valid--> COMPLETED
VERIFYING --missing receipt--> PARTIAL
REQUESTED/SUPPRESSED/SWEEPING --bounded legal hold--> BLOCKED_HOLD
BLOCKED_HOLD --authorized release or expiry--> SWEEPING
~~~

**Critical distinction:** `COMPLETED` is a **defined product state** meaning live purges verified plus effective suppression of backup resurrection, **not necessarily physical erasure from every still-retained encrypted backup**. User-visible report must distinguish "live data gone" from "backup expires by DATE" and never say "all copies are physically erased" before that is true. If stronger deletion assurance is required, keep `BACKUP_PENDING` or equivalent until backup expiry and key destruction are verified; choose exact public semantics with CHAT_08/10.

Normal command: fresh auth/CSRF → owner-resolved Task/Artifact/Account check → same DB transaction assigns deletion epoch, owner-scoped tombstone, manifest, outbox → return 202 + operation ID → active Run/Watch/notifications revalidate tombstone prior to next external side-effect → sweeper enumerates references with owner and same DB snapshot version → deletes idempotently per store → writes per-store receipt/status → verifier checks no readable copy + no pending derived object + suppression of old backups → publish redacted status.

Expiry: scheduler issues deletion intent with system credential already limited to stored owner/policy; if retention date changes concurrently, compare policyVersion and recordVersion before committing. Default no unlimited collection.

## 7. Pseudocode and concurrency

~~~ts
async function requestDeletion(actor, target, requestKey, expectedVersion) {
  const verified=await authn.requireFreshForPrivacy(actor);
  return db.transaction(async tx=>{
    const resource=await tx.findForOwnerAndTenant(target,verified.userId,verified.tenantId);
    if(!resource) throw NotFound();
    if(resource.recordVersion!==expectedVersion) throw Conflict412();
    const existing=await tx.findIdempotent(verified.tenantId,verified.userId,requestKey);
    if(existing) return existing.manifest;
    const epoch=await tx.nextDeletionEpoch(resource); // monotonic, separate from Run leaseEpoch
    const manifest=await tx.createDeletionManifest(resource,epoch,privacyPolicy.version);
    await tx.insertOwnerBoundTombstone(resource,epoch);
    await tx.outboxAppendPrivacyEvent(manifest.id,"SUPPRESSED"); // same commit
    return manifest;
  });
}
async function sweep(manifestId,workerLease) {
  const m=await db.getDeletionManifestForUpdate(manifestId);
  if(!await authz.verifyServiceOwnerScope(m,workerLease)) throw Denied();
  if(m.state==="COMPLETED") return m;
  if(await holds.activeBoundedHold(m)) return markBlockedHold(m);
  const refs=await inventory.enumerateAllDerivedRefs(m.target,m.ownerUserId,m.tenantId);
  for(const ref of refs){
    await worker.assertLeaseAndDeletionEpochCurrent(m,workerLease);
    if(await receipts.isDone(m.id,ref.store,ref.hash)) continue;
    try{
      await stores[ref.store].deleteIdempotently(ref.key); // external side effect; not in DB txn
      await db.commitReceiptWithCAS(m.id,ref.store,ref.hash,workerLease);
    }catch(error){
      await db.recordPartialSanitizedFailure(m.id,ref.store,error.code);
      return PARTIAL;
    }
  }
  const proof=await verifier.checkNoLiveOrDerivedReadableCopyAndRestoreSuppression(m);
  return proof.complete ? await db.setCompletedCAS(m.id,workerLease) : await db.setPartialCAS(m.id,proof.missing);
}
async function admitHistoricalIngress(payload) {
  const lineage=await ownerResolver.verifyLineage(payload.taskId,payload.runId);
  if(await deletionRegistry.isSuppressed(lineage)) throw DeletedGone410();
  return ingestion.commitIfScopeStillActive(payload); // transactional recheck at commit boundary
}
~~~

**Race rules**:
- Delete vs queued notification/Watch: suppression commit is authorization boundary; outbox worker must recheck deletion epoch just before publish. A remote send already completed cannot be recalled; record a safe uncertain receipt. No new send after suppression.
- Delete vs active browser session: reject further Browser navigation/restore and revoke Credential leases; already-in-flight external action cannot be guaranteed undone.
- Delete vs DB/object partial failure: tombstone remains even if blob purge is delayed, ensuring content inaccessible.
- Two simultaneous deletions: unique (owner,target,active deletion intent)/idempotency and CAS; multiple workers may attempt delete but per-step receipts/lease fence prevent false completion.
- Retention extension vs active delete: no resurrection or extending deletion deadline after owner consent withdrawn.
- Existing shared blob referenced by another owner's active record: reference count and per-owner data encryption/context must not leak; delete only when no authorized reference remains; the original subject's index projection is immediately removed.
- Backup restore: before exposing API/Worker, restore-time reconciliation replays durable tombstone registry and required deletion epochs from operator-protected backup/suppression ledger independent of older data snapshot. If unavailable, restore **BLOCKED_UNSAFE_RESTORE**, not default to active content. A newly restored vault key cannot enable revoked credentials.
- Cross-store transactions cannot guarantee exact physical deletion at the same instant. Retain idempotent manifests and bounded retry with accurate Partial status.

## 8. Failure and security matrix

| Failure | Detection / recovery / visible outcome |
|---|---|
| delete request from other user | owner+tenant mismatch; 404, zero purge, audit safe denied |
| expired session/CSRF invalid | 401/403, zero manifest |
| tombstone committed, worker crashes before blob deletion | resume manifest, idempotent sweep, public PENDING/PARTIAL |
| blob deleted but DB receipt commit lost | re-run delete (idempotent, not re-create), verify absent and write receipt |
| encrypted object shares reference with still-authorized owner | deny cross-owner visibility, per-owner reference removal; defer shared physical deletion |
| trace delete failure | live suppression maintained, retry bounded, state PARTIAL |
| legal hold registered after delete | hold requires operator authority/expiration, BLOCKED_HOLD and public explanation without protected contents |
| cached search or Memory index rebuild | query suppression/deletion epoch before indexing, no resurrection |
| backup restored without tombstone/key revocation ledger | block restore; do not expose old user data |
| stale notification outbox after user disable/delete | revalidate and refuse dispatch; no content sent |
| masking/redaction callback unavailable | do not ingest raw sensitive telemetry; mark privacy telemetry failure |
| export generated then user revokes session | revoke download grant, delete scratch package, future fetch denied |
| security audit log attempts to persist token/body | structured schema filter; reject unsafe log record |
| retention cleaner lease lost | stop batch after fence failure, no unowned updates; partial retry |
| dataset stale or owner mapping inconsistent | quarantine/orphan exception, no default owner inference |

## 9. Security, privacy, encryption and audit

- Audit schema includes `requestId, actorKind, pseudonymousActorRef, policyVersion, decisionCode, resourceKind, safeResourceCorrelation, eventTime, sanitizedReason, retentionDeadline`; excludes prompt, Cookie, API tokens, Browser state, raw URLs with queries, full page text, user email/name, backup key material. Audit sink has non-Agent write access and tamper-evident batched hashes where feasible, but this is not a substitute for privileged operator protection.
- Cryptographic storage at rest and per-secret owner binding from `secrets-and-credentials.md`. Encryption does not remove need for deletion or access control. Physical secure deletion on SSD/cloud object snapshots may not be immediately provable; use logical tombstone, configured expiry and documented assurance level. Key destruction needs backup-key audit.
- Consent/purpose minimization: raw page data and browser screenshots may contain third-party PII; extract only what Task needs and redact before LLM or external telemetry. A site providing HTML does not grant the right to store/reuse it indefinitely.
- If masking tool is remote, raw telemetry would itself egress; such tooling prohibited absent explicit owner-approved bounded recipient. Langfuse EE ingestion masking code may fail open by configuration; this Harness requires **fail closed** when sensitive telemetry cannot be safely transformed.
- Logs must not contain original body; correlation IDs do not encode User IDs/URLs. External event payloads from X/Telegram/Gmail retain lower trust and may carry third-party sensitive data; minimize before persistent index.
- Owner cannot arbitrarily purge immutable security evidence needed to investigate a live abuse incident, but audit content must stay minimal and hold must be reviewed with expiry and policy, not an unbounded pretext.

## 10. Metrics and performance evaluation

Measure `privacy_deletion_requested/suppressed/partial/completed`, backlog age, per-store sweep latency p50/p95, outstanding backup expiry, count of orphaned references, duplicate receipts, stale reingestion blocked, raw-secret leakage canary rate, retention overrun duration and failed export grant rate. Never tag telemetry with raw user content/secret or full URL.

Initial proposed limits, not measured guarantees: sweeper ≤100 objects per transaction/work batch, 1 deletion worker per owner at a time, worker lease 60s with renewal, 3 transient retries per store attempt with bounded exponential backoff, 5 minutes max single sweep stage, exports ≤1 GiB and active 24h maximum, signed download grant 10 minutes and one owner. Hard policy must specify behavior when limits exceeded (Partial and operator action). Evaluate 2 users, 1000 artifacts, browser profiles, watches/outbox and backups across repeated crashes, compare resource cost and absence of leak. Real SLO not yet validated.

## 11. Persistence, transactions, migration [PROVISIONAL: CHAT_10]

Required physical concepts (not DDL here): `resource_privacy_index` per owner/tenant/workspace/resource/data class/retention mode/createdAt/expiresAt/hold state; `deletion_manifests` operation ID/target/owner/deletionEpoch/state and policyVersion; `deletion_steps` store kind/ref hash/status/attempts/receipt; `deletion_tombstones` target/owner/epoch persistent cross-backup; `export_manifests` export owner/expiry/keyRef/status; minimal `audit_events` separate from model trace. Composite FKs and uniqueness prevent cross-tenant references. SQL owner is CHAT_10, event/Resource type CHAT_01, controlled API CHAT_08.

Migration phases:
1. EXPAND: introduce privacy classification, owner binding, tombstones, default retention policy without assuming unowned records belong to first user. Quarantine old records lacking owner.
2. BACKFILL: inventory artifacts/trace/browser/Watch/cache/index and attach source/owner/purpose from canonical lineage; ambiguous scope remains inaccessible.
3. VALIDATE: parallel read/write with tombstone checks and no unclassified inserts; run deletion E2E and restore drill before enforcement.
4. CONTRACT: prevent read/write bypass and enable finite sweeper, asynchronously clean legacy orphans, retire old unguarded API endpoints.
5. ROLLBACK: never roll back tombstones or reactivate deleted content. Older binary that cannot read deletion epoch must **not** start.

Retention policy updates are versioned and frozen for already-created data unless new consent shortens retention. Backup expiration is determined by physical backup lifecycle, no unbounded indefinite snapshot. Stale backup reopening must consult tombstone ledger held separately from user data snapshots.

## 12. API/UI proposals [PROVISIONAL: CHAT_08]

| Operation | Auth / response |
|---|---|
| `GET /api/v1/privacy/inventory` | authenticated owner; categories, retention deadlines and safe sizes only |
| `POST /api/v1/privacy/exports` | fresh auth/CSRF, idempotency; 202 operationId; no raw credentials |
| `GET /api/v1/privacy/exports/{opId}` | owner-only safe progress; explicit partial/truncated status |
| `POST /api/v1/privacy/deletions` | fresh auth/CSRF, Idempotency-Key+If-Match, 202 manifest ID; tombstone after DB commit |
| `GET /api/v1/privacy/deletions/{opId}` | owner-only status plus completed/pending store categories and backup expiry |
| `POST /api/v1/privacy/deletions/{opId}/cancel` | **Not offered** after suppression commit; cancellation cannot restore deleted content |
| `GET /api/v1/privacy/policies` | category/expiry and retention rationale; sensitive metadata minimized |

Do not present "complete" on 202 acceptance. Show deleted live data status and separate backup expiration limit; if hold prevents purge, surface bounded reason and expiry. Audit/retention operator-only changes must never be exposed as model-authored Tool.

## 13. Implementation PR sequence (different runtime repository)

1. **PRIV-PR1** data classification, safe structured log/tracing schema, audit sink canary tests; privacy inventory with owner lineage.
2. **PRIV-PR2** deletion tombstone + manifest + outbox DB transaction and suppression checks in Task/Worker/Watch/Browser/SSE.
3. **PRIV-PR3** idempotent object/search/cache/trace/Browser/Memory/notification sweeper, lease/fencing and partial verification.
4. **PRIV-PR4** encrypted export and download grant, backup restore reconciliation, secret/Browser credential revocation.
5. **PRIV-PR5** 2-user E2E, kill-and-resume, wrong backup, orphan detection and legal hold policy review, production retention metrics.

## 14. Tests — designed only, NOT RUN

| TEST | Setup / action | Expected |
|---|---|---|
| PRIV-T01 | ingest all classes including PDF/screenshot and unknown source | each has owner and explicit privacy class/policy; unknown quarantined |
| PRIV-T02 | canary Cookie/token/private prompt in trace/logger/error/export | unauthorized locations contain **0** canary bytes |
| PRIV-T03 | user B invokes deletion/export for A, CSRF missing, session expired | no delete/export data, 401/403/404 |
| PRIV-T04 | two duplicate deletion POSTs with same key; crash at tombstone transaction | one manifest/epoch, durable suppression, 202 not "done" |
| PRIV-T05 | Watch queued, Tool pending, derived index refresh after suppression | no new Tool/data reingest/notification/exposure |
| PRIV-T06 | DB+object+Browser+Memory+Trace+Cache+Outbox with cross-referenced artifacts | each owned ref absent/unreadable after sweep or explicit PARTIAL |
| PRIV-T07 | kill sweeper before delete, after object delete but before DB receipt, after receipt | rerun idempotent; only correct owner, no false completion |
| PRIV-T08 | one storage backend offline and another succeeds | PARTIAL; cannot claim completed; pending store listed |
| PRIV-T09 | restore old backup snapshot with deleted resource, missing tombstone ledger | BLOCKED_UNSAFE_RESTORE; deleted data never exposed |
| PRIV-T10 | structured audit of login, denial, approval, deletion, error | safe metadata only; no token, private URL or PII body |
| PRIV-T11 | 1000 expired records and finite worker locks; owner A/B distinct policy | cap/lease enforced; in-scope A only; no indefinite retries |
| PRIV-T12 | export with short-lived authenticated owner-only link, then revoke session | expired/revoked download denied; no raw vault secret |
| PRIV-T13 | cross-user attempt with same workspace but different ownerUserId | no count/data/purge crossing same-tenant user boundary |
| PRIV-T14 | active legal hold with expiry, hold revoked, worker restart | bounded block and resumed purge after expiry/approval |
| PRIV-T15 | redaction fails or external masker times out | sensitive telemetry not ingested raw (fail closed) |
| PRIV-T16 | pending Telegram notification and callback after delete | no later private content sent/ingested |
| PRIV-T17 | retention policy edit lengthens expiry, old unowned rows, CAS version conflict | migration blocked/quarantine, no silent extension |
| PRIV-T18 | UI inventory/progress/exception under owner A, user B fetches | all categories correct and B cannot access A |
| PRIV-T19 | DEK key destruction test with backed-up wrapped key still alive | physical erasure NOT claimed until all key copies verified |
| PRIV-T20 | reference shared across user-owned artifacts, delete A only | B's authorized content remains; A's access/index removed |
| PRIV-T21 | Watch/Browser worker loses deletion epoch while polling | no stale task or browser state resurrected |
| PRIV-T22 | backup expiry and controlled restore after tombstone replay | old data stays suppressed; completed assurance correctly labeled |
| PRIV-T23 | failure during per-object media deletion, then repeated sweeper | no missed artifacts, no double count of success |
| PRIV-T24 | public audit export includes attacker-crafted source URL | only safe correlation code; private query stripped |

All fixture stores/emulated providers; **no real user data, credentials or production deletion**. Actual tests executed: **none**.

## 15. Traceability, requirements → tests → scenario

| REQ | TEST IDs (prefix `PRIV-T`) | SC |
|---|---|---|
| PRIV-001 | T01,T17 | 06,08 |
| PRIV-002 | T02,T10,T12 | 06,08 |
| PRIV-003 | T03,T12 | 06,08 |
| PRIV-004 | T04,T07 | 07,08 |
| PRIV-005 | T05,T16,T21 | 07,08,11 |
| PRIV-006 | T06,T20,T23 | 06,07,08,15 |
| PRIV-007 | T04,T07,T23 | 07 |
| PRIV-008 | T08,T09,T22 | 07,08 |
| PRIV-009 | T09,T19,T22 | 07,08 |
| PRIV-010 | T02,T10,T24 | 08 |
| PRIV-011 | T11,T17 | 07,09 |
| PRIV-012 | T03,T12 | 06,08 |
| PRIV-013 | T03,T13,T20 | 06,08 |
| PRIV-014 | T14 | 08 |
| PRIV-015 | T02,T15 | 08 |
| PRIV-016 | T05,T16,T21 | 02,07,08 |
| PRIV-017 | T01,T17 | 07,08 |

## 16. Open dependencies / proposals

| ID / owner | Severity | Proposed change / test / exit |
|---|---|---|
| PRIV-P01 / CHAT_01 | **P0** | canonical UserId/TenantId/ownerUserId and Artifact/Evidence/Event lineage plus deletion tombstone/epoch shared reference; PRIV-T01/T13 |
| PRIV-P02 / CHAT_10 | **P0** | physical owner FKs/tenant↔workspace mapping, manifests/steps/revocation, blob/index/backup sweeper, restore suppression and lease; PRIV-T04/T09/T22 |
| PRIV-P03 / CHAT_02/03/07/13 | **P0** | suppression before new Tool/Watch/notification/event, UNKNOWN prior external effect and retry boundary; PRIV-T05/T16/T21 |
| PRIV-P04 / CHAT_04/06/14/15 | **P0** | Browser state, Memory/Skills/learned action cache, multimodal raw content and derived index lifecycle; PRIV-T06/T20 |
| PRIV-P05 / CHAT_08 | P1 | fresh-auth export/delete API and partial statuses, expiry and hold disclosures; PRIV-T03/T08/T18 |
| PRIV-P06 / CHAT_11 | P1 | multi-store crash, backup restore, key-shred and shared blob regression harness; PRIV-T07/T19/T23 |
| PRIV-P07 / CHAT_00 | P1 | retention periods and legal hold/privacy promise ADR; product-policy owner must approve defaults before production |
| PRIV-P08 / CHAT_09 secrets | P0 | encrypted Browser/credential state, key rotation/revocation and redacted trace policy; PRIV-T02/T09 |

**Provisional/release blockers**: canonical owner/Workspace mapping and UUIDv7 vs DB default UUIDv4, CAS initial recordVersion 1 vs DB 0, Run leaseEpoch persistence and GoalRevision/activeRunId (Round2 PR #9) remain unLocked. No deletion end-to-end, restore drill or retention policy law review has been executed. Strictly keep Draft.

## 17. Sources (inspected 2026-10-30 JST)

- **[S1] [Code verified]** Langfuse `71ddcb9094f53d19176a262f107871d8b2950fb0`, `BatchDataRetentionCleaner` and project cutoff/lease in `worker/src/features/batch-data-retention-cleaner/index.ts`: https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/features/batch-data-retention-cleaner/index.ts
- **[S2] [Test source verified, not run]** `worker/src/__tests__/batchDataRetentionCleaner.test.ts` (per-project periods, expiry/no-expiry, lease loss and recovery): https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/__tests__/batchDataRetentionCleaner.test.ts
- **[S3] [Code verified]** Langfuse `traceDeleteProcessor` in `worker/src/queues/traceDelete.ts` (pending deletion and multi-store processing): https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/queues/traceDelete.ts
- **[S4] [Test source verified, not run]** `traceDeleteQueue.test.ts`: https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/__tests__/traceDeleteQueue.test.ts
- **[S5] [Test source verified, not run]** `traceDeletion.test.ts` (ClickHouse/Postgres/S3 deletion, shared media references): https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/__tests__/traceDeletion.test.ts
- **[S6] [Code verified]** Langfuse `MediaRetentionCleaner` `worker/src/features/media-retention-cleaner/index.ts`: https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/features/media-retention-cleaner/index.ts
- **[S7] [License file verified]** Langfuse `LICENSE` MIT core + `ee/LICENSE` Enterprise restrictions, **do not copy EE source into this project**: https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/LICENSE and https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/ee/LICENSE
- **[S8] [Code verified; EE restricted]** `applyIngestionMasking` in `packages/shared/src/server/ee/ingestionMasking/applyIngestionMasking.ts`: https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/packages/shared/src/server/ee/ingestionMasking/applyIngestionMasking.ts
- **[S9] [Test source verified; EE restricted]** `worker/src/__tests__/ingestionMasking.test.ts`: https://github.com/langfuse/langfuse/blob/71ddcb9094f53d19176a262f107871d8b2950fb0/worker/src/__tests__/ingestionMasking.test.ts
- **[S10] [Code verified]** OpenClaw MIT `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, `src/logging/redact.ts`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/logging/redact.ts
- **[S11] [Code verified]** Playwright Apache-2.0 `38fb53bf4c181abb91f1ca2a2ab741983c5d177c`, `BrowserContext.storageState`: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/packages/playwright-core/src/server/browserContext.ts
- **[S12] [Test source verified, not run]** Playwright storage round-trip: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/tests/library/browsercontext-storage-state.spec.ts
- **[S13] [Docs verified]** OWASP Logging Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html ; NIST SP 800-88 Rev. 1 media sanitization (apply local policy to backups, not a guarantee of remote deletion): https://csrc.nist.gov/pubs/sp/800/88/r1/final
- **[S14] [Hypothesis]** Project-specific retention periods, delete/export APIs, record layouts, backup suppression and crypto-shred proof are proposed architecture not validated or deployed.
- **[S15] [Issue/PR check incomplete]** Specific upstream bug/PR was not verified; cited functions and regression tests are the primary source audit and this is explicitly an evidence gap.

## 18. Review Gate self-audit

- [x] data classes/ownership, alternate solutions, pinned external implementation and tests, license restrictions, privacy/security boundary, API & pseudocode, finite resource budgets, crash/retry/backup restore, 17 MUST requirements all mapped to tests and SC.
- [ ] physical DDL, shared contract/ownership mapping, approved retention periods/hold policy, E2E worker/backup execution, measured throughput, specific upstream issue/PR review. No Implemented/Verified status claimed.
- **Disposition: Draft, NOT LOCKED** pending cross-team owners and security review.
