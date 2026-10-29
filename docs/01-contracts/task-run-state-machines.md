# Task / Run State Machines — Detailed Implementation Specification

> Owner CHAT_01 | Canonical docs/01-contracts/task-run-state-machines.md | Draft / PROVISIONAL, revision 0.1, 2026-10-30 JST | Main b45d4e989998663a00ef09673e69ae1f4191f00d | Depends on PR #2 core-domain, event-envelope 01-02, CHAT_02/03/07/08/09/10.

## 0. 採用案

**Taskはユーザー目標全体、Runは1回の試行**。TaskとRunの状態を単一enumで使い回さない。全状態変更はserver認可、期待recordVersion、現在のRun.leaseEpoch、GoalRevision、認可/予算条件をtransactionで照合し、Event+Outboxを原子的に確定。Taskの永続状態が唯一の正本であり、LLMの自然言語「完了」やWorker heartbeatだけで成功へ移れない。処理の中断・承認待ち・効果不明・明示Cancelを表す状態を持つ。**外部ToolがUNKNOWNの場合は自動再実行しない**。

## 1. 責務・境界・シナリオ

責務: TaskState/RunStateの列挙、全許可遷移、Guard、terminal、Pause/Resume/Steer/Cancel/Retry/Approval/Recover、同時更新防止。非責務: Agent計画/Checkpoint内容（CHAT_02）、Tool side-effect ledger（CHAT_03）、Watch status（CHAT_07）、認可policy（CHAT_09）、DB DDL（CHAT_10）、UI/API（CHAT_08）。Task/RunのID・VersionはPR #2、EventKindは01-02を参照し重複して定義しない。SC-01..09、とくに02,04,06,07,08,09、追加SC-10..16の発火Taskにも同じ状態を適用。

## 2. OSS源コード比較（閲覧のみ、未実行）

| OSS commit/license | 確認したコード/テスト | 示唆と不採用 |
|---|---|---|
| LangGraph ffc3336367e8139668d11e5f0b0e6def57e7c762 MIT | libs/langgraph/langgraph/pregel/_loop.py PregelLoop.tick/put_writes/_put_checkpoint; libs/checkpoint/langgraph/checkpoint/base/__init__.py CheckpointTuple; libs/checkpoint/tests/test_memory.py のrecovery/ancestor tests [S1] | checkpoint/resumeは必要、しかし汎用Web Task/Watch/User authorityのstate enumはSDK外に置く |
| OpenAI Agents SDK Python cd26bbbc52847845fde73b9952b51e62e2bcbdc0 MIT | src/agents/run_state.py RunState approve/reject/to_json/from_json; tests/test_run_state.py のschema version/approval/restoration; tests/memory/test_session.py concurrent/persistence [S2] | SDKが独立復元RunStateの同時resumeを戒める。DB fenced ownerが必要。SDK interruptionをアプリの完了stateと同一視しない |
| PydanticAI c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9 MIT | pydantic_ai_slim/pydantic_ai/conversation.py Conversation.deferred_tool_requests、tests/test_conversation.py test_a_paused_conversation_carries_what_it_is_waiting_on [S3] | 承認待ちの実体はmessage historyと分離保存。Client replayだけで承認しない |

**Alternative A**: ひとつの万能status＋free-form string reason → guardを網羅できず却下。**B**: provider responseだけでstateを推定 → crashed/replayed副作用に不正、却下。**C**: Task/Run 2 state machines + immutable event + DB CAS/fencing →採用。**D**: すべてをevent sourcing replayのみで決定 →初期運用難のため不採用。性能/安全保証は未実測。

## 3. Canonical authority・依存

Task Commandを受理するのは認証済みUser、Watch/Eventの承認済みservice、またはOperator。Run Claim/Checkpoint/Finishはtrusted Workerのみ。Browser takeoverはCHAT_04の手動操作サービスが受け取り、stateはTask Command serviceに依頼。Tool GatewayはactiveRunId/epoch/GoalRevision/budget/cancel freshnessを副作用のdispatch直前に再確認。CredentialをRun stateに含めない。[PROVISIONAL: CHAT_09/03]。

## 4. Requirements

