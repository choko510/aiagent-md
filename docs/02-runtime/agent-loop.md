# Agent Loop — Detailed Implementation Specification

> Owner: CHAT_02 | Canonical path: docs/02-runtime/agent-loop.md | Status: **Draft / provisional** | Revision: 0.1 | Research date: 2026-10-30 | Base main: `92b5827ed16a7d4ae61e8146ce382795398293cb` | Dependencies: CHAT_01/03/09/10 contracts

## 0. 結論と採用案

**採用: DB-firstの決定的なStep Orchestrator + 交換可能なLLM Adapter + Tool Gatewayでの唯一の外部実行。** LLMは「次の行動を提案するだけ」。Agent Loopはステップ・制約・予算・承認状態を追跡し、外部効果を発生させるかはCHAT_03のGateway/Policy/Side-effect Ledgerが決める。非同期Workerの寿命とTask/Runの寿命は一致しない。Stepは必ず再構築可能な永続境界で停止し、UIのストリーミングは永続的な事実の表示を先取りしない。

**重要な非保証:** DB checkpoint、LLM outputの保存、idempotency keyだけでは、外部APIが受け付けた直後にWorkerが死ぬケースの「厳密なexactly-once外部操作」を保証できない。結果照会できない副作用は `UNCERTAIN_EFFECT` の暫定扱いで停止し、同じ操作の自動再発行は禁止する。これは公式OSSの一般的能力に対する追加安全要求である。根拠は[S1][S3][S5][S7][S10]。

**初期デフォルト案（確定値ではない）:** 1 Runあたり最大24 logical model turns、Tool提案60、同時read-only Tool最大4、同時side-effect Tool最大1、経過30分、費用のユーザー設定上限未設定時は安全な保守値（例USD 2.00）を必須とする。数値は運用測定とCHAT_02 budget module/CHAT_09 policyレビューで確定する。ユーザー上限が無制限を許す実装は禁止。長時間Watchは1回の無期限LLM Runでなく新しい有限Runを起動する。

設計状態は **Draft**。以下のローカル型・状態ラベルは他Ownerが未Lockedのため `[PROVISIONAL: CHAT_01]` または `[PROVISIONAL: CHAT_03]`。本書は共有Schema、DB DDL、HTTP APIの正本ではない。

## 1. 目的・責務・非責務・受入シナリオ

### 解決する問題
- 「検索→情報抽出→比較→確認」のような任意のWeb調査を、固定サイト・固定手順によらずLLMの複数ステップで実施する。
- Tool提案をモデル出力から分離し、schema/policy/approval/予算を通過して初めて実行できるようにする。
- 一時停止、承認待ち、Worker再起動、Steer、Cancel、期限・コスト上限時に、既に取得済みの証拠を失わず、二重副作用を起こさず、最後の確定点から続ける。
- LLM finalの文面だけで成功とせず、Goal条件・EvidenceとVerifierの確認でSUCCESS/partial/uncertainを区別する。

### 非責務
Task作成・lease運用（CHAT_02 task-run-manager）、Goal更新/plan再計算（同goal-and-planner）、Evidence内容の検証（同result-verifier）、checkpoint物理設計（同checkpoint-and-recovery）、Tool schema/policy/承認/Side-effect Ledger（CHAT_03）、共有型/イベント（CHAT_01）、認証/SSRF/秘密（CHAT_09）、DDL/Queue/Outbox（CHAT_10）、UI/SSE（CHAT_08）は各正本を参照。Loopはこれらの機能を独自再実装しない。

### 受入シナリオ
SC-01商品同一性と価格条件、SC-03 SNS出典/注入、SC-04ホテル人数・期間・予約承認、SC-05汎用調査、SC-06認証Handoff、SC-07 Kill/Steer/Cancel/二重配送、SC-08権限分離、SC-09費用/レイテンシー。SC-02はWatch起動の有限Runとして間接対応（定期評価はCHAT_07の決定的Worker）。

## 2. 既存OSS横断調査

調査は「最新mainの指定Commit」を固定してGitHub原本のコードとテストを読んだもの。ローカルでclone/build/testを行ったわけではない。パッケージ版は当該Commitのメタデータ表示であり、リリースタグとの同一性を保証しない。

| 対象 / snapshot | 実コード・テストで確認した機構 | 活用方針 / 不足 | License |
|---|---|---|---|
| LangGraph, main `ffc3336367e8139668d11e5f0b0e6def57e7c762`, pyproject `1.2.18` | `PregelLoop.tick/put_writes/_put_checkpoint`, `PregelRunner.tick` がtick単位の書込み・checkpoint・並列タスクを扱う。 `tests/test_pregel.py::test_interrupt_multiple` は複数interrupt/resumeを実証するテスト定義 [S1][S2] | Checkpointを分割する境界の参考。**Tool外部効果のexactly-once保証は採用しない**。Python Pregelを今回のTS runtimeへそのまま組み込まない | MIT [S11] |
| PydanticAI, main `c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9`, dynamic version | `process_tool_calls` は `early/graceful/exhaustive` と `sequential` barrier、deferred/unapproved Toolを区別。 `tests/test_run_cancellation.py` は取消し中の完了結果保持・履歴再開・取消し再主張のテストを定義 [S3][S4] | バッチ順序とCancel境界を参考。外部副作用は推論ライブラリへ直接持たせずGatewayへ移す。Python Sidecar利用時も制御はTS側 | MIT [S12] |
| OpenAI Agents Python SDK, main `cd26bbbc52847845fde73b9952b51e62e2bcbdc0`, pyproject `0.24.0` | `AgentRunner._run_impl` はTool後の `NextStepRunAgain`、`NextStepInterruption`、`NextStepFinalOutput` を分岐し、`RunState`へ承認中断を収める。`test_hitl_session_scenario.py` が承認/拒否/rehydrateを、`test_tool_approval_call_id_reuse.py` がcall_id再利用・リプレイを検査 [S5][S6] | HITLのインターフェース、再開の参考。provider call_id単体を承認鍵・side-effect idempotencyとみなす設計は却下 | MIT [S13] |
| Mastra core, main `ec3ba11dad428caeb3b4afae77d1669462e8c129`, package `1.81.0-alpha.0` | `run-lease-fence.ts::verifyRunLease/startRunStep` と `thread-lease-loss-abort.test.ts` は旧Runの新Tool発火禁止、既実行Step完了、レプリカ間lease喪失をテスト。 `tool-approval.ts::resolveDeclineReason` は拒否理由を保持 [S7][S8] | 外部操作直前のfence再確認・旧Owner停止を参考。in-process MapをDBのlease正本として採用しない | `@mastra/core` package.jsonはApache-2.0を宣言 [S14] |

**Issuesは報告であって検証済みバグ事実ではない。** LangGraph #9240/#8464はDurabilityと外部副作用exactly-onceの区別、OpenAI Agents Python #5413/#5409は承認のスコープや実行後保存前停止という故障モデルを提示する [S9][S10]。現行mainの再現結果とは断言しない。これらの反例を本HarnessのFault Injection Testに採用する。

### 採否の比較
1. **採用A: 自作の小さいdeterministic loop + Gateway** — 永続化/権限境界を全Toolで統一可能。実装責任とテスト工数は増すが、セルフホストTS+Python Sidecarと互換でProviderを切替可能。
2. **不採用B: 特定Frameworkのrunnerを唯一のRuntime正本としてそのまま使用** — Python実装への結合、Tool handlerの権限付与やcheckpointと外部効果の差、承認/再開のSchema差異が大きく、共通契約をFramework都合で変更しかねない。Adapter内の推論処理に利用することは可能。
3. **不採用C: LLMに全Toolを直結して毎回会話履歴だけ保存** — 承認バイパス、Crash後の重複、副作用の結果不明とBudget計測欠落に対処できない。
4. **不採用D: どのモデル応答も全Toolを最大並列実行** — 注文/予約/投稿など非可換な外部操作と承認待ちToolの混在で不可逆な部分実行が発生しやすい。read-onlyのみ上限付き並列化。

## 3. 依存関係・正本リンク・所有権

