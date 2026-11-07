# Sandbox Execution — 自律コード・Shell実行のRun契約

> Owner: CHAT_02 | Spec: 02-13 (proposed) | Status: Draft / PROVISIONAL | Updated: 2026-11-07 | Cross-owner dependencies: CHAT_01/03/04/06/09/10/11. No code exists and no product tests were run.

## 0. Goal and design

Enable general-purpose agent to write, compile, test and run tools inside **untrusted Execution VM's Runtime Cell**, separate from Trusted Control VM kernel. This extends existing agent orchestration, not replaces it. Every command is an explicit canonical Tool Gateway intent **sandbox.exec** (or equivalent versioned tool name) and every process is an Execution child of the current Task/Run. Agent's generated code, CLI packages, output and stdout remain untrusted. Permission to launch a local process grants no permission to execute on host or send external requests. Shell access is powerful within the Cell and must be bounded, audited and revocable.

The pre-existing declarative DAG sandboxed-tool-pipelines remains useful for cheap deterministic read-only aggregation; do not delete it. Workloads choose deterministic function / typed pipeline first when practical, Sandbox Exec when program generation offers real benefit. Browser automation still passes the Browser Broker; arbitrary Playwright CDP attach or JavaScript injection into protected Browser Context is not granted by exec.

Runtime/Task/Run checkpoint and lease ownership remain with CHAT_02 task-run-manager and agent-loop; physical DB with CHAT_10; Tool intent, approval and side effects with CHAT_03; Sentinel and Cell admission with CHAT_09. A subprocess may use trusted APIs only via narrowly exposed gateway IPC. A direct network request cannot bypass Sentinel.

## 1. Strict model proposal vs server-owned admission — P0-4 resolution

The LLM / Agent may provide only `SandboxExecProposalV1`. Server-generated ID, owner, tenant, Task/Run, GoalRevision/LeaseEpoch, ToolIntentId, Cell/VM identity, runtime profile, image, policy, actual budget and network mode **must never be accepted from a model response**. A strict schema rejects unknown keys, rather than ignoring them and accidentally trusting them downstream. These are logical candidate types pending CHAT_01/02/03/09 contract ratification. Machine-readable **strict schemas** and schema tests (also Draft) are at [Proposal](../../contracts/muse/sandbox-exec-proposal.schema.json), [Admission](../../contracts/muse/sandbox-exec-admission.schema.json), [ExternalAction](../../contracts/muse/external-action-proposal.schema.json) and [negative/positive tests](../../audit/muse/validate_contract_schemas.py). They are part of the cross-owner contract review, not optional implementation examples.

~~~typescript
type Int64DecimalString = string; // validated signed 64-bit decimal, parsed via BigInt
type SandboxExecProposalV1 = Readonly<{
  tool: "sandbox.exec";
  argv: readonly string[]; // nonempty, bounded; shell only via explicit interpreter
  cwdRelative: string; // task-workspace-relative normalized path
  stdinArtifactRef?: string;
  requestedPackageRefs?: readonly string[]; // from Package Broker, no raw URLs
  requestedLimits?: { wallMs?: number; memoryBytes?: number }; // hints only
}>;
type SandboxExecutionAdmissionV1 = Readonly<{
  executionId: string; toolIntentId: string; proposalDigest: string;
  ownerUserId: string; tenantId: string; taskId: string; runId: string;
  goalRevision: Int64DecimalString; leaseEpoch: Int64DecimalString;
  workspaceId: string; workspaceRevision: Int64DecimalString;
  cellId: string; vmIdentity: string; imageDigest: string; attestationRef: string;
  policyEpoch: string; policyDigest: string;
  runtimeProfile: "secure_isolated"|"restricted_compute"; // mandatory SERVER field
  networkMode: "none"|"broker_rpc_only"; // no direct Internet
  limits: { wallMs: number; cpuMs: number; memoryBytes: number; pids: number;
    stdoutBytes: number; stderrBytes: number; diskBytes: number; networkBytes: number };
  expiresAt: string; oneUseNonce: string;
}>;
type SandboxExecOutcomeV1 = Readonly<{
  executionId: string;
  finalState: "succeeded"|"failed"|"cancelled"|"timed_out"|
    "blocked_unsafe"|"uncertain";
  exitCode?: number; signal?: string; startedAt?: string; endedAt?: string;
  stdoutArtifactRef?: string; stderrArtifactRef?: string;
  producedArtifacts: readonly {ref:string;sha256:string;sizeBytes:number}[];
  imageDigest: string; workspaceRevision: Int64DecimalString;
  attestationRef: string; externalEffectsState:"none_verified"|"known"|"uncertain";
}>;
~~~

