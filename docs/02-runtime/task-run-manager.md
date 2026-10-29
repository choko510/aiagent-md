# Task and Run Manager — Detailed Implementation Specification

> Owner: CHAT_02 | Status: Draft [PROVISIONAL: CHAT_01/03/09/10] | Rev 0.1 | Research date: 2026-10-30 | base main: b45d4e989998663a00ef09673e69ae1f4191f00d

## 0. 結論と採用案
DBのTask/RunとCurrent leaseEpochを唯一の正本とし、Queueをat-least-once通知と扱う。1 Task active Runは最大1。クラッシュは同Runを新epochで回復、terminal後の再試行だけ新runId/attemptNo。Cancel/SteerとGateway dispatchをDB上で線形化する。

## 1. 目的、責務と非責務、受入シナリオ
Idempotent Task受付、Run生成、atomic claim/heartbeat/expiry、親子Task admission、cancel/steer/pauseのcommand受付、finished result commit。 Tool Executorそのものは持たない。
非責務: 共有Domain/State/EventとUUIDはCHAT_01、Tool Policy/Approval/Side-effectはCHAT_03、BrowserSessionはCHAT_04、認可はCHAT_09、物理DDL/Queue/OutboxはCHAT_10、E2EはCHAT_11。TaskとConversationの寿命は独立し、Watch/X投稿起点のTaskはconversationId=nullを許可する。

## 2. 既存OSSの実装・テスト比較
| Upstream | Source + test | Limit |
|---|---|---|
| Mastra main ec3ba11 | durable/execution-fence.ts::readRunGeneration/getExecutionClaim, run-lease-fence.ts::verifyRunLease; durable/__tests__/durable-agent-execution-fence.test.ts | Run-domain auth not supplied |
| pg-boss main 71ea068b, package 12.38.0 MIT | src/plans.ts::create, test/attemptFenceTest.ts (stale complete/fail/heartbeat rejected) | queue retryCount is not application leaseEpoch |
| LangGraph main ffc3336 | pregel/_runner.py::PregelRunner.tick, tests/test_pregel.py::test_pending_writes_resume | checkpoint not global DB CAS |
**採用案**: Domain-owned TaskRunManager and queue adapter. **却下案**: queue job id/retry counter as Run identity; in-memory lock as distributed lease。上流コード・テストは実際にGitHub上で読んだが、テストを実行したわけではない。ここで提示するPortは新規設計であり上流既存APIではない。

## 3. 依存グラフと所有権
~~~text
REST/Telegram/Watch/Event -> Task/Run Manager -> Runtime Host -> AgentLoop
                      |             |                |
                DB CAS/Outbox    scoped DI     Model Adapter
                      |             |                |
                  CHAT_10       Auth(CHAT_09)  Tool Gateway(CHAT_03)
                                                -> Browser(CHAT_04)
~~~
Runtimeが署名済みのidentity/ownerとcurrent leaseEpochをDBで検証し、Tool実行前にGatewayが同じ正本へ再照会。Queue message内のtenant/ownerは権限に使用しない。正本の強制順序はAuth→Task/Run→fence→Budget→Tool Gateway。LLMからの実行指示では変わらない。