```text
UI / Telegram (CHAT_08) ─▶ Task/Run Manager (CHAT_02)
                                  │ acquired lease + fencing token
                                  ▼
                             AgentLoop (本書)
          ┌───────────┬───────────┼─────────────┐
          ▼           ▼           ▼             ▼
    Context/Goal   ModelRouter   Budget      ResultVerifier
       (02)           (02)       (02)           (02)
          └───────────┴───────────┼─────────────┘
                                  ▼ proposal
                     Tool Gateway (CHAT_03)
                   schema → policy → approval → budget
                                  ▼
                   Side-effect Ledger / Executor
                         (CHAT_03 + 10)
                          │           │
                Browser Worker     API/HTTP/MCP
                   (CHAT_04)       (CHAT_04/05)
```

| データ/機能 | Owner (唯一の正本) | Loopが使うもの |
|---|---|---|
| Task/Run/Conversation/Step?、State/Event/Error/Evidence/Artifact IDs | CHAT_01 `../01-contracts/*` | opaque IDsと契約版を取得。独自の共有型を公開しない |
| Job lease、heartbeat、claim、fencing、recovery | CHAT_02 `task-run-manager.md`, `checkpoint-and-recovery.md` | claimされたRunのみ処理しCAS/leaseを確認 |
| Tool registry、Tool call IO、policy/approval、side-effect ledger | CHAT_03 `../03-tools/*` | prepare/execute/reconcileの結果のみ取り込む |
| Browser Session | CHAT_04 | Gateway経由のsession参照のみ、Page/Context直接所有禁止 |
| 権限、ユーザ隔離、secrets、egress | CHAT_09 | actor/tenant/capabilityを検証へ渡す。LLMにCredential不可 |
| Transaction、checkpoint storage、queue/outbox、監査 | CHAT_10 | 複数表更新をtransaction unitへ依頼、具体DDLはここで定義しない |
| Stream UX | CHAT_08 | committed eventのみ確定表示、deltaはプレビューと識別 |

## 4. 要求定義

以下のMUSTは実装の必須条件。内部初期数値のみProvisional。

| REQ-ID | 優先 | 要求 / 観測可能な達成条件 | SC |
|---|---|---|---|
| LOOP-001 | MUST | 同一run/stepは一意なownership token/CASで開始・コミット。旧Workerからの書込み/新Tool開始を拒否 | 07 |
| LOOP-002 | MUST | Model応答のTool提案は全部保存・正規化後、Gatewayへ渡す。ModelはExecutorを直接呼べない | 07,08 |
| LOOP-003 | MUST | Schema/Policy/Approval/Budgetがすべて成功するまで当該Toolを開始しない。失敗はfail closed | 04,08 |
| LOOP-004 | MUST | Tool intent永続化成功より前に外部Executorを呼ばない。再配達時は同じ論理呼出しを識別 | 07 |
| LOOP-005 | MUST | 外部副作用の結果が不明なら自動再実行せずUNCERTAINとして照会または明示的回復待ち | 07 |
| LOOP-006 | MUST | 承認待ちは既存Step/Tool引数を保持してRun停止。再開時にactor/スコープ/期限/引数hashを再検証 | 04,06,07 |
| LOOP-007 | MUST | Cancel/Steer/Pauseは安全境界で適用。Cancel観測後、新しいLLM/Toolをdispatchしない | 07 |
| LOOP-008 | MUST | 完了候補はGoal制約/根拠/時刻をVerifierが判定し、未検証成功を通知しない | 01,03,04,05 |
| LOOP-009 | MUST | 有限steps/time/tokens/cost/tool calls。未計測の課金を無制限に続行しない | 09 |
| LOOP-010 | MUST | LLM streamingはtentativeとdurable eventsを分け、再接続時に重複確定表示しない | 05,07 |
| LOOP-011 | MUST | 同一Step内のTool call順序と全結果/欠損を記録。write系Toolは直列化 | 07 |
| LOOP-012 | MUST | Crash後はDB正本から状態を再構築し、既存Toolの再照会・未確認扱いを経て再推論 | 07 |
| LOOP-013 | MUST | HTML/SNS/Tool結果の指示はuntrusted dataのまま。secretと権限はpromptに渡さない | 03,08 |
| LOOP-014 | MUST | Provider error、malformed output、拒否、timeoutを分類して有限再試行/partial終了 | 05,07,09 |
| LOOP-015 | MUST | append-only traceは順序とrevisionを識別し、古いstream callbackはRun状態を上書きしない | 07 |
| LOOP-016 | MUST | 既存Evidenceの観測時刻とGoal revisionを保持し、条件変更後に古いEvidenceで成功判定しない | 01,04,07 |
| LOOP-017 | SHOULD | read-onlyを最大4件並列化でき、実測に基づき上限を調整可能 | 05,09 |
| LOOP-018 | SHOULD | LLM/Toolの再試行理由、費用・遅延の内訳を秘匿した監査記録で表示可能 | 07,09 |
| LOOP-019 | LATER | 複数独立調査の子Runへ安全委任 (CHAT_02 subagent-delegation) | 05 |
| LOOP-020 | EXPERIMENT | Provider streaming途中の損失を縮める追加checkpoint。確定前Tool dispatchは禁止 | 07,09 |

## 5. 共通契約・型・API

**以下はLoop内部で必要とするPortの提案であり、共有JSON schemaではない。** 名前/enumは `[PROVISIONAL: CHAT_01]`; Gateway request/response は `[PROVISIONAL: CHAT_03]`。実装時は共通契約にmapperを置く。

```ts
// Logical types only. Exported shared shapes require CHAT_01 decision.
type OpaqueId = string;
type Revision = number;
type LeaseEpoch = number; // [PROVISIONAL: CHAT_01] safe integer, DB bigint range-check before conversion

interface LoopIdentity {
  tenantId: OpaqueId; // server-resolved tenant binding
  actorId: OpaqueId; // fresh ownerUserId; queue/prompt values are not authority
  taskId: OpaqueId;
  runId: OpaqueId;
  goalRevision: Revision;
  leaseEpoch: LeaseEpoch; // canonical Run.leaseEpoch, NOT separate runGeneration
  expectedTaskRecordVersion: number; // recordVersion is CAS counter, initially 1
}

interface LoopPorts {
  runManager: {
    assertCurrent(id: LoopIdentity): Promise<void>;
    controlSnapshot(id: LoopIdentity): Promise<
      {cancel: boolean; pause: boolean; steerRevision?: Revision; deadlineAt: string}>;
    suspend(id: LoopIdentity, reason: string, payloadRef?: OpaqueId): Promise<void>;
  };
  checkpoint: {
    load(id: LoopIdentity): Promise<PersistedLoopSnapshot>;
    commitModelStep(id: LoopIdentity, expectedVersion: number,
      fullModelOutputRef: OpaqueId, normalizedCallRefs: OpaqueId[],
      usage: UsageReceipt, eventRefs: OpaqueId[]): Promise<number>;
    commitToolOutcome(id: LoopIdentity, expectedVersion: number,
      logicalCallId: OpaqueId, gatewayReceiptRef: OpaqueId): Promise<number>;
    commitTerminal(id: LoopIdentity, expectedVersion: number,
      verifiedResultRef: OpaqueId, terminalKind: string): Promise<void>;
  };
  model: {
    generate(input: BoundedModelInput,
      onPreview: (delta: UntrustedModelDelta) => void,
      abortSignal: AbortSignal): Promise<ModelReplyWithUsage>;
  };
  tools: {
    prepare(id: LoopIdentity, frozen: FrozenToolProposal):
      Promise<GatewayPrepareReceipt>;  // durable intent+policy+approval gate
    execute(id: LoopIdentity, receipt: GatewayPrepareReceipt):
      Promise<GatewayOutcome>;         // Gateway owns execution + side effect ledger
    reconcile(id: LoopIdentity, logicalCallId: OpaqueId):
      Promise<GatewayOutcome | "UNRESOLVED">;
  };
  verifier: {
    verify(id: LoopIdentity, candidateRef: OpaqueId):
      Promise<"ACCEPTED"|"NEED_MORE_EVIDENCE"|"PARTIAL"|"UNCERTAIN"|"REJECTED">;
  };
  budget: { reserveBeforeModel(id: LoopIdentity): Promise<void>;
            settleModel(id: LoopIdentity, receipt: UsageReceipt): Promise<void>;
            assertToolAllowance(id: LoopIdentity): Promise<void>; };
  stream: { publishTentative(delta: UntrustedModelDelta, attemptId: string): void; };
}

interface FrozenToolProposal {
  runId: OpaqueId;
  stepOrdinal: number;
  proposalOrdinal: number;
  logicalCallId: OpaqueId;
  providerCallId?: string;          // diagnostic only; NOT authority/idempotency key
  toolName: string;
  schemaVersion: string;
  canonicalArgumentsHash: string;
  argumentsArtifactRef: OpaqueId;   // full args retained in access-controlled store
  goalRevision: Revision;
}
```