`runtimeProfile` is required in **Admission**, not in Proposal (01-04 Tool IO uses the same split). `development_only` is a deployment option, not a selectable production admission. A proposal containing `executionId`, `ownerUserId`, `taskId`, `leaseEpoch`, `policyEpoch`, `runtimeProfile`, `imageDigest`, `networkMode`, `cellId`, etc. fails validation. The server resolves current owner/tenant/Run fence from DB, checks the scoped Workspace/artifact handles and only then constructs Admission after immutable Gateway Intent commit.

## 1.1 Child code's external operations are separate intents — P0-5 resolution

Arbitrary guest programs are **offline by default** and cannot use `curl` or `https.request` to reach Internet. The only allowed outside communication is an authenticated `broker_rpc_only` interface; it exposes typed operations, **not a generic TCP proxy or CONNECT tunnel**.

~~~typescript
type ExternalActionProposalV1 = Readonly<{
  operation:"http.read"|"connector.invoke"|"browser.action"|"notify.send"|"package.fetch";
  args:unknown; // validated against versioned registered Tool schema
  requestId:string; // dedupe hint; NEVER an authentication grant
}>;
type ExternalActionReceiptV1 = Readonly<{
  externalIntentId:string; executionId:string; policyEpoch:string;
  status:"blocked"|"waiting_approval"|"committed"|"uncertain";
  outcomeRef?:string; resultArtifactRef?:string;
}>;
~~~

For **every** child/executable external operation: (1) Broker rehydrates trusted identity from VM/session/Execution, validates exact operation/method/recipient/body sensitivity/owner/cost, (2) Gateway persists **new independent Tool Intent + Side-effect Ledger record** BEFORE sending, (3) Sentinel authorizes final concrete action and data disclosure, (4) Trusted HTTP Fetch, Privsep Connector, Browser Broker or Notifier uses the **Enforced Egress Transport**, (5) immutable external receipt + Evidence/Artifact and usage commit, (6) bounded redacted result returns to guest. The original `sandbox.exec` intent is **not** authority for child HTTP/POST. If server/transport ACK is lost after possible send: status `uncertain` and **no blind retry**, even if shell process disappeared. Unsupported arbitrary raw TLS/CONNECT is denied; only typed broker RPC may read/write.

## 1.2 Execution process isolation — P0-6 resolution

**Agent Loop / Agent Orchestrator runs in Trusted Control VM**, with no untrusted generated-code execution in that process or guest. Execution Supervisor and generated shell run in a **distinct Untrusted Execution VM guest kernel**, with user namespaces, cgroups, seccomp and LSM as defense in depth. No shared UID/PID namespace, writable filesystem, privileged socket or CDP with Orchestrator/Gateway/Vault. Per-Execution RPC capability is single-use, TTL-limited, session/VM/Run/Lease/policy-bound, and limited to a narrow Tool subset; possession of guest VM identity/token alone cannot authorize arbitrary Tool calls. On revocation/Cancel/Steer stop new RPC, terminate complete process tree (cgroup.kill where available), verify empty cgroup and deny stale artifact commit. Separate subagent Task/Run has its own capability-budget shrink and Workspace scope.

## 2. Execution lifecycle and race rules

