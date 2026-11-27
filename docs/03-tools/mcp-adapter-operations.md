# MCP Adapter — Operations Note

> Draft implementation note. Status: **IMPLEMENTED_PARTIAL / NOT DEPLOYABLE YET**. The package and
> executor work in tests, but production registration, PostgreSQL persistence and a real-provider
> verification are still gated (see `mcp-adapter-implementation.md` and `mcp-adapter-security.md`).

## 1. Feature flag and configuration

```text
MCP_ADAPTER_ENABLED=1          # required; absent/0 = no connect, list or call
AIAGENT_PROFILE=production     # plain http:// and loopback providers are refused outside test
```

Provider configuration is admin-owned. Each provider records at least:

- `provider_id`: admin-assigned, stable, `^[a-z][a-z0-9_-]{2,63}$`. Never `serverInfo.name`.
- `tenant_id` / `owner_user_id`: the credential and catalog scope.
- `endpoint`: approved HTTPS URL, including path.
- `auth_mode`: `none`, `bearer` or `api_key_header`; a non-none mode requires
  `credential_binding_ref` and, for bearer, only `Authorization`.
- `credential_version`: bumped when the broker rotates the credential; stale versions are refused.
- `policy_revision`: must match the pinned snapshot/entry; any policy change invalidates old pins.
- `endpoint` path/host changes alter the endpoint fingerprint and invalidate old pins.
- `enabled`: admin disable/revoke switch.
- `allowed_tools`: source names allowed by admin policy; the executor intersects this with the pinned entry.

Limits have hard ceilings in `config.py`. Lowering them is safe; raising them requires a code review because
the SDK/egress behaviour must remain bounded.

## 2. Onboarding checklist (after the integration gates land)

> **HTTPS prerequisite:** the existing forward-proxy contract forbids `CONNECT` and this adapter does not
> terminate TLS. The Sentinel/proxy egress path can terminate TLS through a reviewed connector; that
> connector is not implemented or exercised in this slice. Remote HTTPS is an unexecuted gate, not a
> permanent limitation. The current tests are loopback HTTP only.

1. Security/owner approves the provider endpoint, tenant/owner scope and credential binding.
2. Credential Broker stores the secret; the configuration stores only the binding ref and version.
3. Admin assigns risk class and approval policy per tool. `annotations.readOnlyHint` is ignored.
4. Discover with the feature flag on in staging; inspect the snapshot digest, tool count, schema bytes and
   quarantined tools. Publish is all-or-nothing.
5. Verify a read call and a write call through the real Gateway, including approval and ledger rows.
6. Verify revoke: disable the provider or call `SessionManager.revoke`; no new session is opened.
7. Verify an ambiguous write produces UNKNOWN and is not auto-retried.

## 3. Health and observability

The current slice emits safe telemetry events through `TelemetrySink`:

- `mcp.tools.list_changed` (provider id only)
- `mcp.call.completed` (provider id, alias, status, uncertainty flag)
- `mcp.call.failed` (provider id, alias, error code, retryable/uncertain flags)

No credential, authorization header, raw endpoint query, prompt, tool description or result body is emitted.
Production metrics should aggregate by provider/tenant and error code, with alerting on:

- repeated `EGRESS_DENIED` / `HTTP_SESSION_LOST` / `TRANSPORT_RESET`;
- unsupported protocol revisions;
- snapshot publish failures / stale snapshot count;
- UNKNOWN writes awaiting reconciliation (highest priority);
- latency p95 and response bytes per provider.

Provider health states from the canonical spec (needs-auth, degraded, unavailable, quarantined) are **not**
implemented as stored state or API in this change.

## 4. Revocation and incident response

1. Set `enabled=false` for the provider (or revoke its id in the session manager) **before** rotating or
   deleting the credential.
2. Stop new calls; do not replay an UNKNOWN write. Do not trust a provider-supplied `structuredContent.receipt`
   as proof of the effect; reconcile with provider-owned evidence.
3. Rotate the credential and increment `credential_version`; old sessions remain invalid because every
   operation opens a new session and checks the version.
4. If a malicious tool/schema is suspected, unpublish/revoke the snapshot entry and keep the feature flag
   off until review.
5. Preserve audit evidence (intent id, effect key, error code, permit denial proof) without copying secrets.

## 5. Failure handling

- `FAILED` before send: safe to correct and retry through normal Gateway admission.
- `UNKNOWN`: never auto-retry. Reconcile externally, record the result, then let the operator decide.
- `list_changed`: treat pending listings as invalid. The callback awaits invalidation of every pin for the
  provider. A new discovery must publish a new generation; old pins are tombstoned and fail closed rather
  than silently continuing on the old surface.
- 404/session loss: reconnect only for a new operation. Existing write calls stay UNKNOWN.
- Cancellation: local stop is not a remote cancel. Do not mark the effect as absent.

## 6. Rollout stages

| Stage | Gate |
|---|---|
| Local tests | 90 adapter tests pass (this change) |
| Staging shadow | real provider, no write tools; verify TLS/auth/list/call/read normalization |
| Staging writes | approval + ledger + UNKNOWN reconciliation; kill/restart test |
| Multi-tenant | PostgreSQL RLS test, tenant-filtered model surface, cross-tenant denial test |
| Production | Security review sign-off; all above; feature flag enabled per provider, not globally |