| Req | Level | Condition | SC |
|---|---|---|---|
| SM-001 | MUST | Task/Run状態が本仕様のenumとguardだけで変化 | 07 |
| SM-002 | MUST | terminalは通常のworker再開やSteerで非terminalへ戻せない | 07 |
| SM-003 | MUST | 同一TaskでactiveRunは最大1、attemptNoが一意 | 07 |
| SM-004 | MUST | leaseEpoch+recordVersion CASで旧Worker write/effect拒否 | 07 |
| SM-005 | MUST | user Pause/Resume/Cancelがsafe boundaryと一致 | 07 |
| SM-006 | MUST | approval待機はserver-stored intentとactor/args/revisionを固定 | 04,06,07 |
| SM-007 | MUST | Run checkpoint recoveryとterminal Retryを別操作にする | 07 |
| SM-008 | MUST | 外部副作用UNKNOWNは専用待機状態で盲目的再試行0 | 07 |
| SM-009 | MUST | budget/deadlineは有限で状態表示され、延長は明示承認 | 09 |
| SM-010 | MUST | Steer後に旧GoalRevisionで新規Tool開始不可 | 04,07 |
| SM-011 | MUST | Task成功はVerifierと必要Evidence/result refs確認後 | 01,04,05 |
| SM-012 | MUST | Task/Run state+Event+outbox同一transaction commit | 02,07 |
| SM-013 | MUST | Worker crash/restart後、現行TaskとRunの復帰可否を確定可能 | 02,07 |
| SM-014 | MUST | 親子Taskのterminal/Cancel伝播は明示policy、勝手に全子を停止しない | 07 |
| SM-015 | MUST | 権限失効後、未開始の新規Tool dispatch拒否 | 08 |
| SM-016 | MUST | error/partial/uncertain resultを成功に偽装しない | 01,03,07 |
| SM-017 | SHOULD | Pauseとapprovalを同時入力しても単一の正規待機理由に収束 | 07 |
| SM-018 | SHOULD | stale policy/unknown state schemaを隔離し運用監査 | 07 |
| SM-019 | LATER | user-visible Retry from historical checkpoint with explicit fork | 07 |

## 5. Canonical schemas and guards

~~~ts
type TaskState =
  | "QUEUED" | "RUNNING" | "WAITING_INPUT" | "PAUSED"
  | "WAITING_RECONCILIATION" | "CANCELLING"
  | "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED"
  | "EXPIRED" | "BUDGET_EXHAUSTED";
type RunState =
  | "CREATED" | "CLAIMED" | "RUNNING"
  | "WAITING_APPROVAL" | "WAITING_INPUT" | "PAUSED"
  | "WAITING_RECONCILIATION" | "CANCELLING"
  | "SUCCEEDED" | "PARTIAL" | "FAILED" | "CANCELLED"
  | "EXPIRED" | "BUDGET_EXHAUSTED";
type ControlKind = "PAUSE"|"RESUME"|"CANCEL"|"STEER"|"APPROVE"|"REJECT"|"RETRY";
interface TransitionCommandV1 {
  schemaVersion:"1.0"; commandId:string; actor:ActorRef;
  taskId:TaskId; runId:RunId|null; command:ControlKind;
  expectedTaskVersion:string; expectedRunVersion:string|null;
  expectedGoalRevision:string; leaseEpoch:string|null;
  payloadRef:string|null; requestedAt:UtcInstant;
}
interface TerminalProof {
  verifierResultRef:string|null; evidenceRefs:string[];
  budgetReceiptRef:string|null; unresolvedEffectRefs:string[];
  finalResultKind:"complete"|"partial"|"uncertain"|"unavailable";
}
~~~

**Enum ownership:** 上記TaskState/RunStateは唯一の共有正本案。RunState class in OpenAI SDKとは別。Watch独自状態はCHAT_07。SchemaVersion unknown majorはreject。Version/goal/epochはcore-domainに定めたlossless decimal string、DB BIGINT、内部BigInt。Task.activeRunIdとRun.taskId一致のDB条件。Clientから期待versionとstateを送っても更新はDB loaded currentのguardだけで決定。

