# Heartbeat and Goal Loop — Detailed Implementation Specification

> Owner: CHAT_07 | Path docs/07-automation/heartbeat-and-goal-loop.md | Status: Draft / [PROVISIONAL: CHAT_01/02/03/09/10] | Revision 0.2 | Research 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-02,05,07,08,09,10,11,16
>
> この文書は本番コードではない。全GOL-Tテストは**設計済み・未実行**。Goal/Task/Run/Context/Checkpoint/Steer/ToolはCHAT_01/02/03の正本を参照し、本書では**継続チェックを安価・有限・安全に開始する条件**のみ定義する。

## 0. 採用案

「AIに長期間目標を覚えさせ常に考え続けさせる」を採用しない。永続GoalはDBに保存された**目的・条件・進捗と次回判定時刻を持つユーザー意図**であり、Schedulerのtickは**決定的な理由選別器**だけを実行する。新しいObservation/外部Event/期限/ユーザー操作/明示的な依頼など重要な状態変化がない限り、LLMは呼び出さない。やるべきことが残る場合でも、利用可能なBudget/Tool approval/source freshnessを判定し、最小の研究TaskをRun Managerへ渡す。

1. **No idle inference**: idle heartbeatはDB/queueのメタデータを確認するだけ。変更がない場合は「何もしない」が正式な成功結果。
2. **Durable continuation**: Goal revision、次に必要なチェック、前回結果、依存待ち、承認待ち、継続予算、停止理由を保存。Agent loopの文脈はCHAT_02が持ち、Heartbeat自体は未信頼Webをプロンプトへ再貼付しない。
3. **Bounded goal**: maxTurns、maxModelCalls、maxUSD、maxElapsed、maxTasks/child concurrency、Tool quotas、最大連続失敗、nextRetryを全て有限値で持つ。quota holdで一分ごとに同じAIを呼ばない。
4. **Revision/fence aware**: Goal conditionをSteerするとgoalRevisionが増え、旧revで作った継続Intentはworker開始前とTool開始前にreject。Run leaseEpochはCHAT_01/02の共有正本を使用し、Heartbeat claim epochで代用しない。
5. **No unconditional side effects**: 監視結果で購入・予約・投稿・送金を実行せず、必要な場合はCHAT_03承認を待つ。待機中もSchedulerはbusy loopしない。

## 1. Scope/責務/非責務

Owns: Goal続行のtick判定、heartbeat coalesce、状態変化の根拠とwake trigger、max cost/interval、stale dependency、予算/人間承認待ちの期限付停泊、停止/再開時の通知、イベント由来の軽量起動、再計画の入場決定。

Not owns: 目標の正式共有型/Conversation/Task/RunはCHAT_01、Planner/Agent loop/Model Router/Budgetの実装はCHAT_02、Tool Gateway/Approval/Side-effect ledgerはCHAT_03、Schedulerの時刻処理は07-01、Observationは07-03、通知Outboxは07-04、EventIngressはCHAT_13、DB DDLはCHAT_10、ユーザー別credential/egressはCHAT_09。

SC-05の長期一般研究、SC-07のcancel/steer/restart、SC-09の費用/モデルrouting、SC-02継続監視、SC-10自律機会探索の費用制限、SC-11 Xイベント契機の研究、SC-16好みの明示更新/不要な通知制限に対応。非目標: ホストshellで自律管理、毎tick「何かない？」とAIに問うこと、推論による承認迂回、未認可の広告投稿、非対応streamから即時通知を保証。

## 2. 既存OSS source/test比較（2026-10-30）

