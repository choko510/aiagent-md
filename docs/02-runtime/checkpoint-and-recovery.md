# Checkpoint and Recovery — Detailed Implementation Specification
> Owner CHAT_02 | Status Draft / Provisional | Rev 0.1 | Research 2026-10-30 | Base main b45d4e989998663a00ef09673e69ae1f4191f00d

## 0. 結論
DB-first checkpointはRunの進行位置を記録するが、Tool外部効果をexactly-onceにしない。Model full response・Tool intent・effect receipt・Event/Outboxの独立した永続境界とfenceを要求。再起動ではLedger分類 COMPLETED/NOT_STARTED/UNKNOWN を読み戻し、UNKNOWN非冪等操作の自動再実行を永久に禁止する。

## 1. Scope、非Scope、Acceptance
Snapshot schemaとcommit順序、再開アルゴリズム、Crash killpoint、古いcallback禁止、Tool結果のlost-ACKとwrite-after-effect、immutable evidence/artifactと部分成果。DB物理DDL・Tool executorとProvider lookupは他Owner。
共有Task/Run/State/EventをCHAT_01から取り込み、Toolの承認/副作用をCHAT_03、DBはCHAT_10、OwnerはCHAT_09、UIはCHAT_08に委譲する。Watch/Event起点でconversationId=nullでもTask/Runは動く。SC: SC-05/07/08/09。

## 2. OSS実ソース/テスト比較
| OSS | Code and tests verified | Adoption |
|---|---|---|
| LangGraph ffc3336 / 1.2.18 MIT | libs/langgraph/langgraph/pregel/_loop.py::PregelLoop._put_checkpoint/put_writes, _checkpoint.py::create_checkpoint/checkpoint_superseded, libs/langgraph/tests/test_pregel.py::test_pending_writes_resume | progress cursor/checkpoint参考、side-effect保証は無い |
| Mastra ec3ba11 / Apache-2.0 | durable/execution-fence.ts::getExecutionClaim, durable/__tests__/durable-agent-crash-recovery.test.ts::captureCheckpoints/recoverFrom, durable-agent-execution-fence.test.ts | stale ownerが保存/terminal送信できない試験を参考 |
| PydanticAI c2ac8cc / MIT | durable_exec/_operation.py::ModelRequestId/ToolsetCallToolId/ParameterTransport, tests/test_run_cancellation.py::test_tool_cancels_run_and_history_is_resumable | logical step identityを利用、外部Python記録をDB正本としない |
| Issues | LangGraph #9240/#8464 (Issue report; no reproduction) | durabilityとexactly-onceを区別 |
採用: 短命トランザクションによるcheckpoint/ledger/outboxとRunEpoch fencing、reconcile-only UNKNOWN。却下: every token checkpointと単独process memory、checkpoint完了=外部API成功という前提、UNKNOWNの盲目retry。違うOSSの宣伝ベンチマークを流用しない。現行commitのSourceとテスト定義をGitHubで直接読んだが、自環境では実行していない。

## 3. Ownership/依存
~~~text
Task/Run Manager -> Checkpoint/Control Port -> AgentLoop
                   |                        |
                  CHAT_10 DB CAS        ToolGateway(CHAT_03)
                   |                        |
                 Events Outbox      Ledger + Approval
                      	                     |
                  REST/SSE(CHAT_08)   Browser(CHAT_04)
~~~
Task.recordVersion初期1案、Run.leaseEpoch number安全整数/DB bigint、Task.goalRevisionとRun.inputGoalRevisionは独立。Tool attempt fenceとRun epochを交換不可。UUIDはUUIDv7、Tenant/Workspace mappingは未Locked。

## 4. Requirements
| REQ | Level | Observable contract | SC |
|---|---|---|---|
| CPR-1 | MUST | full Model response確定後にのみTool提案とcursorをcommit | SC-05/07/08/09 |
| CPR-2 | MUST | 外部Tool intentとpolicy/approvalをcommitしない限りexecute 0 | SC-05/07/08/09 |
| CPR-3 | MUST | CheckpointはrunId,leaseEpoch,stepOrdinal,appliedGoalRevision,version,hashを記録 | SC-05/07/08/09 |
| CPR-4 | MUST | 旧epoch/旧recordVersionのwriteはCASで常に失敗 | SC-05/07/08/09 |
| CPR-5 | MUST | COMPLETEDのTool receiptは同logicalCallIdで再利用し物理再実行0 | SC-05/07/08/09 |
| CPR-6 | MUST | NOT_STARTEDは同Intentで初回dispatch可、IN_FLIGHT/UNKNOWNは照会のみ | SC-05/07/08/09 |
| CPR-7 | MUST | 結果不明のside effectは自動再実行0、UNCERTAINとして保留 | SC-05/07/08/09 |
| CPR-8 | MUST | Model usage/priceはattemptIdで一意に課金反映、lost ack後の二重計上0 | SC-05/07/08/09 |
| CPR-9 | MUST | Event/Outboxはstate commitとatomicでgapを検出・再送可 | SC-05/07/08/09 |
| CPR-10 | MUST | Snapshot schemaVersion不一致/Corruptionはrecover停止し安全なmigrationを要求 | SC-05/07/08/09 |
| CPR-90 | SHOULD | 同条件benchmarkでCPU/時間/IO/再開コストを計測 | SC-09 |
| CPR-91 | EXPERIMENT | 複数replica性能改善を安全性テスト後にのみ採用 | SC-09 |

