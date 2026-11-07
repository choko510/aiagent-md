# Sandbox Execution — 自律コード・Shell実行のRun契約

> Owner: CHAT_02 | Spec: 02-13 (proposed) | Status: Draft / PROVISIONAL | Updated: 2026-11-07 | Cross-owner dependencies: CHAT_01/03/04/06/09/10/11. No code exists and no product tests were run.

## 0. Goal and design

Enable general-purpose agent to write, compile, test and run tools inside **untrusted Runtime Cell**. This extends existing agent orchestration, not replaces it. Every command is an explicit canonical Tool Gateway intent **sandbox.exec** (or equivalent versioned tool name) and every process is an Execution child of the current Task/Run. Agent's generated code, CLI packages, output and stdout remain untrusted. Permission to launch a local process grants no permission to execute on host or send external requests. Shell access is powerful within the Cell and must be bounded, audited and revocable.

The pre-existing declarative DAG sandboxed-tool-pipelines remains useful for cheap deterministic read-only aggregation; do not delete it. Workloads choose deterministic function / typed pipeline first when practical, Sandbox Exec when program generation offers real benefit. Browser automation still passes the Browser Broker; arbitrary Playwright CDP attach or JavaScript injection into protected Browser Context is not granted by exec.

Runtime/Task/Run checkpoint and lease ownership remain with CHAT_02 task-run-manager and agent-loop; physical DB with CHAT_10; Tool intent, approval and side effects with CHAT_03; Sentinel and Cell admission with CHAT_09. A subprocess may use trusted APIs only via narrowly exposed gateway IPC. A direct network request cannot bypass Sentinel.

## 1. Proposed Tool contract, not yet shared-schema Locked

~~~typescript
type SandboxExecInput = {
  executionId: string; // server-generated UUID, CHAT_01 owns canonical format
  taskId: string; runId: string; goalRevision: string; leaseEpoch: string;
  toolIntentId: string; cellId: string; workspaceRevision: string;
  argv: readonly string[]; cwd: string; envNames: readonly string[];
  imageDigest: string; policyEpoch: string;
  limits: { wallMs: number; cpuMs: number; memoryBytes: number;
    pids: number; stdoutBytes: number; stderrBytes: number;
    diskBytes: number; networkBytes: number; };
  stdinArtifactRef?: string;
  requestedNetworkProfile: "none"|"sentinel_mediated";
};
type SandboxExecOutcome = {
  executionId: string; finalState: "succeeded"|"failed"|"cancelled"|
    "timed_out"|"blocked_unsafe"|"uncertain";
  exitCode?: number; signal?: string; startedAt?: string; endedAt?: string;
  stdoutArtifactRef?: string; stderrArtifactRef?: string;
  producedArtifacts: readonly {ref: string; sha256: string; sizeBytes: number}[];
  imageDigest: string; workspaceRevision: string; attestationRef: string;
  externalEffectsState: "none_verified"|"known"|"uncertain";
};
~~~

All unrecognized fields, environment names, or unregistered image digests must be rejected. No direct arbitrary host path in cwd; allow only scoped workspace-relative normalized paths. No raw credentials in env, stdin, filesystem or log. Only versioned opaque artifact references. Exit code zero means **process success only**; independent Goal Verifier determines Task success and user-facing facts must have Evidence timestamps.

## 2. Execution lifecycle and race rules

1. Model proposes script generation, patch, terminal command, test or package install. AgentLoop freezes command plus workspace generation and target profile.
2. Gateway validates the exact Tool schema, owner, Run/Goal lease, budget, side-effect classification and persisted intent. No external I/O before durable intent.
3. Cell Admission attests required KVM+Container boundary, mapped UID, seccomp/LSM/cgroup, image and Sentinel readiness. If absent: BLOCKED_UNSAFE. Never launch with --privileged/no-sandbox.
4. Host-side supervisor creates bounded process tree, scoped workspace overlay, deterministic environment allowlist and temporary pseudoterminal if needed. Subprocess identity carries current Run/lease/policy to Sentinel.
5. Execute inside Cell; stream bounded/redacted logs as tentative UI updates; final artifacts/usage/receipt become durable only on verified commit.
6. Cancel/Pause/Steer/revoke expires new-command authority immediately. Pause may stop dispatch; in-flight process is checkpointed only when safe. Cancel kills whole process tree and revokes IPC/egress grants. Old lease cannot commit.
7. On restart, reconcile previous status using Execution ID, process/cgroup evidence, Workspace revision and effect ledger. Never claim a dead process succeeded or replay a non-idempotent effect blindly.
8. Temporary Skills/scripts may be used by the current Task under inherited capabilities; promotion to persistent Skill requires CHAT_06 lifecycle and review.

Sandbox Exec may execute any local shell program allowed by the Cell, but it cannot expand its own effective capabilities, grant itself network, access Vault, install host packages, change protected runtime/Tool policy, or persist an always-on daemon outside managed Task/Watch lifecycle. Self-modification of task workspace scripts is supported; modifying protected Harness runtime source or production binaries is forbidden.

## 3. Parallelism, package management and quotas

Main Agent may dispatch bounded parallel read-only executions and subagents, each with parent Task identity, shared budget reservation, child cell/workspace boundary and least-privilege subset. Per-task caps cover process count, spawned executables, memory, storage, stderr, stdout, CPU, walltime, network and model tokens. Package installation is task-local and untrusted: reproducible lockfile+hash where feasible, package lifecycle scripts subject to same egress/exec policy; no pip/npm postinstall host access, no Docker-in-Docker or arbitrary privileged package managers. Expensive/unknown resource use terminates safely.

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
