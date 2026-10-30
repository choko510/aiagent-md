# Event Envelope and Stream — Detailed Implementation Specification

> Owner: CHAT_01 | Canonical: docs/01-contracts/event-envelope-and-stream.md | Status: Draft/PROVISIONAL | Rev 0.1 | Research 2026-10-30 JST | Baseline main b45d4e989998663a00ef09673e69ae1f4191f00d | Dependencies: core-domain-models PR #2, state-machine 01-03, CHAT_02/03/07/08/09/10/13.

## 0. 結論と採用設計

**PostgreSQLで永続したDomain Eventが真実で、Web SSE/Telegram/Workerの送出は派生配達。** ひとつのドメイン変更とEvent appendとOutbox意図は同一transaction。Event ID（UUIDv7）、aggregate単位sequence、tenant delivery offset、trace/causation、schema versionは独立。SSE再接続は権限scope付きopaque cursorで再配信する。at-least-onceでありexactly-once送達を主張しない。モデルのtoken/chunk previewは持続的な業務Eventに昇格しない。

## 1. 責務と非責務・受入シナリオ

Own: Envelopeの必須属性、append/sequence/ordering/schema evolution、Event→Outbox、subscription read model、SSEの重複・欠落・resume解釈、Trace連携。Non-own: 各domainでの実行可否（01-03）、Tool副作用台帳（CHAT_03）、Scheduler（CHAT_07）、物理DDL（CHAT_10）、REST HTTP routeとSSE wire transport（CHAT_08）、認可policy（CHAT_09）、Webhook external event normalization（CHAT_13）。対応SC-01..09、およびSC-10..16：特に02,06,07,08,11,12,13,15,16。

## 2. 参考OSS実コード比較（2026-10-30静的読解、テスト未実行）

| OSS/commit/license | 実ソース・テスト | 参考点と不採用点 |
|---|---|---|
| LangGraph / ffc3336367e8139668d11e5f0b0e6def57e7c762 / MIT | libs/langgraph/langgraph/pregel/_loop.py の PregelLoop.put_writes/_put_checkpoint/_emit; libs/langgraph/tests/test_stream_events_v3.py の TestV2Stream/stream eventsケース [S1] | 実行中のstream更新・checkpointを別処理で扱う。StreamPartの内部形式はアプリ固有の永続SSE cursor/tenant認可を代替しない |
| OpenAI Agents SDK Python / cd26bbbc52847845fde73b9952b51e62e2bcbdc0 / MIT | src/agents/stream_events.py の RawResponsesStreamEvent / RunItemStreamEvent / AgentUpdatedStreamEvent と tests/test_stream_events.py の event order/handoff/tool search [S2] | raw model deltaと処理済みRun itemが異なる。これをpreviewとcommit済みdomain eventの区別に採用。SDK自身のstreamをreconnect可能な履歴正本にしない |
| MCP TypeScript SDK / 39ae8e6192cf43273aaaf2de4169dfa9f7216e28 / repo MIT→Apache-2.0 transition、core-internal package.json=Apache-2.0 | packages/core-internal/src/types/spec.types.2025-11-25.ts の JSONRPCRequest/Notification/ProgressToken、test/e2e/scenarios/dynamic.test.ts [S3] | external protocol request/progress tokenは内部event IDでも認可の根拠でもない。protocol revisionの変化をadapterで吸収 |

Issue [S4] LangGraph #8115 のcheckpoint書込競合報告はIssue report、当Harnessで未再現。独立SSE outbox transaction採用の理由の一つだが問題発生率は不明。

代替: (A) WebSocket/SSEへ直接送信→再起動欠落するため却下。(B) メモリ内event queueだけ→長期監視/復帰が破綻し却下。(C) DB append + transaction outbox + replay cursor→採用。(D) 全Domainをevent-sourcing-onlyで再構築→初期リスクとmigration複雑度が大きく保留。外部Broker/Kafka導入は初期対象外。

## 3. Dependency・権限と正本

Task/Run/Watch/ExternalEventを所有するServiceは自分が管理するEntityの状態変更を検証し、EventWriterへcommit依頼する。単独LLM、Browser、Tool result、ExternalEventは直接append不可。イベントはすべてtenant+owner lineage、visibility scopeとaggregate referenceを持つ。ActorRefはcore-domain、認可主体解決はCHAT_09。Event状態名は01-03、Tool IOは01-04、EvidenceRefは01-05、Errorは01-06を参照。ExternalEvent（CHAT_13）は非信頼の観測データであり、この内部DomainEventと同じ型にしない。