| OSS / SHA / license | 閲覧済み実コード・テスト | 今回への評価 |
|---|---|---|
| **Hermes Agent** fcc26fbd9f1641755e49793d7ed608f3b7214c3d, MIT [S1,S2] | hermes_cli/heartbeat.py: HeartbeatManager.due_prompt, abandon_fire, pause/resume（idle-only, claim-before-turn + unstarted rollback）; tests/hermes_cli/test_heartbeat.py: missed coalesce, paused, resume reanchor, stale manager claim | idle gateと**未開始のclaimを安全に戻す**発想を参考。Heartbeat promptを毎回ユーザーロールに注入する方式は本Harnessではcostに不適なので却下 |
| **Hermes Goals** 同commit, MIT [S3,S4] | hermes_cli/goals.py: GoalState, GoalManager, GoalContract, max_turns/parse & transport failure guards; gateway/run_goals.py: GatewayGoalsMixin、tests/hermes_cli/test_goals.py、tests/gateway/test_goal_resume_restart.py | persistent goal, bounded turns, wait gate, explicit resumeが継続を再開する回帰テストを参考。HermesのAgent内部judge/API fail-openは採用しない；LLM判断の不確定なら費用/権限を優先してpause/needs-review |
| **OpenClaw** 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT [S5,S6] | src/cron/default-proactive-job.ts: createDefaultProactiveJob, resolveDefaultProactiveCadenceMs, provisionDefaultProactiveJob (idleOnly)、src/cron/service/jobs-scheduling.ts: errorBackoffMs, resolveCronRestartRetryAtMs、service.restart-catchup.test.ts | bounded cadence/idleOnlyとrestart backoffを参考。Proactive agent promptを無条件送信する構成は採用せず、diff/needsWork preconditionでskip |
| **Hermes quota policy** 同commit, MIT [S7] | cron/quota_hold.py: plan_hold, _recovery_worthwhile; quota exhausted後、連続失敗時のre-armを抑止する機構。テスト個別ファイルは未監査 | エラー中に毎tick再起動し費用を使う事態を防ぐ。Provider-specific quota parsingは標準化された429/retryAfterへGateway側で変換 |

**代替案**: A) 永続Agent会話を常時streaming・heartbeat promptで維持→推論/記憶コストと権限範囲が無界、却下。B) 毎tick低コストLLMで「続ける？」→空tickで固定費、無意味なプロンプトと注入リスク、却下。C) DB checkpoint+deterministic predicate/Watch/Event wake、必要時だけ限定Run→**採用**。D) 全てを長期Temporal Workflowへ移管→運用コンポーネント増、owner契約との整合未検証、将来Adapterの可能性のみ。

## 3. Architecture & authority

~~~text
User goal & constraints (CHAT_01/02, immutable goalRevision)
   -> GoalContinuationPolicy [THIS] -> DB Goal checkpoint/nextDue [CHAT_10]
      -> Scheduler 07-01 (no model)
      -> Observation 07-03 / EventIngress CHAT_13
      -> dependency/approval/budget gate [CHAT_02/03/09]
      -> bounded admission (unique goalId+goalRevision+wakeReasonKey)
      -> Task/Run Manager [CHAT_02]
        -> Planner/ModelRouter/Tool [CHAT_02/03]
        -> Result verifier + Checkpoint [CHAT_02]
      -> Goal progress / event [CHAT_01/02]
      -> Next check or completion/needs approval [THIS]
      -> Notification Outbox 07-04
~~~

**Authority**: trusted User/verified Bindingだけがgoal/constraints/limitsを作成・Steerできる。X投稿、検索結果、LLM生成の提案は**信頼しないdata**。Goalのための「自動検討」が許可されていても外部購入/予約/投稿は別承認。ownerUserId/tenantId vs workspace_idを調停するまでDB scopeは[PROVISIONAL: CHAT_09/10]。公開Goal/Task/Run IDはCHAT_01 UUIDv7提案を尊重、dedup hashを公開IDとして使わない。

## 4. Requirements