**ID/validation invariants:**
1. `logicalCallId` はRun+Step ordinal+提案ordinalから安定決定し、再送時も同じ。providerのcall_idが衝突・欠落しても別Runを相互承認しない。追加のcanonical arguments hashとtool schema versionで完全性を確認。計算方式/DB unique keyはCHAT_01/03/10に提案。
2. Modelの応答は構文schema検証、unknown tool拒否、argument size/深さ上限、call数上限を適用。unknown fieldの扱いはtool schemaに一致させる。拒否データをmodelへ返す場合もcredentialを含めない。
3. `GatewayPrepareReceipt` は `READY/WAITING_APPROVAL/DENIED/INVALID/OVER_BUDGET` の概念を表現。承認は特定actor/tenant/run/leaseEpoch/goalRevision/tool/args hash/有効期限に結び、検証・消費が原子的であることをOwnerへ要求する。
4. Event envelopeと永続化はCHAT_01の`event-envelope-and-stream.md`に委譲。Tool intentとeffect ledgerの正本はCHAT_03。Outboxを持たない直接SSE送信は正本にしない。

### 例: LLMの2件Tool提案（*内部例であり共有JSON契約ではない*）

```json
{
  "runId": "run-123", "stepOrdinal": 3, "goalRevision": "2",
  "proposals": [
    {"proposalOrdinal": 0, "toolName": "web.search", "argumentsArtifactRef": "art-100", "mode": "read"},
    {"proposalOrdinal": 1, "toolName": "hotel.book", "argumentsArtifactRef": "art-101", "mode": "effect"}
  ]
}
```

このケースは **全Toolのprepare結果を確定してからdispatch**。予約がWAITING_APPROVALならread Toolの先行実行も既定では行わずStepをsuspendする。再開時も元引数を凍結して承認し、Goal条件が変わっていれば旧予約提案を失効させモデルへ再計画を要求。自由な同一Step内再選択は禁止。

## 6. 状態遷移・sequence・ライフサイクル

**論理Step状態（共有enumではない）**:

```text
READY
  ├─ claim/fence ─▶ MODEL_RESERVED ─▶ MODEL_IN_FLIGHT
  │                                     ├─ model failure ─▶ RETRY_DELAY / PARTIAL
  │                                     └─ full response committed ─▶ PROPOSALS_STORED
  │                                         ├─ final-only ─▶ VERIFY
  │                                         └─ calls ─▶ GATEWAY_PREPARE
  │                                           ├─ approval missing ─▶ WAITING_APPROVAL
  │                                           ├─ denial/invalid ─▶ TOOL_FEEDBACK_READY
  │                                           └─ all ready ─▶ TOOL_EXECUTE
  │                                              ├─ confirmed outputs ─▶ TOOL_FEEDBACK_READY
  │                                              └─ uncertain effect ─▶ WAITING_RECONCILIATION
  └─ before next model boundary ◀─ TOOL_FEEDBACK_READY (new Step)
VERIFY ─▶ COMPLETE / PARTIAL / NEED_MORE_EVIDENCE (new Step)
any safe boundary ─▶ PAUSED / CANCELLED / BUDGET_EXHAUSTED / TIMED_OUT
```

`WAITING_APPROVAL`, `WAITING_RECONCILIATION` はWorkerを占有しない永続待機。Run全体の正式State遷移はCHAT_01の`task-run-state-machines.md`が所有する。Lease切れの旧Workerは必ず停止し、**既にExecutorに渡った操作の物理キャンセル完了までは約束しない**。

### 正常系シーケンス
1. RunManagerがDBの現行Runをclaimし、canonical Run.leaseEpochをLoopへ渡す。Task.activeRunIdとRun.taskId、owner binding、goal revisionを同時に検証する。
2. Loopは永続snapshot+goal revisionを読込、cancel/pause/steer/期限/コストを確認する。
3. ContextManagerが最小限のtrusted constraints、観測時刻付きEvidence参照、Tool schemaを構築。Budgetで次のLLM callをreserve。
4. ModelRouter/Model Portを呼ぶ。stream tokenはtentativeなプレビューとして配信できるが、Tool callは部分streamから絶対実行しない。
5. full responseとusageを検証。raw response artifact、canonical tool proposals、step cursor、usage receipt、Event outboxを同一論理commit（CAS/fence）として保存。
6. Tool無しならVerifierへfinal候補を渡す。Toolありなら全提案をfreeze→Gateway.prepare。準備はintent永続化とPolicy/Approval判定を含む。
7. 少なくとも1件WAITING_APPROVALならrunを待機し**全てのToolをまだexecuteしない**。承認後は同じproposal群を再読込しactor/revision/hashを照合。
8. 全件READYならREAD-onlyは上限付き並列、side-effectと順序依存Toolは直列。実行直前にRunManager fence・controlを再確認しGatewayも自身の正本でfence・policyを再確認。各outcomeをDBへ保存。
9. Result未確定のToolがあればreconcile。安全判定ができない副作用は新Stepへ進めずUNCERTAIN待機。確定結果（失敗含む）だけを次のModel promptへ返す。
10. 各Tool feedback完了後にboundary controlを確認し、次のStepへ。最終回答はVerifierで制約/根拠を判定しterminal snapshot/outboxをcommitして初めて成功表示。

### 異常系の特別シーケンス
- **Lost acknowledgment**: Gatewayが結果を保存済みでLoopがreceiptを失ったら、`reconcile(logicalCallId)` で保存済み結果を回収。Executorを直接再呼出ししない。
- **Write-after-effect**: Executorが副作用成功→結果DB書込前に死亡。Gatewayがprovider idempotency key/照会APIで結果を確定できれば記録、不可ならUNCERTAIN・要手動判定。操作の再発行はしない。
- **Steer**: 旧revisionに対する提案を停止し、新Goalで新Step。既存観測は出典・観測時刻付きで残すが、旧目標の成功条件へ自動流用しない。
- **Cancel in-flight**: 未実行の提案は取消し、実行中read呼出しはAbortを試みる。実行中side-effectは不確定としてGateway照会対象。キャンセル受理以後、新規LLM/tool dispatchを禁止。
- **Human Handoff**: ログイン/challenge時はBrowser Workerの状態を保存しWAITING_INPUT。BrowserSessionをLoopへ直接移管せず、認証データをモデルへ送らない。

## 7. 内部アルゴリズム・擬似コード

設計疑似コード。下位Portは正本Ownerが実装し、各transactionは短命。**ネットワーク待ち中にDB transaction/row lockを持たない。**

