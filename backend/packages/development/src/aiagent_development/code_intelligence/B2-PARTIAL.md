# Slice B2: partial snapshot preparation

`prepare_projection` verifies a complete immutable Git tree against the identity
and tree selected by its trusted caller. It creates an in-memory manifest of
blob IDs, SHA-256 digests and lengths. It rejects incomplete listings, links,
submodules, LFS pointers and resource-limit failures. It does not transfer or
materialize the files. The caller-provided identity is not authentication, and
the manifest is not a grant, an execution receipt, or proof of isolation.

B2 is **PARTIAL**. There is no indexer runner or artifact broker here. The
existing supervisor accepts `exec.run` authenticated through the tool gateway,
but provides no execution-bound artifact download, source projection upload,
execution-time lease revocation or source/output separation contract. The
Context artifact store is scoped to its own authenticated Context flow and is
not an execution artifact transfer. The supervisor now bounds retained stdout and stderr while reading and terminates the
process group on overflow. This does not establish a secure artifact transfer. Reusing that channel for SCIP or declaring a mock to be
an authenticated broker would be unsafe.

Remaining owner work includes authenticated per-execution source and artifact
transfer, atomic cancellation/lease checks at publication, safe no-follow file
handling and bounded streaming, immutable source projection, executable and
image attestation, and execution enforcement. These must be tested against the
real supervisor and authority, including egress and revocation; only then can
the actual index be passed to `load_index()` with trusted evidence. A verified
manifest alone cannot establish any of these properties. No host execution,
package install, network upload or production integration is performed here.

Projection selects Python source and pyproject.toml, excludes sensitive filenames,
and rejects private-key markers. Content is not proven secret-free: coverage is
always PARTIAL. Neither the manifest nor selection authorizes transfer.

## Sandbox result contract and WSL diagnostics

Gateway `DONE`/`CONFIRMED` records settlement of the Tool intent and effect,
not exit-zero or complete command output. `SandboxCellExecutor` now returns
`outcome`, `exitCode`, `timedOut` and `truncated`; malformed responses are
UNKNOWN. Output clipping by either the Supervisor or Gateway marks truncation.
Development evidence rejects timeouts, truncated or inconsistent results; a
nonzero completed exit remains a failed command, not successful evidence.
CPU/OOM limits cannot currently be distinguished reliably from a signal or
nonzero exit, so callers must not infer a specific resource cause.

WSL diagnostic (not production Cell verification): the venv Python resolves
under the user's home directory, outside the Supervisor Landlock system and
workspace allowlists, so it cannot be executed after confinement. Using
`/usr/bin/python3` instead led to bootstrap exit 125. `strace` showed fork/clone
returning EAGAIN; the test supervisor sets RLIMIT_NPROC=32. In a temporary test
copy, raising this limit to 256 allowed the same command to start. Linux counts
NPROC by real UID; visible process counts alone do not establish the accounting
within nested user namespaces. User/PID/network namespace creation and a simple
fork succeeded in a separate diagnostic; the calling process reported seccomp
mode 0 before the Supervisor's filter. This is not proof of Cell or network
isolation. No production policy was relaxed or changed for this diagnostic.

## Development execution evidence: blocked pending owner contracts

The current `ExecutionRequest` carries a plan reference, not an actual Test ID;
`exec.run` does not return a verified immutable workspace commit or an
execution-bound artifact. The real ToolGatewaySandboxAdapter therefore refuses
before creating an intent or running a command for build, static-check and test.
It does not invent a test ID or claim that the base commit was executed. Mock
ports remain test fixtures and are not execution evidence. The Workflow checks
any supplied source SHA against its expected target, and requires the committed
SHA for post-commit receipts; planned test receipts must name a required Test ID.

To restore the real adapter, the owners must specify an authenticated receipt
binding tenant, run, lease, intent, execution, workspace revision, actual
verified source commit, and, for test executions, a trusted Plan Test ID and
command/recipe binding. Workflow must compare those values to its own expected
commit and plan under the existing authority. This is a proposal, not a new
canonical grant. Merely accepting request/model values as proof is forbidden.

