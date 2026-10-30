# Runtime Host and Session — Detailed Implementation Specification

> Owner: CHAT_02 | Status: Draft [PROVISIONAL: CHAT_01/03/09/10] | Rev 0.1 | Research date: 2026-10-30 | base main: b45d4e989998663a00ef09673e69ae1f4191f00d

## 0. 結論と採用案
Run-scoped dependency injectionを採用し、Worker processとTask/Run/Browser Sessionの寿命を分ける。HostのSessionはrequest-specific contextのみで、ConversationでもBrowserContextでもない。Boot validation→Run claim→scope create→AgentLoop→bounded drainの責務を固定する。

## 1. 目的、責務と非責務、受入シナリオ
Run executionの依存注入、worker start/stopとhealth、独立したcontext/abort/clock/model, Browser worker handle、リソース破棄/Recovery入口。Task起点はUIでもWatchでも同じ。
非責務: 共有Domain/State/EventとUUIDはCHAT_01、Tool Policy/Approval/Side-effectはCHAT_03、BrowserSessionはCHAT_04、認可はCHAT_09、物理DDL/Queue/OutboxはCHAT_10、E2EはCHAT_11。TaskとConversationの寿命は独立し、Watch/X投稿起点のTaskはconversationId=nullを許可する。

## 2. 既存OSSの実装・テスト比較
| Upstream | Source & test function | Limit |
|---|---|---|
| Mastra main ec3ba11 | durable/run-registry.ts::RunRegistry, markRunActive; durable/execution-fence.ts::getExecutionClaim, DurableExecutionFenceError; durable/__tests__/durable-agent-concurrent-runs.test.ts (run isolated) | in-memory registryは永続真実でない |
| PydanticAI main c2ac8cc | agent/abstract.py::AbstractAgent.run; tests/test_deps.py::test_deps_used/test_deps_override | Python Agent lifetimeをTask正本としない |
| LangGraph main ffc3336 | pregel/_loop.py::PregelLoop.__aenter__, main.py::Pregel; tests/test_pregel.py::test_pending_writes_resume | Python実装をTS Hostに無条件移植しない |
**採用案**: 依存Portを明示した独立Worker Host. **却下案**: 単一の長命global mutable AgentContext、Provider SDKを永続Task正本とする案。上流コード・テストは実際にGitHub上で読んだが、テストを実行したわけではない。ここで提示するPortは新規設計であり上流既存APIではない。

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
| RHOST-001 | MUST | 起動時にDB schema互換/TLS/secret/egress policyを検証し不一致ならready=false、実行0 | SC-05/06/07/08 |
| RHOST-002 | MUST | RunごとにContextとAbortControllerを独立させ、他ownerのMemory/Browser token混入0 | SC-05/06/07/08 |
| RHOST-003 | MUST | Run claimなしのexecuteを拒否し、current leaseEpochだけ処理 | SC-05/06/07/08 |
| RHOST-004 | MUST | Queue内authorityを拒みDBのRun→Task→tenant/ownerを取得 | SC-05/06/07/08 |
| RHOST-005 | MUST | Worker crash後はDB snapshotとToolLedgerからrestore | SC-05/06/07/08 |
| RHOST-006 | MUST | drain開始後は新claim/Toolを拒否し既処理を有限時間で回収 | SC-05/06/07/08 |
| RHOST-007 | MUST | Browser WorkerだけがPageを所有、HostはBrowserSession opaque refのみ | SC-05/06/07/08 |
| RHOST-008 | MUST | 旧Workerのlate callback/usage/eventをCASで拒否 | SC-05/06/07/08 |
| RHOST-009 | MUST | DB unavailable時readiness=false、外部副作用開始0 | SC-05/06/07/08 |
| RHOST-010 | MUST | 同時Run/接続/時間/メモリのfinite capを設定 | SC-05/06/07/08 |
| RHOST-901 | SHOULD | レイテンシーと資源コストを同条件Fixtureで測り、数値上限を調整可能 | SC-09 |
| RHOST-902 | LATER | 安全な複数replica運用を測定後に拡張 | SC-07 |

