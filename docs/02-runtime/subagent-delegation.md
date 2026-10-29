# Subagent Delegation — Detailed Implementation Specification

> Owner: CHAT_02 | Canonical: docs/02-runtime/subagent-delegation.md | Status: **Draft / [PROVISIONAL: CHAT_01/03/09/10]** | Revision: 0.1 | Research date: 2026-10-30 | Base main: 3044c046b1caeea70c5ef6edfea7c57387c6c6da

## 0. 採用案と絶対条件

**採用**: Main Agentを基本にし、独立した検索・比較などで並列実行による利益があるときのみ、Task/Run Manager経由で**子Task**を作成する。子Taskは親と同じtenant/owner、**親より強くない権限、分割予約されたBudget、有限の同時数**を持つ。委任は新しい独立Runを並列に持つが、親のactiveRunが子Runを直接所有するわけではなく、各Taskには高々1 active Runを設ける。親は子のEvidence付き結果を検証し、子が「完了」と書いたことを信用して無条件成功にはしない。

**非保証**: 子ToolがHTTP APIで不可逆作用を行った直後、DBへ結果記録前に停止した場合、親/子とも厳密なexactly-once外部効果を保証できない。Tool Gatewayのside-effect ledgerでUNCERTAINとし、盲目的な再配送や別子Agentでの再試行を禁止する。

**不採用**: 任意の再帰Agent対話（意思決定の振動と無限費用）、親の秘密・Cookieや全Historyを丸ごと子へ送信、model outputに由来するRoleによる権限昇格、親の承認トークンを子の別Toolに流用する設計。

## 1. 範囲・非責務・受入シナリオ

**範囲:** 子へ任せる資格判定、子Task生成、依存Goal抽出、Budget/Tool capability/tenant/ownershipの継承、有限並列、子Runの監視とリトライ、親Cancel/Steerの伝播、子の独立Evidenceと結果統合を定義する。適合シナリオ: SC-01（商品比較の独立候補）、SC-03（SNS横断ソース）、SC-04（複数ホテル）、SC-05（調査ドメイン別）、SC-06（Browser Handoff）、SC-07（Crash/Cancel/二重配送）、SC-08（Security）、SC-09（Budget）。

**非責務:** 共通Task/Run ID/State/Event/GoalRevisionとParent relationはCHAT_01、Tool Registry/approval/side-effect ledgerはCHAT_03、BrowserSession実体はCHAT_04、Memory/SkillはCHAT_06、認可はCHAT_09、DDL/Outbox/JobはCHAT_10、E2EはCHAT_11が正本。各子のWorker実行はCHAT_02 Runtime Host/Task Manager/Agent Loopを再使用し、子固有のShadow Runtimeを作らない。

## 2. OSS横断調査（Source code and tests inspected）

