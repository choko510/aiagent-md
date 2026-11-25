# Device Gateway pairing core — isolated development slice

This package is not mounted in the API and cannot authorize or dispatch device operations. `PairingService.begin` must receive the existing authenticated API `Scope`; this is a caller obligation, not enforced by the structural Python protocol. The owner and tenant are assigned from that scope. Device-provided tenant and owner on `attach`/`consume` are lookup hints under RLS, never evidence of identity: the random bootstrap token and signed challenge are also required.

The server creates a two-minute request, random 256-bit bootstrap token and nonce, and new identity/device IDs. The device proves possession of its Ed25519 private key. The core then returns an eight-digit code for delivery to the device and stores only a keyed digest. The authenticated owner inspects the registered public-key fingerprint and binding digest, then approves that exact request with the code. A transaction consumes the approval once and creates the identity. Five failed attempts lock the request. `repr` excludes token, nonce and code. A lost response after a state change can require a new pairing; it is never silently replayed.

The signing input is a **candidate binary** domain-separated format, shared with the Rust implementation and verified against `device-client/fixtures/pairing-proof-v1.json`. It is not a locked shared JSON Schema, an API message, or a production protocol. The WSS socket and local DPAPI identity are not wired to this service. No endpoint, authenticated connection token, heartbeat, revocation propagation, account-switch barrier, Tool Gateway/Sentinel authorization, grant, permit, effect ledger or receipt is implemented here. The test SQL is deliberately not a production migration. Before enabling a real service, integrate canonical generated wire schemas and actual API authentication, establish least-privilege database roles and verified RLS, complete restart/restore and replay controls, rate-limit requests at the network boundary, protect and rotate the HMAC key, and perform security review and full E2E tests. A TLS connection or registered identity does not grant operation rights.

Ed25519 verification uses PyNaCl 1.6.2 with a point check and signature verification. `PairingService` rejects a linked libsodium that accepts the upstream mixed-order regression point for [CVE-2025-69277](https://github.com/jedisct1/libsodium/commit/ad3004ec8731730e93fcfbbc824e67eadc1c1bae), including a potentially older system library. The [PyNaCl changelog](https://github.com/pyca/pynacl/blob/main/CHANGELOG.rst) identifies the patched bundled 1.0.20-stable build. This self-test does not replace dependency provenance or a production cryptographic review.

`requirements-test.lock` pins and hashes the isolated test environment without modifying the shared backend lock. To run the tests, provision a **fresh disposable PostgreSQL database named `device_gateway_test` on loopback**, set `AIAGENT_DEVICE_TEST_ADMIN_DSN` to its administrator DSN, and run:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --isolated --no-project --python 3.13 \
  --with-requirements backend/device_gateway/requirements-test.lock \
  python -m pytest -c backend/device_gateway/tests/pytest.ini backend/device_gateway/tests -q
```

The fixture refuses non-loopback or differently named databases, creates a test schema and non-superuser role, and drops only those fixture objects afterwards. It tests real PostgreSQL locking, tenant/owner RLS, replay and fault behavior; it does not establish production migration, deployment or HTTP/WSS integration.