## 4. Requirements

| REQ | Level | 条件 | SC |
|---|---|---|---|
| EVT-001 | MUST | mutation+Event+Outboxを1 DB transactionでcommit | 02,07,11 |
| EVT-002 | MUST | eventIdはUUIDv7、tenantId+aggregateId+aggregateSeqは一意 | 07,08 |
| EVT-003 | MUST | major/minor schemaVersion判定とunknown major拒否、無言変換禁止 | 07 |
| EVT-004 | MUST | owner/audience scopeで全read/replayを再認可 | 06,08 |
| EVT-005 | MUST | SSE再接続でcommit済みeventが欠落しない。重複はclient dedupe | 07 |
| EVT-006 | MUST | 同一aggregateのsequenceは1から単調増加・隙間は欠落ではなくrecordで説明 | 07 |
| EVT-007 | MUST | causationId/correlationId/traceIdでTask/Watch/Toolを追跡 | 02,07,11 |
| EVT-008 | MUST | previewとcommittedを分け、previewでは決済/完了を描画不可 | 04,07 |
| EVT-009 | MUST | 切断中にreplay保持期限切れなら明示GONE＋snapshot再同期 | 07 |
| EVT-010 | MUST | unknown origin/invalid enum/不適切payloadをdurable eventに受理しない | 08 |
| EVT-011 | MUST | Event delivery workerがACKを失っても改ざん/重複処理を防ぐ | 07 |
| EVT-012 | MUST | Event sequence bigintをwire decimal stringで無損失表現 | 07 |
| EVT-013 | SHOULD | filtered streamの非連続offsetをgapと誤検知しない | 02,11 |
| EVT-014 | SHOULD | progress/message/terminalの観測遅延を計測 | 09 |
| EVT-015 | LATER | archived eventをcold storageからreplayする管理API | 07 |

## 5. Canonical Event 型・validation

~~~ts
type EventId = Brand<string, "EventId">; // trusted server UUIDv7
type DecimalInt64 = string; // exact canonical positive signed-int64 decimal, 1..INT64_MAX
type EventKind =
  | "task.created" | "task.state_changed" | "task.goal_changed"
  | "run.claimed" | "run.progress_committed" | "run.state_changed"
  | "watch.triggered" | "watch.state_changed"
  | "tool.intent_recorded" | "tool.outcome_recorded"
  | "approval.requested" | "approval.resolved"
  | "evidence.recorded" | "result.published"
  | "notification.queued";
interface DomainEventEnvelopeV1 {
  schemaVersion: "1.0"; eventId: EventId; eventKind: EventKind;
  tenantId: TenantId; aggregateType: "task"|"run"|"watch"|"approval"|"tool"|"evidence"|"notification";
  aggregateId: string; aggregateSequence: DecimalInt64;
  deliveryOffset: DecimalInt64; // tenant stream offset, allocated transactionally
  taskId: TaskId|null; runId: RunId|null; watchId: WatchId|null;
  ownerUserId: UserId|null; audience: "owner_private"|"operator_audit";
  causationEventId: EventId|null; correlationId: string; traceId: string|null;
  actor: ActorRef; occurredAt: UtcInstant; committedAt: UtcInstant;
  payloadSchemaVersion: "1.0"; payload: JsonObject; // event-kind validated, non-secret
}
interface StreamCursorV1 {
  version: 1; tenantId: TenantId; viewerUserId: UserId;
  subscriptionFingerprint: string; lastDeliveryOffset: DecimalInt64;
  issuedAt: UtcInstant; expiresAt: UtcInstant; // authenticated MAC covers all
}
~~~

**EventKind表は初期v1の共有提案**。追加eventは01-02のownerレビューを経てminor update、非互換payloadは新major/kind。Payload JSON Schemaはevent-kindで判別しunknown key reject。任意のHTML、Cookie、raw URL token、tool arguments、モデル生出力をEvent payloadへ直接含めずArtifact/Evidence/Secret-safe referenceに置く。ownerUserId=nullはoperator auditなど専用政策だけで許可し、一般公開のnull ownerは拒否。[PROVISIONAL: CHAT_09]

