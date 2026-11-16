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
