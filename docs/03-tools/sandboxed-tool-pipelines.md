# Sandboxed Tool Pipelines — Detailed Implementation Specification (03-06)

> Owner: CHAT_03 | Canonical: `docs/03-tools/sandboxed-tool-pipelines.md` | Status: **Draft [PROVISIONAL]** | Revision: 0.2 | Research date: 2026-10-30 JST | main SHA: `b45d4e989998663a00ef09673e69ae1f4191f00d`
>
> Dependencies: CHAT_01 Tool IO / Task / Run / Event / Evidence; CHAT_02 Run Budget / Checkpoint / Steer; CHAT_03 Registry, Gateway, Approval, Ledger, MCP Adapter; CHAT_04 BrowserSession; CHAT_05 Connectors; CHAT_09 Auth/Egress/Sandbox; CHAT_10 DB; CHAT_11 QA; CHAT_12–15 extension consumers.

## 0. 採用する設計

モデルが記述する任意JavaScript/Python/シェルを実行する「Code Mode」ではなく、**厳密な型付き宣言的DAG（有向非巡回グラフ）と、固定された純粋変換ライブラリ**を初期実装とする。検索、HTTP取得、Connector API、確定的なフィルタ・集約・数値計算を一回の計画から複数実行できる。ただし**すべての外部Tool呼出は既存Tool Gatewayへ送る**。Pipeline独自のネットワーク、Cookie、任意filesystem、ホストShell、LLM生成コード評価、Docker socketを持たない。

初期フェーズではread-onlyで副作用がないと**Gatewayが判断した**Toolノードと、監査済み純粋変換のみ許可する。購入・予約確定・投稿・送信・送金・破壊的変更など、書込みを含むDAGは拒否し、通常の個別Gateway＋承認経路に戻す。学習済みBrowser recipeやXイベント起動Taskでもこの制約は同じ。将来write pipelineを許すなら追加ADR、承認と副作用台帳の整合テストが必須。

DAGの成功は各ノードが**実際にコミット済みのEvidence**を持つという意味であり、TaskのGoal達成はCHAT_02 Verifierが最新goalRevisionで別途判定する。未確認価格・空室・読み取れない金額は0ではなくUnknown/Partial。無期限のLLM自己再帰・無限pollは禁止。

## 1. 目的・非責務・SC
対象は一度に多数のread-only Toolを並列・段階的に実行し、Artifactから必要部分だけ集約する汎用処理。利用例：SC-01商品/買取価格複数取得、SC-03 X検索、SC-04 3人総額条件の比較、SC-05複数一次資料、SC-02 watchの有限な1回の取得、SC-10未知候補発見、SC-11イベント起動、SC-12差分処理、SC-14トレンド評価、SC-15アップロード資料解析。

対象外：ホストOS操作、任意Script/Runtime、無制限なネットワーク、Browser WorkerのPage直接所有、独自副作用/承認/Secret管理、バッチによる未承認の予約や購入、Watch/イベント/通知の正本変更、DAGの自己増殖。SC-06 Browser隔離、SC-07再起動、SC-08 prompt injection、SC-09費用計測、SC-13 recipe無断再生防止、SC-16ユーザー設定からの権限昇格防止を横断要件とする。