## 5. I/O契約・型・Validation
~~~ts
interface RuntimeIdentity {
 tenantId:string;ownerUserId:string;taskId:string;runId:string;
 inputGoalRevision:number;appliedGoalRevision:number;
 leaseEpoch:string;authIdentityVersion:number;
}
interface RuntimeScope {
 identity:Readonly<RuntimeIdentity>;abort:AbortSignal;
 clock:{utcNow():string;monotonicMs():number};
 model:ModelPort;gateway:GatewayPort;checkpoint:CheckpointPort;
 budget:BudgetPort;logger:RedactedTracePort;
}
interface Host {
 boot():Promise<void>;
 executeClaim(job:{taskId:string;runId:string}):Promise<void>;
 drain(deadlineUtc:string):Promise<void>;
 health():{ready:boolean;live:boolean;reason?:string};
}
~~~
公開IDはCHAT_01 UUIDv7 validator、Run.leaseEpochは安全なJS number (0..Number.MAX_SAFE_INTEGER) とDB bigintのlossless変換。recordVersionはCAS用（初期1提案）、goalRevisionは条件世代、Tool Attempt fenceは別トークンであり**相互代用不可**。すべて[PROVISIONAL: CHAT_01/10]。Owner/tenant/workspaceはサーバー照会で結合する。Task.activeRunIdはそのTaskのRun以外へ向けられない。

## 6. 状態遷移と正常Sequence
~~~text
STARTING -> VALIDATING -> READY | BLOCKED
READY -> CLAIMED -> SCOPE_CREATED -> LOOP -> FINISHED | SUSPENDED | UNCERTAIN
READY -> DRAINING -> STOPPED
CRASH -> RESTART -> EXPIRED_LEASE -> NEW_CLAIM -> REHYDRATE
~~~
Boot: validate DB major and adapter versions→ready signal→receive queue pointer→atomic claim→verify owner→DI scope→loop. Approval待ちはscopeを解放しcheckpointを維持。Shutdownはqueue止める→dispatch fence閉じる→Abort通知→drain→lease release-if-current。
同じTaskでactive Run最大1。Runを新規作成するのは前Runのterminal以降の再試行だけ、Worker crashは既存runIdを維持しleaseEpochを増やす。Cancel/Steerのcommit後は新しいTool dispatchを拒否、既発生の外部副作用はGateway ledgerから照会する。

## 7. 実装可能なアルゴリズム
~~~ts
async function executeClaim(pointer: JobPointer) {
  if (!host.ready || host.draining) return;
  const claim=await runManager.claim(pointer);
  if (!claim) return;
  const task=await store.loadAuthoritativeTask(claim.taskId);
  assertTenantOwnerActiveRunAndSafeEpoch(task,claim);
  const scope=createPerRunScope(claim,task);
  try { await agentLoop.run(scope); }
  finally {
    scope.abortController.abort();
    await closeAllScopedResourcesWithinDeadline(scope);
    await runManager.releaseOnlyIfCurrent(claim);
  }
}
~~~
transaction中にLLM/外部HTTPを呼ばず、acquire/commitを短く保つ。Epoch mismatchまたはowner変更時はcallbackやStreamを破棄し確定結果を上書きしない。

## 8. 障害・競合・Timeout matrix
| Condition | Recovery |
|---|---|
| Config/Schema mismatch | BLOCKED; health status; new claim zero |
| Worker OOM | lease expiry then new epoch, checkpoint/ledger recovery |
| Duplicate queue | only successful DB claimer creates scope |
| Failed Browser restoration | await human handoff; never reuse previous Page handle |
| Old callback after takeover | fence rejection, no emitted terminal |
| Drain timeout with effect | UNKNOWN side effect, do not force success |
| DB dead on boot | fail closed, bounded reconnect |
| Tenant disabled during run | gateway fresh auth recheck blocks dispatch |
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
H1 DI/validation/health; H2 claim context and Run loop bridge; H3 graceful drain/restart; H4 Browser binding; H5 hostile concurrency and recovery.
各PRで新旧Worker互換性/DB migrationを確認。共有契約が未Lockedの間、後段の外部副作用実装を本番公開しない。

