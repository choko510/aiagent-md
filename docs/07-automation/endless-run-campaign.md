# CAM-01 — Endless Run Campaign / Continuous Goal Controller
> **Draft / PROVISIONAL** | 2026-11-07 | Owners CHAT_01/02/03/07/08/09/10/11 | 実装・実機検証未実施。

## 0. ユーザー意図と要件
ユーザーが「全体最適化をずっと」「UI/UX改善を続けて」「新機能を10個追加して」「特定テーマを調査し続けて」と指示した際、毎回1 Taskの成功で止まらず、**停止指示・設定した件数/費用/時間/利用制限に達するまで**次々と新しい独立Cycleを開始する。終わりのない1つのRunや無制限のLLM self-talkは実装しない。各Cycleはfinite budget、lease、checkpoint、dedupe、verifierを持つTask graph。継続型Campaignはそれらを束ねる永続Goalの別リソースとして扱い、一般Web/EC/研究/開発で再利用する。既存 [Heartbeat](heartbeat-and-goal-loop.md) の「意味のないtickではLLMを呼ばない」という原則を維持し、**前サイクル完了/未完改善候補発見/承認待ち解消**等の確かな次作業がある場合には止まらず次のCycleをdispatchする。単なるperiodic watchdogでなく成果物駆動の継続。

## 1. Mode / user-facing parameters
| mode | work-item creation | done criterion |
|---|---|---|
| `full_optimization` | profile/benchmark/bottleneck→3案→統合→修正→比較→3レビュー | 前後同条件で測定改善・重大退行なし |
| `uiux_evolution` | Browser探索→UI問題→視覚案/デモ→必要承認→実装 | user-approved UI, scenario+visual regression pass |
| `feature_factory` | 独立機能案→Demo→ユーザー承認→実装→レビュー | distinct approved feature count (10等) |
| `bug_hunter` | 検証・再現→修正→回帰→レビュー | distinct verified fixed issue |
| `custom_goal` | user-defined objective/evidence | specified measurable acceptance |

設定には `targetCount`(任意整数)、`continuousUntilStopped`、`demoReviewPolicy`、`designReviewPolicy`、`expertAdvisorPolicy`、`maxTotalCost`、`maxProviderRequests`、`maxTotalTokens`、`maxActiveCycles`、`perCycleLimit`、`activeUntil`、`quotaRecoveryMode`、`allowedExternalActionScope`を設ける。初期値は必ず有限のOperator安全上限; `continuousUntilStopped` は無制限課金や権限解除ではない。UIで「API利用枠まで」と指定した場合でもProviderごとの利用量取得の可用性、課金額/推定額と未確定Usageを区別し、**失敗応答を残高0や残高無限と仮定しない**。ユーザーが意図的に停止しない間は、作業候補があり現在の許可範囲・安全上限・依存/利用制限で許される限りcontinuationする。

## 2. Aggregate / states（既存Task/Run状態とは別）
```ts
type CampaignV1 = { campaignId:string; ownerUserId:string; tenantId:string;
 goalSpecRef:string; goalRevision:string; mode:string; projectId?:string;
 targetCount?:number; continuous:boolean; completedCount:number;
 spentMicroUsd:string; reservedMicroUsd:string; unknownUsageRef?:string;
 budgetEnvelopeRef:string; lastCycleId?:string; nextWorkItemRef?:string;
 state:"ACTIVE"|"WAITING_INPUT"|"WAITING_APPROVAL"|"QUOTA_WAIT"|"PAUSED"|
  "BLOCKED"|"STOP_REQUESTED"|"STOPPED"|"TARGET_REACHED"|"LIMIT_REACHED";
 recordVersion:string; controlVersion:string };
type CycleReceiptV1 = { campaignId:string; cycleId:string; goalRevision:string;
 workItemFingerprint:string; resultState:"VERIFIED"|"PARTIAL"|"FAILED"|"BLOCKED";
 resultArtifactRefs:string[]; qualityGateRef:string; countContribution:0|1;
 committedAt:string };
```
このschemaは01/10のowner lock前であり、UUIDv7/Int64DecimalString/recordVersion/tenant関係を新たに正本化しない。CycleReceiptの`countContribution`は実行時検証された成果が1件の場合のみ原子的に付与、legacy distinct key `(campaign,workItemFingerprint,completedCriterionVersion)` (superseded by §2026-11-08) とし、重複配信/replay/remote PR重複でカウント増加禁止。Optimizationは同じ小手先のノイズ改善を繰り返さず、安定したベンチと改善効果を根拠として記録。

