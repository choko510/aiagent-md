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

## B2 source / artifact primitives (not an authenticated broker)

The projection encoder rechecks the immutable Git snapshot and emits bounded,
deterministic framed chunks with a SHA-256 over the exact complete wire bytes.
POSIX staging validates the expected scope/metadata, each SHA-256 and Git blob
ID, rejects unsafe paths, uses descriptor-relative no-follow creation, and
cleans temporary data on normal errors and context exit. It yields an open
source directory descriptor only after the complete framed digest matches.
The trusted parent must be private to the broker UID and inaccessible to the
Cell UID; the input stream and expected receipt are not authentication.
Blocking transports must enforce their own wall-time deadline. Crash recovery
and orphan cleanup are not implemented.

The Supervisor has a private, non-RPC source-descriptor execution hook. It
requires a root-owned source descriptor and a distinct non-root Cell UID. Its
Landlock rule requires ABI >= 3 and a disjoint source/output directory: source
is readable but not writable, and output remains writable. The descriptor must
come from trusted staging with no pre-existing writable aliases or hardlinks.
This hook is not wired to an Authority, Gateway or Source Broker; a signed
ordinary command grant alone does not authenticate its source snapshot.

An internal capture primitive reads only `index.scip`, rejects symlinks,
hardlinks, oversized files and detected replacement, and copies into an
unlinked read-only descriptor with a SHA-256 of the exact captured bytes. It
requires a private trusted parent and assumes all untrusted output writers have
already stopped. It does not authenticate execution, transfer an Artifact,
check cancellation/lease, or publish. An official scip-python fixture has been
captured and revalidated by the B1 decoder. Separately, an opt-in Docker test
ran scip-python on a new synthetic Git snapshot through the private Supervisor
hook, captured its output and queried B1 twice. That test bypasses authenticated
transport and canonical Execution authority and is not a production E2E.


The opt-in fixture recipe is in `scripts/scip_cell_fixture/`. Its Node binary,
indexer, OpenSSL configuration, source/output descriptors, empty environment
file and argv shape are fixed; the recipe digest covers the argv template and
environment digest. Image verification and execution authorization are not
implemented. The fixture grants its dedicated Cell group traverse-only access
to a source parent during execution while Landlock enforces source read-only;
this relies on per-task Landlock confinement and must not substitute for
authenticated transport or distinct-VM validation. Crash-time permission
restoration and orphan staging cleanup remain unimplemented.


A bounded Git tree proof is now independently checked against the caller's
expected commit and tree before the opt-in source stage. The check hashes raw
commit and tree objects, verifies selected paths and blob IDs, and rejects
missing, duplicate, malformed, extra and unsupported objects. The staging step
independently checks each Git blob ID and SHA-256 against the manifest. This
proves only object relationships relative to the supplied expected identity; it
does not authorize a repository or authenticate the execution. SHA-1 Git
object IDs remain legacy collision-sensitive and are marked `LEGACY_SHA1`.

## Execution preparation and transactional Artifact storage

Migration 0013 adds operator-provisioned repository bindings and recipe
approvals, verified Workspace snapshot metadata, and a distinct PREPARED
Execution ID in the existing canonical PostgreSQL database. The Workspace
operation independently verifies bounded Git object proof and source bytes,
then rechecks the scoped Run and binding under database locks. Recipe binding
checks the actual Tool Intent request digest, Plan content/Test ID, toolchain
and approved image digest. PREPARED is not an authorization to execute: the
provider authorization, running image attestation, authenticated source and
artifact transfer, and Supervisor receipt authority are not wired.