**整数規約**: aggregateSequence/deliveryOffsetは符号なしでなくsigned PostgreSQL BIGINTの正値1..9223372036854775807。JSONは正規十進文字列; leading zero、指数表記、JSON number、0/負値、範囲超過をreject。Event IDはUUIDv7であり配達順序をID timestampで決めない。aggregateTypeとaggregateIdが指定Taskに属することをTransactionで検証する。イベントのpayloadはImmutable。Event schemaVersion≠Resource recordVersion≠Tool schemaVersion。

## 6. Sequence/ordering/flow

~~~text
Authenticated Command -> Domain Service -> DB TX BEGIN
  -> tenant/owner + expected recordVersion + transition validation
  -> domain state CAS / ledger intent / checkpoint change
  -> lock per-aggregate sequence row -> next sequence
  -> lock per-tenant stream cursor -> next delivery offset
  -> INSERT event envelope + outbox reference
  -> COMMIT
      -> UI API ack (durable id)
      -> Outbox relay at-least-once -> SSE dispatcher / queue
          -> authenticated owner stream -> replay from cursor, client dedup
~~~

**順序**: 同aggregateではCOMMITの原子的sequence順。異aggregateではtenant deliveryOffsetの昇順をtransportに使用するが、異なる因果関係が時系列上保証されるとは断言しない。CausationIdとrecordVersionで因果を説明する。stream filteringでoffsetが飛ぶのは正常（非認可イベントは配信せずcursorだけ進む）。Outboxの並行consumerが逆順で配信した場合、serverは履歴DBからoffset昇順再構築。何かのeventが存在しないと判断する条件は保持範囲の欠損またはaggregateSequenceの不一致。

## 7. Pseudocode（実装予定、SDK APIではない）

~~~ts
async function appendInOwnerTransaction(tx, ctx, mutation, input) {
  requireTrustedServiceOrAuthorizedUser(ctx);
  await tx.checkTenantOwnerAndCurrentVersion(input.aggregate, ctx);
  const updated = await tx.applyDomainMutationCAS(mutation);
  const n = await tx.allocateNextAggregateSequence(input.aggregate);
  const offset = await tx.allocateNextTenantDeliveryOffset(ctx.tenantId);
  const event = validateEventKindAndAudience({
    ...input, eventId: serverUuidV7(), aggregateSequence: decimal(n),
    deliveryOffset: decimal(offset), committedAt: tx.databaseClock(),
    actor: ctx.serverResolvedActor
  });
  await tx.insertImmutableEvent(event);
  await tx.insertOutboxUnique(event.eventId, event.eventId);
  return updated;
}
async function resumeStream(auth, opaqueCursor, filter) {
  const viewer = await authenticate(auth);
  const cursor = await verifyMacAndDecode(opaqueCursor);
  requireSameTenantViewerAndFingerprint(cursor, viewer, filter);
  const snapshot = await db.readStreamBoundsAndAuthorizedEvents(viewer, filter);
  if (cursor.lastDeliveryOffset < snapshot.earliestRetainedOffset - 1n)
    return signalResetRequired(snapshot.snapshotVersion);
  for await (const e of snapshot.eventsAfter(cursor.lastDeliveryOffset)) {
    if (!await authorizeEventAgain(viewer,e)) continue;
    emitSse(e.eventId, sanitizeEventForAudience(e)); // at-least-once
    updateCursorToDeliveredOffset(e.deliveryOffset);
  }
}
~~~

**Client replay:** persist eventId dedupe set per stream and last committed cursor; if received older/duplicate event ignore; on reconnect, present latest authenticated signed cursor. If no cursor, fetch snapshot+current cursor atomically where possible. *Optimistic preview* has a separate per-run ephemeral channel and previewId, never writes durable event/ETag; upon reconnect simply discard stale previews. Preview/committed event collision resolved in favor of persisted Event.

## 8. Failure matrix

| Fault | Recovery | Expected |
|---|---|---|
| crash before event insert/commit | whole transaction rollback; command re-submit using idempotency receipt | no state mutation without event |
| crash after COMMIT before SSE | outbox relay and replay | event durable |
| ACK lost after SSE | duplicate resend | client idempotent by eventId |
| subscriber disconnect | replay after last cursor | no false completion |
| old cursor/retention gap | 410/RESET_REQUIRED + authorized snapshot resync | no silent skip |
| event schema unknown | reject or quarantined event with audit; old renderer not crash | explicit unsupported |
| new owner after revocation | per-message fresh authz and connection close | no post-revocation data |
| 2 producers same aggregate | CAS + sequence lock | unique n, one updater |
| large payload/raw secret | schema rejects; artifact ref only | no secret leak |
| stale worker lease | EventWriter checks Run epoch and Task state | old owner append 0 |
| out-of-order relay | offset sort/replay from DB | reconstruct committed ordering |
| tool UNKNOWN and cancel | persist uncertainty; no invented success | needs_reconciliation state only |