```ts
async function executeRun(identity: LoopIdentity, ports: LoopPorts): Promise<void> {
  while (true) {
    await ports.runManager.assertCurrent(identity);
    const s = await ports.checkpoint.load(identity);
    const control = await ports.runManager.controlSnapshot(identity);

    if (control.cancel) return suspendAs("CANCEL_REQUESTED");
    if (control.pause) return suspendAs("PAUSED");
    if (control.steerRevision && control.steerRevision > identity.goalRevision)
      return suspendAs("REPLAN_REQUIRED");
    if (deadlineOrFiniteLimitReached(s, control)) return finalizePartial("LIMIT");

    // Critical: Resume the persisted phase. Never regenerate if the full
    // model response/proposals for this step were already committed.
    if (s.phase === "WAITING_APPROVAL") {
      const decision = await revalidateBoundApprovalAndRevision(s, identity);
      if (decision === "WAIT") return;
      if (decision === "STALE") return suspendAs("REPLAN_REQUIRED");
    } else if (s.phase === "TOOL_EXECUTE" || s.phase === "WAITING_RECONCILIATION") {
      // Reconstruct from ledger. COMPLETED/FAILED: reuse receipt; NOT_STARTED:
      // dispatch later; IN_FLIGHT/UNKNOWN effect: reconcile, NEVER blind retry.
      const calls = await classifyCallsFromGatewayLedger(s, identity);
      if (calls.hasUnknownEffect && !await reconcileUnknownEffectsWithoutReexecution(calls))
        return suspendAs("WAITING_RECONCILIATION");
    } else if (!s.hasCommittedModelResponse) {
      await ports.budget.reserveBeforeModel(identity);
      const request = await buildBoundedTrustedContext(s, identity);
      const attemptId = createEphemeralAttemptId();
      const response = await ports.model.generate(
        request,
        delta => ports.stream.publishTentative(delta, attemptId),
        getCancellationSignal(identity)
      );
      await ports.runManager.assertCurrent(identity);
      const normalized = parseAndBoundEntireResponse(response);
      await ports.checkpoint.commitModelStep(identity, s.version,
        normalized.fullResponseRef, normalized.proposalRefs,
        response.usage, normalized.eventRefs);
      await ports.budget.settleModel(identity, response.usage);
      continue; // re-read durable snapshot; do not dispatch from local uncommitted data
    }

    const fresh = await ports.checkpoint.load(identity);
    if (fresh.hasFinalCandidate && fresh.proposals.length === 0) {
      const verdict = await ports.verifier.verify(identity, fresh.finalCandidateRef);
      if (verdict === "NEED_MORE_EVIDENCE") {
        await persistVerifierFeedbackAndAdvanceStep(identity, fresh);
        continue;
      }
      await ports.checkpoint.commitTerminal(identity, fresh.version,
        fresh.finalCandidateRef, mapVerdictToTerminal(verdict));
      return;
    }
    const prepared = [];
    for (const p of fresh.proposals) {
      if (await gatewayHasTerminalReceipt(p.logicalCallId)) continue;
      await ports.runManager.assertCurrent(identity);
      await ports.budget.assertToolAllowance(identity);
      prepared.push(await ports.tools.prepare(identity, p)); // idempotent intent
    }
    if (prepared.some(p => p.needsHumanApproval)) {
      await ports.runManager.suspend(identity, "WAITING_APPROVAL");
      return; // no tool execution yet
    }
    if (prepared.some(p => p.deniedOrInvalid)) {
      await persistSafeToolErrorsAndAdvance(identity, prepared);
      continue;
    }
    for (const group of planBoundedExecutions(prepared)) { // NOT_STARTED only; reads parallel, effects sequential
      await ports.runManager.assertCurrent(identity);
      if (await isCancelledOrRevisionChanged(identity)) return suspendAs("INTERRUPTED");
      const outcomes = await executeWithGatewayFenceAndTimeout(group, ports.tools, identity);
      for (const o of outcomes) {
        if (o.effectUncertain) {
          await persistUncertainAndSuspend(identity, o);
          return;
        }
        await commitOutcomeWithCAS(identity, o);
      }
    }
    await persistToolFeedbackAndAdvanceStep(identity);
  }
}
```

**擬似コードの依存上の留保:** `suspendAs`、`commitOutcomeWithCAS`、`executeWithGatewayFenceAndTimeout`、`persistVerifierFeedbackAndAdvanceStep`、`deadlineOrFiniteLimitReached` は本書のアルゴリズム上の処理単位を示すもので、存在するSDK APIを意味しない。実コードではCHAT_01のState Machine/CHAT_03のLedger/CHAT_10のtransaction APIに実装・マッピングする。モデル料金reserveとsettle間でWorkerが死ぬ場合、reservedを即時返金せずUsage照会・保守的推定・明示的会計状態とする。T2で保存したUsageReceiptは一意なattemptIdで会計に反映し、再開でsettleを再実行しても二重計上しない。NOT_STARTEDはGatewayが初回dispatchできるが、UNKNOWN_EFFECTは結果照会のみ許可する。

### Tool groupの決定
- `read`: 副作用なし・同一Sessionの書換競合なし・Gatewayがreadonlyと判定したToolだけ並列（初期最大4）。
- `effect` または `unknown`: 直列。Side-effect classificationはLLMの自己申告ではなくRegistry/Policyに従う。
- Browserの同一Page/Sessionに対する操作は、たとえreadとして宣言されていてもBrowser Workerの所有/lock規約に従い直列化し得る。
- Toolのエラーを黙って成功に変換せず、recoverable feedback / policy denial / terminal failureを分離。
- 同じ応答にfinal textと未処理Toolが混在するとき、finalとしてcommitしない。Tool settlement後に再評価する。LLMによるTool呼出しゼロでもVerifierが証拠不足なら成功ではない。

## 8. 障害マトリクス

| 事象 | 検出 / 永続状態 | 自動リトライ | 復旧・通知 |
|---|---|---|---|
| Model HTTP 429/503 | error category, attempt count, backoff deadline | 有限回、budget reserve前提 | circuit breaker/代替Model、上限ならPARTIAL |
| Model responseの途中でdisconnect | full response未commit、previewはtentative | 新logical attempt可（重複課金は計上） | 同一Stepで新応答、Tool実行なし |
| Malformed tool JSON/unknown Tool | proposal validation failure | 同一結果を再実行せず有限LLM feedback | 安全な修正要求またはPARTIAL |
| Model response保存前Worker死亡 | Model応答は未確定、Tool未実行 | Model再要求は可 | 推定費用を保持 |
| Tool prepare intent保存前死亡 | Gatewayは実行前であることを保証 | 再prepare可 | 同一logicalCallId |
| Tool intent保存後/Executor開始前死亡 | ledger READY/NOT_STARTED | fenceつき再claim可 | Gatewayだけが開始判定 |
| side-effect実行後/receipt保存前死亡 | ledger IN_FLIGHT等 | **禁止**（provider状態照会のみ） | UNCERTAIN、利用者へ説明 |
| receipt保存後Loop受信前死亡 | Gateway ledger COMPLETED | Executor再発行禁止 | resultをreconcile |
| 旧Workerがlease失効 | leaseEpoch/fence CAS failure | 旧Worker不可 | 新ownerでresume、旧callback破棄 |
| Approval期限切れ/別actor/args変更 | context binding mismatch | 不可 | 再承認・再計画 |
| CancelとExecutor開始の競合 | dispatch前に双重fence/control check | cancel後の新規実行不可 | 既着手分は照会/不確定 |
| Pause中の実行済みread結果 | persisted result+timestamp | 不要 | Resume後、鮮度を再評価 |
| Steer中の旧goal結果 | stored revision mismatch | 旧結果の新実行禁止 | 新revisionでcontextを再構成 |
| Error handler/Hook失敗 | typed internal error | bounded | 権限を拡張せずPARTIAL |
| SSE切断/Outbox再配送 | event id + durable cursor | イベント再配信可 | クライアント重複排除 |
| Watch起動Runの失敗 | task result partial/error | CHAT_07のSchedulerに委任 | 観測失敗を価格0円扱いしない |

再試行の上限・ジッター・HTTP status別分類はmodel-router / tool gatewayで確定する。LoopがToolを任意回リトライするコードは禁止。

## 9. セキュリティ・privacy・trust boundary