| Project / pinned commit | 実コード・関数/テスト | 具体的な学びと非採用点 | License |
|---|---|---|---|
| [DeepAgents](https://github.com/langchain-ai/deepagents/tree/3efa098130d6670fe713f841072bc11ff40545da) | `libs/deepagents/deepagents/middleware/subagents.py::SubAgent,TaskToolSchema,_build_task_tool,_validate_and_prepare_state`。 `tests/unit_tests/test_subagents.py::test_multiple_subagents_invoked_in_parallel,test_private_state_does_not_propagate_between_sibling_subagents,test_subagent_inherits_parent_user_metadata` [S1][S2] | サブAgent間private state分離と並列化は参考。LangGraph internal Task Toolでは我々のDB Task/Run/tenant境界を保証しない。 | MIT |
| [DeepAgents](https://github.com/langchain-ai/deepagents/tree/3efa098130d6670fe713f841072bc11ff40545da) | `middleware/async_subagents.py::AsyncTask,StartAsyncTaskSchema,CancelAsyncTaskSchema,_parent_reference,_notify_parent`。 `tests/unit_tests/test_async_subagents.py::test_launch_context_snapshot,test_fork_preserves_evicted_human_content` [S3][S4] | 独立非同期Taskの親通知とContextスナップショットを参考。HTTP Authorization Headerの転送を当Harnessの資格情報委譲の実装として直接コピーしない。 | MIT |
| [OpenAI Agents Python](https://github.com/openai/openai-agents-python/tree/cd26bbbc52847845fde73b9952b51e62e2bcbdc0) | `tests/test_agent_as_tool.py::test_agent_as_tool_returns_final_output,test_agent_as_tool_inherits_parent_run_config_when_not_set`、`tests/test_agent_as_tool_output_guardrails.py::test_default_agent_tool_exports_guarded_final_output` [S5][S6] | agent-as-toolの結果/guardrailの境界が参考。モデルSDKを親子Taskの永続的所有者にしない。 | MIT |
| [Mastra](https://github.com/mastra-ai/mastra/tree/ec3ba11dad428caeb3b4afae77d1669462e8c129) | `packages/core/src/agent/subagent.ts::SubAgent,isAgentCompatible`, `durable/__tests__/durable-agent-delegation.test.ts` (cold worker history, input-only tool context cases) [S7][S8] | Plugin typeとdurable delegationの分離を参照。Runtimeに直接有効なUser Credentialを共有しない。 | @mastra/core Apache-2.0 declared |

**採否比較:** A. 親子関係をDB永続Task（CHAT_01）で持ち、子ごとにRun/leaseをclaim、Budgetを原子的分割し、結果をEvidence manifestへ統合する方式 **採用**。B. subagentをLLM同一プロセスの内包関数としてのみ実装する方式 **不採用**（再起動、lease、監査、Cancel、費用計上で正本とずれる）。C. 複数LLMが無制限に協議して結論する方式 **不採用**（権限と費用上限、収束性を保証できない）。

## 3. Authority / 依存図 / 正本

~~~text
Authenticated User / Scheduler / Event (CHAT_08/09)
     -> Parent Task / active Run (CHAT_01/02)
         -> Delegation admission & bounded child budget (CHAT_02)
             -> Child Task idempotent creation (CHAT_02 Manager + CHAT_10)
                 -> child Run leaseEpoch/Worker/AgentLoop
                     -> Tool Gateway / Browser / API/HTTP (CHAT_03/04/05)
                     -> Child Evidence + childResult (CHAT_01)
         -> Parent merges evidence, verifier decides Goal success
~~~

- 子Runは親の`leaseEpoch`を使用しない。親Task→子Taskの参照は同tenantと同ownerで循環なし。各Taskの`activeRunId`は自己TaskのRunのみを指す。
- 子Taskの`conversationId`はnullable、親にConversationがない場合も架空Conversationを作らない。子が会話UIへ直接投稿することを既定で許可しない。
- 権限/Consent/GoalRevisionはテキストで「引き継ぐ」のではなく、DBで検証された不変のscope/新Goal snapshotから再構築。ToolやBrowser Contextの継承ではなく同一owner上で必要な個別Tool capabilityを再解決する。
- Budget reservationは親の全Budgetを子へ複製するのでなく、子ごとに保守的最大額を確保し、親に残るavailableを減らす。子のunusedを戻すときはsettlement receiptが必要。
- 親Cancel/Steerは子Taskを含む新規操作の停止をTaskManager/Gatewayで線形化。派生子Goalの意味が変わるなら新GoalRevisionで再計画し、旧承認を失効。
- User/Tenant/Workspaceは[PROVISIONAL: CHAT_01/09/10]。UserId = TenantId = WorkspaceId を仮定しない。

## 4. Requirements

| REQ-ID | Level | Observable requirement | SC |
|---|---|---|---|
| SUB-001 | MUST | サブエージェントを明確に独立した調査が存在する場合のみ起動し、1主Agentを既定とする | SC-05/09 |
| SUB-002 | MUST | 子TaskはparentTaskId/tenantId/ownerUserIdを正本から継承し同一Tenant・owner以外を拒否 | SC-07/08 |
| SUB-003 | MUST | 子TaskのBudget/Tool権限/Egressは親より厳しいか同等のみ（capabilityの権限拡張0） | SC-08/09 |
| SUB-004 | MUST | 親子Task循環を禁止し深度/子数/全同時実行/total model callsの有限上限を検査 | SC-07/09 |
| SUB-005 | MUST | 子Taskの作成と親予算予約・参照・Event・Outboxを原子的に保存し二重forkを拒否 | SC-07/09 |
| SUB-006 | MUST | 子のRunは自分自身のrunId/leaseEpoch/goalRevisionでclaimし、親のlease tokenを借用しない | SC-07/08 |
| SUB-007 | MUST | 親Cancel/Steer/権限剥奪後に新規子Toolを起動せず、in-flight効果はLedger照会に残す | SC-07/08 |
| SUB-008 | MUST | 子のWorking Contextとprivate stateを兄弟/親に無制限複製せず、必要な制約/証拠参照のみ委譲 | SC-05/08 |
| SUB-009 | MUST | 子から親へ戻る結果はEvidence/observedAt/confidence/goalRevisionを含む機械検証可能な形式 | SC-01/04/05 |
| SUB-010 | MUST | 子の完了文だけで親がSUCCESSとせず親GoalVerifierを通過する | SC-01/05 |
| SUB-011 | MUST | 子失敗/partial/unknownを親で正確に区別し、再試行は新Taskでなく既存ledgerを照会して判断 | SC-07 |
| SUB-012 | MUST | UIの停止/承認/再開と人間Browser Handoffは親子で所有権を維持し、承認を自動継承しない | SC-06/07/08 |
| SUB-013 | MUST | 子の実行は独自タスクを作り、並列Result merge時に出典時刻・重複と制約不一致を検証 | SC-03/05 |
| SUB-090 | SHOULD | 子タスク追加によるp50/p95/cost改善を同条件で実測し、不要な子を減らす | SC-09 |
| SUB-091 | LATER | 実際の速度改善と隔離テスト後に専門Agent poolを増やせる | SC-05/09 |

## 5. 内部型とPort契約（CHAT_01正本ではない）

~~~ts
type ChildAdmissionDecision =
  | {kind:"ALLOW"; reasonCode:string}
  | {kind:"REJECT"; reasonCode:"NOT_INDEPENDENT"|"BUDGET"|"AUTH"|"DEPTH"|"CANCEL"|"UNKNOWN"};
interface DelegationRequest {
  parentTaskId:string; parentRunId:string;
  parentGoalRevision:string; childPurpose:string;
  sourceEvidenceRefs:string[]; proposedToolRefs:string[];
  maxCostMicroUsd:string; maxModelTurns:number;
  maxToolCalls:number; deadlineUtc:string;
  commandId:string;
}
interface ChildTaskGrant {
  childTaskId:string; parentTaskId:string;
  tenantId:string; ownerUserId:string;
  inheritedGoalRevision:string; childGoalRevision:string;
  allowedToolRefs:string[]; budgetReservationId:string;
  authIdentityVersion:number; maxDepth:number;
}
interface DelegationPort {
  admit(parentRunClaim:RunClaim,req:DelegationRequest):Promise<ChildAdmissionDecision>;
  createChildExactlyOnce(parentRunClaim:RunClaim, req:DelegationRequest):Promise<ChildTaskGrant>;
  listChildren(parentTaskId:string):Promise<ChildTaskGrant[]>;
  collectResults(parentTaskId:string):Promise<ChildEvidenceManifest[]>;
  requestCancelSubtree(parentTaskId:string,commandId:string):Promise<void>;
}
~~~

**Validation:** all public Task/Run UUIDs UUIDv7 (CHAT_01)。 `commandId`/provider call ID/external Hashは別型。 `maxCostMicroUsd`は非負Decimal整数文字列、NaN/負数/オーバーフロー拒否。 `maxDepth`、child count、max concurrencyとwall clockはfinite policy上限。 `allowedToolRefs`はToolRegistry/Policyの現行capabilityの**集合積**で、個別ApprovalやCredential tokenではない。すべて[PROVISIONAL: CHAT_01/03/09/10]。

## 6. 状態遷移 / Sequence

~~~text
Parent ACTIVE
   -> DELEGATION_PROPOSED -> ADMISSION (no external effects)
      -> REJECTED (parent continues alone)
      -> CHILD_CREATED (parent budget reserve + child Task + outbox commit)
         -> CHILD_RUN_CLAIMED -> CHILD_RUNNING
            -> CHILD_WAITING_INPUT / CHILD_PARTIAL / CHILD_UNCERTAIN / CHILD_DONE
         -> PARENT_COLLECT -> VERIFY -> NEXT STEP / PARENT_FINISH
Parent CANCEL -> subtree STOP_NEW_WORK; in-flight effect RECONCILE
Parent STEER -> previous revision child output marked STALE for changed conditions
~~~

1. Plannerが複数候補を検出→各委任目的、入力ソースと個別費用/Tool許可を提案（Tool Gatewayではない）。
2. TaskManagerは親Runをcurrent epochとTask current GoalRevisionで読み、policy/owner/Budget/cancelを検証。
3. DB txで親のbudget reservedを確保、子Task+immutable child Goal+親子リンク+command dedupe receipt+Event/Outboxを作成。commit後にQueueへ送信。失敗時は全てrollback。
4. 子Runは自分のTask/Run claimと独立`leaseEpoch`、Auth/Tool Gatewayを通して処理する。BrowserSessionを勝手に共有しない。
5. 子はEvidence manifestとGoalRevisionをcommitし、親へ状態通知。親はステータス/出典/時刻/同一性を照合する。重複した同一ソースは独立確認の数にカウントしない。
6. 子がWAITING_APPROVALの場合、親が持つ別操作承認は流用禁止。同一ownerへの明示的な承認依頼を生成し、期限切れ/GoalRevision変更なら再承認。
7. 親のCancelはcommand receipt確定後、開始前の子Toolを止め、開始済み副作用を不可逆なものとしてledger照会。子が自律的に親のCancelを覆せない。
8. 親がGoalのhard constraintsを変えたら子結果の`goalRevision`を検査し、旧結果は不変Evidenceとして保存しても新Goal達成証明には自動流用しない。

## 7. 実装可能な擬似コード

~~~ts
async function delegate(parentClaim: RunClaim, req: DelegationRequest) {
  return db.tx(async tx => {
    const runPointer=await tx.readRunPointer(parentClaim.runId);
    const parentTask=await tx.lockTask(runPointer.taskId); // Task before Run (shared lock order)
    const parentRun=await tx.lockCurrentRun(parentClaim.runId);
    if(parentRun.taskId!==parentTask.id) throw StaleParentPointer();
    assertSameTenantAndOwner(parentRun,parentTask);
    assertCurrentEpoch(parentRun,parentClaim.leaseEpoch);
    assertActiveRunAndGoal(parentTask,parentRun,req.parentGoalRevision);
    assertNotCancelledOrRevoked(parentTask);
    assertDAGandFiniteDepth(parentTask,req);
    const existing=await tx.lookupIdempotencyByParentAndCommand(parentTask.id,req.commandId);
    if(existing) return assertSamePayloadAndReuseReceipt(existing,req);
    // Reserve the unique fork command first: any competing request locks/reuses its receipt.
    await tx.claimUniqueDelegationCommand(parentTask.id,req.commandId,canonicalHash(req));
    const scope=await authz.intersectParentAndChildCapabilities(parentTask,req.proposedToolRefs);
    const budget=await tx.reserveChildBudgetWithinParent(parentTask,req.maxCostMicroUsd);
    const child=await tx.createChildTaskWithImmutableGoal({
      parentTaskId:parentTask.id, tenantId:parentTask.tenantId,
      ownerUserId:parentTask.ownerUserId, conversationId:null,
      childGoal:deriveNarrowGoal(req), budgetReservationId:budget.id,
      scopeRefs:scope
    });
    await tx.addEventAndOutbox(child);
    return child;
  });
}
async function collect(parentClaim:RunClaim) {
  const children=await childRepo.listByParent(parentClaim.taskId);
  const manifests=await Promise.all(children.map(c=>evidenceRepo.readVerified(c)));
  const merged=dedupeBySourceHashAndObservationTime(manifests);
  await assertParentStillCurrentAndAuth(parentClaim);
  return resultVerifier.verifyParentWithChildEvidence(parentClaim,merged);
}
~~~

**実装順の注意:** 上の`lookupIdempotencyByParentAndCommand`と予算予約は同一txで行われるが、**既存の同一command receiptがあるなら先に照会して予約を増やさない**よう実装する。新規意図の一意キー確保と親Budget確保はロック順序を一定にし、同一keyの100同時請求で子一件/予約一件。子作成後にQueue失敗してもOutboxから再送できる。親のResource lock中に外部Tool/HTTPは行わない。

## 8. 障害/競合/再開マトリクス

| ケース | 検出と決定 | 外部効果 |
|---|---|---|
| 重複fork command | parent+commandId uniqueness、args hash比較 | 子Task1、予算予約1 |
| 別ownerの子Task試行 | trusted DB auth binding拒否 | 開始0 |
| 子Task作成後Queue喪失 | Outbox再配送、child Task再利用 | 二重子作成0 |
| 親Cancelと子create競合 | 同親行lockで線形化、cancel commit後create拒否 | 新子0 |
| 親Cancelと子Tool実行競合 | Gatewayがparent controlVersion & child epochを直前検査 | Cancel後新effect0 |
| 子Worker crash | 同子Runの新leaseEpochで復帰 | 既Tool Ledgerから回収 |
| 子Tool成功後結果保存前Crash | UNKNOWN_EFFECT、provider receipt照会 | 自動再実行禁止 |
| 子Steer中の親Goal変化 | goal lineage比較、旧proposal失効 | 古いTool起動禁止 |
| 子がBudgetを使い切る | 部分結果と未確認Evidenceを保存、親へ可視化 | 新Tool0 |
| 子が親へ嘘の成功 | ResultVerifierでhard constraint/Evidence検証 | Parent SUCCESS不可 |
| 兄弟からの私的State混入 | Context ACL/Projectionで拒否 | data leak 0 |
| Browser Handoff中の親取消 | User grant失効、Browser Workerへstop要求 | child fresh approval |
| 子のLate callback | child epoch + parent lineage CAS | 親Result上書き不可 |

## 9. Security / privacy / trust boundary

- 親のUser権限/owner/tenantは子へ渡す抽象的制約であり、User access token、Cookie、Secret値をコピーしない。DB上でfreshに本人/親子link/identityVersionを再検証する。
- 承認の子への伝播を既定で禁止。明示的なTool Approvalを特定actor/child Run/GoalRevision/tool/argument hashに結びつける。親が承認した別操作で予約確定を許さない。
- BrowserContext/Page/credentialはCHAT_04 Browser Workerだけが所有。子が同時に親Browserを直接操作することはない。Browser Session lock取得できなければ安全に待機。
- 親からの委任はPolicy/Egressが許可したToolのみ。Web/SNS内容の「子Agentを増やせ」命令は非信頼であり、実行権限を変更できない。
- Host Shell/SSH/Docker socket、無断購入/送金/予約/投稿、無制限の兄弟対話、Python Sidecarのprivileged accessは禁止。
- 子結果のPrompt injection/偽造Evidenceは親へ直接trusted instructionとして入れず、Provenance/ObservedAt付きのuntrusted dataである。

## 10. Finite Budget, Metrics & Performance

初期案（測定前の暫定制限）: 1親最大child8、深さ3、親の同時子実行最大4、各子maxModelTurns<=12、親のremaining costを下回る子reservation。子の予約総和+親自身の消費/未確定額が親Budgetを超えない。幅/深さの上限は階層全体に適用し、親が複数階層を作ることで無限に回避できない。親/子のModel requestのUsageは一意attemptIdで精算。

測るもの: delegation admission率、不要な子の発生率、parent/child finish latency p50/p95、child cancel propagation遅延、Budget重複予約、Evidence重複率、Source URL/observedAtの欠落率、parent GoalVerifier mismatch、Tool side effects重複回数。効果検証では同じ問題をMain Agent単独/子並列で共通Model/Token/Tool/Browser制限に揃え、事後に採否を判断する。マーケティング性能数字は流用しない。

## 11. Persistence, Transaction, Migration [PROVISIONAL: CHAT_10]

- `Task.parentTaskId`とrootを含む階層、TaskLinkのidempotency keyとparentTaskId、Goal snapshot、Budget reservation、Event/Outboxを同一トランザクションへ。前回で親Budget reserveした子を新たに重複起動しない。
- `Task.conversationId` nullable、`Task.activeRunId`が同Task Runのみを参照、`Task.goalRevision` immutable、`Run.leaseEpoch` bigint (安全整数範囲)、`recordVersion`初期1はCHAT_01/10へCross-team proposal済。
- `workspace_id`と`tenantId`のserver-verified mappingが必要、ownerUserIdを別Userに黙って変更しない。
- 子Taskの削除/停止で親Evidenceを削除しない（不変な履歴とSource refsを維持）。Child Completion/UnknownをOutboxで届け、Lost ACK/二重配送でもparent stateを二重確定しない。
- 初期schema versionからのMigrationは停機/expand-contractと旧Runの承認済みToolの審査を必要とする。UUIDv7と既存v4 defaultは混同せず、不正legacy IDを隔離して変換方針を所有者が決定。

## 12. UI / REST / Telegram [PROVISIONAL: CHAT_08]

Task詳細には親子TaskのGoal、状態（Queued/Running/Waiting/Partial/Uncertain/Done）、各子の消費上限と使用量を別に表示。Cancel操作は親のCommand受付と子の実際の停止を区別し、費用/外部効果がまだ調査中の場合は明示。ユーザーは子への権限付与と個別承認を把握できるが、CookieやTokenは閲覧不可。SSEはTask ID/parentTaskId/commit sequenceを含むredacted Eventを配信する。親子作業をHTTP request内に閉じず202 receiptで開始する。

## 13. 実装PRの分割

| PR | 依存と成果 |
|---|---|
| SUB-A | CHAT_01/10の親子リンク/UUIDv7/RecordVersion契約後、DelegationAdmission＋Depth/DAG validator |
| SUB-B | Auth/Policy/Tool cap交差判定、Budget親子予約を同一Tx |
| SUB-C | Idempotent子Task作成、Outbox dispatch、子Run独立claim/leaseEpoch |
| SUB-D | 親Cancel/Steer権限失効の伝播、BrowserSession lock/Handoff |
| SUB-E | Evidence manifestの子→親統合と親Verifier、部分/疑義状態 |
| SUB-F | Fault injection、adversarial multi-tenant、Mock Benchmark、migration compatibility |

## 14. 受入テスト（**全件Designのみ / NOT RUN**）

| TEST-ID | Setup/action→Expected |
|---|---|
| SUB-T01 | 独立性がない一連のfetch→子0、同一Agentで処理 |
| SUB-T02 | 子作成時他Tenant/ownerを注入→拒否、永続子0 |
| SUB-T03 | 子が親にないBrowser/Post権限を要求→拒否、Tool0 |
| SUB-T04 | 深度超/同時子過多/parent cycle→各拒否、既存Task不変 |
| SUB-T05 | 同一fork commandを100並列→子Task1・予算予約1・Event1 |
| SUB-T06 | 子のold leaseEpochでTool起動→新fenceが拒否、親のepochは影響なし |
| SUB-T07 | 親Cancel直後と子Tool開始競合→Cancel先commitなら開始0、既着手はUNKNOWN追跡 |
| SUB-T08 | private state中の別兄弟Secretから読出し試行→漏えい0 |
| SUB-T09 | 子結果にEvidence URLとobservedAt無し→親への確定結果に採用不可 |
| SUB-T10 | 子が成功と主張しGoal条件未達→親SUCCESS0 |
| SUB-T11 | 子の外部操作後kill→UNKNOWNで停止、再実行0 |
| SUB-T12 | 子に別UserのBrowser approvalを流用→拒否 |
| SUB-T13 | 並列子の異なる容量・価格・日付のEvidence merge→不一致拒否 |
| SUB-T99 | Main Agent単独/子3並列/子8並列の同一Mock Research100反復→p50/p95とUSD/taskとEvidence精度を記録し閾値を後から決定 |

## 15. REQ → TEST → SC

| MUST | TEST | Acceptance |
|---|---|---|
| SUB-001 | SUB-T01 | SC-05/09 |
| SUB-002 | SUB-T02 | SC-07/08 |
| SUB-003 | SUB-T03 | SC-08/09 |
| SUB-004 | SUB-T04 | SC-07/09 |
| SUB-005 | SUB-T05 | SC-07/09 |
| SUB-006 | SUB-T06 | SC-07/08 |
| SUB-007 | SUB-T07 | SC-07/08 |
| SUB-008 | SUB-T08 | SC-05/08 |
| SUB-009 | SUB-T09 | SC-01/04/05 |
| SUB-010 | SUB-T10 | SC-01/05 |
| SUB-011 | SUB-T11 | SC-07 |
| SUB-012 | SUB-T12 | SC-06/07/08 |
| SUB-013 | SUB-T13 | SC-03/05 |

## 16. 未決事項とCross-team proposals

| ID / owner | Severity | 必要な決定/テスト |
|---|---|---|
| SUB-P01 CHAT_01 | P0 | Task.parentTaskId、same-tenant/owner、childTask limitとGoalRevision、Run.parent lineageの共有契約。SUB-T02/T04/T06 |
| SUB-P02 CHAT_10 | P0 | child create+Budget reservation+receipt+outbox transaction、unique keys、同Task activeRun FK。SUB-T05 |
| SUB-P03 CHAT_09 | P0 | childのAuth identityVersion、Tool capability intersection、tenant↔workspace bindingとrevocation。SUB-T02/T03/T08 |
| SUB-P04 CHAT_03 | P0 | child Tool Gateway approval binding/side-effect UNKNOWN、parent cancelとchild dispatch linearisability。SUB-T07/T11/T12 |
| SUB-P05 CHAT_02 Budget & Manager | P0 | 子数/深度/parallel cap、親子budgetのatomic reservation/release、親Cancel propagation。SUB-T04/T05/T07 |
| SUB-P06 CHAT_04 | P1 | 既存BrowserSessionのlockとchild human takeover、Cookie隔離。SUB-T12 |
| SUB-P07 CHAT_08 | P1 | 親子Task Graph/UI/SSE、Cancel propagationとUncertain表示。SUB-T07/T11 |
| SUB-P08 CHAT_11 | P1 | 2+ replica parent/child kill test、予算予約/結果集約、跨tenant adversarial fixtures。SUB-T01〜T99 |

**重要な前提未確定:** PR #2のUUIDv7とDB PR #10のUUID生成、Task.conversationId nullableとDDL NOT NULL、recordVersion初期1とDB revision初期0、Run.leaseEpoch保存欠落、activeRunId/GoalRevision永続化、owner/tenant/workspace binding。単独判断でLockedにしない。

## 17. Sources

調査日すべて2026-10-30。GitHubのソース関数とテスト定義を読んだ（**自分でテストは実行していない**）。Code verifiedとTest definition verifiedはHarnessがImplementedである意味ではない。

- [S1] **Code verified** DeepAgents `subagents.py` `SubAgent,_build_task_tool,_validate_and_prepare_state`、SHA `3efa098130d6670fe713f841072bc11ff40545da`: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/subagents.py
- [S2] **Test definition verified** DeepAgents private-state and parallel sub-agent scenarios: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/test_subagents.py
- [S3] **Code verified** DeepAgents asynchronous child Task types/methods: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/async_subagents.py
- [S4] **Test definition verified** async launch/context/fork/recovery: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/test_async_subagents.py
- [S5] **Test definition verified** OpenAI Agents Python SDK child Tool delegation and config: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_agent_as_tool.py
- [S6] **Test definition verified** Agent-as-Tool output guardrail: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_agent_as_tool_output_guardrails.py
- [S7] **Code verified** Mastra SubAgent interface/isAgentCompatible: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/subagent.ts
- [S8] **Test definition verified** Mastra durable delegation/cold Worker history: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/durable/__tests__/durable-agent-delegation.test.ts
- [S9] **License verified** DeepAgents MIT: https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/LICENSE ; OpenAI Agents Python MIT: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/LICENSE ; Mastra Apache-2.0 declaration in @mastra/core package.json: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/package.json

**検証状態:** この仕様RepositoryにHarness本体コードはない。すべてのSUB-Txxテストは設計のみであり未実行。実行・実測・合格・Locked等の主張はしない。


### Round 4 cross-owner int64 reconciliation (2026-10-30)
Version/Epoch fields above follow CHAT_01's canonical signed-int64 **decimal string** wire format, validated and compared with internal BigInt and persisted as PostgreSQL BIGINT; JSON number conversion is prohibited. Other scalar number fields (bounded counts, percentages, step ordinals) remain number unless separately versioned. Regression test proposed: `9007199254740992` and `9223372036854775806` round-trip exactly, reject unsafe Number and stale authority; not executed against Harness code.