| ID | Level | Expected | SC |
|---|---|---|---|
| GOL-001 | MUST | 無変更HeartbeatでLLM calls=0 | 09 |
| GOL-002 | MUST | goal/checkpoint/nextDueをDB保存しrestart後同じrevisionで再開 | 05,07 |
| GOL-003 | MUST | wake reasonがない場合、新Task/Model開始0 | 05,09 |
| GOL-004 | MUST | maxTurns/modelCalls/USD/time/tools/concurrencyを有限に適用 | 07,09 |
| GOL-005 | MUST | budget depletedで自動再planせずpaused/deferredと説明 | 07,09 |
| GOL-006 | MUST | cancel/Steer/owner revoke後旧goalRevisionから新Tool 0 | 07,08 |
| GOL-007 | MUST | 同じgoalRevision+wakeReasonKeyから継続Intent最大1件 | 07 |
| GOL-008 | MUST | Busy Runある場合別Runを増やさずdefer/coalesce | 07,09 |
| GOL-009 | MUST | 依存のAPI/人間承認待ちはWAITING_INPUT/APPROVALでpark | 05,07 |
| GOL-010 | MUST | Judge/tool失敗が有限回続けばreasonとともに自動pause | 07,09 |
| GOL-011 | MUST | 既存Observation/出典の鮮度と条件を再照合し既知結果だけで再planしない | 05,10,14 |
| GOL-012 | MUST | Model選択はBudget+Policy Routerへ委譲、Heartbeat自身はModel資格を持たない | 09 |
| GOL-013 | MUST | 期限切れ/完了Goalは新Taskを起動しない | 05 |
| GOL-014 | MUST | quota hold/429で期限付き停止し同tick連続再試行0 | 07,09 |
| GOL-015 | MUST | 未承認の購入/予約/投稿/送金は依然許可待ち | 07,08 |
| GOL-016 | MUST | 処理結果のUNKNOWN/Partialと実際の完了/承認を混同しない | 07 |
| GOL-017 | MUST | Watch/外部EventからのWakeが同じbudget/permission下で動く | 10,11 |
| GOL-018 | SHOULD | 完了・停止・NeedApprovalの理由と費用を可視化 | 05,16 |
| GOL-019 | LATER | 信頼度付きGoalの分割や依存Graphの優先queue | 10 |
| GOL-020 | EXPERIMENT | Goal checkerだけと毎tickモデルを呼ぶ方式の同fixtureコスト比較 | 09 |

## 5. I/O型・インターフェース [PROVISIONAL: CHAT_01/02/10]

~~~ts
type GoalWake =
 {schemaVersion:"1.0"; goalId:string; tenantId:string; ownerUserId:string;
  goalRevision:number; wakeReason:"timer"|"new_observation"|"external_event"|"user_resume"|"approval_resolved"|"dependency_resolved";
  reasonKey:string; scheduledForUtc?:string; sourceEvidenceRefs?:string[];};
type GoalContinuationPolicy =
 {maxTurns:number; maxModelCalls:number; maxUsd:string; maxWallTimeMs:number;
  maxChildTasks:number; maxParallelRuns:number; minCheckIntervalMs:number;
  maxConsecutiveFailures:number; providerRatePolicyRef:string;
  allowBackgroundResearch:boolean; expirationUtc?:string;};
type GoalCheck =
 {kind:"no_work"|"admit"|"waiting_input"|"waiting_approval"|"defer_budget"|
  "defer_quota"|"blocked"|"complete"|"needs_review";
  reasonCode:string; nextCheckUtc:string|null; evidenceRefs:string[];};
~~~

これらは**既存Task/Run/Goal共有型を上書きする宣言ではない**。goalRevisionはTask goalの意味的世代、recordVersionはDB CAS、Run.leaseEpochはwork fencing、GoalContinuationClaimEpochはscheduler局所所有権。数値はsafe integer/decimalsの仕様をCHAT_01/02に委譲し、bigint→numberの精度喪失を禁止。

**必須Validation**: goal owner=authenticated principal、explicit purpose/constraints、bounded expenses、budget cap、期限、provider access、approval scope。Modelの出力はGoal revision変更Authorityではない。Goal関連のURLはsource refとしてのみ渡し、実URLアクセスはTool GatewayでSSRF/egressを再検証。計算はUTCのDB clock、user-visible TZはGoal設定に保持。Idempotencyは(tenant,goalId,goalRevision,wakeReasonKey)のcanonical hashingで重複Run0。

## 6. 状態遷移・Sequence

~~~text
ACTIVE
 -> IDLE_NO_CHANGE -> ACTIVE(nextCheckDue)
 -> WAIT_DEPENDENCY/WAIT_APPROVAL/WAIT_INPUT -> ACTIVE(user/approval/event resume)
 -> DEFER_BUDGET/DEFER_QUOTA -> ACTIVE(next permitted time after re-evaluation)
 -> ADMITTED -> RUNNING -> CHECKPOINTED
     -> COMPLETE
     -> NEEDS_REPLAN(reason change) -> ACTIVE (bounded)
     -> WAIT_* or DEFER_* or PAUSED_FAILURE
 -> CANCELLED (terminal) / EXPIRED (terminal)
~~~

状態名は**局所Goal checker結果案**でCHAT_01 Task Stateを定義しない。PauseとCancel/Steerの線形化ポイントはCHAT_02の正本。SteerはgoalRevision+1の正しいcheckpointを保存し、古いcontinued intentを拒否。外部EventでGoalが起動してもそのEvent payload自体を新指示として使わない。