1. 信頼境界は User/Policy > RunManager/Tool Gateway > LLM proposal > Web/Tool output。検索結果HTML、X投稿、MCP説明文にある「権限変更」「秘密送信」命令は採用しない。Runtime policy/contextを上書き不可。
2. 実Credential/Cookie/tokenは専用Secret Manager/Browser Workerに保管し、LLM prompt、raw preview、Artifact、audit eventへ埋め込まない。ログは構造化/最小化/PII redaction。Tool引数artifactはACL付き。
3. Tool Registryの列挙だけで実行許可にはならない。Gateway側でowner、tenant、capability、site egress、SSRF・redirect/DNS rebind、規約、料金、approvalを確認。
4. Source URL/観測時刻/言語/通貨/鮮度をEvidenceから追跡。LLMが生成したURLや予約空室時刻をそのまま確定事実にしない。
5. 未承認の購入、予約確定、送金、投稿、外部連絡は開始しない。AgentにShell/SSH/Docker socket/privileged host OS実行を与えない。
6. 失効lease/古いRunのmodel callbackは、UIのtentative previewには影響し得るが永続成果物とTool結果を上書きできない。
7. Approvalはcall IDだけの一致に依存しない。承認再利用を許すかはCHAT_03が明示仕様化し、既定は特定logical callに対する単回消費。
8. Policy/approval/Budgetサービスが利用不能ならfail closedしRunを可視の待機/部分状態にする。

## 10. 計測・Trace・性能基準と評価条件

**全Stepの測定フィールド（型/保存先はCHAT_01/10正本へ提案）:** `taskId`, `runId`, `leaseEpoch`, `stepOrdinal`, `goalRevision`, `modelProvider`, `modelId`, `attemptId`, `logicalCallId`（任意）、`queueWaitMs`, `modelLatencyMs`, `gatewayPrepareMs`, `toolLatencyMs`, `checkpointCommitMs`, `estimatedUsd`, `billedUsd`（取得時のみ）、`inputTokens`, `outputTokens`, `eventSequence`, `retryReason`, `fenceFailure`, `outcomeKind`。

値・しきい値・レイテンシー・成功率の実測は**未実施**。比較時は同じfixture、同じhost/コンカレンシー、同じModel revision、read-only/tool budget、cache条件で10回以上試験し、p50/p95、失敗率、費用/Task、Evidenceの条件合致率、二重副作用回数を取得する。優先判定は **無承認副作用0 / 二重side-effect0（試験fixture） / 旧fenceからのTool新規開始0 / 制約違反成功0**。これらは計画上の合格値であり達成実績ではない。LangGraph等の外部benchを本Harnessの値として転記しない。

## 11. 永続化・Transaction・Migration提案

CHAT_10へ要求するlogical transaction boundaries:
- **T1 run claim**: canonical run leaseEpochの原子的claim; 同一runに複数active ownerを許さない。
- **T2 model step commit**: full raw response artifact参照、normalized proposal一覧、step cursor、token/費用receipt、durable event/outboxを一貫して保存。CAS/fence failure時の破棄。
- **T3 gateway prepare**: logicalCallId一意のintent、args hash、分類、承認参照、policy versionをcommitしてからexecutorを起動。
- **T4 tool completion**: ledger terminal outcome、artifact/evidence refs、outboxの原子commit。外部APIそのものとは原子transactionにならない。Executor発火直前にはGatewayが現行fenceとcancel/control版を検査し、dispatch claimの線形化ポイントを定義する。
- **T5 next step/terminal**: Tool feedbackとcursor、revised constraintsの整合性を確保。terminalと成果物/eventを一緒に保存。
- **T6 outbox**: ack/replay可能なイベントを配信。SSE/Telegram配信成功とDB commitを混同しない。

**Migration/compatibility:** snapshot/Tool receiptにschemaVersionを付け、旧schema read adapter、immutable run history、rollback可能な段階デプロイを要求。migration中in-flight effectはunsafe retry不可。旧Workerと新Workerが同時動作するときはProtocol version/fence incompatibilityを検知し停止。スキーマDDLやunique index構造は本書では作らない。

## 12. UI・外部APIへの影響

CHAT_08への提案: StepごとのProgress `queued/model_preview/model_committed/approval_wait/tool_running/reconcile_wait/verifying/partial/finished` を区別し、**previewと確定Resultの視覚的意味**を分ける。承認画面にはtool、対象、影響、金額、実行先、期限、凍結引数の安全な概要を出すがsecretは表示しない。Watch登録はDBにWatch IDが発行されるまで「登録済み」と表示禁止。ブラウザHandoffはBrowserSession ownershipを維持。Resume APIには単純な「再実行」ではなくRunID、revision、expected version/approval tokenの整合検証を要求。イベントの重複排除とgap recoveryを設計する。

ユーザー可視の停止表現: `Needs approval`、`Waiting for login`、`Partial (budget/time)`、`Uncertain (external result cannot be confirmed)`、`Cancelled (some in-flight effect may need reconciliation)` を明確に分ける。正式enum名はCHAT_01。

## 13. 実装フェーズ・依存PR順序

| Phase | Implementable deliverable | 依存 / Gate |
|---|---|---|
| P0 Contracts | Task/Run/Step/Evidence/Error/Event shared contracts、GoalRevisionとlogicalCall identity合意 | CHAT_01、CHAT_00、CHAT_03 |
| P1 Deterministic loop skeleton | FakeModel/FakeGateway、Step遷移、checkpointとCAS、bounded budgets。副作用ゼロ | CHAT_02/10、TEST-001〜007 |
| P2 Gateway bridge | freeze/prepare/approval/serial effects、reconcile/UNCERTAIN、fencing | CHAT_03/09、TEST-008〜015 |
| P3 Resume/Steer/Streaming | old callback排除、pause/cancel、draft preview vs durable、replay | CHAT_01/08/10、TEST-016〜021 |
| P4 Verifier/Research scenarios | SC-01/03/04/05の根拠・鮮度・partial、Watch起動 | CHAT_02 verifier + CHAT_04/05/07、TEST-022〜025 |
| P5 Fault/Benchmark | replica kill、DB障害、adversarial、コスト実測と移行 | CHAT_11、TEST-026〜030 |

テストIDの内容を本書14章に定義する。P0が未Lockedの間、後続実装はfeature-flagと暫定adapterで分離し、正式版をVerified扱いしない。

## 14. テスト計画（すべて **Designed / Not run**）

各TestはMock/controlled fixture限定。実EC購入、予約、送金、X投稿、Watch登録通知を実サービスで無断実行しない。