The Gateway Artifact owner encrypts bounded captured bytes and uses a database
transaction to recheck scope, Run, Lease, Goal, current snapshot, binding,
recipe, Plan and Tool Attempt before publication. DB tests use explicitly fake
capture and SCIP verifiers and an operator-simulated FINISHED state. No real
FINISHED transition or production verifier is configured; the Development
adapter remains fail-closed. Idempotency, cancellation-before-publication and
concurrent publication are tested against disposable PostgreSQL, but live
revocation during network transfer and real broker crash recovery remain open.
The operator-provisioned repository/recipe rows are trusted policy inputs;
provider authorization and image provenance are not established by their text
references alone. SHA-1 snapshot integrity remains LEGACY_SHA1 / PARTIAL.

The Supervisor's private execution hook can now consume a local revocation
event: when set, it kills and reaps the process group and discards partial
stdout/stderr. A real Docker Cell test covers the process-stop path. This event
is not yet fed by authenticated Run Authority polling or cancellation RPC;
a disconnected or failed authority therefore does not yet trigger it. It is
not a completed live-lease or production revocation implementation.

The Gateway now has an internal Run Authority watcher that signals a local
revocation event when the canonical claim becomes stale, is cancelled, the DB
fails, or an authority check exceeds its timeout. A ready event is set only
after the first successful check. The real-PostgreSQL test covers a committed
Run cancellation; unit tests cover failure and stalled authority. The watcher
is not yet connected to an authenticated remote Cell cancellation channel or
started by the production adapter. A stop signal must only be used after the
execution has ended and publication authority has been rechecked. The local
poller cannot terminate a remote process during transport failure; live Cell
lease revocation remains incomplete.

The private projected-execution path now registers its signed Execution/Attempt
binding while active. An authenticated Tool Gateway `exec.cancel` RPC can only
signal an exact tenant, Execution, Attempt and binding digest match; mismatches
return no cancellation. Docker Cell tests exercise a real running Supervisor,
a rejected wrong RPC key, wrong scope and successful scoped cancellation.
A Gateway relay helper forwards a local Authority-watch event through that RPC.
These pieces are not yet orchestrated by a production execution path. In
particular, loss of the RPC channel does not yet trigger an independent Cell
heartbeat watchdog, and cancellation racing initial registration is not closed.
No Artifact may be considered safe to publish on this basis alone.

A Cell-side watchdog now expires a projected execution when a scoped
heartbeat stops, kills/reaps the process and refuses late renewal. Heartbeats
use authenticated RPC and must match tenant/Execution/Attempt/binding. The
internal Gateway helper checks canonical Run authority before each heartbeat and
fails closed on DB/RPC error. Real Docker Cell tests cover timeout and scoped
heartbeat/cancellation. This remains unwired to the actual source broker and
production dispatch; solving registration races,
Supervisor restart/process cleanup, and real transport failure are still required.

Projected execution now requires a finite 0.1–10 second watchdog window;
it cannot start through the private hook without one. Generic sandbox.exec is
unchanged. There is still no production orchestration or authenticated Source
Broker, and Supervisor process-death cleanup is not yet guaranteed.

Projected executions now request a parent-death SIGKILL in the Supervisor
launcher and PID-namespace child, using a pidfd to detect the fork/prctl race.
A projected-only seccomp rule prevents the child from clearing or weakening
that signal. Docker tests kill the Supervisor after the child starts and verify
that the synthetic child cannot create its later marker. This is defense in
depth, not a cgroup-empty proof: privileged/set-ID exec semantics, VM restart,
orphan staging cleanup and production kernel isolation remain unverified.

A private Supervisor prototype now validates the signed Execution binding before
launch, requires a clean zero exit and no pre-existing `index.scip`, captures
bytes through the bounded no-follow helper, and signs the exact digest/size with
a separate receipt key. Docker tests exercise a real Supervisor and reject
binding mismatch, stale pre-existing output and nonzero exit. Real DB tests
exercise signer/verifier interoperability using a synthetic FINISHED state.
This remains unexposed by RPC: per-Execution output isolation, authenticated
transfer, real completion transition, key provisioning, image attestation and
revocation during capture/publication are still required. In particular, the
existing Task workspace is shared and must not be considered a safe production
Artifact origin merely because an output file was absent before execution.

