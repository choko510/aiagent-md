# Tool Approval & Policy — 詳細実装仕様 (03-03)

> CHAT_03 | `docs/03-tools/tool-approval-and-policy.md` | **Draft / [PROVISIONAL]** | rev 0.2 | 2026-10-30 | baseline main `b45d4e989998663a00ef09673e69ae1f4191f00d`
>
> Owner of approval/effect decision: CHAT_03. Principal/fresh-auth/secret/egress: CHAT_09. Canonical Task/Run/Event/Errors/UUIDv7: CHAT_01. UI/REST/Telegram: CHAT_08. Run step barrier: CHAT_02. Physical transaction: CHAT_10. Ledger: 03-04.

## 0. 設計決定
Approvalを**Tool名だけに対する一度限りの「Yes」**として実装してはならない。対象ユーザー・Tenant・Run・Goal revision・Tool revision・凍結済み引数・外部対象・金額・リスク・期限・policy epoch・fresh authenticated actorを厳密に結び、DBに決定を保存したうえで、Gatewayが実行開始時に**single-use consume**する。許可は権限付与の代替ではない。承認者にもネットワーク/データアクセス許可が無ければ実行不可。

- 中央Policy engineは**deterministic, fail-closed, least privilege**。モデルの文/HTML/X投稿/MCP annotations/Skillからallowlistを変更しない。
- `read/sensitive/write/irreversible`は初期分類。分類は**Toolの静的メタデータだけでは足りず**具体引数・対象URL・resource owner・送信データ・操作種別で保守的に上げる。曖昧なら安全側のirreversibleまたはdeny。
- 通常のread-only検索・計算は認可範囲で承認不要。ただし機微な閲覧、外部メッセージ送信、購入・予約・投稿・送金は承認対象。ユーザーが承認した「定期監視の作成」と、そのWatchから許可されたread/notify実行は別のauthorityで、勝手に購入権限へ拡張しない。
- Policy結果は`ALLOW`, `NEEDS_APPROVAL`, `DENY`に分類し、`NOT_APPLICABLE`やpolicy service failureをALLOWに落とさない。Approvalが有効でもdispatch直前のfresh policyとRun fenceを再確認。
- UI Web/Telegramは**同じPendingApproval Record**へ操作するだけ。Telegram chatIdを承認者のUserIdに見立てない。重要な外部決済系はfresh authenticationと二段階の内容表示を必須候補とする。
- Approvalを取り消す操作は**未dispatchの効果**を止める。外部送信後に取り消しても送信をなかったことにしない。UNCERTAINとreconciliationを残す。

## 1. Scope・non-goals・SC
承認要求の作成・提示・期限・approve/deny/revoke・Policy evaluationと失効、決定再開。対象はWeb ResearchからSC-01商品調査、SC-04ホテル、SC-07 cancellation/approval replay、SC-08悪意MCP、SC-11 event-driven launch、SC-13 learned action replay、SC-16通知優先設定まで。**全ルートが同一Gateway経由**である。

非対象：ログインの実際のcookie保管、MCP oauth、Browser CAPTCHA解除、正確な支払完了の判定（03-04 Ledger/Provider）、REST具体route、DB SQL、チャットの自然言語理解、外部ポリシーSDKの採否。

### 1.1 承認の境界表（決定案）
| operation | 最低risk | Default authority / approval | 否認条件例 |
|---|---|---|---|
| 公開Search/HTTP GET・数値計算 | read | data scope許可で自動 | blocked URL、quota、site規約 |
| 会員価格・個人Gmail・非公開資料の読み取り | sensitive | explicit capability + provider OAuth; Policyが新scope/機微内容への承認を要求可能 | 別tenant、未認可data source |
| Watch作成/停止・通知先追加・メール下書き保存 | write | scope限定明示command/承認ルール。Watch作成は設定永続化を確認 | forged sender、無許可宛先 |
| 購入・ホテル予約確定・送金・投稿・送信・不可逆削除 | irreversible | **毎実行の明示的approval**（将来事前委任なら別ADR+上限/取り消し/テスト必須） | fresh-authなし、target/amount不一致、expired、policy deny |
| Brokerを介したXイベント調査起動 | read | 事前承認済みEventSubscriptionのread/notify grant内だけ | イベント本文が新Tool権限を要求 |
| 学習済みBrowser recipeでClick | dynamic | 単なるlabelに依存せず実際のsite action/targetを評価。不可逆なら毎回approval | DOM変更・確認画面遷移時にre-evaluate |