| TEST-ID | 種別 / Setup → Action | Expected / 合格条件 |
|---|---|---|
| LOOP-TEST-001 | Unit: fake modelでfinal-only | full reply保存→Verifier ACCEPTEDの後にだけterminal 1件 |
| LOOP-TEST-002 | Unit: JSON不正/未知Tool | Gateway.execute 0、有限修正feedbackまたはPARTIAL |
| LOOP-TEST-003 | Contract: 同じStepのmodel response commitを2 Workerで競合 | CAS成功1、旧fence commit 0 |
| LOOP-TEST-004 | Integration: 途中streamでTool callだけ見える | full response commit前Gateway.prepare/executeとも0 |
| LOOP-TEST-005 | Unit: steps/cost/timeout/token limitを各1つ超える | 次のLLM 0、上限理由つきPARTIAL/待機 |
| LOOP-TEST-006 | Unit: read 5件＋effect 2件、maxRead4 | read同時4以下、effect同時1、proposal順序/結果保存 |
| LOOP-TEST-007 | Contract: Toolなしの最終文面が根拠不足 | 成功0、Verifier NEED_MORE_EVIDENCE またはPARTIAL |
| LOOP-TEST-008 | Integration: approval必須の購入Tool＋readTool | 承認前execute全0、承認後に両Toolのfreeze args不変 |
| LOOP-TEST-009 | Security: 別actor/tenant/runのcall_id再利用承認 | 承認無効、effect 0 |
| LOOP-TEST-010 | Security: approval後のargs hash/revision変更・期限切れ | 承認失効、effect 0、再承認/再計画 |
| LOOP-TEST-011 | Fault: DB intent writeを強制失敗 | 外部API呼出し0 |
| LOOP-TEST-012 | Fault: 外部副作用成功後result writeでkill | GatewayはUNCERTAINまたはprovider照会、同一副作用再発行0 |
| LOOP-TEST-013 | Fault: ledger完了後Loopへack喪失 | replayから保存済み結果回収、Executor実行回数1 |
| LOOP-TEST-014 | Fault: 承認保存後Runner再起動 | 凍結提案のみ再開、重複承認/重複Tool 0 |
| LOOP-TEST-015 | Integration: Tool GatewayがPolicy/secret service失敗 | fail closed、effect 0、監査記録が残る |
| LOOP-TEST-016 | Concurrency: lease譲渡直後に旧callbackが到着 | DB/stream durable cursor変更0、新Tool開始0 |
| LOOP-TEST-017 | Fault: CancelをTool dispatch前にCAS競合 | 新規Tool 0、既着手は未確認と識別 |
| LOOP-TEST-018 | Unit: Pause/resumeでLLMが既にcommit済み | 同一Stepのモデル呼び直し0 |
| LOOP-TEST-019 | Unit: Steerで日時/人数/駐車場条件更新 | 旧goalでの完了0、revision更新後にVerifier判定 |
| LOOP-TEST-020 | Integration: SSE切断とoutbox二重配送 | committed event重複表示0、gap後リプレイ可 |
| LOOP-TEST-021 | Fault: model streaming中kill（未commit） | side-effect 0、tentative破棄、再試行時の費用を追跡 |
| LOOP-TEST-022 | E2E fixture SC-01: 型番/容量不一致とクーポン条件 | 同一商品誤同定0、見積/実測・時刻分離 |
| LOOP-TEST-023 | E2E fixture SC-04: 3名/総額/駐車場 | 不一致候補SUCCESS 0、予約実行0 |
| LOOP-TEST-024 | E2E fixture SC-03/05: 注入された検索結果 | policy変更0、原典/観測時刻保持 |
| LOOP-TEST-025 | E2E fixture SC-06/02: ログイン要求/Watch Task | WAITING_INPUT復帰; Watch jobは有限Run、取得失敗を価格0としない |
| LOOP-TEST-026 | Fault: Worker killをT1〜T6全境界で網羅 | commit/回復が分類でき、盲目的side-effect retry 0 |
| LOOP-TEST-027 | Fault: DB outage / lease backend silent | 新規effect 0、保守的停止、復旧後fenced resume |
| LOOP-TEST-028 | Security: SSRF redirect/private IP/evil MCP | Gateway/egressが遮断、secret prompt/log露出0 |
| LOOP-TEST-029 | Benchmark: 同fixtureでAPI/HTTP/Browser比率、p50/p95、USD | 原環境/モデル版/測定条件を記録、採否を実測で決定 |
| LOOP-TEST-030 | Migration: snapshot旧versionと旧Worker混在 | unsupported schemaは安全停止、old callback不受理 |

**実施状況:** 上記は仕様上のテストケースを設計したもの。Harness本体はこのRepositoryに存在せず、テスト実行・測定・passの主張はしない。参照OSSのテストファイルは読み取ったが、当方で実行はしていない。

## 15. Acceptance Criteria / Traceability

| MUST REQ | 対応TEST | 受入SC | 合格判定 |
|---|---|---|---|
| LOOP-001 | 003,016,027,030 | SC-07 | 旧fenceの永続commit/新Tool開始0 |
| LOOP-002 | 002,004,011 | SC-07,08 | モデル→Executor直結0 |
| LOOP-003 | 008,009,010,015,028 | SC-04,08 | 無承認副作用0 |
| LOOP-004 | 011,013,026 | SC-07 | intentなしのExecutor開始0 |
| LOOP-005 | 012,013,026 | SC-07 | unknown effect盲目的再発行0 |
| LOOP-006 | 008,009,010,014 | SC-04,06,07 | 再開でもactor/hash/revision拘束 |
| LOOP-007 | 016,017,018,019 | SC-07 | Cancel後の新Tool/LLM開始0 |
| LOOP-008 | 001,007,019,022,023,024 | SC-01,03,04,05 | 根拠不足のSUCCESS 0 |
| LOOP-009 | 005,021,029 | SC-09 | 上限後追加課金依頼なし |
| LOOP-010 | 004,020,021 | SC-05,07 | previewとcommit混同0 |
| LOOP-011 | 006,012,013 | SC-07 | write並列0、結果欠損なし |
| LOOP-012 | 012,013,014,016,026 | SC-07 | crash時の状態分類可能 |
| LOOP-013 | 015,024,028 | SC-03,08 | 注入から権限昇格0 |
| LOOP-014 | 002,005,021,027 | SC-05,07,09 | 無限retryなし/partial可視 |
| LOOP-015 | 003,016,020,030 | SC-07 | old eventが現行状態を上書き0 |
| LOOP-016 | 019,022,023,024 | SC-01,04,07 | revision不一致の成功0 |

SHOULD LOOP-017 → TEST-006/029、LOOP-018 → TEST-020/029。LATER/EXPERIMENTは本PRの必須実装受入から除外。実際のE2E/測定結果の検証OwnerはCHAT_11。

## 16. 未決事項・Cross-team proposals

| ID / Owner | Severity | 正式に決める内容 / 暫定解 / 完了条件 |
|---|---|---|
| CP-02-01 / CHAT_01 | **P0** | `Step` / model attempt / Tool logicalCallIdの共有型とRun Stateへの対応。Run+step+ordinalとargs hash、goalRevisionの一意性。正本で採用/拒否とschema version公開 |
| CP-02-02 / CHAT_03 | **P0** | Gateway `prepare/execute/reconcile`、side-effect分類、approval binding、IN_FLIGHT→UNCERTAINと再照会ルール。effect完了後kill fixtureに合格 |
| CP-02-03 / CHAT_10 | **P0** | ledger/step/usage/outbox各transactionとfence/CAS、unique index、migration、DB障害時fail closed。lost-ack/old worker fixture合格 |
| CP-02-04 / CHAT_09 | **P0** | actor/tenant/run/args hashの単回承認拘束、secret非露出、egress/SSRF、read-only Tool判定の政策正本。権限悪用試験合格 |
| CP-02-05 / CHAT_02 manager/recovery | P0 | lease TTL/renewal/fencingとcheckpoint version、cancel/steer線形化ポイント。旧fence Tool実行0 |
| CP-02-06 / CHAT_02 budget/model | P1 | 初期24 turns/60 calls/30min/USD2上限案、cost reservation/不明usage、rate limitとfallback課金。上限fixture合格 |
| CP-02-07 / CHAT_01 + CHAT_08 | P1 | Tentative previewと永続Eventの識別、resume replay cursor、UIのuncertain表示。SSE再接続fixture合格 |
| CP-02-08 / CHAT_02 verifier + CHAT_00 | P1 | Goal successの形式とEvidence freshness、partial/uncertainと次Toolの許可。SC-01/04 fixture合格 |
| CP-02-09 / CHAT_04 | P1 | 同一Browser Session/Page排他、Handoff時のTool中断、cancel効果。SC-06合格 |
| CP-02-10 / CHAT_07 | P2 | Watchの有限Run起動と決定的threshold、通知outboxの重複。SC-02合格 |
| CP-02-11 / CHAT_11 | P1 | killpoints T1〜T6、fake provider idempotency問い合わせ、3+ replica競合、budget regression harness。TEST-026〜030を実行 |
| CP-02-12 / CHAT_00 | P1 | Frameworkの直接組込みではなく独立loopを維持するADR、compatibility/ライセンス更新時点で再検討。ADR承認 |

**ブロッカー:** CP-02-01〜05は共有契約未Locked。既存主張と競合する正本が登場したら一方を黙って変更せずADR/Ownerへ提案する。正式な共有State名、DB列名、Event名、Gateway RPC名は本書では確定しない。

## 17. Sources

調査日はいずれも **2026-10-30**。原則コミットSHA固定URL。 `Code verified` = 参照OSSのコード/テストを実際に読んだこと、**当Harness実装やテスト実行の意味ではない**。