The sandbox now provides a private per-Execution output-directory primitive.
It accepts only a validated projected Grant identity, creates an exclusive
UUIDv7-named directory beneath a root-owned private parent without following
symlinks, and removes it on normal context exit. Docker tests reject duplicate
execution directories, forged traversal identifiers and symlinked parents.
This primitive is not yet connected to Supervisor dispatch or Artifact capture;
the current private capture prototype still uses a shared Task workspace and
must remain disabled for production. Crash cleanup and authenticated broker
ownership are still required.

### Supervisor capture fixture update

The private projected capture hook now creates or validates an exclusive output directory for the signed Execution ID and rejects replayed projected Executions after Supervisor restart. A privileged Docker test ran scip-python twice using a read-only Git projection, an Execution-specific output descriptor, Supervisor-signed capture and the B1 decoder. Both artifacts were 1,362 bytes with 11 occurrences and PARTIAL coverage; definition and cross-file reference queries succeeded. The signing key, Grant and binding in that fixture are test-only. This is not an authenticated Source/Artifact RPC, DB lifecycle integration, image attestation or a production Development workflow. Replay protection requires Supervisors to share the same private journal and filesystem lock; storage loss, distributed replicas on different journals and crash-orphan cleanup remain open. A failed or interrupted execution must not be treated as a published Artifact.

### Source RPC staging prototype

An opt-in, process-local session registry now connects the existing authenticated
Supervisor RPC and binary frame receiver to Git-proof staging over a Unix socket.
The test checks the staged file and rejection of replay, wrong transfer key,
wrong binding, revoked authority callback, trailing bytes and truncated frames.
The session registry is **not** enabled by the Supervisor entry point. Test
registration alone is not canonical DB authority and cannot admit execution.
Production work must provide independently authenticated cross-VM registration
bound to canonical RUNNING authority, durable replay protection, deadline and
revocation throughout staging, and a safe asynchronous handoff to the existing
execution/capture path. Artifact transfer, atomic publication, recovery,
Development Adapter and end-to-end DB/Supervisor/SCIP validation remain open.

The source staging hook can now await an asynchronous consumer while keeping the
verified directory descriptor and staging context open. An opt-in Docker test
uses this hook to connect the real Supervisor RPC to a real `scip-python` run,
signed capture, and B1 definition/reference lookup. Its session registration,
Grant, binding, and keys are test-only: the Gateway does not yet authorize or
dispatch this path. Normal Supervisor startup still disables source sessions.
On graceful server shutdown active RPC handlers are cancelled and awaited before
the server closes; crash and cross-VM reconciliation remain unimplemented.

The trusted Gateway can now atomically transition a prepared Execution to
RUNNING and issue one short-lived source ticket in PostgreSQL migration 0014.
The ticket commits to the canonical Execution/Attempt/Snapshot/Recipe, Run and
Lease, source digest and manifest. An authenticated `source.register` RPC can
validate that ticket, metadata and a Supervisor-owned authority callback, then
persist session and execution replay fences in the Supervisor journal. The
registration is opt-in and unavailable without a server-configured authority
factory; the normal entry point does not configure one. The PostgreSQL/socket
test uses a test-only authority callback, so it does not establish a live remote
lease or production dispatch. A lost issuance or registration reply remains
ambiguous and must not be retried as a new execution. The internal
`register_issued_source` wrapper attempts to mark an uncertain registration
UNKNOWN even after lease expiry; if PostgreSQL is unavailable this mark cannot be
confirmed and the existing RUNNING reservation still forbids another issuance.
Crash reconciliation is not implemented. Source Git verification
and staging now run in a worker with cooperative revocation checks, while the
verified directory remains alive across an asynchronous consumer. Arbitrary
proof/manifest metadata must fit the existing 4 MiB control frame; support for
larger Git proofs is not yet implemented. Production watchdog wiring and
crash recovery remain open.