## 2. OSS一次資料比較：実コードとテスト
| OSS / commit / license | 実ファイル・関数・テストと観測 | 採否 |
|---|---|---|
| OpenClaw [`24e2a8f`](https://github.com/openclaw/openclaw/tree/24e2a8f80c809e572192194dab99f0bec2b0f743) MIT | [`src/agents/code-mode-execution.ts::runCodeModeExec`](https://github.com/openclaw/openclaw/blob/24e2a8f80c809e572192194dab99f0bec2b0f743/src/agents/code-mode-execution.ts#L82-L184) にdeadline、ToolSearchRuntime、worker実行の分離。[`code-mode.test.ts`](https://github.com/openclaw/openclaw/blob/24e2a8f80c809e572192194dab99f0bec2b0f743/src/agents/code-mode.test.ts#L693-L805) にstore byte制限、wait時の変更可視性、provenance継承テスト。 | Code/test inspected **未実行**。Resource budgets/非信頼provenanceを参照。任意コードmodeは本製品の初期要求を超すため導入しない。 |
| PydanticAI [`3809c1b`](https://github.com/pydantic/pydantic-ai/tree/3809c1b861036197de4a06ae3ee38e27e5455100) MIT | [`code_mode/_toolset.py::CodeModeResourceLimits/_resolve_resource_limits`](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/src/pydantic_ai_harness/pydantic_ai_harness/code_mode/_toolset.py#L314-L364) はCPU duration/memory/suspensionの有限デフォルトを定義し、明示unlimited optionもある。[`code_mode/_capability.py`](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/src/pydantic_ai_harness/pydantic_ai_harness/code_mode/_capability.py#L58-L95)。 | Code inspected。上限設計は参照、unlimitedを採用しない。Python実行機構は直接流用しない。 |
| Mastra [`2c6da6f`](https://github.com/mastra-ai/mastra/tree/2c6da6f449f7385475a843e26e05834450303a89) Apache-2.0（非ee） | [`packages/core/src/workflows/default.ts::DefaultExecutionEngine.serializeSnapshotWrite`](https://github.com/mastra-ai/mastra/blob/2c6da6f449f7385475a843e26e05834450303a89/packages/core/src/workflows/default.ts#L120-L161) はRun単位のsnapshot writeを直列化し、並列armsが古いcheckpointで上書きする競合に対処。ソースコメントにIssue #26214。 | Code inspected。DAGのcheckpoint/CAS要件の根拠。独自SDKへの全面依存は却下。Issue本文は未検証。 |
| LangGraph [`ffc3336`](https://github.com/langchain-ai/langgraph/tree/ffc3336367e8139668d11e5f0b0e6def57e7c762) MIT | [`libs/langgraph/langgraph/pregel/_runner.py::PregelRunner.tick`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_runner.py#L132-L250) はTaskの並行実行、commit、例外/interruptを調停。 | Code inspected。有限DAGのready schedulingに適用、一般retry規則を副作用には移さない。 |
| 上流問題報告 | [LangGraph #9397](https://github.com/langchain-ai/langgraph/issues/9397) は並列ノードが完了してもcheckpointに残らず復帰で再実行した問題。 | **Issue report**。こちらで未再現。Harnessの実行保証とみなさない。 |

### 2.1 代替案
A: LLM生成JS/PythonをVMで実行 → 高い表現力に対しsandbox escape/権限の間接昇格/無制限loop/egressが難しいため**初期不採用**。B: 専用固定Workflowのみ → 商品やホテル用途に偏り、汎用Agentの目標に反するため**却下**。C: **型付きDAG + function allowlist + Gateway** → 汎用的な多段データ処理と安全境界の両立で**採用**。D: 大量ページすべてを巨大LLM promptへ → Context/Cost/証拠欠落が増えやすく、性能実測までは採らない。

コードのライセンスを無視した転載は行わず抽象的なパターンのみ採用する。性能比較を別環境のベンチ数字で断定しない。

## 3. Owner・信頼境界
~~~text
LLM/User/Event untrusted DAG proposal
        ↓
PipelineCompiler (CHAT_03): schema validate, acyclic, version, caps
        ↓
DB immutable plan + budget reservation (CHAT_10/02)
        ↓
PipelineWorker: finite topological scheduler, current Run fence
        ├─ read_tool → Gateway (CHAT_03: policy + approval + ledger)
        │                  └─ Browser Worker / MCP / Connector by owner
        └─ pure_transform → static audited function library (no I/O)
        ↓
Node Receipt CAS + Artifact/Evidence reference (CHAT_01/10)
        ↓
AgentLoop Verifier (CHAT_02): goal success is separate
~~~

- CHAT_01 owns public Entity UUIDv7, Task/Run/Event/Error/Artifact/Evidence. Pipeline local identifiers are opaque until CHAT_01 approves an Entity. ToolRef is **not** a UUID auth grant。
- CHAT_02 owns Task/Run claim, cancel, pause/steer, goal revision, budget. Scheduler cannot initiate a new Task or expand allowed capabilities。
- CHAT_03 Gateway owns external call; Pipeline only dispatches it。CHAT_04 owns BrowserSession, never parallel direct Page calls。CHAT_09 owns tenant identity, secrets, SSRF and process sandbox。CHAT_10 owns DDL/atomic outbox。
- Source content (HTML/X/PDF/MCP Tool descriptions) is data and cannot add DAG nodes or mutate Policy. Current authenticated principal must be rehydrated from DB, never from user-provided plan fields。

## 4. Requirements
| REQ | Level | Measurable result | SC |
|---|---|---|---|
| PIP-001 | MUST | Versioned declarative DSL only; reject arbitrary eval/import/script/host command | 08 |
| PIP-002 | MUST | Compile checks typed edges, missing refs, duplicate node IDs, cycles, max depth/fanout before dispatch | 07,09 |
| PIP-003 | MUST | All external Tool nodes use Tool Gateway registry/policy/approval/ledger, no shortcut | 07,08,11 |
| PIP-004 | MUST | Phase-1 external write nodes denied, including mislabeled read remote tool | 01,04,08 |
| PIP-005 | MUST | Effective capabilities = Task/Run authority intersection Tool scope; nested work cannot add authority | 08,11 |
| PIP-006 | MUST | Finite nodes, concurrency, provider quotas, CPU, bytes, token/monetary budgets | 09 |
| PIP-007 | MUST | Topological order with deterministic tie-break and stable pure transformations | 05,07 |
| PIP-008 | MUST | Node intent/outcome persist before downstream scheduling, restart won't blindly replay effect | 07 |
| PIP-009 | MUST | Cancel, Steer, expired leaseEpoch/goalRevision/policyEpoch stop new starts | 07 |
| PIP-010 | MUST | Partial/unreadable/unknown source stays Unknown with provenance, never 0 or confirmed success | 01,04,07 |
| PIP-011 | MUST | Untrusted document/tool content cannot rewrite DAG or policy/capability/secret | 08,11,15 |
| PIP-012 | MUST | Transform has no filesystem/process/network/secret; all SSRF/egress checks through Gateway | 06,08 |
| PIP-013 | MUST | Model-facing results bounded and raw artifacts securely referenced with evidence | 05,09,15 |
| PIP-014 | MUST | Aggregate reservation + deadline + bounded retry; no infinite loops or unlimited subagents | 07,09 |
| PIP-015 | MUST | Immutable pinned planHash/Tool revision/Goal revision/args hash/provenance, versioned resume | 07 |
| PIP-016 | MUST | Authenticate owner/Tenant UUIDv7 and map workspace scope; no cross-tenant cache reuse | 07,08 |
| PIP-017 | SHOULD | Parallel independent read nodes and aggregate/de-duplicate without breaking source freshness | 01,05,09 |
| PIP-018 | SHOULD | Fixed functions for map/filter/sort/group/dedupe/decimal sum; reject arbitrary lambda | 01,10,14 |
| PIP-019 | LATER | Approval-aware write DAG through a new ADR and fault-injection proof | 07,08 |
| PIP-020 | EXPERIMENT | Reconsider tightly sandboxed code only after threat model+performance measurements | 09 |

## 5. Local TypeScript Contracts / Schema
Types below are **CHAT_03-local projections**; authoritative shared ToolIO/Task/Event and versioning belong CHAT_01, SQL CHAT_10.

~~~ts
type EntityId = string;      // validated canonical UUIDv7 generated server-side
type ToolRef = string;       // opaque Run-scoped Registry mapping, NOT grant
type NodeKind = "read_tool" | "pure_transform";
type FixedFn = "filter"|"map_fields"|"sort"|"group_by"|"dedupe"|"sum_decimal"|"merge";
type PipelineNode =
  | { id: string; kind: "read_tool"; toolRef: ToolRef;
      dependsOn: readonly string[]; argsTemplate: Readonly<Record<string, unknown>>;
      outputTypeId: string; requestBudgetUnits: number }
  | { id: string; kind: "pure_transform"; functionId: FixedFn;
      inputNodeIds: readonly string[]; params: Readonly<Record<string, unknown>>;
      outputTypeId: string };
interface PipelineDefinition {
  schemaVersion: "1.0"; pipelineRef: string; taskRef: EntityId; runRef: EntityId;
  expectedGoalRevision:string; nodes: readonly PipelineNode[];
  requestedOutputNodeIds: readonly string[];
}
interface CompiledPlan {
  planHash: string; immutableNodeOrder: readonly string[];
  runRef: EntityId; goalRevision:string; policyEpoch: string;
  toolRevisionBindings: readonly {nodeId:string; toolId:string; revision:string}[];
  maxQuoteUsd: string; expiresAt: string;
}
interface NodeReceipt {
  planHash:string; nodeId:string; intentRef?:string; toolRevision?:string;
  status:"PENDING"|"RUNNING"|"DONE"|"FAILED"|"PARTIAL"|"UNCERTAIN";
  argsHash:string; outputArtifactRef?:string; sourceRefs:readonly string[];
  observedAt?:string; recordVersion:string;
}
interface PipelinePort {
  compile(untrusted:unknown,runRef:EntityId):Promise<CompiledPlan>;
  run(planHash:string,runRef:EntityId):Promise<NodeReceipt[]>;
  resume(planHash:string,runRef:EntityId):Promise<NodeReceipt[]>;
  cancel(planHash:string,runRef:EntityId):Promise<void>;
}
~~~

Allowed `argsTemplate` is a JSON object whose dynamic references are only typed `outputs.<nodeId>.<approvedField>` selections. No dynamic property indexing, code strings, JS expressions, prototype fields (`__proto__` / `constructor` / `prototype`), unbounded glob, lambda, regex supplied without static review, arbitrary HTTP URL string interpolation, secret/policy/role interpolation. Omitted and null fields do not silently coerce to zero. Functions version pinned and implemented in audited library, not downloaded from URLs. Monetary operations use exact decimal/minor-unit arithmetic and currency equality, not JS float for profit.

~~~json
{
  "schemaVersion": "1.0",
  "pipelineRef": "internal-opaque-plan",
  "taskRef": "019a2fc0-4b21-7af0-9c22-34f78b6a1d04",
  "runRef": "019a2fc0-4b21-7af0-9c22-34f78b6a1d05",
  "expectedGoalRevision": 1,
  "nodes": [
    {"id":"a","kind":"read_tool","toolRef":"scoped-1","dependsOn":[],
     "argsTemplate":{"query":"product prices"},"outputTypeId":"SearchResult","requestBudgetUnits":1},
    {"id":"b","kind":"read_tool","toolRef":"scoped-2","dependsOn":[],
     "argsTemplate":{"query":"buyback prices"},"outputTypeId":"SearchResult","requestBudgetUnits":1},
    {"id":"merge","kind":"pure_transform","functionId":"merge",
     "inputNodeIds":["a","b"],"params":{},"outputTypeId":"MergedSearch"}
  ],
  "requestedOutputNodeIds":["merge"]
}
~~~

**Finite initial caps (proposed, not benchmarked):** 32 nodes, max depth 8, max fanout 8, max 4 simultaneous read calls per Run, max 1 per BrowserSession, 2/provider if no lower provider limit; definition 64KiB, args 64KiB/node, fixed transform at most 1,000 items and 8MiB cumulative intermediate memory, model excerpt 8KiB/node and 128KiB/plan, default deadline 30s (explicit finite higher policy possible), max 2 retries for proven safe read-only Tool, and zero additional LLM calls inside first-generation Pipeline Worker. No `unlimited` setting. Caps reviewed by CHAT_11 and CHAT_02 for actual host measurements.

## 6. State, sequence and step barrier
~~~mermaid
stateDiagram-v2
  [*] --> VALIDATING
  VALIDATING --> REJECTED: invalid/overbudget/unauthorized
  VALIDATING --> READY: durable immutable plan
  READY --> RUNNING: current Run fence
  RUNNING --> RUNNING: verified node checkpoint+next topological layer
  RUNNING --> PARTIAL: independent node failed
  RUNNING --> UNCERTAIN: ledger reports ambiguous effect
  RUNNING --> DONE: all mandatory result nodes confirmed
  RUNNING --> PAUSED: safe boundary pause
  RUNNING --> CANCELLED: cancel prevents new starts
  PAUSED --> RUNNING: fresh lease+policy+goal check
~~~
These are local Pipeline status labels, not CHAT_01 Task/Run state authority; DONE still requires CHAT_02 goal verifier.

~~~mermaid
sequenceDiagram
  participant L as Agent Loop
  participant C as Compiler
  participant W as Pipeline Worker
  participant G as Gateway
  participant D as DB & Artifact
  L->>C: untrusted DAG + RunRef
  C->>C: validate nodes, schema, authority, budget
  C->>D: immutable plan+budget reservation
  D-->>W: planRef + Run lease
  loop finite topological layers
    W->>D: load current goal+Run leaseEpoch+ready nodes
    W->>G: prepare/execute read-only Tool intents
    G->>D: durable intent+outcome+evidence
    G-->>W: approved result or partial/uncertain
    W->>D: CAS node receipts and checkpoint/outbox
  end
  W-->>L: bounded summary, Evidence refs
~~~

**Barrier:** The CHAT_02 AgentLoop's full-step approval barrier remains authoritative. Pipeline cannot start a read Tool in the same step while an irreversible Tool proposal awaits approval. The first iteration prohibits write nodes, so any unexpected policy reclassification => halt/deny the DAG, not attempt autoapproval.

## 7. Compiler/Executor pseudocode
~~~ts
async function compile(raw: unknown, runRef: string) {
  const ctx = await runManager.loadTrustedRunFromDb(runRef);
  const def = strictPipelineSchema(raw);      // unknown authority keys rejected
  if (def.expectedGoalRevision !== ctx.goalRevision) throw StaleGoal();
  assertWithinCaps(def, LIMITS);
  const order = stableKahnTopologicalSort(def.nodes); // throws for cycle/dangling deps
  const toolBindings = [];
  for (const node of order) {
    if (node.kind === "pure_transform") {
      const fn = trustedFixedTransformRegistry.get(node.functionId);
      if (!fn || !fn.isPure || !fn.bounded) throw InvalidTransform();
      assertTypedInputsAndOutputs(node, def, fn);
    } else {
      const binding = await registry.resolveForGateway(node.toolRef, ctx);
      const evaluated = await policy.classifyValidatedArgsAndTarget(ctx,binding,node.argsTemplate);
      if (!evaluated.provablyPureRead) throw WriteToolForbidden();
      assertSchemaRevisionScopeAndProviderLimits(binding, ctx);
      toolBindings.push(binding);
    }
  }
  const quote = await budget.upperBoundCost(def, toolBindings);
  if (!quote.finite || !await budget.canReserve(ctx, quote)) throw OverBudget();
  return await store.commitImmutablePlanAndReservationCAS(ctx,def,order,toolBindings,quote);
}
async function execute(planRef: string, runRef: string, signal: AbortSignal) {
  const plan = await store.getImmutablePlanForRun(planRef,runRef);
  while (true) { // bounded by finite node count/deadline/remaining budget
    const ctx = await runManager.loadTrustedRunFromDb(runRef);
    await assertCurrentLeaseGoalPolicyBudgetAndDeadline(ctx,plan);
    const receipts = await store.getCommittedReceipts(plan);
    if (mandatoryOutputsConfirmed(plan,receipts))
      return summarizeFromEvidence(plan,receipts);
    const ready = await store.claimReadyNodesCAS(plan,ctx.leaseEpoch,4);
    if (ready.length === 0) return summarizePartialOrSuspend(plan,receipts);
    await runAtMost(ready,4,async node => {
      const result = node.kind === "read_tool"
        ? await gateway.prepareAndExecuteFrozenRead(ctx,node,plan)
        : await fixedTransforms.runBounded(node,receipts);
      await store.casPersistNodeReceiptAndOutbox(ctx,plan,node,result);
    },signal);
  }
}
async function resume(planRef:string,runRef:string) {
  const ctx = await runManager.reclaimSameRunAndIncrementLeaseEpoch(runRef);
  const plan = await store.getImmutablePlanForRun(planRef,runRef);
  if (plan.goalRevision !== ctx.goalRevision || !await policy.stillAuthorizes(plan,ctx))
    return NeedsRecompile();
  await ledger.reconcileMayHaveSentNodes(plan); // no blind write replay
  return execute(planRef,runRef,ctx.signal);
}
~~~

`gateway.prepareAndExecuteFrozenRead` is shorthand for the same 03-02 Gateway `prepareRunStep` and `executePrepared` Port; it cannot implement a second executor or waive full-step approval gating. Node key is stable `(runId, planHash, nodeId, immutable argsHash)`, but provider-facing idempotency and side-effect recovery belong 03-04. Same RunId after crash, larger leaseEpoch; a new attempt after terminal failure is a **new RunId**. Plan can't replace current Task goal on Steer. Confirmed node result can be reused only with pinned policy/source freshness and evidence; invalidating cache causes revalidation not covert replay.

Pure transform semantics: `sum_decimal` requires same currency and bounded precision, `dedupe` never treats different storage/colour/capacity as identical without CHAT_12 Entity Resolver, `merge` preserves source URL, observedAt, and evidence confidence, `group_by` and `sort` are stable with finite keys. Every output carries typed Unknown/Partial where required, not an invented real-time observation.

## 8. Failure / recovery matrix
| Case | Guard | Recovery/result |
|---|---|---|
| Arbitrary Script/module/import/eval or host path | strict DSL parser | reject, 0 Tool starts |
| Cycle, self-ref, duplicate ID, missing dependency | compile Kahn + uniqueness | reject before budget reserve |
| Graph size/fanout/depth/bytes over cap | deterministic limits | fail early; no LLM loop |
| Tool named "read" actually writes | dynamic target/risk policy | reject entire plan; use normal Gateway |
| Cross-tenant borrowed ToolRef or Artifact | owner+Run DB verification | deny, no hidden metadata |
| Two Workers claim same node | DB CAS + Run leaseEpoch | only current claimant commits |
| Worker kill before node intent commit | Gateway no outside send | safe re-prepare same logical node |
| Worker kill after Tool mayHaveSent | Ledger UNCERTAIN | status inquiry/manual; no blind replay |
| Cancel or Steer mid-layer | new dispatch fence per node | in-flight results preserved; new starts 0 |
| BrowserSession reused in parallel | CHAT_04 ownership lock | serialize/defer, no duplicate Page owner |
| Cache hit after credential revocation | fresh policy epoch | unusable, no private data reuse |
| HTML/X/PDF embeds "extend pipeline" instructions | data trust tier | graph immutable, no privilege escalation |
| SSRF via redirect or computed URL | Gateway egress before every hop | denied |
| Missing hotel price/incorrect headcount | type/goal validator | Unknown/Partial, not cheap room |
| Output with null/false/zero | actual JSON Schema validation | preserve legitimate scalar data |
| Raw Tool output too large | Artifact, excerpt byte caps | explicit truncated/partial |
| Transform CPU/RAM exhausted | hard process resource guard | bounded partial/error, no OOM cascade |
| Concurrent checkpoint writes arrive out of order | CAS monotonic recordVersion | old write rejected |
| Provider 429/timeout | bounded safe-read retry policy | no unbounded provider requests |
| Older saved Plan after schema update | pinned revision mismatch | recompile under current policy |
| Restored backup missing external results | ledger reconciliation gate | never assume external action absent |
| X event duplicates cause same Task trigger | CHAT_13 event dedupe + Gateway | no new authority |
| Dataset shows price 0 on failed fetch | typed Unknown | does not create false-profit estimate |

## 9. Security
No runnable model-generated code, dynamic import, JS prototype access, arbitrary regex, filesystem/process/network globals or unrestricted library functions. Transform worker has confined OS credentials and a **fixed whitelist** of pure operations; actual process sandbox and user namespace/container capabilities CHAT_09 owns. Regex/text transformations, if later added, require precompiled bounded patterns, not arbitrary backtracking expressions.

All network access—including browser/connector/MCP—routes through Gateway and CHAT_09 SSRF/egress (DNS rebinding, redirect, private/link-local/metadata IPv4/IPv6) and per-service authorization. Tool outputs treated as data. No Secret/Cookie/API key presented to model, DAG args, logs, checkpoints, output excerpt; only secure credential bindings inside trusted executor. Cache includes tenant/owner/credential version/source provenance and freshness; never shares private outputs cross-owner.

Audit `planHash, runId, toolId/revision, goalRevision, leaseEpoch, policyEpoch, nodeReceiptVersion, argsHash, quote/actual usage, evidence refs, observedAt` with redacted details. Do not use model's claimed tenant/Role/approval. Phase-1 write prohibition is enforced **again at Gateway**, not only graph compiler.

## 10. Observability / limits / benchmark
Counters/histograms: `pipeline_compile_ms`, `pipeline_nodes_per_plan`, `pipeline_ready_queue_depth`, `pipeline_tool_latency_ms`, `pipeline_gateway_denied_total`, `pipeline_uncertain_total`, `pipeline_budget_reject_total`, `pipeline_bytes_in_out`, `pipeline_checkpoint_conflict_total`, `pipeline_cache_hit_total`, `pipeline_total_usd`. Avoid high-cardinality IDs as Metric labels; protected traces may correlate intent.

CHAT_11 Benchmark fixture: 100 immutable source pages and 20 research questions; same model, host, Tool budget and source snapshots. Compare sequential Tools vs 4-way DAG, measure p50/p95, #API calls, #model tokens, USD/task, RSS, CPU, evidence correctness, source timestamp preservation, false SKU merges and unauthorized effects. No result claims until executed. Safety targets in synthetic fixture: unauthorized calls 0, cross-tenant leakage 0, unbounded loops 0, duplicated nonidempotent effects 0.

## 11. Persistence, transaction, migration [PROVISIONAL: CHAT_10]
Logical stores: `pipeline_plan` (version, immutable hash, run/task owner, goalRevision, tool revisions, budget), `pipeline_node_receipt` (stable node ID, current claim fence, expected recordVersion, state, Artifact/evidence refs), `pipeline_checkpoint` (ordered committed nodes, output refs), `pipeline_outbox` (through shared Event/outbox, no independent sender).

T1 validated immutable Plan+budget reservation/outbox commit before any Tool. T2 each Tool node invokes Gateway (which persists intent and effect ledger before external request), pure transforms commit local data only. T3 node CAS commit with `leaseEpoch`, goalRevision, expectedRecordVersion; never overwrite newer checkpoint. T4 fan-in consumes only confirmed node receipts with same planHash, propagates Uncertain/Partial and preserves source timestamp. Data artifacts persisted separately with orphan cleanup; missing Artifact does not become a confirmed value.

**Known cross-team P0 inconsistencies (not silently repaired):** CHAT_01 public Entity UUIDv7 vs CHAT_10 default `gen_random_uuid()`; logical `recordVersion=1` vs SQL `revision DEFAULT 0`; nullable `Task.conversationId` vs SQL NOT NULL/CHAT_08 required; CHAT_01 `leaseEpoch:string` vs CHAT_02 bigint and incomplete DB fencing; `tenantId`/`ownerUserId` vs SQL `workspace_id`. Requires owner-approved migration/codec and integration tests before Locked. No SQL tables redefined here.

## 12. UI/API proposals [PROVISIONAL: CHAT_08]
Read-only per Task: pipeline list, node status, provenance/source/time, partial/unavailable reason, estimated vs actual usage, conditional resume/cancel commands. User interface must distinguish Plan accepted from external calls started and Evidence confirmed. Telegram only controls approved Task state and cannot submit arbitrary executable code. Expose Artifacts via authenticated bounded read, not raw signed private URLs in model/chat. The actual REST routes/types belong CHAT_08.

## 13. Implementation PR phases
1. P1 strict DSL parser, typed dependency validator, deterministic Kahn compiler, allowlisted pure transforms and zero-network tests.
2. P2 immutable Plan persistence, budget reservations, node receipt CAS, outbox integration with CHAT_10 and lease/Goal contract.
3. P3 Gateway-only read Tool node dispatch, provider quotas, bounded fan-out, source/Artifact normalization.
4. P4 cancel/steer/restart/lease takeover, browser locks and safe Gateway-ledger reconciliation.
5. P5 fault injection, security fuzz, measured performance and capability audits; release gate requires CHAT_01/09/10 P0 closure. Write DAG beyond Phase-1 requires separate ADR.

## 14. Tests — designed, **not executed**
| TEST-ID | Setup → Action | Expected |
|---|---|---|
| PIP-T01 | Script/eval/shell/SQL AST in plan | parse fail; executor calls 0 |
| PIP-T02 | cyclic/self/dangling/duplicate node IDs | compiler fail before budget |
| PIP-T03 | 33 nodes / depth 9 / fanout 9 | finite rejection |
| PIP-T04 | plan supplies tenantId/policyEpoch/secretRef | unknown keys rejected |
| PIP-T05 | misclassified read Tool points to purchase POST | deny as write; no effect |
| PIP-T06 | 2 independent search nodes | bounded parallel, stable merge |
| PIP-T07 | BrowserSession two simultaneous actions | exclusive owner, no direct Page access |
| PIP-T08 | 2 workers claim same node | CAS one winner |
| PIP-T09 | kill worker after Tool send before checkpoint | ledger UNCERTAIN, no second effect |
| PIP-T10 | cancel/steer just before next node | no new Tool starts |
| PIP-T11 | revoke tool/provider after compile | Gateway freshness check denies |
| PIP-T12 | HTML/X/PDF asks to add nodes/exfiltrate secret | unchanged DAG/Policy |
| PIP-T13 | 169.254/::1/redirect/DNS rebinding Tool input | egress deny |
| PIP-T14 | 1001 items / 8MiB+ intermediate / oversized output | bounded partial/reject |
| PIP-T15 | missing price/wrong currency | Unknown, no coerced 0 |
| PIP-T16 | two models same name but different capacity | no false auto-merge |
| PIP-T17 | 3-person hotel total unverified | no "available" false result |
| PIP-T18 | concurrent checkpoint writes commit out of order | stale CAS fails |
| PIP-T19 | output contains token/private URL | model/log redacted |
| PIP-T20 | resume with old ToolRef/goalRevision | stale denied/recompiled |
| PIP-T21 | X event creates conversation-less Task | correct Run/tenant checks |
| PIP-T22 | Browser learned recipe / attachment script | policy not bypassed |
| PIP-T23 | cross-tenant cache hit | result not leaked |
| PIP-T24 | credential revoked while cache populated | fresh authorization denies |
| PIP-T25 | benchmark 100 pages / 20 tasks, 1 vs 4 parallel | report measured p95/CPU/USD/evidence only |
| PIP-T26 | UUIDv4 / recordVersion0 / overflowing lease / workspace mismatch | strict contract reject/migration |
| PIP-T27 | provider 429 and repeated transient faults | bounded retry and hard time/cost |
| PIP-T28 | one source fails in otherwise successful DAG | result explicitly Partial |

All use Mock/Fake providers, never real purchases or bookings. Code and upstream test inspection are **not** evidence of executing test cases in this repository.

## 15. REQ→TEST→SC traceability
| REQ | TESTs | SC | Acceptance |
|---|---|---|---|
| PIP-001 | T01,T04,T12 | 08 | arbitrary code executed 0 |
| PIP-002 | T02,T03,T26 | 07,09 | bad graph denied before I/O |
| PIP-003 | T05,T07,T09 | 07,08,11 | Gateway bypass 0 |
| PIP-004 | T05,T21,T22 | 01,04,08 | unauthorized writes 0 |
| PIP-005 | T04,T11,T23 | 08,11 | no widened capability |
| PIP-006 | T03,T14,T27 | 09 | finite resource use |
| PIP-007 | T06,T15,T16 | 05,07 | stable deterministic outputs |
| PIP-008 | T08,T09,T18 | 07 | durable checkpoint, no blind replay |
| PIP-009 | T10,T20,T24 | 07 | stale/cancel/steer starts 0 |
| PIP-010 | T15,T17,T28 | 01,04,07 | Unknown stays Unknown |
| PIP-011 | T04,T12,T22 | 08,11,15 | source instructions cannot rewrite plan |
| PIP-012 | T01,T13,T19 | 06,08 | no SSRF/secret/host escape |
| PIP-013 | T14,T19 | 05,09,15 | bounded model output |
| PIP-014 | T03,T14,T25,T27 | 07,09 | finite budget |
| PIP-015 | T09,T18,T20 | 07 | goal/tool/plan revision pinned |
| PIP-016 | T23,T24,T26 | 07,08 | tenant/artifact isolation |

PIP-017/018 SHOULD tested by T06/T15/T25. PIP-019 LATER needs new ADR. PIP-020 EXPERIMENT assessed only after T25 and threat review. `Txx` abbreviates `PIP-Txx` here.

## 16. Cross-team proposal / unresolved
| ID | Owner / Severity | Contract dependency and acceptance |
|---|---|---|
| PIP-X01 | CHAT_01 P0 | Pipeline/ToolCall/Artifact IDs and Event/Unknown outcome, UUIDv7 vs opaque; T09/T26 |
| PIP-X02 | CHAT_02 P0 | same RunId reclaim leaseEpoch, budget/goal/steer fence; T10/T20 |
| PIP-X03 | CHAT_09 P0 | Worker sandbox and capability/egress/tenant-policy mapping; T12/T13/T23 |
| PIP-X04 | CHAT_10 P0 | Plan/node CAS/outbox persistence, UUIDv7/version/nullable conversation/leaseEpoch migration; T08/T18/T26 |
| PIP-X05 | CHAT_03 (03-02/04) P0 | every node Tool through Gateway+Ledger, uncertainty and retries; T05/T09 |
| PIP-X06 | CHAT_04 P1 | BrowserSession single owner and lock; T07 |
| PIP-X07 | CHAT_05 P1 | API quota/transport batching and terms; T27 |
| PIP-X08 | CHAT_08 P1 | Pipeline progress/partial/artifact UX; T17/T28 |
| PIP-X09 | CHAT_11 P1 | concurrency+fault+benchmark fixture and finite targets; T18/T25 |
| PIP-X10 | CHAT_12/13/14/15 P1 | entity resolver/event/recipe/multimodal feeds data only, not permissions; T16/T21/T22 |
| PIP-X11 | CHAT_00 P1 | Code Mode/sandbox alternate deployment ADR; first release disallows arbitrary script |

Cannot be Locked until P0 contracts and cross-PR acceptance checked. All ownership edits are proposals, not modifications to other areas.

## 17. Sources and proof status
- [S1] [OpenClaw `runCodeModeExec`](https://github.com/openclaw/openclaw/blob/24e2a8f80c809e572192194dab99f0bec2b0f743/src/agents/code-mode-execution.ts#L82-L184), [`code-mode.test.ts`](https://github.com/openclaw/openclaw/blob/24e2a8f80c809e572192194dab99f0bec2b0f743/src/agents/code-mode.test.ts#L693-L805), commit `24e2a8f`, [MIT LICENSE](https://github.com/openclaw/openclaw/blob/24e2a8f80c809e572192194dab99f0bec2b0f743/LICENSE), inspected 2026-10-30 — **Code verified/test inspected, not executed**.
- [S2] [PydanticAI sandbox resource limits](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/src/pydantic_ai_harness/pydantic_ai_harness/code_mode/_toolset.py#L314-L364), [CodeMode](https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/src/pydantic_ai_harness/pydantic_ai_harness/code_mode/_capability.py#L58-L95), commit `3809c1b`, MIT — **Code verified**.
- [S3] [Mastra `serializeSnapshotWrite`](https://github.com/mastra-ai/mastra/blob/2c6da6f449f7385475a843e26e05834450303a89/packages/core/src/workflows/default.ts#L120-L161), commit `2c6da6f`, Apache-2.0 non-ee — **Code verified**.
- [S4] [LangGraph `PregelRunner.tick`](https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/langgraph/langgraph/pregel/_runner.py#L132-L250), MIT, [Issue #9397](https://github.com/langchain-ai/langgraph/issues/9397) = **Issue report only**, not reproduced.
- [S5] [Project Brief](../00-foundation/project-brief.md), [Architecture Rules](../00-foundation/architecture-rules.md), [SC-01..16](../00-foundation/acceptance-scenarios.md), [Round 2 prompt](../../prompts/ROUND2_AUTHOR_CONTINUE.md). These are requirements, not OSS runtime evidence.
- [S6] DSL shape, numerical caps, concurrency, cost and performance are **design hypotheses** only, require implementation and test. Project runtime tests actually run: **0**.


### Round 4 cross-owner int64 reconciliation (2026-10-30)
Version/Epoch fields above follow CHAT_01's canonical signed-int64 **decimal string** wire format, validated and compared with internal BigInt and persisted as PostgreSQL BIGINT; JSON number conversion is prohibited. Other scalar number fields (bounded counts, percentages, step ordinals) remain number unless separately versioned. Regression test proposed: `9007199254740992` and `9223372036854775806` round-trip exactly, reject unsafe Number and stale authority; not executed against Harness code.
