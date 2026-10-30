# Steer Queue, Pause and Resume — Detailed Implementation Specification
> Owner CHAT_02 | Status Draft / Provisional | Rev 0.1 | Research 2026-10-30 | Base main b45d4e989998663a00ef09673e69ae1f4191f00d

## 0. 結論
Control commandは現在のRunのcontrolVersion/goalRevisionに対する持続的な命令。Cancel > Authorization revoke > Steer > Pause > ordinary dispatchの優先順位を固定する。Steerは次の安全境界で適用し、旧Goalの承認済みToolを遅延実行しない。Queueで新しい指示が増えても即時にLLM/Toolを無期限起動しない。

## 1. Scope、非Scope、Acceptance
TaskへのUser Steer/Queue/Pause/Resume/Cancel、停止対象の選択、制御の直列化、in-flight副作用境界。ユーザー意図の形式/再計画はGoal/Planner、Tool abort/Effect結果はGateway、RESTとEventは他Owner。
共有Task/Run/State/EventをCHAT_01から取り込み、Toolの承認/副作用をCHAT_03、DBはCHAT_10、OwnerはCHAT_09、UIはCHAT_08に委譲する。Watch/Event起点でconversationId=nullでもTask/Runは動く。SC: SC-04/06/07/08。

## 2. OSS実ソース/テスト比較
| OSS | Code/test | Adaptation |
|---|---|---|
| Mastra ec3ba11 | agent/signals.ts::normalizeSignal/signalToLLMMessage, durable/__tests__/durable-agent-signal-preemption.test.ts (queued signal/preemption) | control入力をDB命令キューへ分離 |
| Mastra ec3ba11 | durable/__tests__/durable-agent-queued-tool-approval.test.ts (suspend/resume) | 未完承認と新しい命令の競合テスト |
| PydanticAI c2ac8cc | agent/abstract.py::AgentRunEvents.cancel, tests/test_run_cancellation.py::test_pre_cancelled_token_does_not_start_run, test_external_cancellation_keeps_completed_tool_result | cooperative cancelを補助的に採用 |
| OpenAI Agents Python cd26bbb | src/agents/run.py::AgentRunner._run_impl / tests/test_hitl_session_scenario.py | approval restoration参考、永続制御正本には使わない |
採用: CASで順序づけるcommand queue＋Gateway dispatch前のcontrol fence。却下: AbortSignalだけで取消確定、Model streamに指示を注入して承認済みToolを続行、replayで全命令を再投入。違うOSSの宣伝ベンチマークを流用しない。現行commitのSourceとテスト定義をGitHubで直接読んだが、自環境では実行していない。

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
| STR-1 | MUST | Commandはauth+Idempotency-Key+If-Matchで一意receiptを返す | SC-04/06/07/08 |
| STR-2 | MUST | Cancel commit後は新しいLLM/Tool開始0、in-flight effectはreconcile | SC-04/06/07/08 |
| STR-3 | MUST | Pause commit後は新しいstepを開始しない、すでにbeginしたreadの結果は保持 | SC-04/06/07/08 |
| STR-4 | MUST | SteerはTask.goalRevisionを原子的に+1してimmutable goalを保存 | SC-04/06/07/08 |
| STR-5 | MUST | 旧goalRevision Tool intent/approvalは新Revisionで無効 | SC-04/06/07/08 |
| STR-6 | MUST | 優先順位 Cancel>Revoke>Steer>Pause を競合中にも適用 | SC-04/06/07/08 |
| STR-7 | MUST | Resumeは現行principal/owner/goalRevisionとCheckpointを再検証 | SC-04/06/07/08 |
| STR-8 | MUST | 同じcommand key再送は同receipt、異body409 | SC-04/06/07/08 |
| STR-9 | MUST | preemptしたmodel partial streamはtentative廃棄、Usageはunknown含め追跡 | SC-04/06/07/08 |
| STR-10 | MUST | Handoff/承認待ち中のSteerはHuman Grantを不意に拡張しない | SC-04/06/07/08 |
| STR-90 | SHOULD | 同条件benchmarkでCPU/時間/IO/再開コストを計測 | SC-09 |
| STR-91 | EXPERIMENT | 複数replica性能改善を安全性テスト後にのみ採用 | SC-09 |