## 5. 内部Port / TypeScript pseudo contracts
~~~ts
interface LocalCheckpoint {
 runId:string; leaseEpoch:number; stepOrdinal:number;
 expectedRecordVersion:number; appliedGoalRevision:number;
 phase:"MODEL"|"INTENT"|"APPROVAL"|"EXECUTE"|"VERIFY"|"WAIT";
 modelAttemptId?:string; modelReplyRef?:string;
 pendingLogicalCalls:string[]; receipts:string[];
 immutableEvidenceRefs:string[]; snapshotHash:string; schemaMajor:1;
}
interface RecoveryPort {
 load(runId:string):Promise<LocalCheckpoint>;
 commit(expected:LocalCheckpoint, next:LocalCheckpoint):Promise<void>;
 reconcile(logicalCallId:string):Promise<"COMPLETED"|"NOT_STARTED"|"UNKNOWN">;
}
~~~
Schema major unknownはreject。Run leaseEpochは非負safe integer、DB bigintから丸め無しで変換。expectedVersion/goalRevision/owner bindingの完全一致が必要。APIは上記Portの正式名称ではなく本書内設計案 [PROVISIONAL]。actorやTask権限はLLM inputから取得しない。

## 6. State machineとSequence
~~~text
Claim new leaseEpoch -> LOAD latest checkpoint/ledger
  -> MODEL_REQUEST -> persist full reply+usage -> MODEL_COMMITTED
  -> PREPARE_TOOL_INTENT -> ledger commit -> APPROVAL_OR_READY
  -> EXECUTE -> RECEIPT + evidence + event/outbox commit -> ADVANCE_STEP
  -> VERIFIER -> terminal commit
Crash at ANY arrow -> only reconstruct from DB; unknown effect wait
~~~

## 7. Algorithmと競合境界
~~~ts
async function recover(claim:RunClaim) {
 const snapshot=await checkpoint.loadValidated(claim.runId);
 await assertCurrentEpochOwnerAndActiveRun(claim,snapshot);
 const calls=await ledger.listByRun(snapshot.runId);
 for(const c of calls) {
  if(c.status==="COMPLETED") await reuseReceipt(c);
  else if(c.status==="NOT_STARTED") await gateway.firstDispatchOnly(c,claim);
  else {
   const result=await gateway.reconcileByProviderReceipt(c);
   if(!result.verified) return suspend("UNCERTAIN_EFFECT");
  }
 }
 if(snapshot.modelReplyRef) return resumeCommittedStep(snapshot);
 return regenerateModelUnderBoundedBudget(snapshot);
}
~~~
**不変条件**: エラーによるTask無言終了禁止。永続状態とoutboxが同一transactionで確定するまで「成功」と表示しない。DB lock中に外部HTTP・LLMを呼ばない。CASをすり抜けた旧Worker/期限切れapprovalのTool発火を防ぐため、Gatewayが外部作用直前に最新epoch・controlVersion・GoalRevision・Authを再確認する。

## 8. Failure/timeout/retry/cancel matrix
| Fault / killpoint | Durable recovery | Forbidden |
|---|---|---|
| before full model commit | new model attempt/estimated usage | partial call execution |
| after model commit, before intent | replay frozen proposals | new model with same step |
| before intent commit | Tool start=0 | untracked effect |
| after intent/approval, before dispatch | first dispatch only if NOT_STARTED | duplicate intent |
| after external effect, before receipt | UNKNOWN→provider lookup/manual review | blind retry |
| after receipt before ack | reuse completed receipt | duplicate execute |
| stale worker checkpoint | CAS rejection | overwrite new generation |
| schema major incompatible | QUARANTINE | guess old state |
ハートビートやQueue再配達は外部効果の成功/失敗証明にならない。外部APIへの作用後にDB保存不能ならUNKNOWN_EFFECTに遷移、照会のみ可能。Userにpartial/uncertainを説明して待機。read-onlyでもサイト規約・rateを守る。

## 9. Security, Privacy, Trust
Host Shell/SSH/Docker socketへのアクセス、無承認購入/予約/投稿、資格情報のLLM露出は禁止。Web/SNS/MCPの文字列を実行命令として採用しない。Authorization serverがRun→Task→Owner/Tenant/Workspaceをfreshに照会。BrowserタブをAgentWorkerが直接制御せずSession referenceのみ。全Outputはredacted。Auditに意思決定/不確定根拠とactor/trace/epochを保存。