1. Model proposes script generation, patch, terminal command, test or package install. AgentLoop strictly parses only ProposalV1; Trusted Control Plane creates Admission with server-owned profile, image, current Run/lease, actual budget and Workspace.
2. Gateway validates the exact Tool schema, owner, Run/Goal lease, budget, side-effect classification and persisted intent. No external I/O before durable intent.
3. Cell Admission attests distinct Trusted/Untrusted VM guest kernels and Cell sandbox, mapped UID, seccomp/LSM/cgroup, image and Sentinel readiness. If absent: BLOCKED_UNSAFE. Never launch with --privileged/no-sandbox.
4. Host-side supervisor creates bounded process tree, scoped workspace overlay, deterministic environment allowlist and temporary pseudoterminal if needed. Subprocess has no public network; child Tool RPC is scoped to current Run/lease/policy and each action gets new Gateway Intent.
5. Execute inside Cell; stream bounded/redacted logs as tentative UI updates; final artifacts/usage/receipt become durable only on verified commit.
6. Cancel/Pause/Steer/revoke expires new-command authority immediately. Pause may stop dispatch; in-flight process is checkpointed only when safe. Cancel kills whole process tree and revokes IPC/egress grants. Old lease cannot commit.
7. On restart, reconcile previous status using Execution ID, process/cgroup evidence, Workspace revision and effect ledger. Never claim a dead process succeeded or replay a non-idempotent effect blindly.
8. Temporary Skills/scripts may be used by the current Task under inherited capabilities; promotion to persistent Skill requires CHAT_06 lifecycle and review.

Sandbox Exec may execute any local shell program allowed by the Cell, but it cannot expand its own effective capabilities, grant itself network, access Vault, install host packages, change protected runtime/Tool policy, or persist an always-on daemon outside managed Task/Watch lifecycle. Self-modification of task workspace scripts is supported; modifying protected Harness runtime source or production binaries is forbidden.

## 3. Parallelism, package management and quotas

Main Agent may dispatch bounded parallel read-only executions and subagents, each with parent Task identity, shared budget reservation, child cell/workspace boundary and least-privilege subset. Per-task caps cover process count, spawned executables, memory, storage, stderr, stdout, CPU, walltime, network and model tokens. Package installation is offline within Execution VM, only from immutable archives staged by a Trusted Package Broker after registry/source/version/hash/license/size checks. npm/pip lifecycle scripts run inside the same confined, networkless Cell. No direct npm/pip registry traffic, Docker-in-Docker or privileged package manager. Expensive/unknown resource use terminates safely.

Use task-owned temporary environment by default. Persistent workspace improvements require typed commit with user scope, conflict CAS, content digest, quota and review; restore never restores old auth/grants. Subagents must not inherit plaintext secrets, cookies or all workspace by default. Hand off only approved artifact slices.

## 4. Error model, evidence and security

New error proposals for CHAT_01: EXEC_UNSAFE_RUNTIME, EXEC_POLICY_DENIED, EXEC_NOT_FOUND, EXEC_RESOURCE_EXCEEDED, EXEC_PROCESS_LOST, EXEC_WORKSPACE_CONFLICT, EXEC_EGRESS_DENIED, EXEC_RESULT_UNCERTAIN. Map to existing canonical error taxonomy after owner review. States/events must distinguish process started vs run completed, intentional kill vs OOM vs timeout, local output vs external side effect. Signed/opaque receipts only; raw logs are capped and redacted. Source text copied from internet can remain dangerous prompt injection and executable code; promote neither to tools nor authoritative instructions automatically.

## 5. Acceptance tests and cross-owner changes