## 3. FSM / scheduling
`CREATE → ACTIVE → [Cycle claimed] → Cycle verification → (target met ? TARGET_REACHED : enqueue next distinct Cycle) → ACTIVE`。
`ACTIVE → WAITING_INPUT/WAITING_APPROVAL` when *all eligible* independent tasks require that human decision; `ACTIVE→QUOTA_WAIT` when safe retry-after known; `ANY→PAUSED/STOP_REQUESTED→STOPPED` when User stops; `ACTIVE→LIMIT_REACHED` at budget/elapsed/limit, `BLOCKED` for unsafe prerequisites or no legal next action。重要な相違:
- **1件の承認待ちでCampaign全体は停止しない**。依存のない開発、提案やデモ作成、証拠調査は継続してよい。全work itemが待機ならpark。
- Cycle verifiable resultのDB commit後、同一短命transactionで `next-work-item dispatch intent + ledger/outbox` と予算予約をCAS。Queueはat-least-onceであるため、同じcontinuation receipt/GoalRevisionから2つのCycleを起動しない。
- Stop/Cancel/revokeがcommitした以降、新しいモデル/子Task/Tool dispatchなし。すでに送信された外部操作はside-effect ledgerでUNKNOWNの可能性を保持し、勝手に逆算や再実行しない。Pauseは既存のPause/Steerと同じ安全境界、ongoing Cycleの状態を残す。
- Provider 429はtyped `retryAfter` + quota class (temporary_throttle, daily_reset, hard_quota, unknown)を区別し、新規課金操作を停止。回復時刻を検証できる `auto_resume` は期限でwakeした後Auth/Budget/Owner再チェック、未知回復時刻は人間入力待ち; 429で短周期retry/busy spin禁止。API rate limitを回避したり自動アカウント追加しない。
- 1件のP0/P1が解決不可で対象Cycleが`BLOCKED`でも、別branchで独立・安全な改善を続けられる。最終合格や件数達成には当該Cycleの合格証が必要。末端の安全制限に達した場合は必ずLIMIT_REACHED/PAUSEDと説明し、絶対無限継続を保証しない。

## 4. Improvement discovery, dedupe and convergence
入力はuser goal、既存verified tests/benchmarks/Browser findings/Backlog/前Cycle採否。候補fingerprint+対象ファイル/機能+目的+baseline SHAを保存して、同一修正の繰り返し/振動/以前の却下案を抑制する。Cycleが成果なしか同じ誤差で失敗したら、別手法/Expert Advice/対象変更を検討し、無目的な継続をしない。No-work、承認待ち、依存API未提供の状態は明示的にpark。新たな証拠やユーザー指示でwakeしたら再開可能。監視キャンペーンなら意味のある時刻/eventが次の仕事を発生させる。

## 5. Budget topology
Campaign-wide hard limitsとCycle envelopeの二層。費用/トークン/step/elapsed/Model Attempt/API calls/storage/browser concurrencyを事前にatomic reserve。Campaign全体で`spent+held+uncertain`がceilingを超えない。Per-cycle unused reserveの返却はsettlement receipt。UNKNOWN provider usageは0として扱わず見積枠を保持。Recovery後もUsage/target countを初期化しない。モデル/provider/model変更は現在のcapability/budgetと許可モデルを再評価。

