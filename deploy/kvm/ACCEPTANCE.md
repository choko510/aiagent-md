# STEP 4 AI-05 — Host acceptance (operator-owned)

This directory adds **verification helpers**, not a new deployment mechanism. They do not grant
production execution permission. The existing `provision.sh` and `verify-topology.sh` remain unchanged.
`CODE_READY` means code and synthetic checks passed, **not** that 2VM isolation was verified.

## 1. Safe read-only checks (Chat / WSL / Linux host)

From the repository root:

```bash
python3 deploy/kvm/acceptance.py capabilities > host-capability.json
python3 deploy/kvm/acceptance.py topology > topology.json
cd backend
uv sync --locked
uv run pytest -q tests/host_verification/test_acceptance.py
```

- The scripts never start/stop a VM, change firewall rules, touch the Docker socket, or open public egress.
- `capabilities` checks the real `/dev/kvm` ioctl, installed QEMU/libvirt, read-only access to the nftables ruleset,
  and vsock API support. A missing permission/capability returns exit 2 and `BLOCKED_HOST_UNAVAILABLE`.
- `topology` uses read-only `virsh` to inspect two **running** KVM guests, distinct UUID/disk sources,
  no Execution VM NIC, no host device/shared filesystem, and the expected vsock CIDs. This is a *static
  topology check*, **not** guest isolation proof. A passing result is `TOPOLOGY_READONLY_PASS`,
  not `HOST_VERIFIED`. Even a complete `/dev/kvm` check does not show that a guest actually booted.
- On Windows PowerShell: `wsl --list --verbose`, `wsl --version`, `git --version`.
  On WSL Ubuntu: `python3 --version`, `uv --version`,
  `test -c /dev/kvm && echo KVM_DEVICE_EXISTS || echo KVM_DEVICE_MISSING`.
  These only describe prerequisites, not KVM acceptance.

Tests in `backend/tests/host_verification` simulate read-only libvirt replies, missing KVM, an
Execution VM with a NIC, stale/duplicate/tampered evidence, CI evidence falsely claiming real host
results, benchmark misconfiguration and unsafe restore state. A CI PASS is **Mock/contract evidence only**.

## 2. KVM 2VM drill (dedicated operator host only; never run on a daily-use Windows/WSL)

**Prerequisites:** an approved non-production KVM/libvirt host; 2 running guests with distinct kernels;
the real supervisor and Sentinel; an isolated disposable Tenant/Run; observer credentials bound to that
disposable setup; operator agreement to disruptions. Do not disable VBS, Memory Integrity, Hyper-V,
or operating system protections to force a PASS.

Run `acceptance.py capabilities` and `acceptance.py topology` on the actual libvirt host and
preserve JSON plus commands, exit codes, timestamps, *sanitized* logs, KVM/libvirt versions, guest
boot IDs and topology XML hash. For each item below, a real-world probe and proof are mandatory.
The script does **not** perform these potentially destructive/privileged tests:

| ID | Real measurement and PASS condition | Required evidence |
|---|---|---|
| `KVM-CELL` | Distinct running guest kernels; control and execution boot IDs; Cell cannot read control PID/file/process memory | host/guest kernel/boot IDs + blocked access (no protected contents) |
| `KVM-NETWORK` | From Execution VM: DNS, TCP, UDP, IPv4 and IPv6 external attempts all fail; only authenticated scoped broker RPC works | guest network inspection + test results |
| `KVM-SENTINEL` | In disposable setup stop Sentinel, prove all external effects fail closed, then restore it | denied requests + process status |
| `KVM-RESTART` | In disposable guest-only setup interrupt/restart Execution VM; no stale worker succeeds | libvirt lifecycle + resulting attempts |
| `KVM-FENCE` | Old lease/grant/policy epoch after restart rejected, fresh authorized grant accepted once | gateway refusal codes + ledger |
| `KVM-FS` | Path traversal, mounts, /proc and cross-task/tenant PID/file access denied | negative test results |
| `KVM-VSOCK` | Unauthenticated request denied, authorized scoped broker request succeeds, raw TCP tunnel denied | broker receipt (redacted) |
| `KVM-ATTESTATION` | Operator verifies real topo/probes; no production startup with absent or false attestation | rejection logs + attestation digest |