The existing `artifact_sha256` has no documented output artifact contract. The
adapter no longer hashes a 4,096-character display excerpt and calls it an
artifact. A future owner contract must specify the exact artifact bytes,
encoding and framing (including stdout/stderr boundaries), its completeness,
digest, scope, and authenticated transfer. Display excerpts must be separately
UTF-8 byte bounded and explicitly marked as excerpts; truncation of required
output must reject evidence. No digest is emitted by the blocked adapter.

## B2b owner-boundary audit

The Workflow now refuses executed build, static-check or test receipts without
a source commit and compares any supplied SHA with the Workflow target. This
is a necessary consistency check only: a populated SHA or `artifact_sha256`
in the existing ExecutionEvidence is not authenticated proof. Fixture ports
use synthetic values and must never be used as production attestation. The real
ToolGatewaySandboxAdapter remains blocked pending the owner contracts below.

The Run Authority already validates current owner, run, lease, goal and active
state in canonical database transactions. The current sandbox grant binds argv,
cwd, tenant, task, run, intent and lease at admission, but does not bind an
execution artifact, verified workspace tree or active revocation stream. The
Context Artifact Store has its own scoped claim and integrity checks, but its
raw/summary storage does not implement an execution-bound Cell transfer or an
atomic publication against a workspace revision. Neither is an appropriate
stand-in for the requested broker.

Required owner work: (1) a Gateway-admitted execution identity and receipt
bound to verified source tree, workspace revision, recipe, Test ID and pinned
toolchain/image; (2) bounded authenticated Source and Artifact transfer with
no-follow file access and Source/output separation; (3) live cancellation and
lease notification to the running supervisor; (4) canonical DB transaction
serializing artifact publication with Run/Goal/Lease/Workspace revocation; and
(5) explicit artifact bytes/digest semantics and concurrency/E2E tests. Until
these exist, there is no authenticated transfer or publication, and no
production receipt should be created from self-declared fields.

WSL diagnostic snapshot: UID 1000 had 6 visible processes and threads; shell
soft/hard RLIMIT_NPROC were 62081; cgroup pids.current was 2235 and pids.max
was `max`; user/PID/network namespace creation was available; the inspecting
shell's seccomp mode was 0. These are observations only. The Supervisor applies
its own seccomp filter and a test NPROC limit of 32. Linux UID/user-namespace
accounting and concurrent workloads must be measured for a dedicated Cell UID
before choosing a limit; 256 from a temporary diagnostic is not a production
recommendation. Separate-guest-kernel Cell and egress isolation remain
NOT VERIFIED.

## B2b-1: Tool Attempt binding is not Execution attestation

The existing Tool Gateway owns `agent.tool_attempts.id`, minted at dispatch and
stored with its Tool Intent and Run lease. It is an attempt identifier, not a
canonical Execution ID; no existing owner in this repository currently issues
a distinct execution identity bound to a verified Workspace snapshot, tree,
recipe, toolchain and Plan Test ID. The Gateway now includes its own attempt ID
in the signed Sandbox grant. The Supervisor echoes the ID from the verified
grant and the Gateway rejects an unmatched response as UNKNOWN. A fake Cell
contract test verifies this binding and signed-grant tampering. This binding
alone must not be used to authorize B2 or construct an authenticated execution
receipt. Current legacy handling is described below.

The ToolGatewaySandboxAdapter stays blocked. The following remain missing:
Authority-issued execution identity with tenant/owner/run/lease/goal and Intent;
trusted Workspace binding to Commit/Tree/Revision; a Plan-owned Test ID bound
to the approved recipe; toolchain attestation; and a trusted receipt that binds
actual result and Artifact bytes. A future owner must define atomic checks and
revocation semantics before this path can be enabled. A fake Cell is not a real
Supervisor E2E or proof of isolation; no Source/Artifact transfer is added.