## 6. UI/Notification
Progress: goal、mode、phase、current/target count、queue（running/waiting-demo/review/blocked）、直近変更SHA、browser debug count、spent/estimated/remaining、rate limitと再開条件、決定待ち、停止/再開/Steer、監査、結果リンク。停止コマンドは即時受付receiptと実際の停止完了を区別。完了報告が必要なときだけ通知、重複outbox防止。操作はauthenticated CAS+Idempotency-Key; GET/SSEは権限付きprojection。

## 7. Acceptance / negative fixtures
CAM-T01: Cycle #1完了→検証済み次WorkItemを自動dispatch、LLMに空の「続ける？」を呼ばない。
CAM-T02: 途中VM/Worker crash→同一Campaign/cycle復旧、countと予算のリセット/二重起動なし。
CAM-T03: feature 4 current-integrated完了+2 waiting approvals→Count=4、独立Taskは進行。
CAM-T04: 10件目のcurrent-integrated receiptがTARGET_REACHED、重複outboxで11件目起動0。
CAM-T05: ユーザーStopとenqueue競合→Stop先行なら新Task0。
CAM-T06: daily quota reset confirmed→QUOTA_WAITから新validation後に再開; hard quota未知→no blind retry。
CAM-T07: cost/step/time ceilingでLIMIT_REACHED、未確定使用量を無料とみなさない。
CAM-T08: Modelの「完了」だけで成果件数増加0。
CAM-T09: 同一最適化が数Cycle連続し改善実測なし→重複候補抑制/他案/park。
CAM-T10: P0/P1存在のFeatureをDONE扱いしない。
CAM-T11: 外部イベント/危険なTool/秘密へ権限拡張0。

## 2026-11-08 P1-04/P1-05 — Canonical Work Identity, Stop Fence, Quota Receipt

The earlier `(campaign,workItemFingerprint,completedCriterionVersion)` key is **superseded**: fingerprint may change when baseline code changes. Server-assigned immutable `logicalWorkItemId` identifies the user-visible outcome independently of commit SHA/cycle. A semantic equivalence group links attempts and new candidates which implement identical behavior. The baseline/result SHA are evidence, not identity. A criterion revision alone cannot recount a previously counted logical feature.

```ts
type CycleReceiptV2 = {
  campaignId:string; cycleId:string; logicalWorkItemId:string;
  workItemFingerprint:string; criterionVersion:string; goalRevision:string;
  baselineSha:string|null; resultSha:string|null; integrationHeadSha:string|null;
  completionStage:"VERIFIED"|"INTEGRATED"|"RELEASED";
  resultState:"VERIFIED"|"PARTIAL"|"FAILED"|"BLOCKED";
  qualityGateRef:string; integrationReceiptRef?:string;
  completedByControlVersion:string; counted:boolean; committedAt:string;
};
type QuotaWaitReceiptV1 = {
  campaignId:string; providerId:string; credentialScopeRef:string; modelRouteRef:string;
  limitKind:"TEMPORARY_RATE"|"DAILY_QUOTA"|"HARD_QUOTA"|"UNKNOWN";
  providerRequestId?:string; observedAt:string; retryAfterAt?:string;
  authoritativeResetAt?:string; evidenceRef:string; controlVersion:string;
  nextResumeGeneration:string; outstandingUsageRef:string; admissionPolicyHash:string;
};
type CampaignControlReceiptV1 = {
  campaignId:string; commandId:string; actorRef:string; command:"STOP"|"PAUSE"|"RESUME"|"STEER";
  previousControlVersion:string; newControlVersion:string;
  committedAt:string; inFlightExternalIntentRefs:string[]; stateAfter:string;
};
type CampaignChildAdmissionV1 = {
  campaignId:string; cycleId:string; childTaskId:string;
  expectedControlVersion:string; expectedGoalRevision:string;
  expectedRunLeaseEpoch:string; expectedPolicyEpoch:string;
  dispatchIntentId:string; expiresAt:string;
};
```
IDs, versions and wire values must be reconciled by 01/02/07/10 owners. All counters exposed in UI are **derived from committed and still-eligible receipts**, not an independent unprotected cached integer. Unique DB constraint is `(tenantId,campaignId,logicalWorkItemId)` for a counted receipt per campaign. `criterionVersion` is stored for audit and determines whether a new result qualifies but is **not** a new counting key. For default Feature Factory the result must have an integration receipt for the **current integrated head** (DEV-08). If another feature changes that head, reevaluate eligibility; never count an old branch SHA as integrated.