- [S1] **Code verified** LangGraph `PregelLoop.tick/put_writes/_put_checkpoint`, `PregelRunner.tick`, main commit `ffc3336367e8139668d11e5f0b0e6def57e7c762`: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py#L461-L665 / https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_loop.py#L1237-L1300 / https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_runner.py#L132-L220
- [S2] **Code verified (test definition, not run)** LangGraph `test_interrupt_multiple`, `test_interrupt_loop`: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/tests/test_pregel.py#L4904-L4986
- [S3] **Code verified** PydanticAI `process_tool_calls` classification, `_ToolCallProcessor`: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L321-L452
- [S4] **Code verified (test definition, not run)** PydanticAI `test_tool_cancels_run_and_history_is_resumable`, `test_cancel_during_only_tool_call_is_resumable`: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/tests/test_run_cancellation.py#L499-L715 ; official deferred tools docs **Docs verified** https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/
- [S5] **Code verified** OpenAI Agents SDK `AgentRunner._run_impl`, `NextStepInterruption`/Resume: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run.py#L1140-L1365 ; official lifecycle **Docs verified** https://openai.github.io/openai-agents-python/running_agents/
- [S6] **Code verified (test definition, not run)** `test_memory_session_hitl_scenario`/reuse security tests: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_hitl_session_scenario.py#L89-L140 / https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py#L782-L820 ; HITL **Docs verified** https://openai.github.io/openai-agents-python/human_in_the_loop/
- [S7] **Code verified** Mastra `verifyRunLease`, `startRunStep`: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/run-lease-fence.ts#L1-L70 ; `resolveDeclineReason`: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/tool-approval.ts#L1-L27
- [S8] **Code verified (test definition, not run)** Mastra multi-replica lease fencing test `runs no tool once a successor holds the lease` and `stops the run that lost its lease`: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/__tests__/thread-lease-loss-abort.test.ts#L190-L315 / https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/src/agent/__tests__/thread-lease-loss-abort.test.ts#L999-L1045
- [S9] **Issue report, not reproduced** LangGraph #9240 durability semantics https://github.com/langchain-ai/langgraph/issues/9240 ; #8464 side-effect idempotency feature proposal https://github.com/langchain-ai/langgraph/issues/8464
- [S10] **Issue report, not reproduced** OpenAI Agents SDK #5413 approval call-id scope https://github.com/openai/openai-agents-python/issues/5413 ; #5409 tool after-effect/pre-state persistence https://github.com/openai/openai-agents-python/issues/5409
- [S11] **Code/metadata verified** LangGraph `libs/langgraph/pyproject.toml` (`version = "1.2.18"`, Python >=3.10), root MIT license: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/pyproject.toml / https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/LICENSE
- [S12] **Code/metadata verified** PydanticAI `pyproject.toml` (dynamic version, MIT), root LICENSE: https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/pyproject.toml / https://github.com/pydantic/pydantic-ai/blob/c2ac8cc49cbec6a3c5c7a3351fa92cbf8e19a0d9/LICENSE
- [S13] **Code/metadata verified** OpenAI Agents Python `pyproject.toml` (`0.24.0`, Python >=3.10), root MIT: https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/pyproject.toml / https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/LICENSE
- [S14] **Code/metadata verified** `@mastra/core` `package.json` (`1.81.0-alpha.0`, `Apache-2.0` declaration; repository root LICENSE not confirmed): https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/core/package.json

### Evidence / implementation status
- 上記４OSSのコードと記載テストはGitHub接続経由で閲覧。依存packageの実動作・CI成功・公式release tagとの完全一致は**未検証**。
- 本repoにHarness本番コードはないため、本書のRuntime実装・Unit/E2E/benchmarkは**未着手/未実行**。
- 本書独自のPort/型/数値、Tool実行順序と障害アルゴリズムは **Hypothesis / proposed design**。既存OSSにそのまま存在するAPIとして扱わない。


## 18. Round 2 — Shared Contract Reconciliation（2026-10-30、Draft加筆）