## 9. Security, privacy and limits

No permission derives from cursor/eventId; MAC-protected cursor binds viewer+tenant+filter+expiry, still rechecks DB revocation. Return no untrusted HTML instructions as authoring authority. Tenant-user ownership enforced before query; missing/ambiguous owner denies. Cross-tenant cursor or signed-but-revoked cursor denies. EventBody is bounded (proposal max 16KiB) and references redacted Artifacts rather than secrets. Stream finite max age, max concurrent streams/tenant, replay page size 100 (PROVISIONAL CHAT_08/10/09); slow client dropped with reset/reconnect, no unbounded RAM queue. Browser takeover and arbitrary external side effects are approval-gated outside this document.

## 10. Trace/performance design

Emit counters: event_committed_total, event_relay_attempt_total, dedup_total, resume_total, resume_gap_total, authz_denied_total, outbox_lag_seconds, stream_backpressure_disconnect_total; trace keys eventId/causation/correlation/taskId/runId, avoid raw secrets. Bench fixtures 1/4/16 producers, 1k/100k events, pageSize 10/100, stream drop/reconnect, artificial DB latency; measure p50/p95/p99, evidence correctness and memory, no invented SLO.

## 11. Storage/transactions/migration [PROVISIONAL: CHAT_10]

Required relational constraints: unique(tenant,aggregateType,aggregateId,aggregateSequence), unique(tenant,deliveryOffset), unique(eventId), unique(outbox.eventId); atomic domain mutation/event/outbox append; monotonic per-aggregate and per-tenant offset allocation. A single tenant offset counter row can become hotspot; benchmark then shard only with explicit cursor redesign/ADR. Outbox relay leases own independent epoch and are not Run leaseEpoch. Retain enough event history for recovery; expiration is advertised. New major reader/writer rollout: additive columns, dual read, schema negotiation, backfill, canonical switch, retire old; do not silently rewrite event IDs or sequence.

## 12. UI/REST/Telegram [PROVISIONAL: CHAT_08]

GET snapshot and SSE must share an authorized cursor handoff to avoid race; EventSource Last-Event-ID alone gives an eventId, not necessarily a recoverable offset, so either map ID→stream cursor server-side or use stable signed opaque cursor (never expose another tenant filter). REST 410 indicates replay window expired; client discards previews, fetches fresh snapshot, resubscribes. UI displays confirmed results only after terminal committed event, while provisional model streaming visually distinguished. Telegram delivery uses separate outbox and dedup; not the user's SSE cursor.

## 13. Implementation PR slicing

EV-PR1 schema/registry, reader compatibility, validation, event kind fixtures; EV-PR2 DB writer aggregate+tenant sequence CAS and outbox; EV-PR3 owner-scope replay cursor, SSE/REST adapter, auth revocation; EV-PR4 crash, disorder, retention, multiwriter and benchmark harness. No code added in this spec repo.

## 14. Tests — designed, NOT run

| Test / type | Setup/action | Expected |
|---|---|---|
| EVT-T01 unit | malformed v7, json number for seq, unknown major, injected new field | rejection, 0 persisted |
| EVT-T02 SQL integration | update Task then kill before event insert | rollback all |
| EVT-T03 SQL integration | update + event + outbox and kill after commit | all durable, relay recoverable |
| EVT-T04 16 writers | same aggregate concurrent mutation and append | unique monotonic sequences and CAS |
| EVT-T05 4 writers | separate aggregates, same tenant | tenant offsets unique |
| EVT-T06 network | duplicate/late SSE and reconnect | final UI exactly one eventId effect |
| EVT-T07 security | tenant A cursor with B user and filter changed | permission denied, bytes=0 |
| EVT-T08 fault | retention removes resume position | explicit 410 + snapshot resync |
| EVT-T09 fault | 2 relays out of order | consumer reconstructs correct ordered durable events |
| EVT-T10 security | Event payload contains raw Cookie, URL token and injection | validation/masking rejects |
| EVT-T11 resource | 1MB model raw token output | stored artifact instead of Event body, cap respected |
| EVT-T12 crash | Tool UNKNOWN, cancel, old Run epoch event attempt | no invented complete, old event rejected |
| EVT-T13 SC11 | duplicate X post/poll event triggers | 1 canonical Task and causal event link |
| EVT-T14 benchmark | 100k events, 16 consumers with fault latency | p95/p99 and RAM observed, bounded queues |
| EVT-T15 stream | unauthorized hidden messages among offsets | no false gap or data leak |
| EVT-T16 stream | preview disconnect/terminal state interleaving | discarded preview; committed truth rendered |