An opt-in capture response now uses the existing binary framing with a distinct
artifact direction key and binding, a 4 MiB limit, signed RPC metadata and a
private read-only receiving descriptor. An internal Gateway helper verifies the
capture receipt through `ExecutionLifecycle.finish()` and calls the existing
atomic publication store; ambiguous failures attempt to mark the execution
UNKNOWN. Fault tests cover modified frames, truncated streams, trailing data,
wrong binding, forged metadata, oversized output and revocation. A Docker test
transfers an actual Supervisor/scip-python capture over this channel and reads
it with B1. Separate PostgreSQL tests exercise transfer to FINISHED/PUBLISHED
using a synthetic index and test-only validator. These are not a unified
Production E2E: the production registration factory, live authority watchdog,
approved recipe/Grant dispatcher, recovery and Development Adapter remain
unconnected. The source-session callback is not an authorization mechanism.

Migration 0015 adds a fenced canonical RUNNING check for Source heartbeats.
The opt-in Supervisor `SourceAuthority` is inactive until the first successful
heartbeat and expires on heartbeat loss; a signed cancellation can also consume
the persistent replay fence before registration. A real PostgreSQL/socket test
exercises snapshot invalidation while a staged-source consumer is active and a separate test covers the
pre-registration cancellation race. The Gateway polling helper and this
Supervisor lease are not yet installed by a production dispatcher, and the
production callback must pass the lease's revocation event into execution and
retain heartbeat monitoring through capture. None of these tests establishes
full production recovery or a separate-kernel VM.

### Fixed SCIP dispatch and canonical integration fixture

An opt-in `AuthorizedScipExecutor` now passes a canonical Tool Attempt through
snapshot/recipe preparation, atomic session issuance, authenticated remote
registration, live DB-backed heartbeats, verified source staging, the fixed
SCIP recipe, signed capture transfer, FINISHED and encrypted PUBLISHED. The
Supervisor derives descriptor-specific arguments only after verifying a
Gateway-signed fixed recipe marker and the full canonical binding. At startup
the opt-in factory measures the Node binary, SCIP package files and fixed
environment on read-only mounts and compares that digest to the approved
toolchain. This measurement does not prove image authenticity, protection from
privileged remounts, dynamic libraries or VM/kernel identity.

The opt-in `test_scip_production_e2e.py` uses real PostgreSQL, ToolGateway,
Supervisor in Docker, scip-python, authenticated brokers and B1 definition and
reference lookup. It uses disposable keys and a Docker fixture image, with no
manual source-session registration, synthetic FINISHED transition or mock
capture receipt. Failure scenarios cover pre-registration cancellation and
disconnect, a real Supervisor crash, Lease expiry, real running/capture
cancellation, Artifact transfer interruption, pre-publication cancellation,
temporary PostgreSQL denial, and real Gateway process death before and after
publication. Concurrent reconciliation is exercised against the canonical DB.
These tests do
not prove separate-kernel isolation, production egress, attestation, or key
provisioning. Fixture toolchain bytes are measured and the fixture's inspected
Docker image ID is the recipe's test image digest; this is not attestation.
The fixture runs the existing B1/GitSnapshot validator locally under the test
profile. Per `SCIP.md` and `README.md`, those components require an externally
isolated Cell in production; `SnapshotScipVerifier` therefore refuses the
production profile. A production isolated validation and navigation service is
still required before this E2E can be called production-ready.
This exercises a Python subset of DEV-REQ-21 / DEV-CIN-T01..07 / SC-34; it does
not establish the complete multi-language scenario or mark that scenario Verified.