> 調査対象の**最新版HEADを全文取得して照合**: PR [#2](https://github.com/choko510/aiagent-md/pull/2) `core-domain-models.md`、[#8](https://github.com/choko510/aiagent-md/pull/8) `rest-api.md`、[#9](https://github.com/choko510/aiagent-md/pull/9) `authentication-and-user-isolation.md`、[#10](https://github.com/choko510/aiagent-md/pull/10) `database-schema-and-migrations.md`、依存PR [#6](https://github.com/choko510/aiagent-md/pull/6)/[#11](https://github.com/choko510/aiagent-md/pull/11)。どのPRもレビュー未完であり、下記は**CHAT_02の採用提案**、共通のLocked決定ではない。

### 18.1 Conflict ledger（各Ownerへ変更提案、他担当ファイルは変更しない）

| ID/severity | 最新PR根拠（exact file/section/symbol） | 相違・問題 | 正本Owner / 選ぶ提案 | 必須変更・互換性・確認Test |
|---|---|---|---|---|
| R2-02-01 P0 | #2 `core-domain-models.md §5 UUIDv7` vs #10 `database-schema-and-migrations.md §5.2 agent.*.id DEFAULT gen_random_uuid()` | trusted APIを通らないDB insertでv4 UUIDが公開される | CHAT_01 id validation / CHAT_10 generator. **server generated UUIDv7を単一正本、SQL ID defaultは削除する提案** | DB migrationはdefault撤去＋既存v4を安全に識別/隔離し移行。Idempotency key/dedupe hashはUUIDと別物。**R2-LOOP-T01** |
| R2-02-02 P0 | #2 `Task.conversationId: ConversationId \| null`; #10 `agent.tasks.conversation_id uuid NOT NULL`; #8 `TaskView.conversationId: Id` | Watch・外部Event起点Taskが作れず、架空Conversation作成を強制 | CHAT_01 cardinality / CHAT_10 DDL / CHAT_08 DTO. **nullableを維持** | DDL nullable, FKはnon-nullのみ実施。REST projectionを `Id \| null` とし作成省略時はnull。migration/fixture **R2-LOOP-T02** |
| R2-02-03 P0 | #2 `recordVersion=1` vs #10 `revision bigint DEFAULT 0` vs #8 §5.5 ETag | CAS初期値不一致。goalRevisionやleaseEpochとも別概念 | CHAT_01 RecordVersion / CHAT_10 row lock / CHAT_08 If-Match。**1開始で統一する案**を提案（0開始のinternal row `dbRevision` とAdapterで分ける代案は棄却: 曖昧・監査難） | 初期行1、mutation CAS `WHERE record_version=expected RETURNING next`、API strong ETagは当該version、If-Match失敗412/欠落428。バックフィル時0→1にあわせETagを再生成。**R2-LOOP-T03** |
| R2-02-04 HISTORICAL P0 (design corrected; tests pending) | #2 previous `Run.leaseEpoch legacy-number` (non-negative); #1旧`fencingToken: bigint`; #10 `agent.runs` にlease列なし、`tool_attempts.fence_token`のみ | Run・Tool Attempt間で別fenceを誤同一視し旧Workerがcommit/dispatchできる | CHAT_01 Run epoch / **CHAT_02** loop+manager / CHAT_10。**canonical Run.leaseEpoch=JS safe integer、DB bigint CHECK safe-range; ToolAttempt fenceは別scope** | `runs.lease_epoch` 非null default0、claimで+1 CAS、`lease_owner`/expiry+activeRun検査。Tool Gatewayは各効果開始前にRun epochと別のattempt fenceをともに検査。unsafe number conversion reject。**R2-LOOP-T04/T05** |
| R2-02-05 P0 | #2 `Task.goalRevision,goalRevisionRef,activeRunId` + `Run.inputGoalRevision` vs #10 `tasks,runs`に対応列なし | Steer後にcurrent plan/approval更新、Taskのactive Run参照を正しく再構成できない | CHAT_01 goal semantics / CHAT_02 applied revision / CHAT_10 storage。**Task.currentGoalRevision/currentRevisionRef及びactiveRunIdを永続化。Run.inputGoalRevisionは初回固定、step.appliedGoalRevisionは別記録** | TaskGoalRevision immutable append、activeRunId参照が同TaskのRunのみ、同Task activeRun<=1、条件変更とEvent/Outbox同tx。CheckpointにappliedGoalRevisionを書き、旧Tool/Approvalをinvalidate。**R2-LOOP-T06/T07** |
| R2-02-06 P0 | #2 Tenant/ownerUserId; #9 Auth `AuthenticatedPrincipal(userId,tenantId)`; #10 workspace_id、`workspaces`のみ | 物理workspaceが認可主体と混同される、Run/Toolがowner lineageを追えない | CHAT_09 identity/role / CHAT_01 model / CHAT_10 identity binding。**tenantId→workspaceIdはserver-verified 1対1 bindingの初期案、userIdは別、ownerUserIdは必須** | user/tenant/member/workspace binding・owner FK（or verified join）、cross-tenant deny。queue payloadはrunId pointerだけで認可不可。禁止者復旧時dispatch0。**R2-LOOP-T08** |
| R2-02-07 P0 | #8 §5.2/5.5 Create Task/Watch/Approval、#10 §6.1/7.1 receipt/outbox、#2 command idempotency | DB receiptとTask/Run/outboxを別commitすればACK喪失で二重起動 | CHAT_10 txn / CHAT_08 route / CHAT_01 command。**domain commit + receipt + event + outbox atomic** | DB前提、HTTP requestをtransaction内保持しない、同key同body同receipt、異body409、If-Matchはversion CAS。**R2-LOOP-T09** |
| R2-02-08 P0 | #1 §8 `UNKNOWN_EFFECT`, #10 §6.2 Tool ledger、#6 Registry§6 caller ref | IN_FLIGHTのlease再取得を理由に副作用を盲目的再実行可能 | CHAT_03 gateway/ledger、CHAT_02 recovery、CHAT_10 persistence。**NOT_STARTEDのみ初回dispatch; UNKNOWNは照会-only、未確認なら人間の判断待ち** | Gateway reconcile returns effect-proof; providerの保証なしに確定済み扱い禁止。old epoch/old intent/old approvalは再利用しない。**R2-LOOP-T10** |

**不採用:** `workspace_id=userId=tenantId`を暗黙に同一視すること、DB v4をUUIDv7として見せること、recordVersionとGoalRevision/leaseEpochを一つの`revision`列に詰めること、永続化前の外部Tool起動、actorが変わった承認の使い回し。

### 18.2 AgentLoopへの拘束条件（今回の直接修正）

- **[ROUND4 CONTRACT RESOLVED AT WIRE BOUNDARY]** `LoopIdentity.leaseEpoch` = `Run.leaseEpoch`: canonical signed int64 decimal **string**, internal BigInt, DB BIGINT; validation rejects JSON number / overflow / exponent. This is a *Run* fence and is distinct from ToolAttempt's own fence. Stale worker writes return zero rows. All DB/SSE/Tool changes require head CAS and explicit ownership verification.
- **[PROVISIONAL: CHAT_09/10]** `tenantId`はtrusted session又はDBのRun→Task→owner bindingから復元し、`workspaceId`はサーバーで照会。User名を含むqueue payloadとModel outputは権限の根拠にしない。Auth失効・Task.owner disabled中はTool開始不可。
- **[PROVISIONAL: CHAT_01/10]** Taskは `conversationId=null` でも有効。Runの存在とConversationは独立。Runが参照できるGoal revisionはTaskのimmutable GoalRevisionであり、Steer後のapplied revisionはcheckpointに記録。旧scopeのTool approvalは実行前にexpire。
- **[PROVISIONAL: CHAT_10]** `recordVersion` 1開始のCASと `leaseEpoch` のfence比較は**双方必要**。毎回のEvent挿入でrecordVersionを上げたとしても、leaseEpoch代用禁止。Event outboxとreceiptはTaskの確定トランザクションで原子化。
- **[PROVISIONAL: CHAT_03]** Gatewayが直前のTool dispatch claimで `Run.leaseEpoch` / `Task.activeRunId` / `goalRevision` / `Auth identityVersion` / `approval scope` / `budget` を最新DBから照合。Timerやmemory上のAbortSignalだけでcancelの線形化を保証しない。確認不能はfail closed。

### 18.3 追加されたMUSTとテスト計画（Designed only / Not executed）

| REQ | MUST（SC） | TEST-ID / Setup→Action→Expected |
|---|---|---|
| LOOP-R2-001 | 公開IDと永続化IDの共通validator一致（SC-07/08） | R2-LOOP-T01: trusted APIとDB補助insertの全IDを検証→UUIDv7以外の新規公開ID0 |
| LOOP-R2-002 | non-Conversation Taskの実行許可（SC-02/07） | R2-LOOP-T02: Watch/Event起点Taskを作成/再読込→conversationId null、偽Conversation0 |
| LOOP-R2-003 | RecordVersion CASとIf-Match一致（SC-07） | R2-LOOP-T03: Version1で2同時PATCH→1成功/1が412、version2 |
| LOOP-R2-004 | leaseEpoch独立・stale rejection（SC-07/08） | R2-LOOP-T04: Worker A/B争奪→旧Run epochのcommit/tool開始0。T05: bigint>MAX_SAFE_INTEGER→safe停止、誤丸め0 |
| LOOP-R2-005 | GoalRevision/activeRun永続整合（SC-04/07） | R2-LOOP-T06: Steer/再起動→旧approval実行0、新goalに対するcheckpoint。T07: activeRunIdを他Task Runへ差替→FK/CAS拒否 |
| LOOP-R2-006 | tenant-owner-workspace照合（SC-06/08） | R2-LOOP-T08: 異workspace ID, revoked owner, queue forged tenant→外部Tool開始0/他Evidence読取0 |
| LOOP-R2-007 | command receipt/outboxの原子commit（SC-02/07） | R2-LOOP-T09: precommit/aftercommit kill+same key replay→Task1、Run1、outbox1、receipt同一 |
| LOOP-R2-008 | UNKNOWNの盲目的再発行防止（SC-07） | R2-LOOP-T10: 予約Tool実行直後kill→provider lookupかUNCERTAIN、実行回数1 |

**実行はしていない**。横断Contract Testの正本化とE2E実行はCHAT_11へ依頼。全MUSTがTestとSCに対応し、旧LOOP-001〜016と整合することをレビューで確認するまで本書はDraftのままとする。


## Round 3 — AgentLoop, append-only checkpoint and terminal Retry integration (2026-10-30)

The earlier AgentLoop pseudocode is a *domain-level port contract*: `commitModelStep`, `prepareIntent`, `commitToolOutcome` may all persist **multiple immutable checkpoint revisions for the same step ordinal**. The physical source of truth is CHAT_10 PR #10 `runs.current_checkpoint_id`→`run_checkpoints(checkpoint_revision)`; CHAT_02 PR #27 supplies the head CAS/reclaim semantics. A committed model reply **must not** be regenerated on resume; a persisted effect whose outcome is UNKNOWN is never blindly resent.

- Claim/Recover preserves same RunID and increments current Run leaseEpoch. Snapshot.writerLeaseEpoch is historical evidence, not equal-to-current requirement; each new checkpoint writer must hold current lease.
- All versions/goalRevision/leaseEpoch crossing a service boundary are canonical signed-int64 decimal strings per CHAT_01; convert to BigInt internally, never JSON number.
- Task terminal is immutable; user Retry creates a new Task with retryOfTaskId lineage, and a new Run may be created on the **same Task only while Task is nonterminal** after earlier Run terminal. CHAT_01 PR #23, CHAT_02 PR #21, CHAT_08 PR #8 are referenced.
- Event publication uses CHAT_01 generic DomainEvent and CHAT_10 `domain_events` tenant deliveryOffset, not Task-only event_sequence.
- **Tests designed, NOT executed**: `LOOP-R3-T01` model committed→crash→same reply ref reused; `LOOP-R3-T02` five checkpoints at one step + recovery no duplicate effects; `LOOP-R3-T03` old worker lease settles late→0 writes; `LOOP-R3-T04` two simultaneous Retry controls terminal vs nonterminal→correct Task/Run immutable mapping; `LOOP-R3-T05` interleaved Watch+Run events→SSE no lost Task events.

Cross-team source of truth: #23 Event, #27 Checkpoint, #10 DDL, #21 Task/Run, #8 REST. These remain Draft pending actual implementation and fault tests.