## 15. Requirement traceability

| REQ | TEST | SC / pass criterion |
|---|---|---|
| EVT-001 | T02,T03 | 07; atomically present or absent |
| EVT-002 | T01,T04,T05 | 07/08; unique IDs/seq |
| EVT-003 | T01 | 07; unknown major 0 accepted |
| EVT-004 | T07,T15 | 06/08; 0 unauthorized bytes |
| EVT-005 | T06,T09 | 07; no loss from retained range |
| EVT-006 | T04,T05 | 07; monotonic per aggregate |
| EVT-007 | T12,T13 | 07/11; cause linked |
| EVT-008 | T16 | 04/07; no phantom complete |
| EVT-009 | T08 | 07; explicit reset |
| EVT-010 | T01,T10 | 08; invalid commit 0 |
| EVT-011 | T06,T09 | 07; dedup |
| EVT-012 | T01,T05 | 07; lossless decimal |
| EVT-013 | T15 | 02/11; filters safe |
| EVT-014 | T14 | 09; telemetry captured |

## 16. Open issues / cross-team proposals

- EVT-P01 P0 CHAT_10: tenant deliveryOffset index/transaction/outbox/replay bounds, row contention and migration. T02..05/T08.
- EVT-P02 P0 CHAT_09: audience owner/tenant authorization in SSE and service principal. T07/T15.
- EVT-P03 P0 CHAT_08: signed cursor and snapshot barrier; 410 recovery, preview semantics; Last-Event-ID mapping. T06/T08/T16.
- EVT-P04 P0 CHAT_02/03: do not commit Run/Tool effects/events with stale lease/GoalRevision. T12.
- EVT-P05 P1 CHAT_07/13: external subscription provider event ID vs DomainEvent ID, Watch trigger lineage. T13.
- EVT-P06 P1 CHAT_00: ADR on tenant stream ordering hotspot vs alternative global cursor.
- EVT-P07 P0 CHAT_01 self: payload eventKind registry must consume state enums after 01-03; not Locked until reconciliation.
No Team owner change is performed in this document.

## 16.1 Round 3 — Canonical Event ↔ SQL ↔ SSEの統一契約（2026-10-30 / Draft）

**既存契約の再定義ではなく、CHAT_10 PR #10の物理設計を正本Envelopeへ合わせるための解決仕様。** `agent.domain_events`には `aggregate_type/aggregate_id/aggregate_sequence`, `delivery_offset`, `event_kind`, `schema_version`, `payload_schema_version`, `task_id/run_id/watch_id`, `owner_user_id/audience_code`, `causation_event_id`, `correlation_id`, `occurred_at/committed_at` を保存する。旧`task_events`単独の設計は全Eventを表現できないため不可。

- `aggregateSequence`は`(tenantId, aggregateType, aggregateId)`ごとに正の連続BIGINT。TXでAggregate counter rowをlock/advanceしEvent+Outboxと原子的にcommit。User/External Event IDを流用して採番しない。
- `deliveryOffset`はtenantごとに連続した正のBIGINT。独立した`tenant_event_counters`をdomain mutationと同一TXでlock/advance。異なるaggregateでも同じtenantのコミット順のCursorで配信し、未commit TXの値は公開しない。単一counterの争奪が性能上のtradeoffであり、実測前にシャード化しない。
- Event IDはtrusted server UUIDv7。Wireの`aggregateSequence`/`deliveryOffset`は`1..9223372036854775807`の正規10進文字列（先頭ゼロなし）。DBの`bigint`↔内部`bigint`↔JSON stringをlossless codecで変換。**Run leaseEpoch/recordVersion等に使うsafe integer制限は別契約**であり、Event offsetに誤って適用しない。JSON numberへの変換禁止。
- `taskId`が`null`の正規Watch/Notification等のEventを許す。`runId`がある場合は同tenantの正しいTaskに属することを必須とし、Task向けSSEへ関連づけて表示する。owner/audienceとpayloadの型整合はServer side Event registryと認可レイヤーが強制する。`operator_audit`を一般Userへ投影しない。Eventの内容自体はuntrusted stringとして処理。
- Domain state/Event/outboxを**同じDBトランザクション**でcommit。Outbox配送成功だけをdomain mutation完了の根拠にしない。リレー重複・順不同は永続Event logからoffset昇順に再生、Client側はeventIdで重複抑制する。
- `GET /tasks/:id`の公開Task/Run/Evidence projectionとtenant event high-waterは同一MVCC snapshot（例 `REPEATABLE READ READ ONLY`）または証明済みmaterialized projection barrierで取得。`snapshotCursor`がprojectionより未来を指した場合、初回SSE再接続でEventが**永久に欠落する**ので絶対に署名しない。
- SSEの署名Cursorはtenant/viewer/Task filter/lastDeliveryOffset/expiryを含み、暗号学的認証と最新の認可を両方行う。Filterによるoffsetの飛びは正常。最古保持範囲外・期限切れは`410`→Snapshot再取得。
- **移行**: 初期導入前なら`0001_initial`を新設計で生成可能。すでに初期migrationを適用した環境では過去SQLを破壊変更せず、新番号のexpand/dual-read/backfill/switch/contractにすること。

