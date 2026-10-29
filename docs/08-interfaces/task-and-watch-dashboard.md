# Task and Watch Dashboard — Durable Run/Watch Operations

> Owner CHAT_08 | docs/08-interfaces/task-and-watch-dashboard.md | Draft / [PROVISIONAL CHAT_01/02/07/09/10] | 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-01,02,04,05,06,07,08,09,10,11,12,14,16

## 0. 採用案と不変条件

**TaskとWatchを別リソースとして一覧化し、各行は永続化されたRead DTOのみから組み立てる。** Taskは調査目的・GoalRevision・Run履歴・費用・Evidence、Watchは保存された監視条件・時刻・結果・トリガー・配送状態。TaskのStopとWatchのPauseを同じ遷移として扱わない。GETとSSEは通信が切れてもServer正本へ収束する。ActionはRESTの認可済みCommand（Idempotency-Key/If-Match）で送信し、202 Acceptedを完了扱いしない。

**主要な利用例:** 一般Web調査の進行状況を開く、購入差益候補の根拠を比較、Watchの次回実行と通知条件を確認、X特定ユーザーのイベントから起動したTaskを追跡、失敗を表示して安全に再試行、承認待ちを認識。静止した監視の状態を勝手に推定しない。

## 1. Scope / ownership

- User dashboard: Task list, Task detail, Run attempts/step timeline, Watch list/detail, status/nextCheckAt/lastObservedAt, Evidence/cost/notifications, filters, pagination, actions pause/resume/cancel/steer/retry, accessible mobile layout.
- Non-goals: Tool/Browser workerの実行、Watch trigger判定、DB scheduler、直接Notification配信、曖昧な状態の修復ボタン、匿名Admin panel。
- Domain Task/Run/State/Evidence/GoalRevision: CHAT_01。Run commands/checkpoint: CHAT_02。Watch/Observation/Outbox: CHAT_07。Identity and ACL: CHAT_09。Persistence physical DDL: CHAT_10。SSE and REST read projection: CHAT_08。
- [PROVISIONAL] UUIDv7はCHAT_01の論理正本。Task.conversationIdは\`null\`可（Watch/Event起点）。recordVersion初期1、Run leaseEpoch/goalRevisionとは別概念。Workspace_idとtenantIdは同一視せずAuthに解決を委譲。

## 2. OSS調査（source and test inspected 2026-10-30）

| OSS commit | 具体的コード / テストと観察 | 採否 |
|---|---|---|
| **Mastra** ec3ba11d [S1-S4] | \`packages/server/src/server/handlers/schedules.ts\` の LIST/GET/PAUSE/RESUME、\`schemas/schedules.ts\` のnextFireAt/lastRun、\`handlers/schedules.test.ts\` の冪等pause/resume・trigger結果試験、\`playground/src/domains/schedules/__tests__/schedules-list.keyboard.test.tsx\` のroving tabIndex・keyboard、\`schedules-list.sort.test.tsx\` のnextFireAt/lastFireAt sort | **採用:** schedule next/last/trigger UI、状態の明示、キーボードテスト、複数ソート。**却下:** 本製品WatchをMastra Schedule rowに直接統合（condition+observation+deliveryの正本が違う）。 |
| **OpenClaw** 8b8c33ef [S5-S6] | \`ui/src/app/app-host.tsx\` \`subscribeEvents\` + cleanup、\`src/gateway/sessions-history-http.ts\` のcursor付きhistory/snapshot/pagination | **採用:** 履歴とlive feedを分け、遷移時に購読dispose。**却下:** transient memory sessionをTask一覧の基礎にする。 |
| **Vercel AI SDK** f8507823 [S7] | \`packages/react/src/use-chat.ui.test.tsx\` のstream resumption / stale partial response replacement | **参考:** Stream resumed時の二重差分描画回避; UI SDKをDB状態の正本としない。 |

全source/test閲覧済み・テスト実行未実施。Mastra standard Apache-2.0/ee例外、OpenClaw MIT、AI SDK導入ライセンス/版は実装時要再確認。コード転載しない。

**Alternatives:** A. polling-only=シンプルだが更新コスト/遅延→fallback。B. SSE-only=再起動/オフラインで一覧が古くなる→却下。C. REST snapshot/cursor pagination + SSE hints+GET reconciliation→採用。D. dashboardが独自Watch stateを管理→所有権違反として却下。

## 3. Information architecture / components

~~~text
/dashboard
  ├─ /tasks               TaskList(filters, pagination, status, source)
  ├─ /tasks/{id}          TaskHeader + Runs + Evidence + Usage + CommandBar
  ├─ /runs/{id}           RunDetails/steps/attempts/tool receipt state
  ├─ /watches             WatchList(condition, timezone, nextCheck, health)
  └─ /watches/{id}        WatchDefinition + observations + trigger/delivery history
  Client Query Cache ----> GET /api/v1/{tasks,runs,watches}
  Stream Reducer  <----- GET /api/v1/tasks/{id}/events (only for visible details)
  CommandBar -----------→ POST /api/v1/.../commands (server CAS and authorize)
~~~

UI file proposals: \`features/dashboard/{task-list,run-timeline,watch-list,action-controller,status-projection}.ts(x)\`, \`components/{EvidenceBadge,UsagePanel,FailureDetails,ActionConfirm,EmptyState}\`, tests colocated and Playwright E2E.

## 4. Requirements

| REQ | Level | Observable | SC |
|---|---|---|---|
| DASH-001 | MUST | Task/Run/Watch list/read uses authenticated tenant-scoped cursor pagination, no cross-user leakage | 02,08 |
| DASH-002 | MUST | Task detail shows server state, activeRunId, attemptNo, goalRevision, cost/evidence and null Conversation correctly | 01,05,07,11 |
| DASH-003 | MUST | Watch shows persisted schedule/timezone/nextCheckAt/last observed/condition/notification target and acquisition errors | 02,12 |
| DASH-004 | MUST | Pause/Resume/Cancel/Steer uses If-Match recordVersion and idempotency; ack != applied, 412 refresh | 07 |
| DASH-005 | MUST | Run failed/partial/uncertain/approval-waiting and tool UNKNOWN are visually distinct; not auto-retried | 06,07 |
| DASH-006 | MUST | App reconnect, navigation, refresh reconcile snapshot and stream without duplicate steps/cost | 07,09 |
| DASH-007 | MUST | Evidence source, observed/fetched time, price/currency, stale/unknown are labeled | 01,03,04,05,14 |
| DASH-008 | MUST | Watch paused/disabled never displayed active because nextDue was stale; no false success/0¥ on acquisition failure | 02,12 |
| DASH-009 | MUST | Mobile 320px and keyboard-only operation, focus recovery, no color-only status, virtual/paged large lists | 07,09 |
| DASH-010 | MUST | External side effects and browser session controls are only routed to authorized approval/takeover UI | 04,06,08 |
| DASH-011 | SHOULD | filter/status/source/date and saved view (nonprivileged preferences), stable URL query | 02,11 |
| DASH-012 | SHOULD | Priority and quiet-hour state surfaced from notification intelligence, not computed in UI | 16 |
| DASH-013 | LATER | Multi-task side-by-side compare after evidence provenance confirmed | 01,10 |
| DASH-014 | EXPERIMENT | Rich per-phase timing estimates measured and labelled estimate | 09 |

## 5. View models / API mapping [PROVISIONAL]

~~~ts
type TaskRow = {
  id:string; recordVersion:string; conversationId:string|null;
  state:string; resultKind:string|null;
  goalSummary:string; goalRevision:string; activeRunId:string|null;
  lastUpdatedAt:string; snapshotCursor:string;
  budget:{confirmed:string|null;estimated:string|null;currency:string;asOf:string|null};
  evidenceCount:number;
};
type RunRow = {
  id:string; taskId:string; attemptNo:number; inputGoalRevision:string;
  state:string; progress:{
    completedSteps:number; pendingSteps:number; unknownSteps:number;
  };
  startedAt:string|null; endedAt:string|null;
  // leaseEpoch/leaseOwner are never client authority or editable UI.
};
type WatchRow = {
  id:string; recordVersion:string; state:string;
  conditionSummary:string; scheduleTimezone:string;
  nextCheckAt:string|null; lastObservedAt:string|null;
  lastAcquisition:"ok"|"error"|"not_run"|"unknown";
  notificationDestinationSummary:string; lastDelivery:"sent"|"queued"|"failed"|"unknown"|null;
};
type OperationPending = {resourceId:string; commandId:string; action:string; acceptedAt:string};
~~~

\`state\` enum is imported from CHAT_01/CHAT_07. No physical DB \`revision=0\` leaked as recordVersion. Distinct \`goalRevision\` and \`recordVersion\` shown only where useful; \`activeRunId\` must be validated same-task. Timestamp string UTC server, locale display with **explicit IANA TZ and absolute tooltip**. Money is decimal string plus currency; no naive JS float. Usage records distinguish estimated/confirmed. LastObservedAt is not nextCheckAt or notification deliveryAt. A failed fetch cannot lower price to zero.

GET \`/tasks\` / \`/watches\` return pages with opaque authenticated cursors from REST spec. Query filter cannot create cross-tenant lookups. Sorted order stable tie-break by id. LastEventSequence must represent Task projection high-water (SSE spec) for gap-free bootstrap. Resource ETag = canonical recordVersion initial 1; not Run leaseEpoch or DB physical revision.

## 6. Lifecycle / interaction sequence

~~~text
Dashboard open → authenticated GET /tasks?limit=20 [safe projection]
  → user selects Task → GET /tasks/:id (recordVersion + snapshotCursor)
  → GET /tasks/:id/runs, GET /runs/:id/usage
  → SSE(after signed snapshot cursor) → reducer applies absolute projections
  → user presses Pause → confirmation → POST /tasks/:id/commands
       headers: If-Match:"N", Idempotency-Key:<intent>
  ← 202 {commandId} / operation accepted, not paused yet
  → refresh GET while runtime applies at safe boundary
  → authoritative status=paused or 409/412/partial/uncertain
  → SSE resume or tab hidden uses bounded reconciliation
~~~

Watch: GET /watches/{id} from canonical WatchService, initial nextCheckAt is nullable; user chooses pause; POST command; pending action label until WatchService confirms revision; manual trigger if future contract approved must use different dedupe key from scheduled slot (CHAT_07). No UI-generated predicted nextCheckAt used as persisted fact.

Workflow:
1. Prefetch only visible row data; never prefetch BrowserSession secret/artifact raw.
2. Tab load GET snapshot and cursor. On SSE event, revalidate event.taskId/scope/projectionVersion/sequence; update absolute fields; if unknown major or gap, refetch.
3. On action confirm, recheck still authenticated, get latest ETag, generate new per-intent Idempotency-Key; POST same body. Stale version 412 is an explicit user review path, **not automatic refresh-and-repeat**.
4. On Cancel, show "停止要求受付済み" until Runtime confirms terminal/uncertain; do not discard inflight unknown effect or cancel audit.
5. On Goal Steer, render version requested vs version applied; old Run inputGoalRevision is immutable; do not update UI based on optimistic text alone.
6. When opening Watch-triggered Task with conversationId=null, show origin Watch/event link and direct Task details; no synthetic chat transcript.
7. On user disable/revocation or tenant switch, clear private client cache and terminate streams.

~~~ts
async function dispatchDashboardAction(resource,action,body,readModel) {
 const normalized=validateAllowedAction(resource.kind,action,body);
 const key=generatePerIntentIdempotencyKey(); // persisted while ACK unknown
 const result=await api.postCommand(resource.id,{action,...normalized},{
   "If-Match": quoteVersion(readModel.recordVersion), "Idempotency-Key":key
 });
 return {status:"accepted-not-applied",commandId:result.commandId,key};
}
function applyDashboardEvent(view,evt) {
 if(evt.taskId!==view.id || dedupeForTask(view.id).has(evt.eventId))return view;
 if(!knownPublicProjectionVersion(evt))return {...view,requiresRefetch:true};
 const next=mergeAbsoluteRedactedProjection(view,evt);
 dedupeForTask(view.id).add(evt.eventId); // bounded LRU, reset when fresh GET snapshot loaded
 return next;
}
~~~

## 7. Failure matrix

| Cause | Detection | UX/recovery |
|---|---|---|
| Missing permission/other tenant | 401/404; stream revoke | remove cached rows, reauth, no leak |
| Concurrent update | 412 If-Match or 409 state | GET latest, ask user to reconfirm |
| Ack lost after commit | network error | same key receipt query/retry; never duplicate action |
| Run crash | state/stale heartbeat | historical steps remain, Runtime recovery, avoid "done" |
| External side effect UNKNOWN | ledger projection | manual reconciliation/approval; no retry button |
| Watch acquisition failure | acquisition_code error | last known value shown stale, no zero |
| SSE gap/retention | 410 or version mismatch | GET snapshot/bootstrap |
| Clock skew/TZ DST | server-supplied ISO + IANA | do not derive nextDue from device clock |
| Queue delay/worker unavailable | accepted but unscheduled | show queue age / degraded worker, not success |
| Notification delivery failure | outbox status | Watch remains stored; retry by CHAT_07 |
| Source record deleted/hidden | 404 | remove target row and cache |
| 100k rows | bounded cursor/page | paginate, virtualize, no unbounded memory |

## 8. Security / accessibility / limits

Authorization on list/detail/command enforced server-side with CHAT_09. Client URL ID is not a capability. Never provide raw tool args, credential, cookie, private browsing output, task hidden prompts, subscriber personal IDs. Redacted error text. Untrusted X/HTML text rendered escaped with safe link handling; no innerHTML. Implicit background prefetch may leak evidence, so only permitted projection/read routes. CSRF protection on cookie-auth commands; no response cache shared across tenants. Action confirmations default deny for irreversible effects; Dashboard cannot create approval or overwrite Tool Gateway policy.

Proposed v1 ceiling: 20 rows/page default, hard 100; server already enforces this. Virtualize when >100 visible step nodes; SSE max 5 active/task-user as shared stream spec, refresh visible tabs only. Mobile layouts use 44px touch targets (design target, pending UX verification), reduced motion, focus managed modals. Estimated cost/time must be explicitly marked. Logs: requestId, tenant-scoped resource ID masked, command status, no raw goal/credentials. Metrics listLatency p95, actionAckLatency, delayedAppliedAge, incompleteEvidenceRate, watchNotificationFailure, staleTimeRate, failedKeyboardTest.

## 9. Persistence / API / migration

No new canonical DB table from dashboard. [PROVISIONAL CHAT_10] indexes by tenant/workspace,status,created_at and watch next_due, run task_id/attempt_no; avoid N+1 joins for lastRun. [CHAT_01/02] Task/Run goalRevision+activeRunId+recordVersion persistence and lease epoch fencing. [CHAT_07] Watch nextDue/stopped/unknown observation semantics and Notification outbox join. UI stores ephemeral filter only; saved filter preferences are separate from fact/evidence and tenant-scoped. Version migration: old row with \`revision=0\` cannot be rendered ETag "0"; server owner must migrate/map. Rollout behind feature flag with contract and UI tests; fail closed for unknown enum.

## 10. Implementation PR split

DASH-P1 canonical read DTOs, cursor and authenticated filters + table keyboard a11y; DASH-P2 Task detail/Run timeline/evidence/cost; DASH-P3 Watch condition/schedule/outbox status; DASH-P4 CommandBar with ETag/Idempotency, 409/412 and pending state; DASH-P5 SSE/refresh/caching, mobile UX, fault/security/benchmark.

## 11. Tests designed, not executed

| TEST | Level/setup → action | Expected | SC |
|---|---|---|---|
| DASH-T01 | API/contract, 2 tenants with 100 Tasks/Watch | 0 tenant leaks; stable cursor, UUIDv7 IDs | 02,08 |
| DASH-T02 | UI, Watch Task has conversationId=null and activeRun | correct Task/Run link, no fake Conversation, goalRevision preserved | 07,11 |
| DASH-T03 | UI, paused Watch, DST timezone, failed acquisition | next time from server/null, acquisition is unknown not 0; notification shows actual status | 02,12 |
| DASH-T04 | Integration, concurrent pause/steer on same ETag | one 202, other 412; second manual review, no blind overwrites | 07 |
| DASH-T05 | UI, failed vs partial vs uncertain vs waiting approval | distinct status, no false terminal or retry UNKNOWN effect | 06,07 |
| DASH-T06 | E2E, refresh/reconnect replay same seq 10 times | one timeline node per event; usage not multiplied | 07,09 |
| DASH-T07 | UI, source URL date/price/currency cross sources | source/time/actual vs estimated; no fabricated live price | 01,03,04,14 |
| DASH-T08 | Integration, paused Watch nextDue stale in DB projection | disabled row takes precedence; never labeled active | 02,12 |
| DASH-T09 | Accessibility keyboard-only, 320px viewport, screen reader | every command reachable, focus visible, no color-only state | 07,09 |
| DASH-T10 | Security, forged browser action or approval ID from Dashboard | blocked; tool execution count 0 | 06,08 |
| DASH-T11 | Fault, server kill between POST commit and response, resend | one command, same receipt, pending label reconciles | 07 |
| DASH-T12 | Benchmark 50k rows/1k watches/100 concurrent viewers | record p95 memory/latency and query plan; no untested SLA | 09 |

MUST trace: DASH-001→T01; 002→T02; 003→T03; 004→T04,T11; 005→T05; 006→T06; 007→T07; 008→T08; 009→T09; 010→T10. Test fixture only; no real purchase/booking or live Telegram.

## 12. Outstanding contract proposals

| ID | Owner | Severity | Acceptance |
|---|---|---|---|
| DASH-P01 | CHAT_01 | P0 | Task/Run state and goalRevision/recordVersion, public ID v7, Task null conversation |
| DASH-P02 | CHAT_07 | P0 | Watch nextDue, notification/outbox, failure vs no-change, change triggers |
| DASH-P03 | CHAT_02 | P0 | pause/steer/cancel applied boundary, reconciliation of unknown effect |
| DASH-P04 | CHAT_09/10 | P0 | Tenant↔workspace mapping and owner isolation |
| DASH-P05 | CHAT_08 REST/SSE | P1 | Snapshot cursor/ETag/Error contract; form and table shared client |
| DASH-P06 | CHAT_11 | P1 | Keyboard/mobile/stream crash fixture acceptance |

## 13. Sources

[S1] **Code verified**, Mastra schedules routes pinned ec3ba11d: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/schedules.ts  
[S2] **Code verified**, schema nextFireAt/lastRun: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/schemas/schedules.ts  
[S3] **Code verified test read**, pause/resume, trigger, 404: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/schedules.test.ts  
[S4] **Code verified tests read**, keyboard/sort: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/playground/src/domains/schedules/__tests__/schedules-list.keyboard.test.tsx and https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/playground/src/domains/schedules/__tests__/schedules-list.sort.test.tsx  
[S5] **Code verified**, OpenClaw app-host subscribe events pinned 8b8c33ef: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/ui/src/app/app-host.tsx (MIT)  
[S6] **Code verified**, OpenClaw sessions history with cursor and SSE: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/gateway/sessions-history-http.ts  
[S7] **Code verified test read**, AI SDK UI reconnect: https://github.com/vercel/ai/blob/f8507823abe78471d6b3cfa2da91a63118edbff4/packages/react/src/use-chat.ui.test.tsx  
**Design Hypothesis:** query layering, UX limits, notification status schema, recordVersion resolution and UI component layout are proposals, not measured/integrated. OSS source read on 2026-10-30; no source test executed.

## 14. Issue報告からの追加テスト

**[I-DASH-01]** [Mastra #22340](https://github.com/mastra-ai/mastra/issues/22340)（2026-10-30、Open、Issue report）はScheduleのdispatch成功とRun completionが異なることを指摘する。DASH-T03/T05に「scheduled fire succeeded / Task Running / Approval suspended / Worker restarted」のfixtureを追加し、Watchのチェックが開始された表示と、Taskが完了した表示を**絶対に混ぜない**ことを検証する。**[I-DASH-02]** [OpenClaw #127367](https://github.com/openclaw/openclaw/issues/127367)（issue titleを検索で確認したのみ・本文未検証）はunbounded history readの懸念。関連本体はSSE replay側でbounded batchへ制約する。両者は本システムでの発生を確認したわけではない。

**Round2 common contract alignment:** [CHAT_01 PR #2](https://github.com/choko510/aiagent-md/pull/2) uses int64 decimal strings for recordVersion/goalRevision; [CHAT_01 PR #23](https://github.com/choko510/aiagent-md/pull/23) uses separate tenant deliveryOffset and per-aggregate sequence. Dashboard stores only signed snapshotCursor, dedups by eventId, and does not render raw tenant counters. DASH-T02/T06 verify distinct Run/Tool events sharing aggregateSequence and immutable GoalRevision string.