## 5. 内部Port / TypeScript pseudo contracts
~~~ts
type ControlKind="CANCEL"|"PAUSE"|"RESUME"|"STEER";
interface ScopedControl {
 commandId:string;taskId:string;actorUserId:string;
 expectedRecordVersion:string; kind:ControlKind;
 issuedAt:string;payloadRef?:string;goalRevisionBefore:number;
}
interface ControlPort {
 enqueue(auth:VerifiedPrincipal,c:ScopedControl):Promise<ControlReceipt>;
 peekBoundary(runId:string,epoch:number):Promise<ControlSnapshot>;
 acknowledge(commandId:string,checkpointVersion:number):Promise<void>;
}
~~~
Schema major unknownはreject。Run leaseEpochは非負safe integer、DB bigintから丸め無しで変換。expectedVersion/goalRevision/owner bindingの完全一致が必要。APIは上記Portの正式名称ではなく本書内設計案 [PROVISIONAL]。actorやTask権限はLLM inputから取得しない。

## 6. State machineとSequence
~~~text
ACTIVE -> PAUSE_REQUESTED -> PAUSED -> RESUME_REQUESTED -> ACTIVE
ACTIVE -> STEER_REQUESTED -> REPLAN -> ACTIVE
ANY active/wait -> CANCEL_REQUESTED -> STOPPED/RECONCILE_PENDING
WAITING_APPROVAL/INPUT -> STEER -> invalidate pending proposal -> REPLAN
~~~
UI command→auth/If-Match CAS+command receipt+outbox→Worker observes at safe boundary→Gateway blocks old dispatch by controlVersion→checkpoint new appliedGoalRevision→ack command→SSE committed progress。

## 7. Algorithmと競合境界
~~~ts
async function atBoundary(claim:RunClaim) {
 const c=await controls.readCurrentAndPending(claim.taskId);
 if(c.cancel || c.authRevoked) return suspendNewWork("CANCELLED_OR_REVOKED");
 if(c.steer && c.goalRevision>checkpoint.appliedGoalRevision) {
  await gateway.invalidatePendingApprovals(claim,c.goalRevision);
  await checkpoints.commitReplanBoundary(claim,c.goalRevision);
  return runPlannerOnCommittedRevision(c.goalRevision);
 }
 if(c.pause) return checkpointAndReleaseLease("PAUSED");
 return resumeCurrentStepWithFrozenIntents(claim);
}
~~~
**不変条件**: エラーによるTask無言終了禁止。永続状態とoutboxが同一transactionで確定するまで「成功」と表示しない。DB lock中に外部HTTP・LLMを呼ばない。CASをすり抜けた旧Worker/期限切れapprovalのTool発火を防ぐため、Gatewayが外部作用直前に最新epoch・controlVersion・GoalRevision・Authを再確認する。

## 8. Failure/timeout/retry/cancel matrix
| Race | Resolution |
|---|---|
| Cancel vs Tool START | same Gateway dispatch claim linearizes; cancel commit first→start denied |
| Steer vs preapproved booking | revision mismatch→old intent denied |
| queued Steer and Cancel | Cancel wins; no new planning |
| two Steer commands | CAS order produces two revisions, only latest applied |
| Pause after external API called | unknown side effect tracked, pause does not erase |
| Resume after Owner disabled | fail closed |
| Handoff while cancelling | human takeover grant revoked, Browser Worker owned |
| Model stream preempted | discard tentative output, no tool based on partial |
ハートビートやQueue再配達は外部効果の成功/失敗証明にならない。外部APIへの作用後にDB保存不能ならUNKNOWN_EFFECTに遷移、照会のみ可能。Userにpartial/uncertainを説明して待機。read-onlyでもサイト規約・rateを守る。

## 9. Security, Privacy, Trust
Host Shell/SSH/Docker socketへのアクセス、無承認購入/予約/投稿、資格情報のLLM露出は禁止。Web/SNS/MCPの文字列を実行命令として採用しない。Authorization serverがRun→Task→Owner/Tenant/Workspaceをfreshに照会。BrowserタブをAgentWorkerが直接制御せずSession referenceのみ。全Outputはredacted。Auditに意思決定/不確定根拠とactor/trace/epochを保存。

## 10. Metrics / budget
control receipt latency, observedBoundary lag, canceled dispatch attempts, discarded stream token counts, stale approvals, pause-to-quiescent time, uncertain effect duration. All bounded and measured after implementation.
finite上限: Run max steps24、Tool proposal60、Runtime30分、read parallel4、effect parallel1（暫定例）。バジェット超過時は新LLM/toolを開始せずpartial。計測漏れを0費用と見なさない。測定条件固定のp50/p95、cost USD/task、constraint violations、effect duplicate countを記録する。

