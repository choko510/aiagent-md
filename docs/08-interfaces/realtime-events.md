# Realtime Events — Durable SSE / Snapshot / Cursor Contract

> Owner CHAT_08 | docs/08-interfaces/realtime-events.md | Status: **Draft / [PROVISIONAL: CHAT_01,02,09,10]** | 2026-10-30 | baseline main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-02,05,06,07,08,09,11,16

## 0. 結論・採用判断

**採用するのは「永続Event log → 権限付きredacted projection → task単位の再生可能SSE」であり、LLMトークンやBrowserのsocketを直接ブラウザへ中継する方式ではない。** 断線時の欠落補完、認可失効、順序、上限、Run再起動との整合をDB正本から構成する。HTTP配信は**at-least-once**とし、クライアントはevent cursorを用いて冪等反映。HTTP/SSEの接続が切れてもTask/Run/Watchは継続する。Last-Event-IDはEventの証拠でも認可capabilityでもない。

Task Event envelope/sequenceはCHAT_01、RunのCheckpoint/Event適用はCHAT_02、認証/委任はCHAT_09、event persistence/retention/indexはCHAT_10が所有。Event名・payloadの最終型はOwnerの契約確定まで暫定。SSEはイベント配送であってWatchのtrigger処理やNotification Outboxを代替しない。

## 1. Scope / non-scope / trust boundary

- **Scope:** GET /api/v1/tasks/{taskId}/events のHTTP SSE、snapshot→live handshake、再接続、Last-Event-ID、逐次投影、二重配信、失効・資格・上限、backpressure、Heartbeat、Gap判定、監査とUI integration。
- **Non-scope:** Event/Stateの意味論（CHAT_01）、Workerの実行/Cancel（CHAT_02）、永続Outbox DDL（CHAT_10）、Telegram配送（08-06）、Browser WebRTC/画面操作（08-05）、実データ取得。EventStreamは原本のsecret/HTML/Tool argumentsを送信しない。
- 認証済みprincipalをCHAT_09で確定し、subscription開始・replay each query・live push前にResource ownershipを検査。URLのTask IDはUUIDv7 server-generated（CHAT_01 §5）、単なる識別子でアクセス権ではない。Task.conversationIdはnull可。Tenant→workspace mappingはCHAT_09/10未合意であり、clientの任意ヘッダーから推定禁止。

## 2. OSS実コード比較（閲覧日2026-10-30）