## 4. Requirements
| REQ-ID | Level | 行動/合格条件 | SC |
|---|---|---|---|
| TRM-001 | MUST | CreateTask command receipt+event+outboxとTaskがatomic commitされる | SC-02/07/08/09 |
| TRM-002 | MUST | 同Task activeRunは最大1、terminal再試行は新Run、crash復帰は同Run | SC-02/07/08/09 |
| TRM-003 | MUST | claimはDB clock下leaseEpoch+1をCASで発行し旧epoch拒否 | SC-02/07/08/09 |
| TRM-004 | MUST | heartbeatはowner/epoch/expiryの一致以外では更新不可 | SC-02/07/08/09 |
| TRM-005 | MUST | Cancel/Steer/PauseとGateway dispatchは順序付け、Cancel後新効果ゼロ | SC-02/07/08/09 |
| TRM-006 | MUST | Steer後の旧goalRevision ToolとApprovalを無効化 | SC-02/07/08/09 |
| TRM-007 | MUST | 子Taskはparentのtenant/owner/予算/権限を超過しない | SC-02/07/08/09 |
| TRM-008 | MUST | Queue二重配達/ack lossで二重Run開始しない | SC-02/07/08/09 |
| TRM-009 | MUST | deadline/cost/steps/concurrencyを有限上限でgateする | SC-02/07/08/09 |
| TRM-010 | MUST | Watch/Event TaskはconversationId=nullを許可し架空Conversationを作らない | SC-02/07/08/09 |
| TRM-011 | MUST | finishはrun+task current epoch CASとresult/event/outboxをatomic commit | SC-02/07/08/09 |
| TRM-012 | MUST | In-flight side effectがUNKNOWNなら失効leaseを理由に再送しない | SC-02/07/08/09 |
| TRM-901 | SHOULD | レイテンシーと資源コストを同条件Fixtureで測り、数値上限を調整可能 | SC-09 |
| TRM-902 | LATER | 安全な複数replica運用を測定後に拡張 | SC-07 |

## 5. I/O契約・型・Validation
~~~ts
interface RunClaim {
 tenantId:string;ownerUserId:string;taskId:string;runId:string;
 attemptNo:number;inputGoalRevision:number;
 leaseEpoch:number;ownerWorkerId:string;leaseExpiresAt:string;
 expectedTaskRecordVersion:number;
}
interface TaskRunManager {
 createTask(actor:TrustedPrincipal, command:CreateTaskCommand):Promise<TaskReceipt>;
 claim(runId:string,workerId:string):Promise<RunClaim|null>;
 renew(c:RunClaim):Promise<string>;
 requestControl(c:VerifiedControlCommand):Promise<ControlReceipt>;
 finish(c:RunClaim,resultRef:string,expectedRecordVersion:number):Promise<void>;
}
~~~
公開IDはCHAT_01 UUIDv7 validator、Run.leaseEpochは安全なJS number (0..Number.MAX_SAFE_INTEGER) とDB bigintのlossless変換。recordVersionはCAS用（初期1提案）、goalRevisionは条件世代、Tool Attempt fenceは別トークンであり**相互代用不可**。すべて[PROVISIONAL: CHAT_01/10]。Owner/tenant/workspaceはサーバー照会で結合する。Task.activeRunIdはそのTaskのRun以外へ向けられない。

## 6. 状態遷移と正常Sequence
~~~text
CREATE_TASK(tx receipt, task goal revision1, outbox) -> QUEUED
   -> ACQUIRE (atomic activeRun/leaseEpoch) -> RUNNING
   -> WAITING_APPROVAL | PAUSED | COMPLETE | PARTIAL | UNCERTAIN
CRASH -> EXPIRED -> RECLAIM same runId, leaseEpoch+1 -> RESTORE
TERMINAL -> OPTIONAL NEXT ATTEMPT new runId
~~~
Cancel priority > security revoke > Steer > Pause > ordinary dispatch. Lease renewal is internal worker liveness; it never certifies that an external side-effect finished. Exact Task/Run state enum from CHAT_01.
同じTaskでactive Run最大1。Runを新規作成するのは前Runのterminal以降の再試行だけ、Worker crashは既存runIdを維持しleaseEpochを増やす。Cancel/Steerのcommit後は新しいTool dispatchを拒否、既発生の外部副作用はGateway ledgerから照会する。