## 10. Metrics / budget
checkpoint write p95, snapshot bytes, ledger UNKNOWN duration, recovery lag, lost ack, corrupt snapshot, orphan artifact counts, model usage reconciliation. Bound snapshot size e.g. 512KiB initial; full HTML in Artifact store, not checkpoint.
finite上限: Run max steps24、Tool proposal60、Runtime30分、read parallel4、effect parallel1（暫定例）。バジェット超過時は新LLM/toolを開始せずpartial。計測漏れを0費用と見なさない。測定条件固定のp50/p95、cost USD/task、constraint violations、effect duplicate countを記録する。

## 11. DB Transactions/Migration [PROVISIONAL: CHAT_10]
T1 claim fence, T2 model+usage+events, T3 Tool intent/approval+outbox, T4 receipt/evidence+event, T5 cursor/terminal+outbox。Model budget reserveとusage settleはattemptId uniqueness。Raw artifact object durability must precede DB reference commit; missing object means explicit corrupted snapshot, no phantom success.
Task/Run/receipt/checkpoint/tool intentの一貫性を原子的トランザクションで保持。UUIDv7 generatorがDB gen_random_uuid()と不一致ならAPIで生成してSQL default削除。conversation_id nullable。recordVersion=1開始提案。schemaVersion保存とexpand/contract migration。旧Workerは新schemaで許可なく書込不可。

## 12. UI/REST [PROVISIONAL: CHAT_08]
User sees recovering/UNCERTAIN/retry denied with previous evidence retained; never exposes raw Tool arguments. Restart continues Run, no new conversationId required.
POST commandは受付receipt、If-MatchはrecordVersionを照合し不足428/不一致412。UIはtentative streamingとcommitted eventを分け、SSE cursorから重複なし再読込。秘密や内部例外文字列をユーザーへ露出しない。

## 13. Implementation PR plan
CPR-A schema codec/migration and fake ledger; CPR-B model/tool checkpoints; CPR-C safe replay + unknown reconciliation; CPR-D two-Worker fencing; CPR-E fault injection at all T1-T5 boundaries.

## 14. Tests (Designed / NOT RUN)
| TEST-ID | Setup/action/expected |
|---|---|
| CPR-T1 | model response partial kill→Tool0 |
| CPR-T2 | intent DB failure→Executor0 |
| CPR-T3 | snapshot restart→same Run/new epoch |
| CPR-T4 | old epoch delayed write→DB unchanged |
| CPR-T5 | after Tool result before ack→Executor count1 |
| CPR-T6 | after effect before DB receipt→UNKNOWN count1, retry0 |
| CPR-T7 | NOT_STARTED intent replay→first dispatch only once |
| CPR-T8 | usage settle replay with same attemptId→charge counted once |
| CPR-T9 | outbox fail after commit→replay same event ID |
| CPR-T10 | corrupt/unknown schema→safe halt no Tool |
| CPR-T99 | 同じfixture 100反復→性能指標と安全性判定を保存。実測なし |

## 15. Traceability
| MUST REQ | TEST | SC |
|---|---|---|
| CPR-1 | CPR-T1 | SC-05/07/08/09 |
| CPR-2 | CPR-T2 | SC-05/07/08/09 |
| CPR-3 | CPR-T3 | SC-05/07/08/09 |
| CPR-4 | CPR-T4 | SC-05/07/08/09 |
| CPR-5 | CPR-T5 | SC-05/07/08/09 |
| CPR-6 | CPR-T6 | SC-05/07/08/09 |
| CPR-7 | CPR-T7 | SC-05/07/08/09 |
| CPR-8 | CPR-T8 | SC-05/07/08/09 |
| CPR-9 | CPR-T9 | SC-05/07/08/09 |
| CPR-10 | CPR-T10 | SC-05/07/08/09 |

## 16. Unresolved / Cross-team
CHAT_01 Step/Checkpoint envelope & appliedGoalRevision, CHAT_03 Tool Ledger classification + provider receipt lookup, CHAT_10 immutable checkpoint/receipt/outbox tx and epoch CAS, CHAT_11 killpoint suite, CHAT_08 recover/uncertain presentation. All are [PROVISIONAL].
本Specは他OwnerのP0未確定事項が解消されるまでLockedにしない。

## 17. Sources / Evidence level (2026-10-30)
- [S1] Code verified https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_checkpoint.py ; https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py
- [S2] Test definition inspected https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/tests/test_pregel.py
- [S3] Code/test inspected https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/execution-fence.ts ; https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-crash-recovery.test.ts
- [S4] Code/test inspected https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/durable_exec/_operation.py ; https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_run_cancellation.py
- [S5] Issue report https://github.com/langchain-ai/langgraph/issues/9240 ; https://github.com/langchain-ai/langgraph/issues/8464
Code verified=ソース関数確認、Test definition verified=テスト本文確認、Issue report=未再現の報告。OSSライセンス: LangGraph/PydanticAIはMIT、@mastra/coreはpackage metadataでApache-2.0。独立実装で丸写ししない。