Default Supervisor startup keeps the source broker disabled. Explicitly enabling
it requires root-owned private source/output/capture directories, a separate
private replay-journal path, distinct protected Grant/RPC/ticket/capture keys,
an unprivileged Cell UID, network namespaces, resource limits and a locally
measured approved SCIP toolchain. These checks do not attest persistent storage,
the VM boundary, image authenticity or production key provisioning. The
standard Cell image is not a SCIP image; a separately provisioned image is
required. The Gateway environment loader does not yet select the scoped SCIP
executor for ordinary runs. Reconciliation confirms a lost reply only when the
canonical database contains a PUBLISHED Artifact whose authenticated ciphertext
can still be decrypted and verified. Unproven RUNNING or FINISHED executions
are fenced UNKNOWN and never retried. There is no authoritative remote
completion ledger or safe automatic resolution of orphaned resources. A durable
Replay Journal must be shared by every Supervisor using the same authority keys;
independent journals cannot prove cross-Supervisor exclusion and are not a
supported production topology. `ToolGatewaySandboxAdapter` remains fail-closed
in production for build/test/static-check. Its current request/evidence
contract describes those command types and has no index operation, while this
slice produces a SCIP Artifact; treating an index as a test receipt would invent
evidence. Generic approved command execution, authenticated command receipts
and their Test ID binding are still required before enabling that adapter.
An opt-in test/development command path now verifies an exact approved argv and
Test ID, requires proof of a complete source projection, uses a fixed measured
executable allowlist, and produces a domain-separated signed receipt containing
outcome flags and output hashes rather than plaintext. A disposable Docker test
exercises build, static-check and test commands, nonzero exit, timeout, output
overflow and read-only source. Migration 0017 and Gateway command-receipt
operations are provisional: the `ToolGatewaySandboxAdapter` test/development
path uses `CommandReceiptReader` and has a real PostgreSQL/Gateway/Docker
Supervisor E2E for build, static-check, test and a nonzero test. This fixture
does not exercise the complete Development Workflow. The path is disabled in
production, and the binary measurement does not attest libraries,
image, key provisioning or source secret classification. Command reconciliation
and complete DB-to-Adapter E2E still require implementation and verification.
The canonical DB receipt persistence and rehydration have a separate PostgreSQL
test with a synthetic process result; it is not a substitute for a real command
E2E. Production command execution and the local B1 verifier both remain
fail-closed until the required isolation and attestation boundaries exist.
The Draft Development request now requires an explicit planned Test ID for test
commands. The workflow checks that ID before dispatch and requires a matching
receipt. Timeout and truncation flags are tri-state: older stored receipts with
unknown flags remain readable but cannot satisfy the test PASS gate. This is a
deliberate fail-closed compatibility change, not proof of generic command
execution; the production command adapter remains disabled.
For a future command executor, the Git proof now records excluded blob count
and the server-selected `require_complete` staging policy rejects projections
that omit any tracked file. This check does not prove source content is free of
secrets and does not enable command execution by itself.

The source broker bounds concurrent reservations to eight sessions and 256 MiB
of encoded source; staging can temporarily require additional space for the
verified projection. Expired pending sessions release their reservations, and
duplicate receive attempts cannot release another handler's lease or quota.
The journal has a hard 64 MiB limit and fails closed when full; safe compaction,
orphan cleanup and multi-VM journal coordination are not implemented. Ticket,
Grant, RPC and capture keys are distinct and loaded from protected files at
startup. Revoking or rotating them currently requires stopping admission and
restarting the Supervisor with the same durable replay journal; live key reload
is not supported. Artifact encryption uses the existing KeyRing: old keys must
remain available for retained Artifacts, and missing or invalid keys make
reconciliation stay UNKNOWN rather than asserting success.

The test/development command adapter now uses a scoped PostgreSQL receipt reader
and checks an explicit target commit against the current snapshot before Tool
Intent creation. The Docker/PostgreSQL E2E exercises `DevelopmentWorkflow`
for build and static-check with a test-only DB preflight; test commands still
enter the Adapter directly because a real Gateway-backed Git commit provider is
not available. No synthetic commit receipt is treated as a successful commit.
Production command execution and local B1 validation remain fail-closed.
