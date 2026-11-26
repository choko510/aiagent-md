# Device Client — LDC-1 Mock slice

Windows-first Rust Device Agent core + Tauri 2 offline simulator. This is **NOT** a paired or production-operable device client. A separate outbound WSS library verifies server certificates and hostnames; it is not yet wired to a Device Gateway or the simulator. Pairing, device authentication, Cua process, Chrome extension, build execution and ADB operations are not implemented. The placeholder channel always rejects pairing and reports offline, even when its legacy configuration flags claim a verified handshake. Placeholder local adapters always fail without spawning a process. There is no hardcoded key, cookie, user profile collection, privileged local server or active network listener.

## Build / test (Codex CLI handoff)

Windows (PowerShell, from repository root):

```powershell
cd device-client
rustc --version
cargo --version
.\ci\verify.ps1
cargo run -p aiagent-device-desktop
```

WSL2 (core and TLS/WSS transport tests; requires Rust, clippy, rustfmt and SQLite C build support):

```bash
cd device-client
bash ci/verify.sh
```

Run on Windows in an ordinary interactive user session. Tauri 2 Windows builds require installed Rust MSVC toolchain, WebView2 and applicable Windows SDK/MSVC tools. Do **not** install or run any Cua/BrowserMCP/ADB service for this mock test. On Windows, test app launch, the status UNPAIRED_OFFLINE, disabled Pair/Operate controls, Local Stop, Mock Revocation and local audit rows. No real device effects should occur. If compilation fails, capture first error and OS/toolchain versions; do not switch to admin or disable Windows security.

The device workflow runs the Windows workspace checks and Linux core/transport checks. Tauri Linux builds require native GTK/WebKit packages; a macOS build is not macOS TCC/real-device verification. `Cargo.lock` pins resolved dependencies. Dependency and licence audits, installer signing, and native GUI verification are separate gates and are not claimed here.

## Trust boundary / status

- `AuthenticatedChannelPort`, `CuaAdapterPort`, `ChromeAdapterPort`, `BuildAdapterPort`, `AndroidAdapterPort` exist as **interfaces**. Only no-effect mocks exist.
- Candidate identity proofs require an unclosed, server-verified WSS socket with the same canonical HTTPS origin as the local identity. This does not establish a heartbeat or authenticate a device session. The `test-ca` feature permits explicitly supplied test roots only in debug builds and fails compilation in release builds; production uses public WebPKI roots.
- `DeviceSession` implements the draft connection challenge and signed sequential heartbeat exchange over WSS, validates each flat JSON frame against the shared draft schema, rejects duplicate keys and invalid decimal counters, and drops the socket after invalid responses or cancellation. Draft wire 1.1 deliberately rejects the earlier unsigned 1.0 frames. A successful test heartbeat is not Tool authorization or a production ONLINE claim. A separate Windows debug fixture has connected to the Python development gateway using DPAPI and a test CA; production API integration, generated shared types and rollout remain outstanding.
- `WorkspaceRoot` can inspect filename metadata under an explicitly selected local directory using a directory capability. It returns pathless project candidates with a short freshness period and never runs Git or reads manifests. This is not yet a user-consent UI, a persistent Project binding, a Git HEAD probe or an authorization to build. See the implementation audit for remaining root-identity and isolation gates.
- Model-controlled argument bytes are SHA-256 hashed, the full operation/target/fence is bound to the journal fingerprint, grants and START_PERMIT must agree on scope/epoch/intent/target/digest and be fresh. All JSON epochs use canonical signed-int64 decimal strings.
- LocalJournal writes STARTED + consumed Permit in a single IMMEDIATE SQLite transaction, uses WAL + synchronous FULL; crash recovery marks remaining STARTED UNKNOWN. No blind retry and no claim of exact-once delivery or Cloud success.
- The simulator journal seals its control envelope with XChaCha20-Poly1305 and a scope- and label-bound HKDF-SHA256 key. It authenticates and atomically migrates legacy ciphertext on read, preserving Stop and Revoke; a failed migration fails closed. Older clients cannot read migrated envelopes. Windows protects the journal master key with DPAPI; Unix currently stores it in a plaintext owner-restricted file. The master key is shared across scopes, other metadata remains visible, and rollback protection and a production key store are missing. Never store sensitive payloads in this simulator journal.
- Local policy uses a positive allowlist for simulated non-browser desktop targets and separate browser Origin grants; Build/ADB are disabled unless explicitly enabled in a **test-only** policy. Unknown target changes must be rejected by future native adapters.
- Durable Cloud receipt reconciliation, semanticEffectKey barrier, handoff CAS, Tauri-to-separate-Agent owner-bound IPC, cryptographic permit validation and revocation propagation remain **AI-07/shared contract integration tasks**. No real driver path can be enabled before these gates.

## Tests / traceability

`guardrails.rs`: LDC-T01 partial owner isolation; T04 stale epoch; T05 crash/unknown and duplicate refusal; T08/T09 desktop-browser boundary; T10/T11 disabled Build/ADB; T15 offline refusal; T30 Stop. Type/permit test cases additionally cover target identity changes, reconnection generations, canonical epoch encoding, digest mismatch, permit reuse and local-only completion. These are **unit/mock tests, not implementation or validation of the full LDC-T01..32**.

Manual Windows Mock GUI tests: launch -> verify disabled real actions -> Stop -> verify disabled controls and audit -> close -> restart (in-memory UI state resets; real secure persistence is not implemented). Mark each actual run PASS/FAIL/NOT_RUN; never infer PASS from a successful PR or a documentation statement. Host lacking Windows/macOS/USB must record `BLOCKED_HOST_UNAVAILABLE`.

The simulator is incomplete. A successful build or simulator test does not establish the production LDC acceptance criteria, `CODE_READY`, `HOST_VERIFIED`, or `PRODUCTION_READY`.