**Terminal**: Task SUCCEEDED/PARTIAL/FAILED/CANCELLED/EXPIRED/BUDGET_EXHAUSTED；Run SUCCEEDED/PARTIAL/FAILED/CANCELLED/EXPIRED/BUDGET_EXHAUSTED。terminalから復帰は不可。明示的な「Retry」は**新Taskではなく同Taskの新Run**を発行するため、terminal Taskを直接再開しない設計と矛盾する。**採用:** Taskはterminal後の再試行を受けない。ユーザーが同じ目標を再試行したい場合、新Task（retryOfTaskId lineage）を作る。**同Task内のRun retry**はTaskが非terminalの時のみ実施。Task終了前にRunがFAILEDならTaskをQUEUEDへ戻して新attemptを試せる。ユーザーへのRetry UIは失敗Taskから新Taskをcreate。[PROVISIONAL: CHAT_02/08; retryOfTaskIdはcore-domain新フィールド提案]。

**Run terminal済みでTask非terminal**: Run.FAILEDまたはEXPIRED等→Task.QUEUED（安全に次試行可能な場合）。Run.SUCCEEDED/PARTIALの後はVerifierに従いTask terminalになるか、追加情報要求でTask WAITING_INPUTとなる。ただしterminal Runは決して書き換えない。

## 6. 完全遷移表と守るべき順序

Task許可遷移:

| From | To | Trigger/Guard |
|---|---|---|
| creation | QUEUED | authorized durable command committed |
| QUEUED | RUNNING | Run claim, budget/rights/goal valid |
| QUEUED | PAUSED, WAITING_INPUT, CANCELLING, EXPIRED, BUDGET_EXHAUSTED | user control, missing required data, Cancel, deadline/cost |
| RUNNING | WAITING_INPUT | Run suspended on user/human takeover/approval |
| RUNNING | PAUSED | safe checkpoint, no newly dispatching tool |
| RUNNING | WAITING_RECONCILIATION | uncertain external side effect |
| RUNNING | QUEUED | last Run terminal failure/retry policy authorizes new attempt |
| RUNNING | CANCELLING | Cancel admitted, new tool dispatch forbidden |
| RUNNING | SUCCEEDED, PARTIAL, FAILED, EXPIRED, BUDGET_EXHAUSTED | verified terminal proof or finite limits |
| WAITING_INPUT | RUNNING, PAUSED, CANCELLING, EXPIRED | validated response + authorization / controls |
| PAUSED | QUEUED, RUNNING, CANCELLING, EXPIRED | Resume with current user/tenant/budget; chosen whether Run remains resumable |
| WAITING_RECONCILIATION | RUNNING, PARTIAL, FAILED, CANCELLING | ledger external receipt reconciliation or human accepted uncertain partial |
| CANCELLING | CANCELLED, WAITING_RECONCILIATION | all new effects blocked; outstanding effects confirmed closed or unknown |
| any nonterminal | FAILED | nonrecoverable internal error, with retained evidence/error |
| any nonterminal | EXPIRED, BUDGET_EXHAUSTED | deadline/spend limit at safe boundary; not when active unknown effect |
| terminal | no transition | historical retry creates new Task |

Run許可遷移:

| From | To | Trigger |
|---|---|---|
| creation | CREATED | Task owns run attempt number |
| CREATED | CLAIMED | atomic lease claim, epoch++ |
| CLAIMED | RUNNING | checkpoint loaded, active ownership checked |
| RUNNING | WAITING_APPROVAL, WAITING_INPUT, PAUSED | durable pause/human input/approval |
| RUNNING | WAITING_RECONCILIATION | ledger outcome unknown, no safe auto-retry |
| RUNNING | CANCELLING | cancel guard committed |
| RUNNING | SUCCEEDED, PARTIAL, FAILED, EXPIRED, BUDGET_EXHAUSTED | verifier/error/limits, proof committed |
| CLAIMED | FAILED, EXPIRED | admission or initialization failure |
| WAITING_APPROVAL | RUNNING, WAITING_INPUT, PAUSED, CANCELLING, EXPIRED | approved/rejected/expired/paused with frozen hash & goal |
| WAITING_INPUT | RUNNING, PAUSED, CANCELLING, EXPIRED | authorized input |
| PAUSED | CLAIMED, CANCELLING, EXPIRED | resumed reclaim fenced; epoch++ |
| WAITING_RECONCILIATION | RUNNING, PARTIAL, FAILED, CANCELLING | external receipt resolves and policy allows |
| CANCELLING | CANCELLED, WAITING_RECONCILIATION | no unknown in-flight action, else reconciliation |
| terminal | no transition | new Run only when Task remains nonterminal |

