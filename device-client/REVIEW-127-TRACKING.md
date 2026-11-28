# PR 127 review tracking

Review baseline: `2fdcc32ad42b7439e2300b986bebfeb55b468d55`. Status is based on implementation evidence, not CI alone. Real device operations remain disabled.

| Finding | Status | Evidence or remaining work |
|---|---|---|
| P1-01 | PARTIAL | Test-schema challenges are channel-scoped; a 64-row limit and expiry cleanup are implemented. A valid existing proof survives a flood; fair admission under saturation and production migration remain open. |
| P1-02 | PARTIAL | Candidate v2 signature binds server scope, identity, device, origin and revocation generation; mutation and Rust/Python TLS/PG tests pass. Local digest remains opaque and arbitrary, generated contract and authenticated enrollment remain required; SID is not remotely attested. |
| P1-03 | PARTIAL | `begin`, `load_control` and `save_control` reject a deleted control row when its initialization marker remains; marker and record creation share a transaction. Simultaneous marker deletion, old snapshots, DB/key deletion and independent rollback fencing remain unresolved; this is not a production enrollment authority. |
| P1-04 | PARTIAL | Simulator Core stop, revoke and unpair latch immediately and persist through the journal; direct reopen is tested. Persistence-failure recovery, process ownership and final delivery fencing remain required. |
| P1-05 | OPEN | Authenticated effect history and cross-device semantic reservation required. |
| P1-06 | OPEN | Verified authority and fresh delivery fencing not integrated. |
| P1-07 | PARTIAL | Unauthenticated challenges no longer update identity restore state. Independent restore authority and barrier remain required; an old process can still act until a new proof succeeds. |
| P1-08 | PARTIAL | Root components are opened one at a time without following links, tested with Windows junction and Linux symlink ancestors. Persistent approved volume/file identity, replacement/alias races and reopen policy remain open. |
| P1-09 | PARTIAL | Opening another instance no longer rewrites STARTED; non-owner views project it as UNKNOWN and only the originating journal instance can finish. Persistent writer lease, process-death proof, authenticated reconciliation and multi-process fault tests remain required. |
| P2-01 | OPEN | Durable pairing lifecycle and safe key publication required. |
| P2-02 | PARTIAL | Development listener rejects excess established connections without queueing; database connection, lock and statement waits have timeouts. TLS socket and PostgreSQL contention tests pass. Handshake-level admission, a pool, tenant/IP quotas, cancellation reconciliation and scheduled cleanup remain required. |
| P2-03 | OPEN | Handle-based journal storage and per-identity key required. |
| P2-04 | OPEN | Handle-based identity store and atomic publication required. |
| P2-05 | OPEN | Session actor and operator trust provisioning required. |
| P2-06 | OPEN | Handle lifecycle, Unicode and I/O deadline required. |
| P2-07 | PARTIAL | UI no longer labels simulated state as mTLS or enables real I/O, and reports persistence only after a successful write. The Tauri legacy recovery command now rejects all requests. Authenticated IPC, distinct live capabilities, the internal simulator recovery method is now gated to tests or a debug-only feature. Native owner confirmation and a real recovery authority remain required. |
| P2-08 | PARTIAL | Windows DPAPI-to-Python/PostgreSQL TLS test is required and passed at `827609f` in Actions run 46112299393; skip is rejected and release test-CA compilation is rejected. Artifact retention and pinned Rust were added afterward and await CI; dependency/license audit policy remains to integrate. |