## 7. 実装可能なアルゴリズム
~~~ts
async function claim(runId,workerId) {
 return db.tx(async tx=>{
  const pointer=await tx.readRunPointer(runId); // non-authoritative initial read
  const task=await tx.lockTask(pointer.taskId);  // global lock order: Task -> Run
  const run=await tx.lockRun(runId);
  if(run.taskId!==task.id) throw StalePointer();
  requireSameOwnerTenantAndActiveRun(run,task);
  if (!claimable(run,tx.dbNow())) return null;
  requireFiniteBudget(task);
  const e=incrementCheckedSafeEpoch(run.leaseEpoch);
  await tx.casRunLease(run.id,run.recordVersion,e,workerId,tx.dbNowPlus(ttl));
  await tx.addRunEventAndOutbox(run.id,e);
  return signedDbClaimView(run,task,e,workerId);
 });
}
async function requestCancel(auth,taskId,version) {
 return db.tx(async tx=>{
  const task=await tx.lockTask(taskId);
  await tx.requireOwner(auth,task);
  await tx.assertRecordVersion(task,version);
  await tx.advanceControlVersionAndState(task,"CANCEL_REQUESTED");
  await tx.addEventAndOutbox(task);
 });
}
~~~
transaction中にLLM/外部HTTPを呼ばず、acquire/commitを短く保つ。Epoch mismatchまたはowner変更時はcallbackやStreamを破棄し確定結果を上書きしない。

## 8. 障害・競合・Timeout matrix
| Condition | Action |
|---|---|
| Concurrent claims | one CAS success; loser no Tool |
| Old heartbeat after takeover | reject stale owner, keep newer expiry |
| Lost API ack | dedupe receipt and Task unchanged |
| Model worker killed | resume same RunId with epoch increment |
| Cancel/Tool race | dispatch claim gate observes latest controlVersion |
| Steer after approval | old revision no execution; new step replans |
| Parent Cancel/child submit race | lock parent, deny child if control changed |
| Effect success but receipt missing | ledger UNKNOWN until provider reconcile |
| Epoch beyond JS safe integer | no new claim; operator repair |
| Task invalid workspace/owner | deny even if ID exists |
- 無期限リトライ禁止。DB/lease backendが不明ならfail closed。既着手Toolの結果が不明ならUNCERTAINとし、Tool Gatewayだけがprovider receiptをreconcileする。
- Ack消失: authoritative receipt/ledgerを再読込。同じ操作のExecutorを直接再呼出ししない。
- Pause/Steer: in-flightで開始済みのread/side-effectは事実として保持し、古いGoalに対する承認は失効させる。

## 9. Security / Privacy / Trust
Root Shell、SSH、Docker socket、privileged containerをAgentへ与えない。Browser/HTTP/MCPはSSRF・DNS再解決・egress allowlistが必須。Secret、Cookie、Access TokenをModel入力、ログ、ArtifactやSSEへ含めない。Web・SNS・MCP説明文はuntrusted data。Auditにはactor/tenant/Run/goalRevision/leaseEpoch/correlationとredacted outcomeのみ。未認証・不正なOwner/劣化した認可サービスではTool開始ゼロ。

## 10. Metrics / bounded budget
Trace metrics: runId/taskId/attemptNo/leaseEpoch/stepCursor, controlVersion, queueWaitMs, claimLatencyMs, executionMs, health/drain time, pending approvals, uncertain effects, tool dispatch denials, checkpoint failures, billed/estimated USD。初期値は同時Run=2、Job buffer=16、deadline=30分（未実測の設計例）。欠測は0として扱わずunknown。テスト前に速度・成功率・費用優位を断言しない。

## 11. Persistence/transaction/migration提案
[PROVISIONAL: CHAT_10] UUIDv7 trusted server生成、DB gen_random_uuid() defaultはUUIDv7要件と矛盾するため除去案。Task.conversation_idはnullable。record_version初期1、Run.lease_epoch bigint default0とcurrent owner/expiry、Task.active_run_id、goal_revisionとimmutable revision history、outboxとCommand receiptを原子的に保存。旧schema migrationでは二重active Runと孤児Goalをquarantineし、old Workerを停止してから移行する。

## 12. REST/UI proposal
[PROVISIONAL: CHAT_08] POST commandはreceiptの202/201、GET Task/Runは状態を確定結果と区別。If-MatchはrecordVersionで428/412、idempotency fingerprintにはtenant/actor/operation/payload含む。SSEはdurable event cursorのresume、tentative model deltaは確定状態としてreplayしない。Browser handoff・approvalはredacted action summaryのみ。