~~~text
Scheduler timer / EventIngress -> GoalWake (durable)
 -> DB: read Goal revision+checkpoint, active Run, dependencies, budget, owner
 -> deterministic cheap check:
     no update -> "no_work", schedule later (LLM 0)
     waiting gate -> park + optional Notification (LLM 0)
     new eligible work -> reserve budget, unique continuationIntent
 -> Worker (checks leaseEpoch / goalRevision)
 -> Task/Run Manager/Planner/Tool Gateway
 -> checkpoint persisted, progress verified
 -> schedule only necessary next check (or complete)
~~~

## 7. 具体アルゴリズム / restart / concurrency

~~~text
onWake(tenant,goalId,goalRevision,wakeReasonKey):
  now := dbClock()
  tx:
    goal := lock tenantScopedGoal(goalId)
    if goal.revision != goalRevision or cancelled/expired/ownerRevoked:
      record skipped_reason; return
    if uniqueContinuation(tenant,goalId,goalRevision,wakeReasonKey) exists:
      return existing
    if activeRun(goal) or pendingApproval(goal):
      persist coalescedWake or WAIT_APPROVAL with finite nextCheck; return
    if noNewEvidenceOrDependencySinceLastCheckpoint:
      persist no_work and monotonically nextCheck; return  # model calls=0
    if goal.consecutiveFailures >= maxConsecutiveFailures:
      pauseWithReason; return
    if providerQuotaHoldUntil > now or budgetHardCapReached:
      persist deferQuotaOrBudget and finite wakeTime; return
    if !goal.policy.allowBackgroundResearch:
      persist WAIT_INPUT; return
    admission := CHAT_02/03.reserveBudgetAndPolicy(goal, caps)
    if !admission.allowed: persist blocked; return
    insert unique GoalContinuationIntent + outbox + checkpoint pointer
    commit
  worker:
    verify current goalRevision, owner auth generation,
      Task goalRevision, Run leaseEpoch, Tool authorization and cap
    run bounded Task/Run via CHAT_02/03
    append verified observation/progress/checkpoint, not raw prompt
    evaluate deterministic next work reason & caps
    if success/complete: mark done once in shared state
    if uncertain: mark partial/uncertain, no unsafe Tool retry
    if quota/rate: set finite next check, no hot-loop
~~~

**Claim after advance crash**: Schedulerが予定時刻を進めたがcontinuation intent未commitならTX rollbackし同keyで再判定。commit後queue ACK喪失はOutboxで復元し、同じGoalWakeからTask複数を作らない。RunnerがTool送信後に落ちたときはRun/Tool ledger照合へ譲り、Heartbeatは「もう一回同じ操作」を決して直接実行しない。in-flight Runある間は後続Wakeを1つにcoalesce（必要なsource refs/priorityを失わない）してから再判定。

**Goal verdict**: deterministic completion verifierの事実が揃う場合だけcompleted。LLMの「完了した」という一文だけでterminal commit禁止。Model-based reasoningが必要ならCHAT_02のModelRouterで、Call数/Token/USD上限を事前予約し、verdictが不確定ならneeds_reviewまたはfinite retryで保留（auto-continue fail-open禁止）。同一条件で永久にjudgeを繰り返さない。

## 8. Failure matrix

| ケース | Detection/recovery | Visible |
|---|---|---|
| idle no change | metadata-only no-op | next scheduled check, model=0 |
| API/LLM quota 429 | policy Retry-Afterを有界に保存、periodicTick再試行禁止 | quota deferred |
| approval not yet granted | Approval owner receipt待ち、同tick AI 0 | needs approval |
| user reply/Steer while Run | revision increment、old intent fence、new snapshot | prior run interrupted/unknown |
| DB crash before intent commit | rollback and replay same wake key | no duplicate |
| Queue ACK lost | outbox relay unique replay | logical Task1 |
| Tool side-effect unknown | CHAT_03 reconcile/approval、blind retry禁止 | uncertain |
| model malformed completion claim | deterministic verifier or needs_review、maxFailures超えるとpause | partial/needs_review |
| heartbeat during busy Run | finite coalesce reason, activeRun unchanged | waiting |
| owner revoked | auth generation recheck at dispatch and Tool gateway | blocked |
| malicious X/html asks to escalate | data only; no new scope/host shell | rejected |
| Goal expired | no additional Task, retain historical record | expired |
| source data stale or missing | request safe bounded fresh observation, else no_work/needs_review | stale |
| crash during quota hold | persisted hold_until; restart reads it | no hot loop |
| unknown ConversationId | Goal can operate without Conversation; no fictitious Conversation insert | correct owner scope |

