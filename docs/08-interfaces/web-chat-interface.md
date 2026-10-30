# Web Chat Interface — Stateful Tasks, Evidence, Budget & Steering

> Owner CHAT_08 | docs/08-interfaces/web-chat-interface.md | Status Draft / [PROVISIONAL: CHAT_01,02,03,06,09] | Research 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-01,02,03,04,05,06,07,08,09,10,11,15,16

## 0. Decision

**Web chat is a client of durable Task/Conversation/Run API, not a place where the AI loop or authoritative event state lives.** A message submits a command, receives Task ID and optimistic local placeholder, subscribes to redacted SSE progress, and GET reconciles authoritative state. No client-side fake success, no conversation auto-creation for Watch/event-launched Task, no runaway generative token stream coupled to HTTP. Browser Handoff, Approval UI, Watch Dashboard are reusable navigations within the same app; they do not maintain parallel copies of Task/Run truth.

## 1. Scope and actors

Target: self-hosted mobile+desktop Web UI (Next.js React candidate, not locked), authenticated user, keyboard/screen reader support. Components: Conversation list, Thread transcript, Message composer, Task/Run cards, phase timeline, evidence drawer, model & budget control, Watch creation summary, paused/uncertain/approval statuses, responsive layouts. Supports arbitrary general research, not dedicated EC/scraper templates. External content is *data*; never interpolate as trusted HTML/JS/system instruction.

Non-scope: Agent planner/executor or prompt policy (CHAT_02/03), conversation schema/goal version (CHAT_01), memory writes (CHAT_06), raw browser takeover UI (08-05), notification delivery (CHAT_07), physical DB (CHAT_10).

Boundary: Next.js Server Components and API endpoints both revalidate CHAT_09 session on server; frontend state stores only redacted projections. Web ↔ Telegram share canonical Conversation/Task IDs. Task.conversationId may be null (Watch/event/API origin). Incoming new message may reference an existing Conversation with ownership verified; UI must not synthesize a Conversation for a non-chat Task.

## 2. OSS source & test review (2026-10-30)