## 11. DB Transactions/Migration [PROVISIONAL: CHAT_10]
Control command/updated task recordVersion/goalRevision/TaskEvent/Outbox must share one transaction. ControlVersionはGoalRevisionと別。Run.inputGoalRevision immutable、checkpoint.appliedGoalRevisionを持つ（提案）。Control storageはCHAT_10と調停。古いcommand ackは再確定しても二重適用しない。
Task/Run/receipt/checkpoint/tool intentの一貫性を原子的トランザクションで保持。UUIDv7 generatorがDB gen_random_uuid()と不一致ならAPIで生成してSQL default削除。conversation_id nullable。recordVersion=1開始提案。schemaVersion保存とexpand/contract migration。旧Workerは新schemaで許可なく書込不可。

## 12. UI/REST [PROVISIONAL: CHAT_08]
Control受付202と実際のPause/Cancel確定を分ける。Queue順・revision・結果に不確定がある場合は明示。Resume時If-Matchと権限が失効なら422/403と再認証操作。
POST commandは受付receipt、If-MatchはrecordVersionを照合し不足428/不一致412。UIはtentative streamingとcommitted eventを分け、SSE cursorから重複なし再読込。秘密や内部例外文字列をユーザーへ露出しない。

## 13. Implementation PR plan
STR-A command receipts/ordered control queue, STR-B bound cancel/pause checkpoint, STR-C steer/new immutable goal, STR-D Gateway revision/approval invalidation, STR-E UI/SSE and fault injection.

## 14. Tests (Designed / NOT RUN)
| TEST-ID | Setup/action/expected |
|---|---|
| STR-T1 | 同command key replay→receipt1 |
| STR-T2 | cancel-before-tool gate→tool0 |
| STR-T3 | pause before next step→new model0 |
| STR-T4 | steer 2回→immutable revision更新・最新版採用 |
| STR-T5 | old approval after steer→effect0 |
| STR-T6 | cancel/revoke/steer concurrent→priority維持 |
| STR-T7 | resume after disabled owner→deny |
| STR-T8 | same key changed body→409 |
| STR-T9 | stream partial cancellation→uncommitted tool0、unknown usage tracked |
| STR-T10 | Handoff revoked then resume→fresh auth required |
| STR-T99 | 同じfixture 100反復→性能指標と安全性判定を保存。実測なし |

## 15. Traceability
| MUST REQ | TEST | SC |
|---|---|---|
| STR-1 | STR-T1 | SC-04/06/07/08 |
| STR-2 | STR-T2 | SC-04/06/07/08 |
| STR-3 | STR-T3 | SC-04/06/07/08 |
| STR-4 | STR-T4 | SC-04/06/07/08 |
| STR-5 | STR-T5 | SC-04/06/07/08 |
| STR-6 | STR-T6 | SC-04/06/07/08 |
| STR-7 | STR-T7 | SC-04/06/07/08 |
| STR-8 | STR-T8 | SC-04/06/07/08 |
| STR-9 | STR-T9 | SC-04/06/07/08 |
| STR-10 | STR-T10 | SC-04/06/07/08 |

## 16. Unresolved / Cross-team
CHAT_01 command State/GoalRevision/Event semantics, CHAT_10 Command+Task CAS/outbox and controlVersion, CHAT_03 dispatch linearization and approval invalidation, CHAT_09 revocation, CHAT_08 status/If-Match, CHAT_04 handoff cancel. Durable auto-steer only after verified user instruction; untrusted Web text must never enqueue control.
本Specは他OwnerのP0未確定事項が解消されるまでLockedにしない。

## 17. Sources / Evidence level (2026-10-30)
- [S1] Code verified https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/signals.ts
- [S2] Test definitions inspected https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-signal-preemption.test.ts ; https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-queued-tool-approval.test.ts
- [S3] Code and tests https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/agent/abstract.py ; https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_run_cancellation.py
- [S4] Code and tests https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run.py ; https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_hitl_session_scenario.py
Code verified=ソース関数確認、Test definition verified=テスト本文確認、Issue report=未再現の報告。OSSライセンス: LangGraph/PydanticAIはMIT、@mastra/coreはpackage metadataでApache-2.0。独立実装で丸写ししない。


### Round 4 cross-owner int64 reconciliation (2026-10-30)
Version/Epoch fields above follow CHAT_01's canonical signed-int64 **decimal string** wire format, validated and compared with internal BigInt and persisted as PostgreSQL BIGINT; JSON number conversion is prohibited. Other scalar number fields (bounded counts, percentages, step ordinals) remain number unless separately versioned. Regression test proposed: `9007199254740992` and `9223372036854775806` round-trip exactly, reject unsafe Number and stale authority; not executed against Harness code.