**Lease expiry is not an automatic terminal transition.** Worker crash can leave Run.RUNNING with expired lease. Reclaimer verifies owner/ledger/Task activeRun and increases epoch under DB CAS; same RunId/attemptNo remains. Task.RUNNING persists unless policy requires explicit PAUSED/reconciliation. Keep heartbeat and lease separate.

**WAITING_APPROVAL** exists only on Run; Task maps it to WAITING_INPUT. **WAITING_RECONCILIATION** means external outcome cannot be safely decided, distinct from ordinary FAILED and lost Tool result. Finished Task cannot hide unresolved effect: terminal proof must specify nonempty unresolvedEffectRefs and a truthful uncertainty result, or remain WAITING_RECONCILIATION. For Cancel with UNKNOWN, remain WAITING_RECONCILIATION (with cancelRequested=true) until settled; no automatic new execution after a user cancel.

**Priority for overlapping controls**: authority revoke > explicit Cancel > deadline/budget exhaustion > Pause > approval > Resume > Steer. This is a proposed deterministic tie rule, but irreversible external in-flight effect status always retained. State and intents are in DB; no user operation can erase a confirmed side effect. [PROVISIONAL: CHAT_02/09].

## 7. Implementation pseudocode

~~~ts
async function transition(ctx, cmd) {
  return db.transaction(async tx => {
    const t = await tx.lockTaskScoped(ctx.tenantId,cmd.taskId);
    requireOwnerAuthority(ctx,t,cmd.command);  // CHAT_09
    requireVersion(t.recordVersion,cmd.expectedTaskVersion);
    const r = cmd.runId ? await tx.lockRunScoped(t,cmd.runId) : null;
    if (r) {
      requireVersion(r.recordVersion,cmd.expectedRunVersion);
      assertSameTaskAndCurrentActiveRun(t,r);
      if (isWorkerAction(cmd)) requireEpoch(r.leaseEpoch,cmd.leaseEpoch);
    }
    const policy = await tx.readCurrentBudgetApprovalAndOwner(t,r);
    const effect = await tx.readSideEffectLedgerForRun(r);
    const decision = transitionTable(t,r,cmd,policy,effect);
    if (!decision.allowed) throw stateConflict();
    if (cmd.command === "STEER") {
      await tx.insertImmutableGoalRevision(t,cmd.payloadRef);
      await tx.invalidateOldGoalApprovalIntents(t,decision.nextGoalRevision);
    }
    await tx.applyChangesUsingCAS(decision);
    await tx.appendEventAndOutboxSameCommit(decision); // 01-02
    return decision.ack;  // committed command != applied effect
  });
}
async function safeResume(worker, runId, oldEpoch) {
  const claim = await db.claimSameRunAndIncrementEpoch(runId,oldEpoch);
  const snapshot = await readCheckpoint(claim.runId);
  const ledger = await getToolIntentAndEffectStates(claim.runId);
  if (ledger.anyIndeterminate) return transitionToReconciliation(claim);
  await enforceUserOwnerGoalBudgetAndExpiry(claim);
  return runAgentLoopAtSafeBoundary(claim,snapshot);
}
~~~

*External Effect starts* require a separate Tool Gateway single linearization point checking current Task controlVersion/GoalRevision, Run leaseEpoch, validated approval/authorization/budget and intended effect key **immediately before dispatch**. DB fence alone cannot undo a remote HTTP request already in flight. Event writer and tool ledger must not publish a success solely from a stale worker.

## 8. Race/fault matrix