| Source/ref | Inspected code and test | Adopt/reject |
|---|---|---|
| **Vercel AI SDK** pinned f8507823 [S1-S3] | \`packages/react/src/use-chat.ts\` \`useChat\` + automatic \`resumeStream\`; \`packages/react/src/use-chat.ui.test.tsx\` tests resumed replay replacing partial output, server/stream errors, optimistic user message; \`packages/ai/src/ui-message-stream/create-ui-message-stream.ts\` and tests | Adopt transport-friendly incremental updates and stable React lifecycle; **reject ephemeral useChat message/stream as canonical Task/approval store**. Different versions may change API; pin only after integration POC. |
| **Mastra** pinned ec3ba11d [S4-S6] | \`client-sdks/react/src/agent/hooks.ts\` \`useChat\` tracking thread/stream/run IDs and approve/decline; \`packages/playground/src/lib/ai-ui/chat/chat-provider.tsx\` stream errors and refresh; \`packages/server/src/server/handlers/error.test.ts\` | Adopt state separation, persisted message reload and approval awareness; do not copy Mastra workflow/agent-specific message schemas. |
| **OpenClaw** pinned 8b8c33ef [S7-S8] | \`ui/src/app/app-host.tsx\` subscription and cleanup, \`src/gateway/sessions-history-http.ts\` snapshot + stream path, cursor handling | Adopt live subscription disposal and transport reconnection; reject directly exposing Gateway tool-Invoke or shared secret operator scope. |

All code/test **read, not executed**. Vercel AI SDK/mastra standard-path licensing to be checked at dependency installation; Mastra ee/ excludes default Apache-2.0. OpenClaw MIT. No OSS code copied.

Alternatives: (A) one chat completion endpoint that directly streams model output—simple but loses durable task/races and approval→reject. (B) full generic workflow editor before chat—overcomplicated for natural language→defer. (C) task-backed chat with separate SSE projection and evidence affordances→adopt.

## 3. Design layers

~~~text
App router / Screen Reader / keyboard
  ConversationSidebar  ChatTranscript  Composer  TaskStatus/RunTimeline
        |                  |                |              |
        +------------------+----------------+--------------+
                         Client controllers
              useConversationQuery + useTaskQuery
              useTaskStream(after snapshot cursor)
              useCommandMutation(idempotency + If-Match)
                            |
                   /api/v1 (CHAT_08 rest-api)
                            |
                       CHAT_09 auth
                            |
          CHAT_01 / CHAT_02 / CHAT_03 / CHAT_07
~~~

**File placements (proposal; app code not in this spec repo):** \`apps/web/app/(private)/chat/[conversationId]/page.tsx\`, \`components/chat/{Composer,Transcript,TaskCard,RunTimeline,EvidenceDrawer,BudgetForm,UncertaintyBanner}.tsx\`, \`features/chat/{task-controller,stream-reducer,command-client}.ts\`, \`lib/api/generated.ts\`, \`lib/ui/redaction.ts\`, \`tests/e2e/chat-*.spec.ts\`. SSR sends only public DTOs; no secrets in hydration JSON. Keep an indexable read-only evidence summary, not raw downloaded HTML in the DOM.

## 4. Requirements

| ID | Priority | Observable acceptance | SC |
|---|---|---|---|
| CHATUI-001 | MUST | user text + constraints + timezone + budget/model controls submit exactly one durable Task, 202 is shown as accepted not completed | 01,04,05,07 |
| CHATUI-002 | MUST | GET snapshot + SSE with cursor dedup; refresh/reopen retains canonical task status and messages | 05,07 |
| CHATUI-003 | MUST | Task/Run state, active/queued/waiting/partial/unknown/final are distinct and reflect server values | 02,06,07 |
| CHATUI-004 | MUST | every factual claim can show Evidence source URL, observedAt, availability/estimate; unsupported claims marked unknown | 01,03,04,05 |
| CHATUI-005 | MUST | allow model/budget choice only among server permitted options; warn before exceeding hard limits, server is final gate | 01,09 |
| CHATUI-006 | MUST | pause/resume/cancel/steer use Idempotency-Key/If-Match; acknowledge vs applied separate; cancellation doesn't erase run history | 07 |
| CHATUI-007 | MUST | approval/takeover are explicit separately secured flows, no unapproved booking/purchase/post | 04,06,08 |
| CHATUI-008 | MUST | secret/raw HTML/external prompt injection not interpreted as trusted UI or executed content | 03,08,15 |
| CHATUI-009 | MUST | conversations may be null for Watch/event Tasks, and direct Task permalink still works | 02,11 |
| CHATUI-010 | MUST | accessible keyboard/mobile layouts; API/stream errors show refresh/retry without duplicate Task submit | 07,09 |
| CHATUI-011 | MUST | usage distinguishes estimated vs confirmed and never sums replayed token deltas twice | 09 |
| CHATUI-012 | SHOULD | compare tables with provenance, downloadable artifact only through auth redirect | 01,04,15 |
| CHATUI-013 | SHOULD | user preference feedback action with reversible state separate from evidence facts | 16 |
| CHATUI-014 | LATER | multi-task split view and evidence side-by-side | 05 |
| CHATUI-015 | EXPERIMENT | suggested research parameters from model; never auto-apply without user confirmation | 05,10 |

## 5. HTTP/UI contracts (views, not domain types)

~~~typescript
type ChatTaskCardModel = {
  taskId: string; conversationId: string|null; currentRunId: string|null;
  recordVersion: string; snapshotCursor: string; // lossless int64 CAS; sealed deliveryOffset
  serverState: string; // imported from CHAT_01 canonical types, not redefined
  commandStatus: "idle"|"submitting"|"accepted"|"rejected"|"unknown_ack";
  resultCategory: "complete"|"partial"|"uncertain"|"unavailable"|null;
  evidenceItems: Array<{
    evidenceId:string; title:string; sourceUrl?:string;
    observedAt:string|null; fetchedAt:string|null; freshnessKind:"current"|"stale"|"unknown";
    certainty:"observed"|"estimated"|"unverified";
  }>;
  budget: {
    max: {amount:string;currency:string}|null;
    confirmed:{amount:string;currency:string}|null;
    estimated:{amount:string;currency:string}|null;
    accountingAsOf:string|null;
  };
};
type ComposeDraft = {
  text:string; conversationId:string|null; timezone:string;
  constraints:Record<string,unknown>; requestedModelId:string|null;
  maxCost:{amount:string;currency:string}|null;
};
// Server provides allowed models/caps; client selection is hint, not authorization.
// Wire DTO exact names are PROVISIONAL pending CHAT_01/08 REST contract.
~~~

Chat textarea initial max8KiB UTF-8, attachments are explicit Artifact references (no base64 embedded in chat message). Validated timezone IANA, dates/year explicit. For hotels show total party/rooms/tax/parking/availability checked timestamp, not a price with no context. For products show matching model/capacity/condition and real profit estimate uncertainty. Every external cite uses safe escaped label and a reviewed destination URL; URL is not fetched by API on click unless approved.

Task submitted command in \`POST /api/v1/tasks\` with client-generated UUID idempotency key **per intent** stored in local component pending queue. Page reload while ack unknown: GET user-scoped active tasks/history or receipt lookup (CHAT_10/08 proposal), and retry only with **same key/body**. New key requires user explicit intent; do not accidentally create duplicate. \`If-Match\` uses CHAT_01 recordVersion initial 1, not DB revision 0. Public IDs must validate UUIDv7 from CHAT_01, not \`t_123\` placeholders. Original Task need not hold Conversation if Watch/event origin.

## 6. Sequences & state transitions

~~~text
User composer ─▶ local pending intent (key + immutable request hash)
             ─▶ POST /tasks (Auth + Budget + strict validation)
             ◀─ 202 {taskId, commandId, recordVersion, Location}
Client GET /tasks/id ─▶ snapshot + highwater cursor
Client SSE last cursor ─▶ ordered redacted updates
Client reducer ─▶ status, step timeline, evidence & costs
       ├─ requires approval -> separate 08-05 UI; no agent execution by UI
       ├─ waiting login   -> browser handoff CTA
       ├─ uncertain       -> warning + reconciliation action, not success
       ├─ paused          -> resume command only after state refresh
       └─ settled         -> final GET refresh; display provenance
~~~

UI local state machine (presentation only): \`draft\`→\`submitting\`→\`accepted\`→\`tracking\`→\`settled\`; distinct \`unknown_ack\` if network failure while acceptance uncertain; \`offline\` is connectivity, not Agent state. A user can \`cancel\` tracking (close view) without cancelling Task; explicit Cancel button creates Command. Steer submits amended instruction; while acknowledgement pending, old goal stays visible with "change pending", immutable goalRevision increments only after server application.

React reducer:

~~~typescript
async function submitChatIntent(draft, pending) {
  const normalized = validateDraftLocally(draft);
  const intent = pending.getOrCreateByRequestHash(normalized, secureRandomUuid());
  try {
    const ack = await api.createTask(normalized, {"Idempotency-Key":intent.key});
    pending.rememberAcknowledged(intent.key, ack.taskId);
    return openTaskFromServer(ack.taskId);      // 202 not completion
  } catch (err) {
    if (isNetworkAmbiguous(err)) return showUnknownAckWithSameKeyRetry(intent);
    if (isValidation(err)) return editDraftWithFieldErrors(err);
    throw err;
  }
}
function applyEventToTaskView(view,event) {
  if (!sameTask(event,view)) throw new Error("wrong task");
  if (dedupeForTask(view.taskId).has(event.eventId)) return view; // never compare aggregateSequence across aggregates
  if (!knownProjectionVersion(event)) return markNeedsSnapshot(view);
  // Reducer applies server absolute snapshots (not cost increments).
  const next = applyAllowedProjectionFields(view,event);
  dedupeForTask(view.taskId).add(event.eventId); // bounded per-Task LRU; snapshot resets window
  return next;
}
async function controlTask(taskId,version,action,intentKey) {
  return api.command(taskId,{action},{ifMatch:quoteVersion(version),idem:intentKey});
  // after acknowledgement refetch until applied; 409/412 -> show refresh
}
~~~

**No direct client mutation of goalRevision/activeRunId/leaseEpoch.** Replay often repeats events; usage and results update by \`recordVersion\`/sequence. If stale stream updates race with newer GET projection, ignore old sequence and keep authoritative latest.

## 7. Detailed UI behavior / microcopy

- **No Task yet:** user may submit general research with a natural-language goal; server may ask missing criteria. UI displays "提出済み条件" from canonical Task not generated guesses.
- **Running:** phase text (search/fetch/validate/compare) plus step type/count and last actual update time. Unknown progress % not fabricated; show indeterminate indicator. Tool payload/credentials hidden. Multiple parallel workers display grouped step lineage, not fake linear temporal ordering.
- **Watch:** separate "Create monitoring" confirmation with condition, timezone, next run, notification destination binding. Only show "監視を登録しました" after saved Watch ID exists in GET. Failed observation = unknown, not ¥0.
- **Evidence:** Claims and citations mapped by evidenceId; show source URL, observation time, source timestamp if known, estimate/confirmed/expired and confidence. HTML-safe text; multiple values cannot inherit one unrelated source. Currency decimal strings must be parsed by a deterministic decimal library, not float.
- **Result incomplete:** partial findings not silently described as final. \`uncertain\` can be a side-effect requiring reconciliation and a "do not retry automatically" CTA; \`unavailable\` lists what couldn't be verified.
- **Approval:** unique immutable action digest, origin/resource, cost/expiry (CHAT_03), 2-step confirmation for irreversible external effect (proposal); explicit deny. Do not render "approve all" checkbox.
- **Budget:** shows configured max, confirmed spend, pending estimate, as-of UTC and currency, not a simple unlabelled token number; Model options returned from server-signed policy. If over budget, present user-settable next step only after server permission.
- **Accessibility:** semantic role announcements for status (debounced max 1 announcement/2s), keyboard commands with visible focus, no autofocus into streaming text, minimum 44 CSS px touch targets design target, reduced-motion preferences, responsive 320px narrow. No color-only status.
- **Privacy:** Logout tears down subscriptions, clears private in-memory query cache, never stores bearer/secret in browser localStorage. Draft cache opt-in; if stored, text may contain sensitive data so session/local storage policy requires CHAT_09 review.

## 8. Failure matrix

| Fault | UI action | Backend guarantee |
|---|---|---|
| POST timeout, unknown commit | maintain same intent key and display "受付確認中" | no second command on same key; receipt lookup |
| SSE disconnect/proxy 502 | show offline badge, GET+cursor replay | Task not cancelled |
| Page refresh while Running | GET Task from server, optional SSE reconnect | original Run remains |
| 412/409 during Steer | show latest versions, ask user to review intention | no overwrite |
| revoked session | stop stream and purge cache, login route | no private data after revoke |
| source evidence stale | show observedAt & stale marker | no realtime claims |
| model failure but tool result uncertain | show unknown, not no-result | no blind side-effect repeat |
| Watch create success, notification unavailable | display Watch + degraded notifications | don't invent delivery success |
| Browser takeover expires | show reauthentication needed | owner lease refused |
| offline draft repeated click | disable repeat only while identical intent pending | user can deliberately start distinct task with new intent |

## 9. Security, budgets, operations

AuthN/Origin/CSRF handled by CHAT_09; all REST/SSE authorization includes tenant and owner. Design against stored XSS, markdown link scripts, HTML prompt injection, DOM clobbering, malicious artifact MIME, speculative prefetch to internal hosts, model-provided credential URLs. UI never renders HTML unescaped, never fetches an untrusted source automatically, does not render hidden thoughts. Allowed destinations optionally \`rel=noopener noreferrer\`; CSP is server configured. Long messages clipped with explicit "show more" using safe text, max visible 1000 result rows virtualized and paginated. API caps and model cost enforced server side; UI numeric limits merely hints. Error telemetry logs requestId and safe state only; no raw prompts/cookies/approval tokens.

Metrics: submitAckMs, renderUpdateLagMs, SSEReplayCount, unknownAckRate, retryIntentReused, authFailure, staleEvidenceCTA, incorrectCostDoubleCount (target 0), accessibility errors. Performance claims remain unmeasured until fixed hardware/API versions. Instrument server cost for first-class display, never estimate without "estimated" metadata.

## 10. Storage/migration and cross-team proposals

- CHAT_01: exact Conversation/Thread/Message/Task view, shared evidence summaries, schemaVersion, status enum, nullable conversation, goalRevision immutable, UUIDv7.
- CHAT_02: Command lifecycle, steering safely applied at boundary, currentRunId/activeRunId, budget+model selection.
- CHAT_03: immutable approval action description, decision CAS, side-effect uncertainty semantics.
- CHAT_06/14: feedback preference scope and Undo; not a raw Fact overwrite.
- CHAT_07/13: Watch/Notification projection+priority/quiet hours, no independent send.
- CHAT_08(rest-api/realtime-events): snapshot cursor in GET Task and stable acknowledge; if no such field, reliable resumption blocked.
- CHAT_09: session handling, human takeover/CSRF, safe private draft storage, tenancy/workspace mapping.
- CHAT_10: durable idempotency receipt lookup, Task/Message/Evidence query, DB indexes, version migration. On format change, old UI must fail-close on unknown critical status rather than silently fall back to "success".
- \`POST /api/v1/conversations\` is separate optional action, not mandatory for Task. Watch/event-created Tasks preserve null conversationId.

## 11. Implementation PR plan

CHATUI-P1 generated typed API client + accessible app shell + auth-aware cache; P2 composer & idempotent Task creation + budget form; P3 snapshot/SSE reducer and reconnect/replay; P4 evidence/usage/status panels + uncertain/partial; P5 approval/takeover/dashboard links and E2E accessibility/performance. Avoid implementing fake production server in this specification repo.

## 12. Tests (design only; no app executed)

| TEST | Setup/action | Expected | SC |
|---|---|---|---|
| CHATUI-T01 | Render general research/Hotel form, submit twice same intent | only one Task command, 202 not "done" | 01,04,05,07 |
| CHATUI-T02 | SSE replay duplicate/refresh/navigation while Run active | one step/cost projection, no missing snapshot | 05,07 |
| CHATUI-T03 | Put Running/Waiting/Partial/Unknown/Completed fixtures in server state | five visually/ARIA distinct states; no fake % | 02,06,07 |
| CHATUI-T04 | Mixed date/currency/price evidence including missing URL | source+observedAt+certainty; missing stays unknown | 01,03,04,05 |
| CHATUI-T05 | Server allows model A, denies model B/budget above hard cap | UI selection gated, server denies forged request | 09 |
| CHATUI-T06 | 2 browsers concurrently Steer same Task old ETag | one applied, 412 latest refresh; no silent intent loss | 07 |
| CHATUI-T07 | Approval/booking mock, user denies and closes page | no unauthorized reservation or tool execution | 04,06,08 |
| CHATUI-T08 | Render injected HTML/script source + malicious PDF title | zero script execution, secrets absent | 03,08,15 |
| CHATUI-T09 | Watch event Task with null Conversation, navigate permalink | no phantom Conversation, exact Task status visible | 02,11 |
| CHATUI-T10 | WCAG keyboard/mobile 320px, reduced motion, screen-reader event updates | all controls keyboard operable, no inaccessible dialog | 07,09 |
| CHATUI-T11 | duplicate SSE usage and price float fixture | no double counting, correct currency/decimal precision | 01,09 |
| CHATUI-T12 | Browser reload after POST commit but response loss | same idem key receipt, no duplicate | 07 |
| CHATUI-T13 | User submits exclude-retailer feedback then Undo | next suggestions reflect scoped preference; original evidence remains | 16 |

**All MUST mappings:** 001→T01,T12; 002→T02; 003→T03; 004→T04; 005→T05; 006→T06; 007→T07; 008→T08; 009→T09; 010→T10,T12; 011→T11. Fixture-only; no real purchases, posts, bookings, external messages.

## 13. Unresolved decisions

| Proposal | Owner | Severity | Acceptance |
|---|---|---|---|
| CHATUI-P01 | CHAT_01 | P0 | status enums, evidence and TaskView canonical fields |
| CHATUI-P02 | CHAT_02 | P0 | command apply vs Ack, goal version & model budget policy |
| CHATUI-P03 | CHAT_08 SSE | P0 | high-water cursor and replay semantic |
| CHATUI-P04 | CHAT_09 | P0 | session+CSRF, TENANT↔WORKSPACE ownership |
| CHATUI-P05 | CHAT_03/04 | P1 | approval/takeover action guarantees |
| CHATUI-P06 | CHAT_10 | P1 | persisted receipt and read indexes |
| CHATUI-P07 | CHAT_11 | P1 | E2E a11y/fault/replay fixture approval |

## 14. Sources (verified source/test **read**, integration execution **not run**)

[S1] Vercel AI SDK \`useChat\`, pinned f8507823, 2026-10-30, Code verified: https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/packages/react/src/use-chat.ts  
[S2] React hook tests incl stream replacement, status, optimistic updates, Code verified/test read: https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/packages/react/src/use-chat.ui.test.tsx  
[S3] \`createUIMessageStream\`, Code verified: https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/packages/ai/src/ui-message-stream/create-ui-message-stream.ts  
[S4] Mastra React agent hooks \`useChat\`, \`approveToolCall\`, \`declineToolCall\`, pinned ec3ba11d, Code verified: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/client-sdks/react/src/agent/hooks.ts  
[S5] Mastra playground \`ChatProvider\` error/stream lifecycle, Code verified: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/playground/src/lib/ai-ui/chat/chat-provider.tsx  
[S6] Mastra handlers error test, Code verified/test read: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/error.test.ts  
[S7] OpenClaw app-host \`subscribeEvents\`, pinned 8b8c33ef, Code verified: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/ui/src/app/app-host.tsx (MIT)  
[S8] OpenClaw \`serveAuthorizedSessionHistory\`, Code verified: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/sessions-history-http.ts  
**Hypothesis** all UI file layout, component boundaries, CSS/a11y behavior, performance limits. No SDK feature verified within this specific Harness, no UI/CI tests executed.

## 15. Issue報告による回帰強化

**[I-CHAT-01]** [Vercel AI SDK issue #22358](https://github.com/vercel/ai/issues/22358)（2026-10-30閲覧、**Closed**、Issue report）: Custom ChatTransportの`resumeStreamIsReplay`フラグが`useChat`経由で失われ、全量replayが古いpartial textに重複追加される事象が報告された。本仕様はSDK側が修正済みかどうかに依存せず、**server Event ID/Task seqとabsolute redacted snapshotを正本**とする。CHATUI-T02に「partial assistant output→切断→全量replay→完成」のとき完成テキストは1回のみ、StepとUsageも重複しない試験を追加する。SDK導入時には対象バージョンで同型の回帰テストを実行する。

**[I-CHAT-02]** [Mastra issue #28224](https://github.com/mastra-ai/mastra/issues/28224)（2026-10-30閲覧、Open、Issue report）: terminal event公開前のRun lease喪失。Runが途中で応答を停止してもUIは架空の完了を宣言せず、GETの永続Task/RunとSSEの最後の正常イベントを比較し、uncertain/refreshを表示する。CHATUI-T03/T12へterminal-event欠落時の表示fixtureを追加する。

## 16. Upstream PR差分の照合（Code verified PR patch）

**[PR-CHAT-01]** [Vercel AI SDK PR #22362](https://github.com/vercel/ai/pull/22362)（2026-10-30確認、**Merged**）の実diffを調査した。`packages/react/src/use-chat.ts` のstable transport wrapperに `get resumeStreamIsReplay() { return getTransport().resumeStreamIsReplay; }` が追加され、`use-chat.ui.test.tsx` に `Hello` 部分表示→network disconnect→`Hello world`全量replay時の重複を防ぐ実テストが追加された。これは [issue #22358](https://github.com/vercel/ai/issues/22358) に対応する修正で、**旧バージョンで起こり得る不具合であり最新mainで未修正とは主張しない**。本Harnessではライブラリ側の修正の有無に依存せず、独自`TaskEvent.sequence`で冪等投影を検証する（CHATUI-T02）。

**Round2 canonical serialization correction:** [CHAT_01 PR #2](https://github.com/choko510/aiagent-md/pull/2) defines recordVersion/goalRevision/leaseEpoch as canonical int64 decimal **strings**, not JSON numbers. [CHAT_01 PR #23](https://github.com/choko510/aiagent-md/pull/23) defines tenant deliveryOffset separate from aggregateSequence; clients use signed snapshotCursor and eventId dedupe, never numeric cross-aggregate comparison. Earlier locally used `lastEventSequence` or numeric recordVersion must not survive generated client. CHATUI-T02/T11 test this contract.

## Formal Round 2 MUST Traceability (full stable IDs; designed tests only)

| MUST REQ-ID | TEST-ID(s) | SC-ID(s) |
|---|---|---|
| CHATUI-001 | CHATUI-T01, CHATUI-T12 | SC-01, SC-04, SC-05, SC-07 |
| CHATUI-002 | CHATUI-T02 | SC-05, SC-07 |
| CHATUI-003 | CHATUI-T03 | SC-02, SC-06, SC-07 |
| CHATUI-004 | CHATUI-T04 | SC-01, SC-03, SC-04, SC-05 |
| CHATUI-005 | CHATUI-T05 | SC-01, SC-09 |
| CHATUI-006 | CHATUI-T06 | SC-07 |
| CHATUI-007 | CHATUI-T07 | SC-04, SC-06, SC-08 |
| CHATUI-008 | CHATUI-T08 | SC-03, SC-08, SC-15 |
| CHATUI-009 | CHATUI-T09 | SC-02, SC-11 |
| CHATUI-010 | CHATUI-T10, CHATUI-T12 | SC-07, SC-09 |
| CHATUI-011 | CHATUI-T11 | SC-09 |

**Execution:** 0 Harness tests run. These are proposed test cases; an independent reviewer must validate each result before Locked.