| New contract test (designed, not executed) | Acceptance |
|---|---|
| EVT-R3-T01 | TaskとWatchを同時発火、Task非所属のEventも保存され、tenant deliveryOffsetが重複しない |
| EVT-R3-T02 | 2 aggregateの多Writer/rollback/faultで、Event+Outboxがall-or-noneかつ連番gapなし |
| EVT-R3-T03 | snapshotとSSE開始間の新Event/異User非可視Eventを混在→Authorized Event欠損0/漏洩0 |
| EVT-R3-T04 | `2^53`超のEvent offsetをDB+JSON+Client間で往復し完全一致、JSON number reject |
| EVT-R3-T05 | outbox逆順/再配信/retention expiry→DB offset replay、Client重複0、Expiredは410 |

**Cross-owner pending**: CHAT_10が物理DDLとCounter allocationを実装案へ反映、CHAT_08がSnapshot/SSE契約を照合、CHAT_09がauthorizationを監査、CHAT_11がcontract/fault casesへ追加。全てレビューと実装テスト前はDraft。

## 17. Sources and evidence grade

[S1] Code verified: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py and https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/tests/test_stream_events_v3.py — PregelLoop.put_writes/_put_checkpoint/_emit and TestV2Stream. MIT LICENSE in this commit.  
[S2] Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/stream_events.py and https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_stream_events.py — event classes and streaming tests. MIT LICENSE.  
[S3] Code verified: https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/src/types/spec.types.2025-11-25.ts and https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/dynamic.test.ts — JSONRPC/Progress request types and fixture. At this pinned commit, packages/core-internal/package.json declares Apache-2.0; root LICENSE preserves older MIT contributions and says license transition. Reuse requires per-file provenance check.  
[S4] Issue report only: https://github.com/langchain-ai/langgraph/issues/8115.  
[S5] Docs verified: project-brief, review-gates and acceptance-scenarios in current main.  
[S6] Hypothesis: independent persisted tenant delivery cursor, MAC-protected viewer cursor, cap values, transaction design. Neither implemented nor benchmarked.

**Review gates:** P0 cross-team contracts unresolved. Test suite designed but not executed. This document remains Draft.

**MCP license evidence (Docs verified, pinned 39ae8e6, 2026-10-30):** https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/LICENSE (MIT→Apache-2.0 transition); https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/package.json (license: Apache-2.0). This project copies no upstream source code.


### Round 4 Event replay retention contract (2026-10-30 / design)
The canonical tenant stream cursor and the persisted retention floor have separate roles. CHAT_10 DB owns `agent.event_retention_floors.earliest_replayable_offset`. An authenticated cursor with `lastDeliveryOffset + 1 < earliest_replayable_offset` cannot be reliably replayed and MUST produce `410 STREAM_CURSOR_EXPIRED` with snapshot re-fetch; a hidden user/Task event offset is NOT proof of expiration. Retention floor is advanced in the same DB transaction deleting only a contiguous safe prefix after Outbox/Artifact dependencies are cleared. Tests designed: EVT-R4-T01 mixed hidden/visible offsets; EVT-R4-T02 safe GC with live outbox prevents deletion; EVT-R4-T03 expired cursor 410/clean resync. No implementation code or SQL GC was executed here.