| Source (pinned SHA) | code & tests inspected | 再利用する技術的知見 / 不採用 |
|---|---|---|
| **OpenClaw** 8b8c33ef [S1-S3] | \`src/gateway/sessions-history-http.ts\` の \`serveAuthorizedSessionHistory\`, \`resolveCursorSeq\` 参照、\`sseWrite\`、stream heartbeat、unsubscribeとclose cleanup; \`src/gateway/http-common.ts\` \`setSseHeaders\`、\`watchClientDisconnect\`; \`src/gateway/http-common.test.ts\` | **採用:** cursor入力検査、購読時の所有者確認、resource cleanupと心拍。**変更:** session transcript-tailは本製品のTaskEvent永続log/tenant高水位に置換。heartbeatはcursorを進めない。 |
| **Vercel AI SDK** f8507823 [S4-S6] | \`packages/ai/src/ui-message-stream/create-ui-message-stream.ts\` \`createUIMessageStream\`、\`create-ui-message-stream-response.ts\` と \`packages/ai/src/ui-message-stream/create-ui-message-stream.test.ts\`、\`examples/ai-e2e-next/app/api/chat/resume/route.ts\` | **採用:** UI chunk化・error-safe stream construction、resumable transportの参考。**却下:** UI SDK streamだけを正本とすること。sampleはrequest生存/stream storage前提を持つので本製品はDB Event logから復旧する。 |
| **Mastra** ec3ba11d [S7-S8] | \`client-sdks/react/src/agent/hooks.ts\` のsubscription/reload、\`packages/server/src/server/handlers/error.test.ts\` の競合分類 | **採用:** 再購読・resume後に現在状態のsnapshotを再評価。**却下:** React hookにRun/Taskの正本を置く。 |

OSS内の関数は閲覧済みだが、ソーステストの実行/動作保証は未実施。OpenClaw MIT、Vercel AI SDK Apache-2.0（licenseは下記の公式Repoを再確認して導入時にPin）、Mastra通常領域Apache-2.0（ee/は別条件）。コードコピーではなく独立設計。

**代替評価:** A. memory emitterだけ: 最低遅延でもサーバ再起動で欠落→却下。B. WebSocketを唯一のtransport: 双方向不要な多くのUIに再接続/認証を実装する負担→v1却下。C. Durable SSE + GET snapshot: replayとHTTP infra適合で採用。D. PostgreSQL LISTEN/NOTIFYを真実の配送元にする: 通知欠落し得るためwake-upヒントにのみ使用。

## 3. Architecture / ownership

~~~text
Runtime / Watch / Tool Gateway
     └─(transaction: domain mutation + canonical event + outbox)─> DB [CHAT_01/10]
            │                                        └─ wake-up hint(optional)
            └─ Authorized projection query (tenant + visibility + seq)
                         ├── snapshot GET /tasks/:id [rest-api.md]
                         └── SSE controller ──> Web chat / Dashboard
                                                 reconnect Last-Event-ID
~~~

CHAT_01 [PR #23](https://github.com/choko510/aiagent-md/pull/23) のDomain Eventは各aggregate内の `aggregateSequence` とtenant全体の `deliveryOffset`（いずれもint64 decimal string）を区別する。Run/Tool/Approval/Evidenceなど別aggregateのTask関連Eventはtenant `deliveryOffset`昇順でSSE配送する。Raw offsetは署名cursorへ封印し、公開JSONに含めない。発行時計やUUID timestampで順序を決めない。ブラウザ外部日時はobservedAtで区別。

## 4. Requirements

| REQ | Level | Acceptance outcome | SC |
|---|---|---|---|
| SSE-001 | MUST | Taskごとの認可SSE endpoint。認証・tenant違反で内容を1byteも送らない | 06,08 |
| SSE-002 | MUST | snapshot high-water とEvent replayでGET→subscribe raceの0欠落を保証（retention内） | 05,07 |
| SSE-003 | MUST | tenant deliveryOffset昇順でTask関連Eventを安全に配送し、eventIdで重複排除。異aggregateのaggregateSequenceを比較しない | 07 |
| SSE-004 | MUST | Last-Event-IDの正当性、別task/tenant/revocation、expiryを検査しinvalid/replay impossibleを安全に処理 | 07,08 |
| SSE-005 | MUST | heartbeatはcursorを進めず、stream/backpressure/idle/connection上限を有限に保つ | 09 |
| SSE-006 | MUST | user/role revoke・Task visibility変更を配信中も検知して閉じる | 06,08 |
| SSE-007 | MUST | secret/credentials/raw HTML/hidden prompts/Tool argumentをprojectionから排除 | 03,08 |
| SSE-008 | MUST | transient disconnectはAgent run cancelを発行しない。再接続で同Taskの最新状態を取得できる | 07 |
| SSE-009 | MUST | event schema未知/older version/retention不足ならfail-closeまたは明示snapshot recovery、捏造進捗なし | 07 |
| SSE-010 | MUST | terminal/partial/uncertain/approval-requiredを別event projectionとGET snapshotで一致させる | 01,02,04,07 |
| SSE-011 | SHOULD | SSE reconnect/backoff、visibility reconnect、offline indicator、UI重複描画回避 | 07,09 |
| SSE-012 | LATER | 複数Task横断feed（独立global cursorかfan-in投影正本が必要） | 02,11 |
| SSE-013 | EXPERIMENT | バッチ済/圧縮済のdelta投影。費用/資源実測後に採否 | 09 |

## 5. API / TypeScript wire proposal

REST v1 GET \`/api/v1/tasks/{taskId}/events\`。Accept: text/event-stream。v1 Webクライアントは **fetch streaming** を使い、初回から `Last-Event-ID: <signed-snapshot-cursor>` ヘッダーを明示送信する（**native EventSourceは任意Headerを指定できないため、snapshot→liveの初期handoffには使用しない**）。クライアントはSSE frame grammarを有限バッファでparseする。GET queryのcursorは原則無効（任意トークンがアクセスログに残る事故を避ける）。legacy native EventSourceだけが利用可能な環境ではGET snapshotをstream自身の最初のframeとしてサーバで再提供する明示別routeが必要であり、同等のzero-gap試験なしにfallbackしない。Eventはevent field（公開safe kind）、id（opaque *task-bound cursor*）、data（small JSON projection）を持つ。idは後述HMAC cursorであり生UUID/生DB sequenceだけにしない。ID/ETagの公開詳細は[REST spec](rest-api.md)に委譲。

~~~typescript
type SafeEventV1 = { // public DTO, NOT canonical CHAT_01 envelope
  projectionVersion: 1;
  taskId: string; // canonical UUIDv7 checked, principal tenant bound
  runId: string | null;
  eventId: string; // canonical UUIDv7 event identity; client dedup
  aggregateSequence: string; // canonical int64 decimal, per aggregate only
  kind: "state"|"step"|"evidence"|"usage"|"approval"|"watch"|"warning"|"cursor";
  committedAt: string; // UTC RFC3339
  data: Record<string, unknown>; // approved per-kind Zod strict schema, finite size
  source?: { evidenceId: string; observedAt: string|null };
};
type StreamCursor = string; // opaque authenticated cursor: tenant, viewer, taskFilter, lastDeliveryOffset, expiry
type SubscribeOptions = {
  taskId: string; lastEventId?: StreamCursor;
  // for a fresh stream, server returns "snapshot-required" when no baseline supplied
};
~~~

Canonical aggregateSequenceとdeliveryOffsetは64bit整数の正規十進文字列で、内部比較はBigInt。公開SafeEventはeventIdとaggregateSequenceだけを示し、tenant deliveryOffsetはSSE id（署名cursor）に封印する。異aggregateで同じaggregateSequenceは正常であり重複と見なさない。可視対象外のEvent offsetを公開しない。

**Wire example（非実行mock）**:

~~~text
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store, no-transform
X-Accel-Buffering: no

event: progress
id: <server-signed-task-scoped-cursor>
data: {"projectionVersion":1,"taskId":"<valid-uuidv7>","runId":null,"eventId":"019a2bc3-4d5e-7abc-8def-123456789abc","aggregateSequence":"3","kind":"step","committedAt":"2026-10-30T00:00:00.000Z","data":{"phase":"search","status":"ongoing"}}

: heartbeat

~~~

**Client auth:** v1は header指定可能なfetch streaming と `credentials:same-origin` によるSecure HttpOnly Cookieを採用。ブラウザのnative EventSourceは任意Headerを付けられないため、このhandshakeの実装には用いない。fetchでもcookie/Origin/CORSの検査をCHAT_09へ委譲し、URL query bearer tokenとJS内の長期tokenを禁止する。read modelとstreamはsession失効時一貫してdeny。

**HTTP pre-stream:** bad ID=400、unauth=401、other tenant/not visible=404、cursor checksum/task mismatch=400（他tenantへ具体的ヒント不可）、retention expiry/replay gap=410 \`STREAM_CURSOR_EXPIRED\`＋ safe snapshot URL、rate=429、service unavailable=503。Header送信後はHTTP statusを変えられないので safe \`event:error\` + close。401/403をSSE dataとして継続配信してしまわない。

## 6. Snapshot-consistent tenant delivery cursor

1. ClientはGET /tasks/{id}から `recordVersion`（int64 decimal string）、`snapshotCursor`（最終反映済みtenant deliveryOffsetを封印）、Task stateを取得する。Task.conversationId=nullも正規ケース。
2. クライアントはGETで得た`snapshotCursor`をfetch streamingの初回`Last-Event-ID`ヘッダーで渡す。[REST §5.6.1](rest-api.md)のTask state/projectionと同じoffset barrierを用いる。Raw numberをcursorとしない。
3. SSEサーバはcursorのtenant/viewer/task filter/version/expiryを検証し、対象Taskに紐づく複数aggregateのEventをtenant deliveryOffset昇順でreplayする。
4. DB query後にSubscriptionを追加してもraceを閉じるため、**subscribe wakeupを先にセット → replay query → wakeup/poll trigger → DB high-water再読込**をループする。LISTEN/NOTIFYだけに頼らず、finite pollingでgapを補う。読出し中に新Eventがcommitしたら必ず次回queryに入る。
5. Serverは最後に送出を許可したtenant deliveryOffsetを署名cursorへ記録するが、client ACKはない。再接続で同じEventが再配信されてもclientはeventId既出を無視、absolute GET stateで再整合する。異aggregateのsequence比較は禁止。**配送exactly-onceは主張しない**。
6. Snapshotとstreamの重複: stream再生により進捗を二度加算しない。ステップと費用の表示はabsolute latest projection + monotonic versionを使い、tokenデルタの再加算は禁止。
7. Run終了/Cancel/Steerは接続継続の有無と無関係。UIでstatus settled時も最新GETを再照会してEvidence/Usageの確定値を表示する。

~~~typescript
async function serveTaskEvents(ctx, taskId, headerCursor) {
  const principal = await auth.authenticateHttp(ctx.req);                 // CHAT_09
  await auth.requireAccess(principal, "read", {kind:"task",id:taskId});
  const after = await cursorService.verify(headerCursor, principal, taskId);
  const stream = openBoundedSse(ctx, {maxBufferedBytes:256*1024});       // provisional cap
  const wake = eventBus.subscribeHint(taskId);                           // never authoritative
  let cursorOffset = BigInt(after.lastDeliveryOffset);
  try {
    while (!stream.closed) {
      await auth.revalidateIdentityAndTask(principal, taskId);           // revocation
      const events = await eventStore.readTaskAfter({
        tenantScope:principal.tenantId, taskId, afterDeliveryOffset:cursorOffset,
        limit:100, consistentSnapshot:true
      });
      for (const e of events) {
        if (BigInt(e.deliveryOffset) <= cursorOffset) continue;
        await auth.revalidateIdentityAndTask(principal, taskId);
        const safe = schemaProjector.toPublicDtoOrFail(e, principal);
        const nextCursor = cursorService.sign(taskId, principal, e.deliveryOffset);
        if (!stream.writeBounded(safe, nextCursor)) throw new BackpressureError();
        cursorOffset = BigInt(e.deliveryOffset);
      }
      if (events.length === 100) continue;
      await waitForHintOrBoundedPollOrAbort(wake, ctx.signal, 2000);
    }
  } catch (err) { stream.safeErrorAndClose(err); }
  finally { wake.unsubscribe(); stream.close(); }
}
~~~

Use a **bounded 2s poll interval**, 15s heartbeat, 100 events read batch, 256KiB queued output/client, 5 concurrent streams/user, 30-minute maximum stream session with reconnect; these are initial **design bounds**, not measured results; test under CHAT_10/11. Replay memory per-stream must be O(100 event projections + 256KiB). If event payload exceeds 16KiB return \`artifactRef\` allowed and separate authorized fetch, never truncate a JSON string into invalid syntax.

## 7. Race / restart / backpressure matrix

| Scenario | Server handling | Client handling |
|---|---|---|
| Event commit between GET snapshot and SSE subscribe | replay seq > snapshot high-water | monotonic apply once |
| Duplicate event/old cursor | DB deliveryOffset/eventId, signed cursor replay | duplicate eventId ignored; no duplicate cost |
| Out-of-order worker event production | DB tenant deliveryOffset assigned transactionally, not worker clock | aggregateSequence per source, delivery order cross source |
| SSE closed before drain | do not cancel task; close buffers/subscriptions | retry same cursor, exponential jitter |
| API crash/restart or proxy buffering | DB replay source; no stream-memory truth | GET snapshot then reopen on replay impossible |
| Tenant auth revoked mid-stream | bounded revalidation and immediate close on revoke event | show reauthenticate; clear private cached read model |
| Worker crash after external unknown effect | emit uncertain/needs-review safe projection | never show completed, link approval/reconcile |
| Cursor expired or Event retained window ended | 410 and snapshot bootstrap, no fake replay | refresh Task and reset cursor |
| Flood 10k events, slow client | bounded buffer; safe backpressure close and metric | cursor retry; optional compact snapshot |
| Unknown schema/new major | stop stream / safe error, alert | don't silently skip unknown security critical event |
| Malicious HTML/URL in Event | redaction and strict DTO filter, no inline HTML | render text safely, no fetch inferred URLs |

**Reconnect:** initial 250ms exponential with full jitter to max 30s; respect 429 Retry-After. Finite attempt per short foreground session, switch to user-visible offline state after 10 attempts; auto retry only while explicitly active and authenticated. Never use reconnect loops to force API billing or increase agent budget.

## 8. Security / privacy

Never copy credentials, browser storage state, raw prompts, model private reasoning, access token, full source HTML into event JSON. \`kind:usage\` returns confirmed/estimated, currency, UTC measurement time; \`kind:approval\` returns only server-produced action summary/expiry and a permission-checked link. Approval decision remains POST with If-Match/Idempotency-Key in rest-api.md. Unknown external observations cannot become trusted Tool commands. Origin allowlist; fetch streaming with cookie/CSRF policy verified against CHAT_09; no cache to shared proxy; no wildcard CORS with credentials. Shutdown unsubscribes from all wake hints and DB read loops. SLO/limits to be re-evaluated with actual hardware.

## 9. Persistence and API/DB cross-team proposals

- [PROVISIONAL: CHAT_01] Public event projection schema/version, Task \`snapshotCursor\` (decimal string), persisted state distinction, retention/high-water cursor guarantee. \`sequence\` physical bigint cannot be blindly cast to JS Number.
- [PROVISIONAL: CHAT_08 rest-api.md] GET Task snapshot returns one signed snapshotCursor tied to owner; without this exact gap-free handshake remains blocked. No raw leaseEpoch in SSE.
- [PROVISIONAL: CHAT_09] AuthSession identityVersion + task ACL revalidation, safe 410/404 status policy, revocation push.
- [RESOLVED DESIGN: CHAT_10 PR #10, still Draft] `agent.domain_events(tenant_id,delivery_offset)`をtenant配信順序の物理正本とし、Task streamは`(tenant_id,task_id,delivery_offset)`の認可済み投影。Task以外のWatch等のEventはTaskIdがNULLでも合法。保持下限はDBで追跡し、安全なprojectionのみ公開。
- Migration: old clients without snapshot cursor may only receive a full bootstrap (no false continuity); new field addition to REST API additive; projection major mismatch fails close. Retention lowering requires advance notice and safe fallback.

## 9.1 Round 3 — Storage/Event/Snapshot 物理契約との接続（CHAT_01 #23・CHAT_10 #10）

このSSEは、旧`task_events`の`task_id+sequence_no`では構築できない。最新のCHAT_10 SQLでは汎用`agent.domain_events`へ統合し、`aggregate_sequence`は同一aggregate内、`delivery_offset`はtenant全体のcommit順序を担保する。

1. TaskのHTTP read modelと`agent.tenant_event_counters.next_delivery_offset-1`を**同一MVCC snapshot**（例：`REPEATABLE READ READ ONLY`）内で取得する。非同期で更新されるprojectionを使う場合、`projection_applied_delivery_offset`を別途追跡し、反映済み地点より未来のcursorを返してはならない。
2. SSE HandlerのAuthorized event replayは`agent.domain_events WHERE tenant_id=? AND delivery_offset > lastOffset AND task_id=? ORDER BY delivery_offset ASC LIMIT ?`が基盤。Run/Tool/Approval等、元Taskに属するEventには正しい`task_id`を確実に付与。Task無所属のWatchイベントはTask SSEではなく、Watcher専用認証ストリームまたは明示関連付けされたTask経由で提供。User未認可のoperator_audit eventsは除外。
3. `owner_user_id`がイベントからnullであっても「見てよい」の意味にしてはならない。アクセスはTask/Watchの最新のowner+tenant policyで決定し、`audience_code`は追加制限として適用する。Cursor内のtenantとviewerを変更して流用不可。
4. Event中の`aggregateSequence`は異Aggregateでは独立、`eventId`と`deliveryOffset`で順序・dedupする。隠れたEventによるoffsetの欠番をgapと扱わず、Serverがoffset範囲を調べて再開可能性を検証。
5. Snapshot作成直後・Subscribe登録直前・Event Emitの競合は`subscribeHint→replay→bounded polling`で収束。Redis/PG NOTIFY/SSE publisherはwake-up hintでありDBが正本。
6. Retentionで古いEventが消え、Cursor位置より古い範囲が失われた場合は`410 STREAM_CURSOR_EXPIRED`。Retentionの`earliestRetainedOffset`は**DBの保持メタデータ**で明示管理し、空のAuthorized Listだけを根拠にexpiredと判定しない。

| Test (designed, not executed) | Expected |
|---|---|
| SSE-R3-T01 | Same tenant Task+Watch+Run emit, hidden events between visible→表示0欠損、偽gap0 |
| SSE-R3-T02 | GET snapshot直後にEvent append→first stream replayに全件現れ、重複加算なし |
| SSE-R3-T03 | 2 parallel outbox relays逆順→UIはDB tenant offsetでソートして結果整合 |
| SSE-R3-T04 | 異user/tenantへのSigned Cursor再利用→401/403/404、event payload 0 bytes |
| SSE-R3-T05 | retention expired + role revoked during stream→410/close、safe GET re-sync |

**このMDとEvent/DBの記述はまだ実装テストされていないためDraft。** DB Ownerが`outbox`とtenant cursorの更新を検証し、CHAT_11がFault/E2Eテストへ反映するまでは`Locked`にしない。

## 10. Proposed implementation PR split

SSE-P1 core projection schemas + redaction + unit schema tests; SSE-P2 durable replay query/cursor signing & GET snapshot bridge with DB fault tests; SSE-P3 stream auth/heartbeat/backpressure and proxy config; SSE-P4 Web-client lifecycle/reconnect/dup filtering; SSE-P5 Load, auth expiry, 410, migration + release gate.

## 11. Test plan (designed, **not run**)

| TEST | Level/setup/action | Expected | SC |
|---|---|---|---|
| SSE-T01 | Contract: unauthorized and other-tenant task subscribe | 401/404 and 0 private event bytes | 06,08 |
| SSE-T02 | Fault: commit event at each boundary GET→subscribe→first read | no gap, stable task seq ordering | 05,07 |
| SSE-T03 | Contract: duplicated eventId, colliding aggregateSequence across Task/Run, replay after partial write | every visible event once, usage not double billed | 07 |
| SSE-T04 | Security: tamper cursor/task/scope/expired/cross-workspace | 400/410/404, no existence leak or false replay | 07,08 |
| SSE-T05 | Load: 5 streams, 256KiB backlog, 10k events | memory bounded; slow stream disconnects and resumes | 09 |
| SSE-T06 | Integration: revoke user/tenant while open; reuse cookie | connection closes, next request unauthorized, secret=0 | 06,08 |
| SSE-T07 | Security: inject cookie, HTML, prompt injection, secret into raw Event | projected bytes contain none; escaped view | 03,08 |
| SSE-T08 | Worker kill after DB commit / API restart | Event replay works, Task not cancelled | 07 |
| SSE-T09 | Contract: unknown projection version and retention gap | explicit 410/error + snapshot resync | 07 |
| SSE-T10 | E2E: uncertain Tool outcome, paused, approval wait, completion | correct distinct UI statuses and aligned GET snapshot | 01,02,04,07 |
| SSE-T11 | Benchmark: 100 Task sources, cold/warm, proxy buffering enabled/disabled | p95 latency and memory recorded, no unproven SLO claim | 09 |
| SSE-T12 | Contract: native EventSource cannot set initial Last-Event-ID header; fetch streaming with snapshot cursor and session cookie | first request includes correct header; no query bearer token; zero-gap proof | 08 |

All MUST REQs map: SSE-001→T01; 002→T02,T08; 003→T03; 004→T04,T09; 005→T05,T11; 006→T06; 007→T07; 008→T08; 009→T09; 010→T10. No external real API/notification/purchase tests. Local integration not executed.

## 12. Unresolved

| ID | Owner | Severity | Resolution/verification |
|---|---|---|---|
| SSE-P01 | CHAT_01 | P0 | Event envelope/commit sequence, visibility semantics, snapshot sequence stable contract |
| SSE-P02 | CHAT_10 | P0 | retention floor, replay query transaction, event/outbox commit ordering |
| SSE-P03 | CHAT_09 | P0 | principal/tenant/workspace mapping, streaming revoke window |
| SSE-P04 | CHAT_08(rest-api) | P1 | GET snapshot includes high-water cursor and schema version |
| SSE-P05 | CHAT_11 | P1 | fault harness producer commit vs subscriber race and proxy behavior |

## 13. Sources and evidence grades

[S1] **Code verified** OpenClaw \`serveAuthorizedSessionHistory\` / \`sseWrite\`, pinned 8b8c33ef, 2026-10-30: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/sessions-history-http.ts (MIT).  
[S2] **Code verified** \`setSseHeaders\`, \`watchClientDisconnect\`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/http-common.ts (MIT).  
[S3] **Code verified test file inspected, not run** https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/http-common.test.ts (MIT).  
[S4] **Code verified** Vercel AI SDK \`createUIMessageStream\`: https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/packages/ai/src/ui-message-stream/create-ui-message-stream.ts (license adoption recheck).  
[S5] **Code verified test file inspected, not run** https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/packages/ai/src/ui-message-stream/create-ui-message-stream.test.ts  
[S6] **Code verified** resumable sample: https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/examples/ai-e2e-next/app/api/chat/resume/route.ts  
[S7] **Code verified** Mastra \`useChat\` and subscription handling: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/client-sdks/react/src/agent/hooks.ts (Apache-2.0 standard paths; ee exception).  
[S8] **Code verified test file inspected, not run** Mastra 409 conflict mappings https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/error.test.ts  
**[Hypothesis / proposed only]:** durable cursor HMAC, stream memory limits, replay algorithm, versioned redacted projection, polling intervals. No integrated application source/benchmark or exact OSS tag correspondences verified.

## 14. Issue/PRの既知故障と追加回帰要求（Issue report、現行統合では未検証）

- **[I-SSE-01]** [OpenClaw #127368](https://github.com/openclaw/openclaw/issues/127368)（2026-10-30閲覧、Open）: 投稿者が session-history SSE で `res.write()` の `false` を無視し、unbounded Promise chainを蓄積する問題を報告。これは**Issue report**であり本Harnessでの再現ではない。上記 `stream.writeBounded` は実装時に `write(false)` で以後の読み取りを停止し、drain/deadlineで復帰、最大256KiB超過で接続終了する仕様とする。SSE-T05に、0-byte reader・4096イベント・最長30秒・複数クライアントで送出キュー上限を超過しない試験を追加。
- **[I-SSE-02]** [Mastra #28224](https://github.com/mastra-ai/mastra/issues/28224)（2026-10-30閲覧、Open）: Workerが実作業を終えた後のterminal event送出時にleaseを失ったとの報告。根因未確定。**DBで確定した終状態とSSEに配信された終状態の分離**を維持し、lease喪失時に「成功」イベントを架空生成しない。SSE-T08/T10にterminal event時のlease loss注入を加え、GET照会で真のRun statusを復元する。
- **[I-SSE-03]** [Mastra #22340](https://github.com/mastra-ai/mastra/issues/22340)（2026-10-30閲覧、Open）: schedule dispatchのhook完了を実際のRun完了と誤認するリスクを指摘。SSEがWatchを`complete`と描画できるのは**実Run stateの終端イベント・正本を照合した場合のみ**。dispatch成功や202受付のみでcompleteへ遷移禁止。SSE-T10で複数resume segmentとWorker restartを試す。

これらのIssuesはソースの具体実装を反証する監査材料であり、OSSが本プロジェクトで同じ故障を起こしたという意味ではない。

## 15. OSS PR差分による追加リスク確認

**[PR-SSE-01]** [Vercel AI SDK PR #22362](https://github.com/vercel/ai/pull/22362)（2026-10-30確認、Merged）はtransport wrapperがreplayフラグを失う問題を修正、React regression testを追加。UI側のtransportがsnapshot replayかdelta continuationかの**識別は必須**。本設計はTaskEvent sequenceに基づくreplayであり、全量Snapshotは既存状態を置換、delta Eventはsequenceでskipする。SSE-T03/T09でこの2経路を明示的に分ける。

**SSE-T13 (designed / SC-07,08):** interleave Task aggregateSequence=2, Run aggregateSequence=2 and Tool aggregateSequence=2 with an unrelated user's Event between tenant deliveryOffsets 41..44; assert all three authorized distinct eventIds delivered, none skipped due to colliding aggregateSequence, no other user's event content or raw deliveryOffset in public payload. Owner: CHAT_01 event envelope; CHAT_10 SQL replay. Source: [PR #23](https://github.com/choko510/aiagent-md/pull/23). Source status Draft, no integration tests run.

## Formal Round 2 MUST Traceability (full stable IDs; designed tests only)

| MUST REQ-ID | TEST-ID(s) | SC-ID(s) |
|---|---|---|
| SSE-001 | SSE-T01 | SC-06, SC-08 |
| SSE-002 | SSE-T02, SSE-T08 | SC-05, SC-07 |
| SSE-003 | SSE-T03, SSE-T13 | SC-07, SC-08 |
| SSE-004 | SSE-T04, SSE-T09 | SC-07, SC-08 |
| SSE-005 | SSE-T05, SSE-T11 | SC-09 |
| SSE-006 | SSE-T06 | SC-06, SC-08 |
| SSE-007 | SSE-T07 | SC-03, SC-08 |
| SSE-008 | SSE-T08 | SC-07 |
| SSE-009 | SSE-T09 | SC-07 |
| SSE-010 | SSE-T10 | SC-01, SC-02, SC-04, SC-07 |

**Execution:** 0 Harness tests run. These are proposed test cases; an independent reviewer must validate each result before Locked.


### Round 4 signed SSE cursor / persisted retention floor (2026-10-30 / design)
SSE replay must consult `agent.event_retention_floors` and actual `domain_events` ordering, not compute retention from owner-filtered visible Events. If `cursor.lastDeliveryOffset < earliest_replayable_offset - 1`, return **410 STREAM_CURSOR_EXPIRED** and force a fresh authorized GET Snapshot; otherwise hidden offsets are legitimate and replay may return zero items without a gap. For an empty tenant, firstOffset=1 is normal. Signed cursors remain bound to viewer/tenant/filter and are checked against fresh auth. GC/outbox integrity is physical CHAT_10 ownership. Tests SSE-R4-T01 owner filter hides some offsets (no false410), T02 expired floor (410), T03 delete+replay/subscribe race (complete or reset). Not executable until server implementation.
