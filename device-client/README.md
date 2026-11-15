# Device Client — LDC-1 Mock slice

Windows-first Rust Device Agent core + Tauri 2 offline simulator. This is **NOT** a paired or production-operable device client. No Cloud Gateway, TLS, pairing, Cua process, Chrome extension, build execution or ADB call exists. The UI has no real action command. There is no hardcoded key, cookie, user profile collection, privileged local server or active network listener.

## Build / test (Codex CLI handoff)

Windows (PowerShell, from repository root):

```powershell
cd device-client
rustc --version
cargo --version
.\ci\verify.ps1
cargo run -p aiagent-device-desktop
```

WSL2 (core tests; requires Rust toolchain and SQLite C build support):

```bash
cd device-client
bash ci/verify.sh
```

Run on Windows in an ordinary interactive user session. Tauri 2 Windows builds require installed Rust MSVC toolchain, WebView2 and applicable Windows SDK/MSVC tools. Do **not** install or run any Cua/BrowserMCP/ADB service for this mock test. On Windows, test app launch, the status UNPAIRED_OFFLINE, disabled Pair/Operate controls, Local Stop, Mock Revocation and local audit rows. No real device effects should occur. If compilation fails, capture first error and OS/toolchain versions; do not switch to admin or disable Windows security.

CI handoff to AI-07: add a shared-workflow job on `windows-latest` running `cd device-client; cargo test --workspace; cargo check --workspace`. Linux-only core test: `cargo test -p aiagent-device-core`. Tauri Linux build requires native GTK/WebKit packages; a macOS build is not macOS TCC/real-device verification. Run `cargo fmt --all` before final review; the script runs tests/compile, not formatting. Lockfile generation/pinning and dependency licence audit also belong to an actual toolchain-enabled job, not claimed here.

## Trust boundary / status

- `AuthenticatedChannelPort`, `CuaAdapterPort`, `ChromeAdapterPort`, `BuildAdapterPort`, `AndroidAdapterPort` exist as **interfaces**. Only no-effect mocks exist.
- Model-controlled argument bytes are SHA-256 hashed, the full operation/target/fence is bound to the journal fingerprint, grants and START_PERMIT must agree on scope/epoch/intent/target/digest and be fresh. All JSON epochs use canonical signed-int64 decimal strings.
- LocalJournal writes STARTED + consumed Permit in a single IMMEDIATE SQLite transaction, uses WAL + synchronous FULL; crash recovery marks remaining STARTED UNKNOWN. No blind retry and no claim of exact-once delivery or Cloud success.
- The metadata-only SQLite mock journal is **not encrypted and not suitable for production**; per-owner OS-protected key storage, secure file permissions on Windows, genuine signed server grants and authenticated live connection are not implemented. Never store sensitive payload bytes in this journal.
- Local policy uses a positive allowlist for simulated non-browser desktop targets and separate browser Origin grants; Build/ADB are disabled unless explicitly enabled in a **test-only** policy. Unknown target changes must be rejected by future native adapters.
- Durable Cloud receipt reconciliation, semanticEffectKey barrier, handoff CAS, Tauri-to-separate-Agent owner-bound IPC, cryptographic permit validation and revocation propagation remain **AI-07/shared contract integration tasks**. No real driver path can be enabled before these gates.

## Tests / traceability

`guardrails.rs`: LDC-T01 partial owner isolation; T04 stale epoch; T05 crash/unknown and duplicate refusal; T08/T09 desktop-browser boundary; T10/T11 disabled Build/ADB; T15 offline refusal; T30 Stop. Type/permit test cases additionally cover target identity changes, reconnection generations, canonical epoch encoding, digest mismatch, permit reuse and local-only completion. These are **unit/mock tests, not implementation or validation of the full LDC-T01..32**.

Manual Windows Mock GUI tests: launch -> verify disabled real actions -> Stop -> verify disabled controls and audit -> close -> restart (in-memory UI state resets; real secure persistence is not implemented). Mark each actual run PASS/FAIL/NOT_RUN; never infer PASS from a successful PR or a documentation statement. Host lacking Windows/macOS/USB must record `BLOCKED_HOST_UNAVAILABLE`.

Status at submission: **IMPLEMENTED / TESTS_NOT_RUN** unless separate CI run and SHA are recorded. Only promote to `CODE_READY` after core unit tests and Tauri build succeed. `HOST_VERIFIED` / `PRODUCTION_READY` are not asserted.