| Fault | Detection | Outcome |
|---|---|---|
| double Run claim | activeRunId+leaseEpoch CAS | 1 owner, other conflict |
| old Worker late output | stale epoch/version | 0 rows and no new side effect |
| crash after Tool effect before result commit | persisted intent UNKNOWN | WAITING_RECONCILIATION, no blind retry |
| Cancel during dispatch | tool claim linearization | if pre-cancel committed, stop; if already in-flight, retain unknown/receipt |
| Pause during LLM generation | Abort model at next safe boundary | committed checkpoint unchanged until safe pause |
| Steer during approval | goalRevision mismatch | approval invalid/replan |
| rejected approval | server bound approval record | no effect, Run resumes alternate plan or partial |
| expiry during human handoff | DB-time deadline | stop new Tool, retain state and notify |
| budget exhausted | deterministic accounting and reservations | BUDGET_EXHAUSTED if no unresolved effects |
| Worker crash with lease still valid | lease TTL expiry + recoverable checkpoint | same Run re-claim, epoch++ |
| user disable | authz identityVersion/fresh check | no new dispatch, honest termination |
| task terminal duplicate finish | status/version CAS | 1 terminal event, receipt replay |
| parent cancel with child in-flight | propagated explicit policy + child lease | no new child work, uncertain child preserved |
| event outbox failure | DB transaction/relay | state never commits without event intent |
| context schema old | versioned loader | no speculative resume, needs repair |
| UI offline | committed stream replay | state read from DB, not UI text |

## 9. Security/limits

Read-only automation still needs tenant+owner and network egress restriction; models/web content cannot invoke transition command or generate permission. No host Shell/SSH/Docker socket, no unknown outbound connector, no secrets or cookie in state/event. User-cancel does not authorize compensation or irreversible action. Bounded max attempts per Task, max Task age, max pending approval TTL, max child Task count and model/tool budget: finite policy [PROVISIONAL: CHAT_02/09]. Enforce operator audit and cross-tenant leak test.

## 10. Metrics/trace

Store transitionAttemptId/recordVersion/leaseEpoch/goalRevision/taskId/runId/actor/causation event (never sensitive raw content). Count state_transition_denied_total, stale_run_fence_total, unknown_effect_wait_total, cancel_to_stopped_seconds, approval_wait_seconds, budget_stop_total. Benchmark 1/2/16 competing workers, kill at each commit boundary, delayed Tool ACK, multiple parallel commands; correctness first (0 unauthorized effects), measure p50/p95/p99; no measured performance figures here.

## 11. Physical persistence [PROVISIONAL: CHAT_10]