## 9. Security/Privacy/Prompt injection

Goal permission is **purpose+read/research scope**、not arbitrary external mutation. Each model/tool invocation rechecks current owner/tenant, goalRevision, Run leaseEpoch, approval scope, budget and secrets. X post, web text, email, browser DOM, user-provided files are data and cannot raise caps, change destination or invoke host shell. SSRF/URL redirect/egress and credential separation are CHAT_09/04/03. Logs contain opaque IDs/redacted errors only, never full Goal private notes/Telegram chat ID/token. Goal deletion/privacy retention is CHAT_09/10.

## 10. Observability, cost and performance method

Metrics: heartbeat_ticks_total, heartbeat_noop_total, goal_wake_admitted_total{reason}, goal_wake_dedup_total, goal_wake_blocked_total{budget,approval,owner}, goal_active_runs, goal_budget_reserved_usd, goal_llm_calls_total, goal_consecutive_failure_total, goal_quota_defer_total, goal_due_lag_seconds, goal_stale_fence_reject_total、model cost/call and bytes. Track goalId revision, wakeReason hash, Task/Run refs, checkpointSeq, evidenceRefs, approvalRef; no raw goal content/secret in labels.

暫定Policy values [PROVISIONAL: CHAT_00/02]: min heartbeat check 5m、maxTurns20、maxModelCalls20、maxElapsed1h/continuation、maxChildTasks5、maxParallelRun1、maxConsecutiveFailures3、maxNotifications/day10、USD budget必須（値はユーザー指定またはAdmin policy、無限を許さない）。上限は仮値で実測なし。1k/10k Goals、95% idle、1% new observation、4% waitでtick throughput/p95 overhead/LLM calls/Goal/day、Run重複率、quota429 storm、restartを比較。合格目標：idleでLLM=0、副作用重複0。時間/単価のSLO数値はbenchmark後決定。

## 11. DB/Transaction/Migration提案 [PROVISIONAL: CHAT_10]

本書が必要とする論理記録: goal_continuation_policy(goalId,tenant,owner,goalRevision,budgetRef,limits,expiration)、goal_wake_ledger(tenant,goalId,goalRevision,wakeReasonKey,sourceEvidenceRefs,nextCheckUtc,admission/disposition)、goal_checkpoint_ref(目標ownerのRun/Task Checkpointへ参照)、goal_dependency_wait(approvalRef,dependencyRef,deadline)、quota_hold_until、Outbox intent。TaskGoal/Run/Checkpoint/Budget実際の正本はCHAT_01/02/10であり**別テーブルにGoalの完全コピーをしない**。

Unique(tenant,goalId,goalRevision,wakeReasonKey)と、activeRunIdからRun.leaseEpochへのCAS・共通Fencingが必要。Goal wake intent+budget reservation+outboxは同一DB tx（別quota serviceの場合はcompensating protocol要検討）。Run checkpointはTask/Run ownerのcommit境界へ委譲。Migrationでは旧session heartbeat promptを永続Goalに無断変換しない、明示ユーザーの設定再確認が必要。Rolling update中に新旧Goal runnerを同じGoalへ並列稼働させない。ROLLBACK後もunique wake/Tool ledgerを残す。

## 12. UI/API/Telegramへの影響 [PROVISIONAL: CHAT_08]

Goal状態、最後に新Evidenceがあった時刻、次check、前回のRun/予算、現在のblocking reason、必要な入力/approval、Stop/Resume/Steerを可視化。安価なno-op tickはノイズ通知しない。ユーザーにGoalが終わったと伝えるのはverified completionの後だけ。Resumeは「paused→activeの表示変更」のみでなく、正しいrevision/Ownerで有限の一回Wakeを確定する必要がある（Hermes restart回帰を参照）。

## 13. 実装PR分割

GOL-PR1: deterministic GoalWake gate + policy/limits validation + fake clock and no-op test。GOL-PR2: GoalWake ledger/Outbox/Quota hold、DB lease/fence、restart。GOL-PR3: CHAT_02 Task/ModelRouter/Verifier接続とsteer/approval/cancel。GOL-PR4: ExternalEvent/Watch bridge、notification/UI/Telegram、Security。GOL-PR5: fault injection/bench/rollout/canary。ユーザーの明示した上限と自動調査scopeを越えない。