Compatibility: Supervisor now rejects unversioned legacy grants by default. The
low-level verifier exposes an explicit allow_legacy option for controlled
compatibility use; the Supervisor does not enable it. Older Supervisors reject
the versioned field. Mixed-version operation is not verified.

## B2b-2: attempt binding version and real-DB fixture

Attempt binding version 1 is a signed field and requires an attempt ID. An
absent field is legacy and is rejected by the Supervisor; unknown or malformed versions are rejected.
The existing Gateway emits v1 when it has a Gateway-owned attempt ID. This is a
versioned Tool Attempt binding, not a B2b Execution contract or a verified
Workspace/Plan/Artifact binding. No version number upgrades legacy evidence to
B2b identity, and old Supervisors reject the additional field. Deployment
requires explicit capability negotiation; mixed versions remain unverified.

An isolated PostgreSQL fixture and a Fake Cell exercise the existing Authority
and Gateway: the signed grant's attempt ID matches the Tool Gateway's DB row,
foreign attempt responses settle UNKNOWN, and stale/cancelled requests are
refused before the fake Cell. This test does not authenticate a workspace or
artifact, check cancellation during execution, establish atomic publication,
or exercise a real Cell. The real Development adapter remains fail-closed.

The existing Tool Attempt, Run fence, Workspace projection and Context Artifact
have different owners and do not together supply an authenticated Execution
record. An owner-approved cross-service Execution contract is needed for
verified Commit/Tree/Revision, Plan Test ID and recipe/toolchain binding, with
canonical DB serialization of publication against cancellation and lease
revocation. Artifact transfer must be authenticated before enabling the adapter.
These owner changes are not implemented by inventing a Development-owned
canonical table or treating the Fake Cell output as authority.


## B2b-3: explicit attempt boundary and Workspace Owner gap

SandboxCellExecutor now refuses a missing or malformed Gateway-issued Attempt ID
before contacting a Cell; it always emits version 1. Supervisor-side verification
rejects unversioned grants unless the caller explicitly requests legacy
compatibility, which the Supervisor does not. This version protects Tool Attempt
binding only; it does not assert verified source or B2b execution identity.

The existing `agent.workspaces` stores membership and ownership, and
`PostgresWorkspacePort.assert_scope` checks active membership. Neither supplies
a verified immutable Commit, Git Tree or Workspace Revision. The TaskPlan holds
Test IDs but no trusted approved-recipe binding. Existing Run Authority and
Gateway admission therefore cannot produce the requested verified binding.
The Development adapter remains blocked; no new Development-owned canonical DB,
self-attested signed fields, Artifact Broker or publication transaction is
introduced. An Owner-approved snapshot and recipe authority plus canonical
Execution/Attempt binding and authenticated Artifact transfer remain required.

## Versioned grant rollout (wire-level compatibility only)

A regression matrix compares the current verifier to the pre-attempt grant
implementation from main `ac8185c4b9ceb7a1c7b52e296e50331e4d783065`:
old Gateway grant / old Supervisor verifier accepts; old / new rejects with
`LEGACY_DISABLED`; new v1 / old rejects as malformed; new v1 / new accepts.
The matrix exercises signed grant formats, not deployed binaries or race-free
rolling upgrades. The v1 Gateway requires an Attempt ID. The low-level legacy
verifier opt-in is not enabled by the Supervisor and is not a production
migration mechanism.

A safe mixed-version online rollout is not established. For a planned cutover,
stop new sandbox admission, wait for in-flight executions to finish, and resolve
UNKNOWN outcomes through the existing authority; do not retry blindly. Keep the
Supervisor's consumed-grant store and keys intact, upgrade Supervisor and
Gateway while admission is stopped, verify the versions and v1 behavior, then
resume. Rollback requires the same quiescence and compatible versions. In-flight
requests, lost responses, host failure and replay-store continuity require
operator handling; this procedure is not a verified production runbook or a
proof of VM isolation. Development's real adapter remains blocked.