DB tables: tasks state_code, current_goal_revision, active_run_id, control_version/record_version, owner; runs task_id/attempt_no/state_code/input_goal_revision/lease_epoch/lease_owner/lease_expires/record_version, immutable checkpoint and effect ledger refs. Use composite Tenant→Workspace owner linkage and FKs. Terminal proof persisted in resource/result refs. State+Event+Outbox commit. DB revision defaults currently conflict (#10 revision 0) and must migrate to initial 1 via explicit compatibility policy. Run lease epoch decimal string maps DB bigint; do not use ToolAttempt fence as substitute.

## 12. REST/UI/Telegram [PROVISIONAL: CHAT_08]

CreateTask returns QUEUED and committed TaskId, not instant success. Commands are asynchronous: returned acceptance != terminal. Pause/Resume/Cancel use If-Match and idempotency key; 409/412 on stale version, 403/404 on denied identity, pending actions show reason and next allowable action. Watch and Event trigger Task may have null conversationId. UI shows WAITING_INPUT, WAITING_RECONCILIATION and partial evidence explicitly; connected channel receives durable Event after commit. Retry after terminal creates a new Task lineage with explicit approval of previous constraints; never mutate old terminal Task to QUEUED.

## 13. Implementation PR slicing

SM-PR1 canonical enums+pure transition reducer and tests; SM-PR2 DB CAS/FK/lease/GoalRevision and atomic outbox; SM-PR3 Gateway approval/uncertain effect integration and safe boundary; SM-PR4 UI/Telegram and fault harness. Deployment of new state codes needs additive migration + dual readers; incompatible major fail closed.

## 14. Tests — designed only, none executed

| TEST | Setup / action | Expected |
|---|---|---|
| SM-T01 | table-driven exhaustive from/to pair | only listed pairs accepted |
| SM-T02 | terminal Run+Task, Retry/Steer/Resume | old terminal immutable; Retry new Task |
| SM-T03 | 16 workers claim same Task | active Run owner 1 |
| SM-T04 | stale epoch after lease expire/reclaim, commit + tool dispatch | 0 stale commits/effects |
| SM-T05 | pause during tool and model calls | no new dispatch post-safepoint |
| SM-T06 | approval client tamper/cross-tenant/replay/revoke | all denied, effects 0 |
| SM-T07 | crash during DB ACK lost | same RunId+increased epoch, no duplicate irreversible tool |
| SM-T08 | tool intent committed, remote effect ACK lost | WAITING_RECONCILIATION, no blind retry |
| SM-T09 | deadline/budget crossing during run | explicit limit state or unresolved wait |
| SM-T10 | Steer after approval before dispatch | old approval invalid, effect 0 |
| SM-T11 | verifier rejects stale hotel occupancy/evidence | SUCCEEDED forbidden |
| SM-T12 | kill after state CAS but before Event/outbox | rollback all |
| SM-T13 | expired lease over reboot | reclaim same RunId, epoch+1 |
| SM-T14 | parent child cancellation | lineage preserved, effect uncertainty preserved |
| SM-T15 | revoked user/tenant mid task | new external effects 0 |
| SM-T16 | 3 evidence missing -> final result | PARTIAL or explicit WAITING_INPUT, not invented SUCCEEDED |
| SM-T17 | simultaneous Pause, Cancel, Resume, Steer | priority deterministic, lost update 0 |
| SM-T18 | schema unknown major migration | quarantine and safe failure |
| SM-T19 | 1000 tasks/4 workers fixture | bounded attempt count, transition throughput measured |

## 15. Traceability REQ→TEST→SC

| REQ | TEST | SC |
|---|---|---|
| SM-001 | T01,T17 | 07 |
| SM-002 | T02 | 07 |
| SM-003 | T03 | 07 |
| SM-004 | T04,T13 | 07 |
| SM-005 | T05,T17 | 07 |
| SM-006 | T06,T10 | 04,06 |
| SM-007 | T02,T07,T13 | 07 |
| SM-008 | T08 | 07 |
| SM-009 | T09,T19 | 09 |
| SM-010 | T10,T17 | 04,07 |
| SM-011 | T11,T16 | 01,04,05 |
| SM-012 | T12 | 02,07 |
| SM-013 | T07,T13,T18 | 02,07 |
| SM-014 | T14 | 07 |
| SM-015 | T15 | 08 |
| SM-016 | T08,T11,T16 | 01,07 |
| SM-017 | T17 | 07 |
| SM-018 | T18 | 07 |

## 16. Owner-dependent open issues

- SM-P01 P0 CHAT_02: Retry Task policy / worker safe boundary / claim+pause interaction, acceptable loss of interim model delta. Need explicit acceptance in AgentLoop PR #1.
- SM-P02 P0 CHAT_03: effect ledger UNKNOWN and allowed reconciliation/terminal proof; approval constraints/old-goal invalidation.
- SM-P03 P0 CHAT_10: CAS+Run lease columns/activeRunId/immutable GoalRevision/FK, atomic event/outbox, migration from revision=0.
- SM-P04 P0 CHAT_09: owner/tenant mapping, service capability, revoked user and sensitive Tool gate.
- SM-P05 P1 CHAT_08: terminal task retry creates new task, nullable Conversation, conflict errors.
- SM-P06 P1 CHAT_07/13: schedule external triggers may not bypass normal Task/Run transitions.
- SM-P07 RESOLVED DESIGN (still Draft): CHAT_01 core-domain now declares retryOfTaskId for new Task created from explicitly retried terminal Task. CHAT_02/08/10 implement corresponding new-Task branch; existing terminal Task immutable. Owner acceptance and executable contract tests remain pending.
Proposed state names require codegen/migration once approved. Keep Draft until agreements; avoid dual canonical state enums.

## 17. References 2026-10-30

[S1] Code verified: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py and https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/langgraph/checkpoint/base/__init__.py and https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/tests/test_memory.py; MIT LICENSE.  
[S2] Code verified: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_state.py and https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_run_state.py and https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/memory/test_session.py; MIT LICENSE.  
[S3] Code verified: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/conversation.py and https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_conversation.py; MIT LICENSE.  
[S4] Issue report: https://github.com/langchain-ai/langgraph/issues/8115.  
[S5] Hypothesis: domain Task/Run state enums, authority tie priority, terminal retry Task lineage, limits. No runtime execution, source tests not run.

**Review gate:** P0 unresolved; Draft only.