## 14. Unit/Contract/Integration/Fault/Security/Benchmark試験
全てDesigned / **NOT RUN**。専用Mock DB/Clock/Provider/Browser/Tool fixturesで試験。実購入/予約/投稿/監視通知を無断発行しない。
| TEST-ID | Setup→action→expected |
|---|---|
| RHOST-T01 | boot invalid schema→ready false + tool0 |
| RHOST-T02 | two Runs/tenants concurrent→no context/secret cross-mix |
| RHOST-T03 | duplicate queue pointer→one scoped runtime |
| RHOST-T04 | kill worker before/after model commit→same Run recovered with new epoch |
| RHOST-T05 | drain while tool active→no new dispatch, uncertain retained |
| RHOST-T06 | Browser Worker restart→no stale Page handle use |
| RHOST-T07 | old callback after takeover→CAS reject+terminal0 |
| RHOST-T08 | forged job owner→DB auth rejects |
| RHOST-T09 | DB outage→bounded stop/ready=false |
| RHOST-T10 | 100 start/stop cycles→no leaked handles/sockets |
| RHOST-T99 | 100件同条件mockでp50/p95、RSS、費用USD、成功率とEvidence品質を計測し実測値のみ記録 |

## 15. REQ→TEST→SC
| MUST | TEST | SC |
|---|---|---|
| RHOST-001 | RHOST-T01 | SC-05/06/07/08 |
| RHOST-002 | RHOST-T02 | SC-05/06/07/08 |
| RHOST-003 | RHOST-T03 | SC-05/06/07/08 |
| RHOST-004 | RHOST-T04 | SC-05/06/07/08 |
| RHOST-005 | RHOST-T05 | SC-05/06/07/08 |
| RHOST-006 | RHOST-T06 | SC-05/06/07/08 |
| RHOST-007 | RHOST-T07 | SC-05/06/07/08 |
| RHOST-008 | RHOST-T08 | SC-05/06/07/08 |
| RHOST-009 | RHOST-T09 | SC-05/06/07/08 |
| RHOST-010 | RHOST-T10 | SC-05/06/07/08 |

## 16. 未決事項とCross-team proposal
P0 CHAT_01 Run/Task/Session identity and leaseEpoch; P0 CHAT_10 CAS/Run epoch/activeRunId/transactional outbox; P0 CHAT_09 tenant/owner binding and revocation; P1 CHAT_04 BrowserSession rebind; P1 CHAT_08 health/recovery status; P1 CHAT_11 fault runner.
契約不一致のOwnerの決裁前は、本書や依存実装をLocked/Verifiedとしない。主なブロッカーはUUIDv7 vs DB UUIDv4、nullable conversation、CAS version初期値、Run.leaseEpochの保存、tenant↔workspaceの認可binding、activeRunId/goalRevision永続化。これらはPR #1のRound2 conflict ledgerにも反映済み。

## 17. Sources (inspection 2026-10-30)
- [S1] Code verified: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/run-registry.ts
- [S2] Code verified: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/execution-fence.ts
- [S3] Test definition inspected: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-concurrent-runs.test.ts
- [S4] Code and tests inspected: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/agent/abstract.py ; https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_deps.py
- [S5] Code and tests inspected: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py ; https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/tests/test_pregel.py
LICENSE: LangGraph / PydanticAI / pg-boss はMIT、@mastra/coreはpackage.jsonのApache-2.0宣言を確認。ソースとテストの読解のみで本Harnessコードを実装/実行したものではない。