Riskの上昇は一方向（read→sensitive/write→irreversible）。低下にはadmin policy versionと再評価が必要。Tool/Providerの`readOnlyHint`は証拠として信用しない。

## 2. OSS実コード・テストとIssue（2026-10-30）
| Upstream/commit/license | code/test/issue verified | 採用・不採用 |
|---|---|---|
| [PydanticAI `3809c1b8`](https://github.com/pydantic/pydantic-ai/tree/3809c1b861036197de4a06ae3ee38e27e5455100) MIT | [`ApprovalRequiredToolset.run_tool`近傍](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/toolsets/approval_required.py#L16-L32) は`ctx.tool_call_approved`と`approval_required_func`で`ApprovalRequired`を挙げる。[`test_toolsets.py`](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_toolsets.py)も参照。 | **Code inspected**。conditional approvalは採用、永続一回限りDB receiptは自前実装。 |
| [OpenAI Agents Python SDK `cd26bbbc`](https://github.com/openai/openai-agents-python/tree/cd26bbbc52847845fde73b9952b51e62e2bcbdc0) MIT | [`run_internal/approvals.py:filter_tool_approvals`](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_internal/approvals.py#L46-L67)、[`tests/test_tool_approval_call_id_reuse.py`](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py#L333-L435)。 | **Code/test inspected, not run**。承認拒否の明示とtool_call_id再利用問題を参考。Context単独承認を外部Side effectの永続Exactly once保証に流用しない。 |
| [OpenClaw `8b8c33ef`](https://github.com/openclaw/openclaw/tree/8b8c33ef334b3754f42afe2a2e212418abfa80f4) MIT | [`src/agents/tool-policy-pipeline.ts:applyToolPolicyPipeline`](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/agents/tool-policy-pipeline.ts#L121-L217)と[テスト](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/agents/tool-policy-pipeline.test.ts#L101-L331)のallow/deny層・plugin ID衝突・policy変更。 | **Code/test inspected, not run**。各層denyとsnapshot freshnessの検査方法を採用。ただしOpenClawのshell/exec capabilityは本製品へ導入しない。 |
| [LangGraph `ffc33363`](https://github.com/langchain-ai/langgraph/tree/ffc3336367e8139668d11e5f0b0e6def57e7c762) MIT | [`libs/prebuilt/langgraph/prebuilt/interrupt.py:HumanInterrupt`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/prebuilt/langgraph/prebuilt/interrupt.py#L51-L85) defines pause/human action structure。 | **Code inspected**。承認待ちのresume conceptを参考。最終的なDB auth/target matchは独立実装。 |
| Issues | [PydanticAI #6452](https://github.com/pydantic/pydantic-ai/issues/6452) UI approval provenance提案、[#10926](https://github.com/pydantic/pydantic-ai/issues/10926) crash後重複のrepro、[Mastra #27485](https://github.com/mastra-ai/mastra/issues/27485) double approveのreported bug。 | **Issue report only**。問題を再現/修正済みと主張しない。 |

### 比較
A. **「tool.requiresApproval=true」だけでUIが承認**：関数内引数/target差替え、同じtool名別provider、別Run、期限/取消しに弱い → **却下**。
B. **チャットに「いいよ」と返すだけで承認**：prompt injection、Telegram identity混同、再送・並行で無制限実行 → **禁止**。
C. **Scope-bound、versioned、single-consume、DB authoritative approval + fresh Gateway policy recheck** → **採用**。UI負荷は増すが強い不変条件を得る。
D. **期間限定・金額上限付き包括委任**：将来の設定として価値はあるが監査/利用者UX難度高 → **LATER**、初期のirreversible用途では不可。

## 3. 依存・権限と正本
Policy ownerはCHAT_03。ただし**誰が誰か、Credentialをどう扱うか、egressをどう守るか**はCHAT_09の正本を消費。User/Task/RunとUUIDv7はCHAT_01、ToolInfoは03-01、run claim/checkpointはCHAT_02、Intent/attempt/outcomeは03-04、外部UI/Telegramのsigned callback認証はCHAT_08/09。CHAT_10は物理トランザクション/列/Indexを定める。

`ApprovalRecord`の主キーは公開EntityとしてUUIDv7案。作成権限はGatewayのみ。認証セッションはChatやX投稿でなく`AuthenticatedPrincipal`でserver再取得。`tenantId`はDB `workspace_id`と同値を仮定しない（CHAT_09/10への明示Mapping要求）。`Task.conversationId`はnullableなのでWatch/イベント起動Taskにも同じapprovalを適用できる。`recordVersion=1`とDB `revision DEFAULT 0`を同じとみなさずMapperが整合するまでP0。

## 4. Requirements
| REQ | Level | Observable acceptance | SC |
|---|---|---|---|
| APV-001 | MUST | すべてのTool Callはdefault denyから始まり、許可はserver-owned policyでのみ決定 | 08,11 |
| APV-002 | MUST | read/sensitive/write/irreversible分類は操作/args/target/ownerで評価、remote hintに依存しない | 01,04,08 |
| APV-003 | MUST | 外部effectは承認要求を永続化し、承認前のExecutor開始0 | 04,07 |
| APV-004 | MUST | approvalはtenant/actor/intent/toolRevision/argsHash/target/amount/goalRevision/expiry/policyEpochへbind | 07,08 |
| APV-005 | MUST | approve/denyはserver-side authenticated principal、fresh auth、exact intent、CASのみで決定 | 06,07 |
| APV-006 | MUST | approvalはatomic single consume、同時Web/Telegramで2回決定/実行できない | 07,08 |
| APV-007 | MUST | revoke/expiry/cancel/steer/permission reductionは未開始effectを止め、再承認またはdenyへ | 07 |
| APV-008 | MUST | 承認後もGateway実行直前に最新capability・budget・Run lease fenceとschemaを再照合 | 07,08 |
| APV-009 | MUST | 失効済・偽造・別tenant approval IDやreplayを拒否し、Tool存在の秘匿 | 06,08 |
| APV-010 | MUST | UI/Telegramの決定はひとつのledgerを更新、202レスポンスは外部実行済みを意味しない | 07,08 |
| APV-011 | MUST | 承認対象の表示は正規化した操作/target/金額/期限/副作用を明示、secretは秘匿 | 04,08 |
| APV-012 | MUST | 未知/不在のAuth・Policy・対象一致・clockはfail closed、過大scopeへ降格させない | 07,08 |
| APV-013 | MUST | 通知・Xイベント・Recipe・添付内容はPolicy/Approvalを更新できない | 08,11,13,15,16 |
| APV-014 | MUST | 監査Outboxとdecision receiptがtransaction内、failed retryでも重複効果を生まない | 07,09 |
| APV-015 | SHOULD | 明示されたrejection理由と再認可案内を提供し秘密・メタ情報を漏らさない | 04,08 |
| APV-016 | LATER | 認可範囲が狭いread-only定期処理に限定したrevocable preapproval grant | 02,11 |
| APV-017 | EXPERIMENT | approval UXによる放棄率と誤承認をsynthetic benchmarkで測る | 09 |

## 5. Policy interface と Approval binding
~~~ts
type Risk="read"|"sensitive"|"write"|"irreversible";
type PolicyDecision="ALLOW"|"NEEDS_APPROVAL"|"DENY";
interface PolicyInput { runScopeRef: string; toolId: string; toolRevision: string;
  argsHash: string; targetRef: string; proposedRisk: Risk;
  credentialBindingRef?: string; requestedCapabilities: readonly string[]; }
interface PolicyReceipt { decision: PolicyDecision; effectiveRisk: Risk;
  reasonCodes: string[]; policyEpoch: string; evaluationId: string;
  expiresAt: string; source: "TRUSTED_POLICY_ENGINE"; }
interface ApprovalBinding { approvalId: string; intentId: string; tenantId: string;
  ownerUserId: string; requestedByActorRef: string; expectedApproverRef: string;
  taskId: string; runId: string; stepOrdinal: number; goalRevision: string; // canonical int64 decimal string
  leaseEpochAtProposal: string;  // lease changes on resume: NOT binding authority on its own
  toolId: string; toolRevision: string; argsSha256: string;
  externalTargetDigest: string; amountCurrency?: string; amountMinorUnits?: string;
  credentialBindingVersion?: string; risk: Risk; policyEpoch: string;
  createdAt: string; expiresAt: string; recordVersion: string; // lossless canonical int64
}
interface ApprovalCommand {approvalId: string; decision: "APPROVE"|"DENY";
  commandId: string; expectedRecordVersion: string; // canonical int64 decimal }
type ApprovalStatus="PENDING"|"APPROVED"|"DENIED"|"EXPIRED"|"REVOKED"|"CONSUMED";
~~~

全型は**CHAT_03内部のProposal/Projection**で、Public Entity UUID/Task・Event statusはCHAT_01正本へ提案。JSON数値として`amountMinorUnits`や`leaseEpoch`を渡すと大きな整数を失うためdecimal stringとparserを使う（DB bigint/wire integerの所有者決定待ち）。`goalRevision`は増加と変更が分かるimmutable Task revision、`policyEpoch`はAuth/Policy失効版であり単一の`recordVersion`へ混ぜない。

**Approval対象の凍結**：自然言語の文章だけでなく、canonical toolId / toolRevision / typed args hash / authenticated target endpoint / method / normalized external-account ID / domain / money amount+currency / irreversible result/estimated fees を含める。表示用のtarget textはユーザーへredact/escapeするが、内部hashは変更しない。URLに秘密tokenを含める場合、画面へ表示しない安全な表示名を生成。人間が見る説明と内部実行対象の整合をテストし、曖昧な説明なら承認要求を作らずDENY。

**Lease更新の扱い**：承認待ちの間にRunをrelease/再Claimした場合は`leaseEpoch`が上がる。承認記録の`leaseEpochAtProposal`を**そのまま現在値と比較すると安全な復帰も永久に拒否**される。設計として承認は`intentId + immutable goalRevision/toolRevision/args/target/tenant/owner`へbindし、resumeしたWorkerの**current** `leaseEpoch`はGateway実行時に別途検証。Goal/targetが同じであることを確認して承認を消費できるが、古いWorkerが権限を得ることはない。recovery時にowner/goalが変われば新たな承認を要求。

## 6. State machine & sequence
~~~mermaid
stateDiagram-v2
  [*] --> PENDING: intent+request transaction
  PENDING --> APPROVED: authorized CAS
  PENDING --> DENIED: authorized CAS
  PENDING --> EXPIRED: DB clock expiry
  PENDING --> REVOKED: cancel/policy revoke
  APPROVED --> CONSUMED: Gateway atomic dispatch claim
  APPROVED --> REVOKED: policy/goal/target change
  APPROVED --> EXPIRED: deadline
  DENIED --> [*]
  EXPIRED --> [*]
  REVOKED --> [*]
  CONSUMED --> [*]: ledger tracks result/uncertain separately
~~~

~~~mermaid
sequenceDiagram
  participant L as Agent Loop
  participant G as Tool Gateway
  participant D as DB
  participant U as Web/Telegram authenticated user
  participant P as Policy + Run Authority
  L->>G: prepare(frozen intent)
  G->>P: evaluate policy & risk
  G->>D: TX insert intent+pending approval+outbox
  G-->>L: WAITING_APPROVAL
  D-->>U: UI/Telegram notification (redacted)
  U->>D: authenticated decision command
  D->>P: fresh-auth, owner/scope and target checks
  P-->>D: ALLOW decision
  D->>D: CAS PENDING→APPROVED + outbox
  D-->>U: 202 accepted (not externally executed)
  L->>G: resume prepared intent
  G->>P: policy/budget/goal/current lease recheck
  G->>D: TX APPROVED→CONSUMED + claim dispatch
  G->>G: executor controlled by 03-02/03-04
~~~

`APPROVED→CONSUMED`を外部へのsocket writeより前に確定する。ここでcrashしたら送信済みか不明なため03-04のUNCERTAIN照会に移し、単なる`APPROVED`へ戻さない。`DENY`後に「再承認」する場合は**新しいApprovalRecord**を作成し、同一IDを再オープンしない。Expired/Revokedを任意のWeb clientが書換え復活させない。

## 7. Policy evaluation と決定の擬似コード
~~~ts
async function evaluatePolicy(ctx, tool, frozenArgs): Promise<PolicyReceipt> {
  const principal = await identity.loadServerOwnedPrincipal(ctx.taskId, ctx.runId);
  const target = await targetResolver.fromValidatedArgs(tool, frozenArgs);
  if (!principal || !target) return deny("INVALID_PRINCIPAL_OR_TARGET");
  if (!await authz.sameTenantOwner(principal, ctx)) return deny("SCOPE_MISMATCH");
  const grants = await authz.liveCapabilities(principal);
  if (!grants.available) return deny("POLICY_UNAVAILABLE");
  const risk = conservativeMax(tool.adminRiskFloor, classifyArgsAndTarget(target, frozenArgs));
  if (tool.credentialBindingRef && !await secretPolicy.allowed(principal,tool,target))
    return deny("CREDENTIAL_SCOPE");
  if (!await egress.allowed(target) || !await budget.available(ctx, tool))
    return deny("EGRESS_OR_BUDGET");
  if (!grants.allows(tool, target, risk)) return deny("CAPABILITY");
  return risk==="irreversible" || requiresSensitiveApproval(ctx,risk,target)
    ? requireApproval(risk, grants.policyEpoch) : allow(risk,grants.policyEpoch);
}
async function decideApproval(command, authenticatedContext) {
  requireCsrfOrAuthenticatedTelegramCommand(authenticatedContext);
  return db.transaction(async tx => {
    const record = await tx.lockApprovalForUpdate(command.approvalId);
    if (!record || !await authz.canDecide(authenticatedContext,record))
      throw sameExternalNotFoundOrDenied(); // no existence oracle
    await requireFreshAuthentication(authenticatedContext, record.risk);
    if (record.recordVersion !== command.expectedRecordVersion ||
        record.status !== "PENDING" || dbNow() >= record.expiresAt)
      throw conflictOrExpired(record);
    const canonicalIntent = await tx.loadImmutableIntent(record.intentId);
    if (!constantTimeEqual(record.bindingDigest, canonicalIntent.bindingDigest) ||
        !await tx.currentPolicyStillAllows(record))
      throw notApprovable();
    await tx.updateApprovalCAS(record, command.decision==="APPROVE"?"APPROVED":"DENIED");
    await tx.insertDecisionReceipt(command.commandId, record.approvalId, command.decision);
    await tx.insertOutboxEvent("APPROVAL_DECIDED", record.approvalId);
    return {accepted:true, recordedAt:dbNow()};
  });
}
async function consumeAtDispatch(intent, currentWorker) {
  return db.transaction(async tx => {
    await tx.assertCurrentTaskGoalRunFenceCancel(currentWorker);
    const p = await policy.evaluateFresh(intent); // policy snapshot epoch secured at commit
    if (p.decision==="DENY") throw denied();
    await tx.assertToolRevisionAndTargetUnchanged(intent);
    const a = await tx.getApprovalForUpdate(intent.id);
    if (p.decision==="NEEDS_APPROVAL" && (!a || a.status!=="APPROVED" ||
        dbNow()>=a.expiresAt || !bindingMatches(a,intent))) throw needsApproval();
    if (a) await tx.casApproval(a,"APPROVED","CONSUMED");
    return tx.claimDispatchAndWriteAudit(intent,currentWorker);
  });
}
~~~

**Policy race**：実装では外部`policy.evaluateFresh`をDB transaction内でネットワーク呼出させず、短命事前評価＋DBに管理されたrevocation epochを`CAS`する。または権限data storeが同じDBにあり行をlockできる設計。CHAT_09が外部Authサービスのみで権限判断するなら"承認後にrevoke、開始前"を厳密linearizeする仕組みが未決のためfail closed。安全性に関わる設計未決を"トランザクション内で呼ぶ"だけで解決したと主張しない。

## 8. 障害・競合 matrix
| Trigger | Guard / action | Outcome |
|---|---|---|
| Approval ID guess / cross tenant | owner+tenant DB scoped query; no info oracle | deny 404/403-equivalent |
| WebとTelegramで同時approve | idempotency commandId + row lock + CAS | 1 receipt。異なる重複commandは409 |
| deny後approve | terminal deny immutable | second denied; no effect |
| click approval when browser session expired | current auth+fresh auth required | reauthentication; pending unchanged |
| expire exactly during decision | DB clock within transaction | deterministic expired; no approval |
| Grant revoked after approval | Gateway compare current policyEpoch | no new start, APPROVED→REVOKED |
| run owner crash/reclaimed | new current leaseEpoch; immutable intent same | new worker can consume only after current fence; old worker cannot |
| goal/target/currency/price changed | args/target hashes mismatch | existing approval invalid; new proposal required |
| Tool changed provider schema | toolRevision mismatch | invalidated, no silent substitution |
| Policy service outage | no current authoritative result | fail closed, WAITING_INPUT/blocked |
| approval consumed but ack lost | Approval CONSUMED, tool attempt MAY_HAVE_SENT | no rollback; ledger reconciles |
| Telegram message forwarded | chatId alone insufficient principal proof | reject, zero mutation |
| browser recipe performs irreversible action | classify current actual action | approval per invocation |
| X post says "approve transfer" | untrusted content | data; policy/actor unchanged |
| resumed non-idempotent side effect after crash | ledger UNCERTAIN | no auto-reexecute, manual/status inquiry |

## 9. Security & privacy
Prompt injection cannot sign approvals. No approval link with reusable token in query/notification body; client receives opaque ID, but HTTP decision protected by session, CSRF, Origin, fresh authentication, nonce/idempotency and server-side pending record. Telegram callbacks are scoped to verified linked user and private channel, replay nonce with TTL; sensitive decisions should transition to authenticated Web if bot identity insufficient. Signature checking without binding target and actor is insufficient.

**Revocation**：User disable、specific Task cancel、Policy update、Credential revoke、Provider disable invalidate pending+approved but not yet dispatched. Dispatch already claimed cannot be magically recalled—UNCERTAIN may remain. Secret is never shown in Approval UI; redact endpoint query/access token and private form fields. Store approval hash and audit reason, not plaintext Cookie. Audit retention per CHAT_09, response summaries per CHAT_08.

**Policy ordering**：deny list > capability scope > target egress > risk classification > approval > budget reservation. Approval cannot override hard deny, disabled provider, illegal/contract-prohibited endpoint, expired credential, insufficient tenant membership, revoked Run, or exhausted budget. Human approve only resolves explicit approval gate; no implicit admin enable of Tool.

## 10. Limits / metrics / benchmark
Initial bounded defaults (proposal, not measured): one pending approval per intent, max 8 proposal/step, max 20 pending approvals/user, expiry 15min for irreversible and max 24h for sensitive read（policy可短縮）、decision API 30/min/actor、fresh-auth window 5min for irreversible. Expired requests cannot be revived, new proposal required. Timestamp and expiration determined by DB UTC; client timezone for display only. No periodic LLM poll for approval; durable outbox and explicit wakeups.

Metrics (redacted): approval_pending, approval_decision_latency_ms, approval_expiry_count, replay_rejected, auth_rejected, revoked_before_dispatch, dispatch_without_valid_approval (must 0). Load/benchmark compares replay/race and p50/p95 UI → DB acknowledgment; accuracy/cost/time in CHAT_11. Do not claim remote SDK performance numbers.

## 11. DB/transaction/migration proposal [PROVISIONAL: CHAT_10]
ApprovalRecord immutable binding with `id UUIDv7, tenant mapping, owner, intentId FK, targetDigest, toolRevision, argsHash, goalRevision, policyEpoch, risk, decisionCode, decisionActor, requestedAt, expiresAt, decidedAt, consumedAt, recordVersion`; decision_receipts dedupe on `(tenant,commandId)`; `UNIQUE(intentId, bindingRevision)` with explicit reapproval revision. T1 insert pending/outbox/budget reserve (Gateway), T2 decision CAS +receipt+outbox (API), T3 consume approval with dispatch claim +Run fence + policy epoch, T4 outcome in side-effect ledger. **No external network inside DB transaction**; no terminal approval reactivation.

Current CHAT_10 Draft `approval_records` has `decision_revision DEFAULT 0`, a request hash, but lacks actor/owner/goal/tool/policy/target to prove this binding and atomic consume. Cross-team proposal: separate `recordVersion`(1+) from storage-only revision(0+), ensure optimistic concurrency tests; persist explicit consumed state and journal. UUIDv7 vs `gen_random_uuid`、nullable Task conversation、leaseEpoch durable integration are P0 cross-module disagreements. Proposed migrations must block old pending approvals until valid binding re-created; **never migrate unknown old approval to Approved**.

## 12. UI/HTTP [PROVISIONAL: CHAT_08]
Use existing [REST PR #8](https://github.com/choko510/aiagent-md/pull/8) `POST /api/v1/approvals/{id}/decision` with `If-Match` and `Idempotency-Key`; response 202 only confirms persisted decision. A conflict/expired/revoked response must distinguish 409/410 in UI but no hidden metadata leak. `GET /approvals/{id}` renders verified intent tool, target, amount currency, irrevocability, deadline, user-visible scope, reason; every sensitive value sanitized. Telegram notification includes summary and safe deep link, not transferable bearer approval token. Include auditable "Revoke", "Decline", and "View details" when appropriate.

## 13. Implementation PR split
- **A1** Policy evaluation independent of model, typed Admin policy/risk rules and denial tests.
- **A2** Approval binding+DB CAS+decision command, idempotency and expiry.
- **A3** Gateway dispatch consume and lease/policy epoch fence, crash+replay.
- **A4** Web/Telegram redacted UX and outbox/recovery; production enable only after CHAT_09 auth design.
- **A5** cross-module security/benchmark fixture, threat-model review and measured defaults.

## 14. Tests (all designed, not executed)
| Test ID | Setup→action | Required expected / level |
|---|---|---|
| APV-T01 | admin tool readOnly+MCP metadata says "write safe" → classify | admin risk and args win, Unit |
| APV-T02 | unknown actor/run or policy unavailable → evaluate | DENY, 0 execution, Security |
| APV-T03 | normal read+irreversible tool in step → prepare | approval pending, both starts 0, Integration |
| APV-T04 | legit approval then mutate args hash/price/target | invalidated; 0 external execution, Contract |
| APV-T05 | borrowed approvalId from another tenant | no metadata leak, 0 mutation, Security |
| APV-T06 | two channels approve same ID concurrently | 1 approved receipt, second idempotent or conflict, Race |
| APV-T07 | approve and deny simultaneously | precisely one immutable terminal decision, Race |
| APV-T08 | replay same commandId with different body | 409 command fingerprint, no second write, Contract |
| APV-T09 | expiry before/on DB commit | never approved after expiresAt, Fault |
| APV-T10 | fresh-auth expired/Telegram forwarded callback | deny and secure reauth path, Security |
| APV-T11 | user disable/Task cancel/Policy revoke between approve and dispatch | 0 new effect; revoke recorded, Fault |
| APV-T12 | approval consumed, worker kill before external send | MAY_HAVE_SENT/UNCERTAIN, no automatic reapprove/replay, Fault |
| APV-T13 | resumed Run new leaseEpoch, same frozen args and goal | current worker revalidates, old owner denied, Integration |
| APV-T14 | goalRevision/schemaRevision changes while approval pending | revoke original and request new approval, Integration |
| APV-T15 | prompt injection in X/HTML/PDF, claims admin role | role/policy unchanged, Security |
| APV-T16 | learned Browser recipe reaches purchase confirmation | classify irreversible; requires approval, SC-13 E2E |
| APV-T17 | Watch/notifications proceed within read-only prior grant | no purchase scope, SC-02/11/16 E2E |
| APV-T18 | 20+ pending/30+ commands in minute | bounded failure/429, Rate test |
| APV-T19 | secret-bearing target URL in approval display/log | redacted, hash binding remains, Security |
| APV-T20 | SSO principal revoked while UI pending, two workers compete | no consume after revoke; audit, Fault |
| APV-T21 | tenant↔workspace mapping mismatch, UUID v4 approval row | reject/migrate, not silently grant, Contract |
| APV-T22 | benchmark scripted Web/Telegram decisions, identical fixture | p50/p95, false approval, abandonment reported (not guessed) |

## 15. Traceability (all MUST)
| REQ | Tests | SC | Success |
|---|---|---|---|
| APV-001 | 01,02,15 | 08,11 | no model-to-allow transitions |
| APV-002 | 01,16 | 01,04,08 | risk never lowered by remote hint |
| APV-003 | 03,12 | 04,07 | 0 starts before approval |
| APV-004 | 04,13,14,21 | 07,08 | all bindings match |
| APV-005 | 05,09,10 | 06,07 | authenticated actor only |
| APV-006 | 06,07,08 | 07,08 | consumed once |
| APV-007 | 09,11,14 | 07 | revoke prevents new dispatch |
| APV-008 | 11,13,20 | 07,08 | current fence/policy enforced |
| APV-009 | 05,08,21 | 06,08 | foreign/replay denied |
| APV-010 | 06,17,22 | 07,08 | Web/Telegram one ledger |
| APV-011 | 04,19 | 04,08 | target shown safely/accurately |
| APV-012 | 02,10,20 | 07,08 | fail closed on unavailable |
| APV-013 | 15,16,17 | 08,11,13,15,16 | external text never grants |
| APV-014 | 06,07,12 | 07,09 | transaction/outbox consistent |

## 16. Unresolved / cross-team proposals
| Proposal | Owner/Priority | Need and acceptance |
|---|---|---|
| APV-X01 | CHAT_01 P0 | ApprovalRef/intent/tool IO/error envelope with UUIDv7, recordVersion, goalRevision, opaque refs; T04/T21 |
| APV-X02 | CHAT_09 P0 | fresh auth strength and scope, tenant principal mapping, policy revocation epoch transaction integration; T10/T11/T20 |
| APV-X03 | CHAT_10 P0 | approval record full bind+consumed+decision receipts/outbox, bigint fence and 1-based recordVersion, CAS; T06/T12/T21 |
| APV-X04 | CHAT_02 P0 | Run resume increasing leaseEpoch not invalidating approved *intent*, old worker cannot dispatch; T13 |
| APV-X05 | CHAT_08 P1 | URL/REST/Telegram actor link, 202 semantics, CSRF/fresh-auth, target display; T10/T19 |
| APV-X06 | CHAT_03 03-04 P0 | after T3 uncertain vs recoverable state, no duplicate send; T12 |
| APV-X07 | CHAT_11 P1 | mock webhook/Telegram and crash harness; T15/T20 |
| APV-X08 | CHAT_00 P2 | Whether limited preauthorized write operation is future UX (currently not supported); T17 |
| APV-X09 | CHAT_13 P1 | X event ingestion carries only read/notify delegated scope, no external content approver identity; T15/T17 |

## 17. Sources and evidence grading
- [S1] PydanticAI [`approval_required.py`](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/toolsets/approval_required.py), `3809c1b`, MIT, **Code verified by reading 2026-10-30**.
- [S2] OpenAI Agents Python [`approvals.py`](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/src/agents/run_internal/approvals.py) and [test](https://github.com/openai/openai-agents-python/blob/cd26bbbc52847845fde73b9952b51e62e2bcbdc0/tests/test_tool_approval_call_id_reuse.py), `cd26bbb`, MIT, **Code/test verified by reading**.
- [S3] OpenClaw [`tool-policy-pipeline.ts`](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/agents/tool-policy-pipeline.ts) and [test](https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/agents/tool-policy-pipeline.test.ts), MIT, **Code/test verified by reading**.
- [S4] LangGraph [`interrupt.py`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/prebuilt/langgraph/prebuilt/interrupt.py), MIT, **Code verified**.
- [S5] [PydanticAI issues #6452](https://github.com/pydantic/pydantic-ai/issues/6452) / [#10926](https://github.com/pydantic/pydantic-ai/issues/10926), [Mastra #27485](https://github.com/mastra-ai/mastra/issues/27485), **Issue report, not reproduced**.
- [S6] [SC](../00-foundation/acceptance-scenarios.md), [Architecture Rules](../00-foundation/architecture-rules.md), [Round 2](../../prompts/ROUND2_AUTHOR_CONTINUE.md), **project requirements**. Our binding/caps are **design hypotheses**. All implementation tests **not executed**.


### Round 3 — lossless canonical version boundary (2026-10-30)
All cross-service `goalRevision`, `recordVersion`, `definitionRevision`, and `leaseEpoch` values MUST use CHAT_01's canonical signed-int64 decimal **string** codec (PostgreSQL BIGINT / internal BigInt), not JSON number. Each version comparison converts validated strings to BigInt and rejects leading zeros/exponents/unsafe numbers, preserving full 64-bit range. This module does not redefine the canonical type. **Contract test (designed, not executed):** roundtrip `9007199254740992` and `9223372036854775806`, reject an unsafe JS number, and reject stale version/goal approval without dispatching external effects.