### Stop linearization and in-flight work
- Every worker admission for a new child task, model call, Browser action, package request, or tool/external write **must** compare `expectedControlVersion`, Task/Run lease and policy epoch under a trusted admission gate. These tokens must be minted by Trusted Control, short-lived and revocable; the model cannot update them.
- STOP commits `STOP_REQUESTED` and increments controlVersion in the same transaction as stop receipt+outbox; new Cycle enqueue and child admission use the **same campaign row lock / equivalent serializable CAS**. Whichever commits first defines ordering. After STOP is committed no new admissions; already-dispatched jobs cannot use their old controlVersion.
- **Physical effect caveat:** an external request admitted and sent before STOP commit cannot always be rolled back. The stop handler revokes preview/broker grants, terminates child processes and attempts transport cancellation; already-sent/ack-lost effects are `UNKNOWN` until reconciled. UI distinguishes STOP_REQUESTED from STOPPED, records in-flight intents, and never promises zero post-stop bytes already sent before linearization.
- RESUME requires authenticated command + fresh identity/policy/goal/budget/quota checks, new generation and controlVersion. A stale queue delivery, timed wake, old advisor/Browser tool or old lease cannot restart a stopped campaign. Idempotent duplicate Stop returns existing receipt, not a new generation.
- When all eligible work is awaiting human decision, the Campaign remains parked; it **does not** generate meaningless model calls to satisfy “endless”.

### Quota recovery
On 429/quota result, record `QuotaWaitReceiptV1` with provider, category, observed response/request correlation, retry-after (if available), confidence source, remaining uncertain usage reservation, and monotonic resume generation. The provider may supply a hint but not a guaranteed reset. Exact reset validation is required before a timed wake; unknown or hard quota must pause for human/configured review. A single persisted wake key `(campaignId,providerId,resumeGeneration)` prevents concurrent reactivation. On wake, recheck authenticated owner grant, current Campaign controlVersion, operator budget, provider route availability, quota estimate, and any external intent uncertainty, then attempt one bounded admission. 429 recurrence reschedules with exponential/capped backoff and no spin/alternate-key bypass.

### Fault matrix
**CAM-T12** STOP commit wins race against a claimed old queue job → old child/tool/model admissions zero.
**CAM-T13** child was admitted and external request already sent just before STOP → cancel attempt, ledger UNKNOWN, no blind retry.
**CAM-T14** duplicate Stop/Resume from two UIs and stale ControlVersion → one authoritative transition and no resurrection.
**CAM-T15** 429 with Retry-After vs uncertain hard quota vs multiple providers → one evidence-bound wait/resume; no premature retry.
**CAM-T16** stale wake on old resumeGeneration after owner revoke → no new cycle.
**CAM-T17** same logical work item in 3 distinct SHAs / criterion revisions → one eligible count.
**CAM-T18** integrated HEAD changed after count → current count recalculated from integration receipts, no stale TARGET_REACHED.

### Integration head invalidation of previously reached targets
For Project-linked Feature Factory Campaigns, TargetReached remains **valid only while** the current integration HEAD has PASS eligibility for at least targetCount distinct logical work items. On a later HEAD change, evaluate the immutable first-count receipt plus latest per-head `campaign_count_evaluations` evidence; if below target, display `REVALIDATION_REQUIRED` and block false “10/10” status. This does not silently re-enable a stopped or cost-exhausted Campaign. For generic non-Project research Campaigns, `INTEGRATED` is not a valid completion criterion and no fake integration receipt is required. Re-evaluation uses a separate record_version and controlVersion admission on any new work.
**CAM-T19**: current integration HEAD changes after TARGET_REACHED; eligibility falls, status REVALIDATION_REQUIRED and no automatic spend; re-evaluation PASS restores target without duplicate count.