**Do not copy guest secrets, tokens, private file contents, customer data or raw receipt bodies into evidence.**
Do not run `virsh destroy`, restart the trusted control guest, remove a VM, or change nftables automatically.
If the disposable test host is not available, mark each KVM item `BLOCKED_HOST_UNAVAILABLE`.
The existing `verify-topology.sh` **writes/removes** an isolation file; it is not a read-only probe
and must only be run separately after operator authorization.

## 3. Bounded Queue JQ-T12

The wrapper imports the existing `backend/tools/bench_queue.py` without changing shared code.
Before use, the operator must mark **only the disposable test DB** with a custom PostgreSQL
setting; the guarded subprocess reads this value before any benchmark writes:

```sql
-- Run ONLY on a verified isolated scratch cluster, not production.
ALTER DATABASE aiagent_hv_disposable SET aiagent.hv_disposable = 'aiagent-hv-disposable-v1';
```

The wrapper rejects a missing marker even when the database name and confirmations match.
Run `cd backend && uv sync --locked` before the benchmark; the subprocess uses the pinned
`uv run --no-sync --locked python` interpreter. On timeout the process group is killed.
That existing tool creates and later drops a scratch database; only a specifically prepared
**disposable** PostgreSQL instance may be used. By default it performs a dry run and no writes.

```bash
# Operator: prepare an isolated scratch cluster and the DB aiagent_hv_disposable first.
# The prompt avoids putting credentials in shell history.
read -rsp 'Disposable PostgreSQL DSN: ' AIAGENT_HV_DISPOSABLE_PG_URL; echo
export AIAGENT_HV_DISPOSABLE_PG_URL
python3 deploy/kvm/queue_gate.py --rows 1000
# Only after independently verifying DB instance/port are NOT production:
export AIAGENT_HV_ALLOW_WRITE=YES
python3 deploy/kvm/queue_gate.py --rows 1000 --execute --ack ALLOW_DISPOSABLE_DB_WRITE
```

- URI must be `postgresql://USER:PASSWORD@127.0.0.1:PORT/aiagent_hv_disposable` (loopback; a
  dedicated remote host may be accessed using a separately secured local tunnel). The URI is never
  printed or passed in the subprocess OS argv; **do not publish the environment or process dumps**.
- Smoke is capped at 10,000 jobs and at most 8 relay/8 consumer threads, 30-second outage,
  and 3600-second timeout (choose a shorter wall-clock time for smoke). A 1M load requires
  `--dedicated-host --rows 1000000 --max-seconds 3600`, explicit double confirmation,
  and a genuinely dedicated test host with adequate DB/WAL/disk monitoring. **Never run the 1M
  test on normal Windows/WSL.** The underlying benchmark also has its own 1800-second limit;
  a timeout is NOT a PASS and may leave an ephemeral DB for operator cleanup.
- The wrapper rejects wrong database names, remote hosts, missing credentials, excessive
  concurrency/row counts, and an absent operator acknowledgement. It redacts child stderr.
- JQ-T14 (live expand/migrate/switch/drain/rollback) must run in an approved disposable test stack
  with running Runs, and verify no lost or unexpectedly retried job. JQ-T16 must verify deadline
  exhaustion produces a distinct visible user state and no further retry. Neither is implemented
  by the existing benchmark; record `NOT_RUN` until real end-to-end drills exist.
- DB superuser access, CPU/disk/RAM, WAL growth and any database created/dropped are
  operator responsibility. Never point this at real user data or use production credentials.

## 4. Quarantined restore inspection (read-only)

A **separately created** restore rehearsal with no external egress, a dedicated database named
`aiagent_hv_restored_quarantine`, revoked external credentials and disabled workers/notifications is
required. The script does **not** perform a restore, PITR, rekey, service start, or `restore-bump`.