| ID | Acceptance |
|---|---|
| EXEC-T01 | Agent writes Python script, runs it in Cell, captures exit/stdout and produced artifact without host writes |
| EXEC-T02 | Runtime compiles/test small program; bound CPU/RAM/disk/process and code output; reports local process status distinctly from Task success |
| EXEC-T03 | Unknown tool image, traversal cwd, symlink escape, poisoned env, unexpected stdin and host file path rejected |
| EXEC-T04 | Package install and nested shell fail to reach forbidden files/network/metadata/secrets |
| EXEC-T05 | Cancel/Steer during subprocess spawning; no descendants survive nor stale evidence commits |
| EXEC-T06 | Crash/restart around external request yields UNCERTAIN, no duplicated purchase/post/booking |
| EXEC-T07 | Two simultaneous executions conflict on workspace generation, CAS rejected and explicit reconciliation |
| EXEC-T08 | Subagent receives narrower capabilities and budget; cannot request new privileged Tool |
| EXEC-T09 | Browser direct CDP attach and admin socket attempts fail despite successful local shell |
| EXEC-T10 | Direct unapproved HTTPS POST/QUIC/DNS exfil from script denied by Sentinel |
| EXEC-T11 | Agent-created temporary Skill works in one task, cannot silently become globally installed |
| EXEC-T12 | Developer-only profile and absent KVM attestation never silently claim production readiness |

Owner handoff: CHAT_01 canonical Execution/Workspace IDs, JSON schema, state/error/event; CHAT_02 AgentLoop, Context/Checkpoint, subagents, budget; CHAT_03 Registry/Gateway/Approval/Side effects; CHAT_06 Skill lifecycle; CHAT_08 command output and log UI; CHAT_09 confinement/Sentinel; CHAT_10 migrations/deployment; CHAT_11 isolation and fault tests. Do not declare LOCKED until all owners reconcile.

## 5.1 Pause/Steer/Cancel + Workspace continuity (2026-11-07)

| Trigger | New Exec / RPC | Running process | Persistent output / external effects |
|---|---|---|---|
| PAUSE | deny new, revoke nonessential grant | bounded cooperative stop, freeze only if safe and restorable; else kill | checkpoint confirmed Artifact, never replay effect blindly |
| STEER | invalidate old GoalRevision/Exec admission | kill old group; re-propose and re-admit | old result quarantined and effect reconciled |
| CANCEL / revoke | deny immediately | cgroup.kill/reap descendants, verify empty | stale Workspace/Run commit blocked; sent effect may be UNCERTAIN |
| VM loss / timeout | deny old session | determine actual state or mark UNKNOWN | restore only validated immutable Artifact hashes |

Workspace restore verifies a signed manifest of file hashes, provenance, owner scope, image and package version, retention expiry and malware/quarantine state. A missing/expired/quarantined Artifact blocks restore explicitly (no silent omission). Same-tenant Execution VM reuse requires complete cgroup/overlay/network/credential grant cleanup; different tenants require destroy/recreate VM. Idle TTL and VM/Cell concurrency must be finite operational settings with benchmark evidence.

EXEC-T13: Agent provides server-only keys in Proposal => DENY. EXEC-T14: raw curl/CONNECT to Internet => denied; typed broker http.read succeeds. EXEC-T15: forged Execution/broker token/replay of scoped action => denied. EXEC-T16: offline staged pip/npm works but lifecycle network is denied. EXEC-T17: Pause/Steer/Cancel kills descendants/revokes grants, effect remains UNKNOWN when sent. EXEC-T18: exact typed Connector write has its own persisted Intent/Approval/Sentinel/Transport receipt.

## 2026-11-07 DEV-05/06: Build/PreviewはCell Shellの権限ではない（Draft）

コード生成・依存インストール・ビルド・Testは本仕様のUntrusted VM sandbox.exec。Git provider/Package Broker/Preview Broker/Deployment等の**外部操作は子プロセスを含め別個のtyped Tool Intent**とし、Sentinel/Transportとeffect ledgerを必ず通す。モデルがrun image/port/runtimeProfile/host bridge/policyを指定して権限を得ることを拒否。初期Web/APIのToolchainはoffline staged; GradleやAndroid/Flutterなどのビルド時の暗黙fetchはBrokerで封じ、対応しない環境はUNAVAILABLEと明示。長時間Previewは独立した有限Execution+WorkspaceRevisionにbindして実行し、Task終了後無許可で生き続けない。Previewの外部からの閲覧は [DEV-05](../16-app-development/build-preview-release.md) の専用認証routeのみ、一般TCP・RAW CONNECT・LAN・CDP直通不可。DEV-BLD-T01..08/SC-29を所有者レビュー待ちの隔離受入試験に加える。