## 14. Tests（**全件設計のみ・未実施**）

| ID | Setup/action | Expected |
|---|---|---|
| GOL-T01 | 1000 idle ticks、Evidence/Dependency不変 | Model calls 0、new Task0 |
| GOL-T02 | Goal保存、DB+Worker再起動、nextDue到達 | same ID/revision+checkpoint、正しい継続 |
| GOL-T03 | timer repeated without meaningful delta | no new model calls |
| GOL-T04 | maxTurns/Model/USD/wallTime/child cap exceed | hard stop、追加Tool/Model 0 |
| GOL-T05 | quota/total USD exhausted | no planner call、defer/paused reason |
| GOL-T06 | steer/cancel/revoke during Tool prepare | old goalRevision /old Run epoch新Tool0 |
| GOL-T07 | 4 scheduler same wake key, Queue duplicate | one continuation intent/Task |
| GOL-T08 | active Run while 5 events arrive | activeRun1、coalesced reason記録 |
| GOL-T09 | approval pending/user input pending for 100 ticks | model calls 0、approval一意 |
| GOL-T10 | malformed model verdict x3/network failures x3 | finite pausing/needs_review |
| GOL-T11 | expired evidence/changed SKU/source unreliability | old contentでconfirmed completion0 |
| GOL-T12 | model policy selected through Router | Heartbeat stored token0、router caps尊重 |
| GOL-T13 | terminal Goal with timer+external events | new Task0 |
| GOL-T14 | Retry-After=1hr, check every1min | provider calls 0 until hold expires |
| GOL-T15 | unapproved booking/purchase request from page | approval required; irreversible Tool0 |
| GOL-T16 | crash after Tool sent, outcome UNKNOWN | no automatic same side-effect retry |
| GOL-T17 | X event duplicate + Opportunity Radar wake | same budget/auth/unique key、Model cap順守 |
| GOL-T18 | resume after auto-paused budget and valid user approval | exactly one continuation wake queued |
| GOL-T19 | Multi-tenant/Goal with no conversation | correct owner scope、fake Conversation作成0 |
| GOL-T20 | benchmark 10k idle goals + 1% active | measured model=0 on idle, latency+USD logged |

## 15. MUST REQ→TEST→SC traceability

| MUST | TEST | SC | 判定 |
|---|---|---|---|
| 001 | T01 | 09 | idle model0 |
| 002 | T02 | 05,07 | restart resume |
| 003 | T03 | 05,09 | meaningful change only |
| 004 | T04 | 07,09 | caps enforced |
| 005 | T05 | 07,09 | budget no call |
| 006 | T06 | 07,08 | old revision/epoch Tool0 |
| 007 | T07 | 07 | 1 continuation |
| 008 | T08 | 07,09 | activeRun1 |
| 009 | T09 | 05,07 | waiting no loop |
| 010 | T10 | 07,09 | failure finite |
| 011 | T11 | 05,10,14 | stale not final |
| 012 | T12 | 09 | Model Router owns selection |
| 013 | T13 | 05 | terminal inert |
| 014 | T14 | 07,09 | 429 hold |
| 015 | T15 | 07,08 | approval boundary |
| 016 | T16 | 07 | uncertain reported |
| 017 | T17,T19 | 10,11 | event/Watch uniform admission |

## 16. Unresolved & Cross-team proposals

| ID | Owner | Severity/必要な合意 |
|---|---|---|
| GOL-P01 | CHAT_01/02/10 | P0 GoalId/goalRevision/Task/Run/Checkpoint/activeRunId/leaseEpochの保存正本。UUIDv7とrecordVersionも統一、T02/T06/T07 |
| GOL-P02 | CHAT_02/03 | P0 budget reservation/cancel/Steer/Tool Intent UNKNOWN/Model RouterとRun共通Gate、T04/T06/T16 |
| GOL-P03 | CHAT_09/10 | P0 tenantId/ownerUserId vs workspace_id mapping、Revoke/IdentityVersionとlong-running Goal、T19 |
| GOL-P04 | CHAT_07 07-01/03/04 | P0 Schedule wake uniquenessとObservation provenance + notification suppression |
| GOL-P05 | CHAT_12/13/14 | P1 Opportunity Radar/ExternalEvent/FeedbackでwakeKeyとBudgetを共用、intelligenceがGoal制約を無断改訂しない |
| GOL-P06 | CHAT_08/11 | P1 Goal resume after budget auto-pauseのtransportとfixture、reason/usage/nextDue projection |
| GOL-P07 | CHAT_00 | P1 初期caps、idle cadence、Goal auto-pause基準、再計画条件をADR |