```bash
read -rsp 'Isolated restored DB DSN: ' AIAGENT_HV_RESTORE_DSN; echo
export AIAGENT_HV_RESTORE_DSN
python3 deploy/kvm/restore_probe.py --inspect
```

Read-only queries verify restore incarnation >1, `schema_control.dispatch_paused`, every Queue paused,
all egress permits revoked, no `SENDING` intent/effect, and no valid Run lease.
Unknown effects remain visible for external receipt reconciliation. If checks fail:
**do not call `adopt-runs`** and do not resume workers. A passing DB snapshot still does not prove
PITR consistency, artifact/secret revocation or that no external effect escaped.

## 5. Evidence manifest + levels

Store a JSON manifest and the small sanitized proof files side by side. Every record must contain
exactly `id,status,execution,evidenceSha256,evidenceFile,hostId,topologyDigest,recordedAt`.
Hashes are lowercase SHA-256 hex without a prefix; `hostId` and `topologyDigest` have
`sha256:` prefixes. Time is UTC ISO8601. Proof files must exist locally, be regular non-symlinks,
be <=1 MiB, and match the declared hash. A checksum is **not** a signature or proof of honesty.

Example one record (replace all placeholders with measured values):

```json
{"checks": [{
  "id": "KVM-NETWORK", "status": "PASS", "execution": "REAL_HOST",
  "evidenceFile": "KVM-NETWORK.json",
  "evidenceSha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "hostId": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "topologyDigest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "recordedAt": "2026-11-11T00:00:00Z"
}]}
```

Run `python3 deploy/kvm/acceptance.py evaluate --evidence /path/to/manifest.json`.
If missing real host checks, the status can be `CODE_READY` or `INTEGRATED` but **never**
`HOST_VERIFIED`. The four gates need respectively:

- `CODE_READY`: CI proof files `HV-U01`, `HV-U02`, `JQ-SMOKE`.
- `INTEGRATED`: above plus `HV-CI`; CI evidence may originate on a different runner.
- `HOST_VERIFIED`: above plus **live**, read-only KVM topology PASS on the same host and
  all eight `KVM-*` REAL_HOST proof files bound to that host ID and digest.
- `PRODUCTION_READY`: above plus REAL_HOST `JQ-T12-1M`, `JQ-T14`, `JQ-T16`,
  `REC-PITR`, `REC-EFFECT-QUARANTINE`, and an `OPERATOR` signoff proof.
  The JSON may say the gate is met, but **productionCutoverAuthorized is always false**;
  an external operator-reviewed approval and separate controlled release are still required.

Missing, duplicate, tampered, stale (>24h), mock-labelled real host evidence or topology mismatch
are rejected. Evidence must be independently examined by an operator. For full reproducibility record
software versions, CPU/RAM/DB disk space, runner, timestamp, checked commit SHA, actual test commands,
exit codes, limits, and proof hashes; do not manufacture PASS rows.

## 6. AI-07 integration requests (shared areas outside AI-05 ownership)

1. Wire `backend/tests/host_verification/test_acceptance.py` into a bounded read-only CI job and
   archive sanitized artifacts; do not add any live VM or 1M load on GitHub-hosted runners.
2. Review restore transition: `admin.restore_bump()` sets `schema_control.dispatch_paused=true`,
   but `QueuePort.dequeue()` only checks `queue.queues.paused`; ensure stale pointers cannot be
   consumed and a restored queue cannot auto-resume before manual adoption.
3. Add supported **actual** KVM/guest scoped-probe endpoints and test-only grants; a static
   libvirt XML cannot prove network, PID, guest kernel, Sentinel-fail-closed or lease behavior.
4. Implement isolated JQ-T14 schema upgrade drill, JQ-T16 user-visible deadline E2E, and a
   guarded PITR drill with restore quarantine; keep all as NOT_RUN until measured.
5. Revisit the existing `verify-topology.sh`: a guest-local vsock request + nft rule check
   alone is insufficient proof of host VM separation or all network paths.