## 13. 実装PR分割
TRM1 task idempotency/receipt; TRM2 activeRun FK & immutable goal; TRM3 lease/CAS/heartbeat; TRM4 cancel/steer dispatch fence; TRM5 fault replay and observability.
各PRで新旧Worker互換性/DB migrationを確認。共有契約が未Lockedの間、後段の外部副作用実装を本番公開しない。

## 14. Unit/Contract/Integration/Fault/Security/Benchmark試験
全てDesigned / **NOT RUN**。専用Mock DB/Clock/Provider/Browser/Tool fixturesで試験。実購入/予約/投稿/監視通知を無断発行しない。
| TEST-ID | Setup→action→expected |
|---|---|
| TRM-T01 | 100 simultaneous same command→Task1/receipt1 |
| TRM-T02 | different payload same idempotency key→409 |
| TRM-T03 | two workers claim→one winner/new epoch1 |
| TRM-T04 | old heartbeat after takeover→no lease extension |
| TRM-T05 | worker kill→same runId+increment epoch |
| TRM-T06 | terminal retry→new runId+attemptNo |
| TRM-T07 | cancel commit vs dispatch→no new side effect |
| TRM-T08 | steer vs old approval→old intent denied |
| TRM-T09 | parent cancel vs child fork→no post-cancel child |
| TRM-T10 | Watch creates null-conversation Task→success |
| TRM-T11 | cross-workspace fake job→denied |
| TRM-T12 | epoch unsafe numeric conversion→reject/no Tool |
| TRM-T99 | 100件同条件mockでp50/p95、RSS、費用USD、成功率とEvidence品質を計測し実測値のみ記録 |

## 15. REQ→TEST→SC
| MUST | TEST | SC |
|---|---|---|
| TRM-001 | TRM-T01 | SC-02/07/08/09 |
| TRM-002 | TRM-T02 | SC-02/07/08/09 |
| TRM-003 | TRM-T03 | SC-02/07/08/09 |
| TRM-004 | TRM-T04 | SC-02/07/08/09 |
| TRM-005 | TRM-T05 | SC-02/07/08/09 |
| TRM-006 | TRM-T06 | SC-02/07/08/09 |
| TRM-007 | TRM-T07 | SC-02/07/08/09 |
| TRM-008 | TRM-T08 | SC-02/07/08/09 |
| TRM-009 | TRM-T09 | SC-02/07/08/09 |
| TRM-010 | TRM-T10 | SC-02/07/08/09 |
| TRM-011 | TRM-T11 | SC-02/07/08/09 |
| TRM-012 | TRM-T12 | SC-02/07/08/09 |

## 16. 未決事項とCross-team proposal
P0 CHAT_01 Task/Run State, GoalRevision, leaseEpoch number, nullable conversationId and recordVersion1; P0 CHAT_10 runs.lease_epoch/lease_owner/expiry + tasks.active_run_id/goal_revision and idempotent receipt outbox; P0 CHAT_03 dispatch claim (lease/control/auth/budget), unknown ledger; P0 CHAT_09 tenant↔workspace/owner lineage; P1 CHAT_08 If-Match controls; CHAT_11 concurrent kill tests.
契約不一致のOwnerの決裁前は、本書や依存実装をLocked/Verifiedとしない。主なブロッカーはUUIDv7 vs DB UUIDv4、nullable conversation、CAS version初期値、Run.leaseEpochの保存、tenant↔workspaceの認可binding、activeRunId/goalRevision永続化。これらはPR #1のRound2 conflict ledgerにも反映済み。

## 17. Sources (inspection 2026-10-30)
- [S1] Code verified: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/execution-fence.ts ; https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/run-lease-fence.ts
- [S2] Test definition read: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-execution-fence.test.ts
- [S3] Code+test inspected: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/plans.ts ; https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/attemptFenceTest.ts
- [S4] Code+test inspected: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_runner.py ; https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/tests/test_pregel.py
LICENSE: LangGraph / PydanticAI / pg-boss はMIT、@mastra/coreはpackage.jsonのApache-2.0宣言を確認。ソースとテストの読解のみで本Harnessコードを実装/実行したものではない。