P0が未決のためDraft。目標が継続できるという宣言は実運用のテスト合格後に限る。

## 17. Sources / pinned evidence (2026-10-30)

- [S1] **Code verified**, Hermes Agent fcc26fbd9f1641755e49793d7ed608f3b7214c3d, MIT: HeartbeatManager.due_prompt/abandon_fire/pause/resume https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/hermes_cli/heartbeat.py
- [S2] **Code verified (test inspected, not run)** Hermes heartbeat tests https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/hermes_cli/test_heartbeat.py ; MIT https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/LICENSE
- [S3] **Code verified**, Hermes goals state/parser/limits and GatewayGoalsMixin https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/hermes_cli/goals.py ; https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/gateway/run_goals.py
- [S4] **Code verified (test inspected, not run)** Goal tests, resume-after-budget pause https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/hermes_cli/test_goals.py ; https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/gateway/test_goal_resume_restart.py
- [S5] **Code verified**, OpenClaw main 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT, createDefaultProactiveJob/resolveDefaultProactiveCadenceMs https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/default-proactive-job.ts
- [S6] **Code verified**, OpenClaw errorBackoffMs/restart retry https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/jobs-scheduling.ts ; restart catchup test https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service.restart-catchup.test.ts
- [S7] **Code verified**, Hermes quota_hold.py plan_hold / _recovery_worthwhile https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/cron/quota_hold.py ; corresponding unit tests **not independently audited**.
- [S8] **Hypothesis**: deterministic GoalWake/unique continuation ledger and limits are independent proposals; real implementation/benchmark absent.
- [S9] **Unverified**: OSS provider quota parsing, actual ModelRouter/Goal serialization compatibility and recovery under 10k goals are not tested in harness.

### Review checklist

- [x] Scope, owner, real OSS code/test/license, rejected alternatives, types/state/sequence/pseudocode, race/restart/quotas/security, 17 MUST traceability/SC.
- [ ] P0 cross team contract agreement, implementation tests and performance results, independent review. Remain Draft.


### Round 2 adversarial review: parked goals and stale active owners

- **[S10] PR report**, Hermes #134751（調査時Open/Draftかどうかは本仕様の実装証明としない）: Goalがwait barrier完了後、外部イベントが来ずidleのままなら継続を再開しないという問題と、persisted stateをpollして**1 wake per session**を再度検査する解決方針。https://github.com/NousResearch/hermes-agent/pull/134751
- **[S11] Issue report**, Hermes #71175（旧v0.19.0環境）: budget exhaustionがclaimを解放する一方で実Workerがなお生存し、次のDispatcherが二人目を開始する競合。現在headの未修正バグを断定しない。https://github.com/NousResearch/hermes-agent/issues/71175
- **[S12] Issue report**, Hermes #98299（旧API server）：slash commandのGoalManagerとstructured API run経路が異なる可能性。Goal ingressをUI/Telegram/APIで統一するための検査材料。https://github.com/NousResearch/hermes-agent/issues/98299

**追加要件 GOL-021 MUST**: WAIT_DEPENDENCYが時間満了または許可済みdependency resolvedとなった場合、外部user turnを待たず**決定的な1回のwake admission**を行う。旧rev・owner失効・まだbusyのRunはその場で拒否する。**GOL-T21 → SC-05/07**: 無入力のGoalをpark→deadline通過→4worker tick→1Wake、LLM起動はpolicy gate合格後だけ。

**追加要件 GOL-022 MUST**: Budget枯渇・iteration上限などによってRun claimが解除されても、旧workerが持つTool実行権限はRun leaseEpoch＋goalRevision再検証で確実に無効化し、後続の新Workerと同時に副作用を実行しない。**GOL-T22 → SC-07/09**: 旧workerをbarrierで保持しRun fence切替、後継runを起動して旧workerを解放→旧worker新Tool0・古いsettlement0、UNKNOWNはTool ledgerへ。テスト未実施。
