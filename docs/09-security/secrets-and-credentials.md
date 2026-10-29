# Secrets and Credentials — Detailed Implementation Specification

> Owner: **CHAT_09** | Path: `docs/09-security/secrets-and-credentials.md` | Status: **Draft / [PROVISIONAL]** | Revision: 0.1 | Research: **2026-10-30 JST** | Base: main `b45d4e989998663a00ef09673e69ae1f4191f00d`
>
> Related: `authentication-and-user-isolation.md` (PR #9), `sandbox-and-capabilities.md`, `network-egress-and-ssrf.md`, `privacy-audit-and-retention.md`; canonical identity CHAT_01, tool policy CHAT_03, browser CHAT_04, connectors CHAT_05, API CHAT_08, physical DDL/ops CHAT_10, E2E CHAT_11.
>
> **Evidence**: OSS code/tests inspected; product code does not exist. Requirements and tests are *designed only*, not executed. No cross-team type, physical SQL or event is Locked by this document.

## 0. 結論と採用案

認証セッションCookie、サイトCookie、APIキー、OAuth access/refresh token、ブラウザstorageState、Webhook signing secret、Telegram bot token、モデルprovider token、暗号鍵を**別の秘密クラス**として識別し、データ面から独立した**credential broker + versioned encrypted vault**で保管する。LLMは`credentialRef`すら列挙・解決する権限を持たず、Tool Gatewayを通じて事前許可されたconnector/browser actionのときだけbrokerが必要なscopeへ秘密を適用する。OpenClawのSecretRef解決とモデル送信側proxyは機構の参考にとどめる。サーバー操作Agentではないため、任意コマンド型secret provider・env全量注入は**採用しない**。

- **正本**: encrypted secret records + key version + owner binding + grants + rotation metadata。DBは秘匿値を持たずsealed payloadのopaque URI/暗号化blobとmetadataのみ。物理配置はCHAT_10。プロバイダー発行・更新はCHAT_05。
- **キー**: per-install master keyはAgentが触れられないoperator管理の別秘密として保持。AEAD(XChaCha20-Poly1305またはAES-256-GCMのいずれかに統一し安全なnonce/ライブラリ試験後選定) + AAD(`installationId/tenantBinding/ownerUserId/secretId/type/provider/version`)。暗号鍵と暗号文を同じ無保護バックアップに入れない。
- **アクセス**: actorのlogin sessionとowner lineageを`AUTH-R2`提案に従って解決。モデル発行の`vault:read`は禁止。Key recovery不可なら再連携/再認証を要求。ローテーションはdual-read/single-writeで安全に移行。
- **副作用**: 秘密が必要なToolを実行する前にGatewayのpolicy/approval/budget/ownerと、Network egressの実宛先を検証。資格情報が失効・不確定なら`NEEDS_REAUTH`、未知の外部操作は`UNCERTAIN`として実行を止める。
- **境界**: HTTPSリクエストHeaderに秘密を適用する際も、Browser/HTTP/MCPが任意の別ホストへ転送できないようprovider origin/path/scope/redirect downgradeを制限。実資格情報をLLM入力・モデル返答・Tool output/Traceへ挿入しない。

## 1. Scope / non-scope / scenarios

対象は保存時、転送時、プロセス内、Browser storageState、ローテーション、失効、バックアップ復元、export/delete/監査とcross-tenant混入。対象外は他サービスの認証迂回、CAPTCHA突破、自動でのアカウント新規作成、ホスト管理者の秘密ファイル閲覧、Arbitrary shell-based provider。SC-06 browser handoff、SC-07 restart/crash、SC-08 secret exfiltrationを必須、SC-02通知・SC-03 X API・SC-04ホテルログイン・SC-11イベントWebhook・SC-15文書内埋込文字列にも適用。

## 2. OSS investigation（ソース・テスト・代替）

| Source / commit, license | 実際に確認した実装とテスト | 採否 |
|---|---|---|
| **OpenClaw** `8b8c33ef334b3754f42afe2a2e212418abfa80f4` / MIT, package 2026.10.9 | `src/secrets/resolve.ts`: `resolveSecretRefValue(s)`, `resolveSecretRefString`、providerバッチ上限とfile/exec deadline; `src/secrets/resolve.test.ts` provider ref limit/誤設定、`src/secrets/model-egress.ts`: `withConfiguredModelEgress`、`src/secrets/model-egress.test.ts`: stream分割されたcredentialマスクとcancel、`src/logging/redact.ts` | **SecretRef/timeout/redaction実例として参考**。任意exec/file provider、Agent側に秘密を解決する構成は採用しない。Code verified [S1-S5] |
| **Playwright** `38fb53bf4c181abb91f1ca2a2ab741983c5d177c` / Apache-2.0, package 1.65.0-next | `packages/playwright-core/src/server/browserContext.ts`: `storageState`、`clearCookies`、`setStorageState`; `tests/library/browsercontext-storage-state.spec.ts` ST-001..005でCookie/localStorage/IndexedDB等の保存復元 | **Cookieは識別可能なsecret資産**である根拠。Playwrightはencrypt/owner authorizationを提供しないので別brokerが必要。Code verified [S6-S7] |
| **Better Auth** `95d158b04cd8217cc17443e809c700f3b6969f97` / MIT, 1.7.14 | `packages/better-auth/src/api/routes/session.ts`: signedCookie/session lookup/ no-store。 `session-api.test.ts`: Cookie属性、未ログイン・store failure | **User login sessionとしてのみ**評価。外部OAuth token vault代替にしない。Code verified [S8] |
| **OWASP Cryptographic Storage / Secrets Management** | キー分離、ライフサイクル、用途限定の助言。**Docs verified**、コード検証ではない [S9] | threat checklistに採用 |

**代替比較**: (A) envに全tokenを展開: 設定簡単だがprocess dump/child env/LLM/ログへの漏洩半径が大きく**却下**。(B) vaultで秘密文字列をAdapterへ返す: 汎用だがMCP/toolが容易に読み取れるため**却下**。(C) 本案のbroker-side credential injection: 実装と運用が増えるがScope/recipientを強制でき**採用**。(D) 外部Vault/KMSを必須: セルフホスト要件に重いので**LATER adapter**。Secretのcrypto自作は禁止し監査済みライブラリを使う。

## 3. Ownership / dependency map

```text
Web admin credential setup (authenticated + fresh + CSRF)
  -> SecretControlAPI (owner + tenant/workspace resolver; CHAT_09)
  -> CredentialBroker (grant issue/rotate/lease/revoke; CHAT_09)
  -> EncStore (sealed values + key version; physical CHAT_10)
Agent/LLM --abstract tool intent--> CHAT_03 Tool Gateway
Gateway --signed, short-lived, bound operation lease--> Broker
Broker --origin-attested header/cookie injection--> CHAT_05 connector / CHAT_04 BrowserWorker
network response --redacted projection--> Evidence/LLM (never token)
```

**Shared type/DDLは本書の正本ではない。** CHAT_01: UserId/TenantId/Task/Run and service actor, CHAT_10: tenant↔workspace/user+FK/metadata key migration, CHAT_03: tool capability/grant ledger, CHAT_04: Browser context, CHAT_05 OAuth/provider implementation, CHAT_08: Credential UI/API and channel binding。異なるownerの決定待ちは`[PROVISIONAL]`。

## 4. Requirements

| ID | Level | Observed expected | SC |
|---|---|---|---|
| SEC-CRED-001 | MUST | secretがモデルprompt、Tool result、Evidence、一般ログ、Telemetry、API projectionに現れない | 06,08,15 |
| SEC-CRED-002 | MUST | persistent secretはper-record AEAD+versioned AADでsealし、keyは別権限境界 | 06,08 |
| SEC-CRED-003 | MUST | tenant+owner+provider+scope+audience+operationでgrant絞込、unbound credential拒否 | 06,08,11 |
| SEC-CRED-004 | MUST | toolのcredential injection前にGateway approval/owner/active job/versionとegress originを検証 | 04,07,08 |
| SEC-CRED-005 | MUST | website Cookie/Browser stateを別user/task/tenantへ復元せず、raw export禁止 | 06,08 |
| SEC-CRED-006 | MUST | rotation/revoke中の旧lease、新leaseとcallbackの競合をversion CASで拒否 | 07,08 |
| SEC-CRED-007 | MUST | OAuth失効/refresh failure/invalid keyをNEEDS_REAUTHとして保存。副作用のblind retry禁止 | 03,07,11 |
| SEC-CRED-008 | MUST | UIの管理画面はmasked metadata/statusのみ、fresh reauth+CSRF・監査必須 | 06,08 |
| SEC-CRED-009 | MUST | バックアップから復元不可の鍵ならfail closed。誤った鍵でnull/空tokenへfall backしない | 07,08 |
| SEC-CRED-010 | MUST | HTTP redirect、cross-origin、protocol downgrade、MCP/Browser actionに秘密を自動転送しない | 05,08 |
| SEC-CRED-011 | MUST | broker cache, secret TTL, request bytes, refresh concurrencyに有限上限、期限切れzeroize候補 | 07,09 |
| SEC-CRED-012 | MUST | delete/rotation/owner disableでcookie/secret leaseとindex/search/traceを保護し、監査outboxを残す | 06,08,16 |
| SEC-CRED-013 | MUST | external page/tool textがsecret Ref、scope、recipientを書き換えられない | 03,08,11 |
| SEC-CRED-014 | SHOULD | hardware-backed KMSまたは外部Vault interface、blind-key rotation drill | 07,08 |
| SEC-CRED-015 | EXPERIMENT | token forwarding proxyをproviderごとに実装・acceptanceで比較 | 09 |

## 5. Internal contracts / validation（Security-owned proposal; cross-team shape not locked）

```ts
type SecretKind = "external_api_key"|"oauth_access"|"oauth_refresh"|"browser_storage"|"webhook_signer"|"bot_token"|"model_provider";
type SecretState = "ACTIVE"|"ROTATING"|"REAUTH_REQUIRED"|"REVOKED"|"DELETION_PENDING"|"DELETED";
type SecretMetadata = Readonly<{id:string; tenantId:string; ownerUserId:string; provider:string;
  kind:SecretKind; scopes:string[]; allowedOrigins:string[]; version:number; keyVersion:string;
  createdAt:string; expiresAt:string|null; state:SecretState; lastUsedAt:string|null}>;
type CredentialGrantRequest = Readonly<{intentId:string; taskId:string; runId:string; goalRevision:number;
  runLeaseEpochDecimal:string; secretId:string; secretVersion:number;
  executorId:string; method:string; scheme:"https"; origin:string; approvedActionHash:string}>;
type ScopedCredentialLease = Readonly<{leaseId:string; expiresAt:string; audience:string; boundActionHash:string}>;
interface CredentialBroker {
  importSecret(operator:AuthenticatedPrincipal, input:SealedSecretUpload):Promise<SecretMetadata>;
  authorizeInjection(request:CredentialGrantRequest, attestedGatewayContext:ServerOnlyContext):Promise<ScopedCredentialLease>;
  inject(leaseId:string, immutableRequest:BoundHttpRequest):Promise<RedactedHttpResponse>;
  revoke(owner:ServerOnlyActor, secretId:string, expectedVersion:number):Promise<void>;
}
```

Validation: `SecretKind` strict enum、provider identifier allowlist、max scope count、canonical ASCII HTTPS origin(禁止userinfo/fragment/hostname wildcard)、constant time token verification when applicable、`runLeaseEpochDecimal` は符号無し10進で非負の64bit値。OriginはDNS/effective IP検査とTLS hostname verificationの両方を満たす。grantに渡すSecret IDは**Gateway自身がTool registry/configとowner DBから選ぶ**。LLMがSecretを選ぶことは禁止。Webhook-signing keyはインバウンド検証専用で外向きHTTPへ注入しない。HTTPS injectionではredirectを`manual`にし、各hopでpolicy再評価後、原則credentialsを破棄する。

**暗号化レコードの概念**（SQL table定義はCHAT_10）:
```json
{"secretId":"<opaque>","ownerRef":"<server-resolved>","schemaVersion":1,
 "keyVersion":"local-master-v2","nonceRef":"<binary-nonce>","ciphertextRef":"<sealed-blob>",
 "aadSchemaVersion":1,"state":"ACTIVE","provider":"example","expiresAt":null}
```
`nonceRef`は秘匿値ではないが再利用不可。GCM 96-bit nonceはCSPRNGで毎回新規生成し同じkeyでの衝突を防ぐ運用設計が必要。XChaCha等は採用ライブラリでnonce長を合わせる。AEADが認証しない平文metadataを権限の証拠に使わない。

## 6. State transitions / sequences

```text
UNCONFIGURED --operator+freshAuth/import--> ACTIVE
ACTIVE --expiry/revoke/provider 401--> REAUTH_REQUIRED
ACTIVE --rotate CAS + new key--> ROTATING --verify new version--> ACTIVE
ROTATING --crash/partial failure--> ROTATING (resume with journal, no unseal fallback)
ACTIVE/REAUTH_REQUIRED --operator revoke--> REVOKED -> DELETION_PENDING -> DELETED
REVOKED/DELETED --restore of older backup--> never ACTIVE without explicit reauth + revocation epoch reconciliation
```

Normal: user grants connector → server authenticates → validates provider scopes → broker stores sealed token with exact owner/tenant → persist metadata/receipt/outbox in transaction → Task requests tool → Gateway freezes approved intent + active user + Run leaseEpoch + goalRevision → broker issues short-lived single-audience capability → connector executes with header injected **outside model runtime** → response stripped of auth/cookie and secret-bearing fields → canonical evidence saved. Browser takeover injects state only in owner-scoped Browser Worker, not by returning JSON state to UI.

Refresh: use per-secret-version lock (DB CAS); exactly one refresh at a time; transient 5xx retry with exponential capped attempts, 401/invalid_grant sets REAUTH_REQUIRED; old refresh token discarded only after new sealed version+provider receipt committed. If provider rotates token but DB commit loses ack, mark refresh`UNKNOWN` until provider re-consent or deterministic reconcile; do not blindly present old token as valid.

Rotation: Phase A install key version N+1 in broker → Phase B encrypt existing N to N+1 using record-version CAS in chunks (bounded 100 records/transaction) → Phase C verify sample and count missing → Phase D switch default key version N+1, keep old key read-only for approved rollback window → Phase E decommission old key after backups/re-encryption proof. On failure, allow only records that can be authenticated; do not garbage collect last decryptable key.

## 7. Executable pseudocode

```ts
async function injectAuthorized(intentId: string, secretId: string) {
  const intent = await gateway.readPersistedApprovedIntent(intentId); // CHAT_03
  const ctx = await authz.resolveOwnerAndScopedServicePrincipal(intent.taskId, intent.runId);
  const s = await vault.loadMetadataForOwner(ctx.tenantId, ctx.ownerUserId, secretId);
  if (!s || s.state !== "ACTIVE" || isExpired(s)) throw NeedsReauth();
  await gateway.recheckBeforeDispatch({intent, owner:ctx, goalRevision:intent.goalRevision,
    runLeaseEpoch:intent.leaseEpoch, approvalHash:intent.argumentsHash});
  const recipient = connectorRegistry.resolveConfiguredRecipient(intent.toolId);
  if (!isExplicitlyAuthorizedOrigin(recipient,s.allowedOrigins)) throw PolicyDenied();
  await egress.assertSafeResolutionAndPinnedTransport(recipient); // CHAT_09 SSRF
  const lease = await leases.atomicCreate({intentId, secretId, secretVersion:s.version,
    recipientOrigin:recipient.origin, ttlSeconds:30, oneUse:true});
  return broker.sendWithInjectedCredential(lease, recipient, {redirect:"manual",
    redactRequestAndResponse:true}); // no raw credential crosses return boundary
}
async function rotateSecret(id, expectedVersion, newValue) {
  const s = await vault.lock(id); // no network calls in DB tx
  if (s.version !== expectedVersion || s.state === "REVOKED") throw Conflict();
  const sealed = await cryptoProvider.seal(newValue, canonicalAAD(s));
  await vault.compareAndSwapSealedRecord(s.id,expectedVersion,sealed); // other leases fenced
  await leases.revokeVersionsAtMost(id,expectedVersion);
}
```

**Boundaries**: `vault.lock` pseudocode indicates bounded row locking only over metadata/CAS; cryptographic work is done before short DB transaction. Do not hold a DB lock during remote token refresh. Dynamic code execution/ arbitrary command secret provider prohibited. Broker cannot reach arbitrary domain from tool argument; hostname must come from administrator-configured connector policy.

## 8. Failure matrix

| Failure | Outcome / retry / visibility |
|---|---|
| vault unavailable, timeout, wrong AAD/key | deny, bounded retry only for unavailable, then `SECRET_UNAVAILABLE`; user sees reauth/temporary failure, never plaintext |
| ciphertext tamper/key mismatch | `SECRET_INTEGRITY_FAILURE`; audit+quarantine, no fallback |
| tenant/owner mismatch | 404/deny with only safe metadata |
| OAuth expired/invalid_grant | `NEEDS_REAUTH` and watch paused where necessary; no arbitrary repeated refresh |
| refresh response received, DB commit lost | `UNKNOWN_REFRESH`; operator consent/verification; no blind retry |
| rotation+worker concurrency | old credential grants revoked by secretVersion, in-flight provider request may still complete; ledger reconciles |
| browser worker killed | browser profile state only restore to same owner after sealed state re-auth; stale lease denied |
| redirect to untrusted origin/private IP | drop auth headers, deny request, audit sanitized host |
| upload/operator cancel | discard temporary plaintext/nonce staging, zeroizable buffers best effort |
| encrypted backup without matching key | fail closed, request reconnect; do not create default placeholder token |

## 9. Security / privacy

- Secret contents never in structured logs, metrics labels, traces, HTML screenshot, HAR, crash dump, plaintext backup, model prompt or user-facing API. **Absolute process-memory secrecy cannot be guaranteed** under privileged host compromise; minimize lifetime/size, isolate broker process and disable debug memory/core dump capture. Redaction is a secondary boundary, never sole control.
- Separate OAuth secrets from user session Cookie, webhook verify-only key, long-lived model provider credential and browser credentials; applying one to another provider is forbidden.
- Avoid echoing sensitive content in exceptions; structured error codes only. `Authorization: Bearer...`, `Cookie`, `Set-Cookie`, URL tokens and form body credentials are scrubbed at collection boundary. Regex-only leakage prevention is insufficient; prefer field allowlist, structured redact + canary tests.
- External-origin DOM may itself display credentials/user private data. It is **untrusted evidence**, not authorization; collect minimally/redact before LLM with denylist+structured classifier; if redaction uncertain, retain access-controlled artifact but do not send to model.
- Encrypted browser profiles and screenshots (if retained) are governed by owner-specific retention and deletion, as are backup copies. Leases and scopes never grant broad filesystem, shell or Docker access.

## 10. Metrics / budget / limits

Count grant.allow/deny (reason only), vault decrypt error, rotation age, stale version rejects, OAuth reauth required, missing key, secret-leak canary failures. Do not label metrics with secretId, full host path, user email or token. Initial test knobs (not production benchmark): max 1 concurrent refresh per secret, 30s grant, max 4 provider retries and total refresh timeout 30s, max 100 rotated records/batch, max 256 KiB token responses, secret buffer lifetime <= request deadline, redaction processing output max 1 MiB; real acceptance requires cap/compatibility measurement. Cost: brokerage deterministic, LLM must not be invoked to recover/rotate tokens.

## 11. Persistence / transaction / migration [PROVISIONAL: CHAT_10]

Physical records: secret metadata `(tenant/workspace,ownerUserId,secretId,provider,kind,version,keyVersion,scopes,allowedOrigins,expiresAt,state,updatedAt)`; sealed object key and nonce/AAD version; grant ledger with hashed grant ID/use count; credential rotation journal and audit/outbox reference. Foreign keys enforce same owner/tenant. Encryption key source is separate from DB and from general worker container; startup refuses no key and missing keyVersion. During upgrade add columns with expand/backfill/validate/contract; old token in logs/config may need operator remediation, never automatically copy plaintext to artifacts. TenantId→workspace mapping per PR #9 reconciliation; do not equate with userId or use raw workspace ID to authenticate.

## 12. API/UI proposals [PROVISIONAL: CHAT_08]

`POST /api/v1/credentials` (fresh auth + CSRF, one-time secret ingress; no echo), `GET /credentials` (masked metadata), `POST /credentials/{id}/rotate`, `POST /credentials/{id}/revoke`, `GET /credentials/{id}/status`; all owner-scoped, mandatory Idempotency-Key and If-Match where relevant, no `GET /credentials/{id}/plaintext`. Webhook signatures validated before account binding; Telegram secret not shown in UI. SSE only status events, no provider error body. Human handoff cannot reveal browser cookie or CDP endpoint.

## 13. Implementation PR plan

1. **CRED-1** Secret classification, broker interfaces, vault schema/crypto/KMS POC and key bootstrapping.
2. **CRED-2** Tenant+owner binding, grant ledger/fencing + tool egress origin-attested injection (CHAT_03/05/09).
3. **CRED-3** Browser state, OAuth refresh and token rotation CAS, reconnect UI.
4. **CRED-4** redaction+trace+backup migration, crash/rotation/compromise simulation, security release gates.

## 14. Test cases — **designed, not executed**

| ID | Setup/action | Pass criterion |
|---|---|---|
| CRED-T01 | canary keys through Tool/Model/Trace/Log/export/errors | raw canary substring **0** in all unauthorized outputs |
| CRED-T02 | flip AEAD ciphertext/AAD owner field and decrypt | failure and no plaintext |
| CRED-T03 | A/B users and sameTenant different owner use secretId swapped | no token bytes transmitted; access 404/deny |
| CRED-T04 | malicious HTML instructs tool to choose different credentialRef/recipient | 0 new grants, no unauth exfiltration |
| CRED-T05 | approval expired, GoalRevision changed or old Run leaseEpoch | dispatch 0, bounded deny |
| CRED-T06 | send authenticated request then 302 to internal/other provider/HTTP | auth header not forwarded; internal request blocked |
| CRED-T07 | concurrent OAuth refresh + crash just before/after DB commit | single current secretVersion; unknown→needs reauth, no blind retry |
| CRED-T08 | rotate N→N+1 while 2 Workers hold old grants | old new dispatch 0; already external effect logged as uncertain if needed |
| CRED-T09 | kill worker/browser, reopen encrypted storageState from A in B | restore denied, cookies cross-owner 0 |
| CRED-T10 | disable user/tenant/revoke secret and replay queued job | no new credential injection after commit |
| CRED-T11 | restore encrypted DB backup without keyVersion | fail closed; explicit re-link required |
| CRED-T12 | 1000 fake credentials, rate-limited ingest, log scanning | configured caps enforced, no high-cardinality token label |
| CRED-T13 | UI GET/list/rotate without auth or without fresh session | 401/403; plaintext 0 |
| CRED-T14 | provider returns 401/429/5xx and refresh invalid_grant | 401=reauth; 429 bounded Retry-After; 5xx capped; no infinite loops |
| CRED-T15 | malformed nonce/reused key version/short entropy key validation | startup/reseal failure, never silently downgrade |

Tests use fake providers and canary secrets; no live provider account or real OAuth token. **Actually executed: none**.

## 15. Traceability (all MUST)

| REQ | TEST | SC |
|---|---|---|
| SEC-CRED-001 | T01,T12 | 06,08,15 |
| SEC-CRED-002 | T02,T11,T15 | 06,08 |
| SEC-CRED-003 | T03,T04,T13 | 06,08,11 |
| SEC-CRED-004 | T05,T06 | 04,07,08 |
| SEC-CRED-005 | T03,T09 | 06,08 |
| SEC-CRED-006 | T07,T08,T10 | 07,08 |
| SEC-CRED-007 | T07,T14 | 03,07,11 |
| SEC-CRED-008 | T01,T13 | 06,08 |
| SEC-CRED-009 | T02,T11 | 07,08 |
| SEC-CRED-010 | T06 | 05,08 |
| SEC-CRED-011 | T12,T14 | 07,09 |
| SEC-CRED-012 | T08,T10,T12 | 06,08,16 |
| SEC-CRED-013 | T04 | 03,08,11 |

## 16. Cross-team proposals / unresolved

| ID / Owner | Severity | Needed decision & test |
|---|---|---|
| CRED-P1 / CHAT_01+10 | **P0** | User/Tenant/Workspace owner binding and secret owner FK; isolation `CRED-T03`, PR #9 `AUTH-R2-T01` |
| CRED-P2 / CHAT_03+05 | **P0** | Tool intent ID/schema/approved action hash and provider origin allowlist; egress `CRED-T05/06` |
| CRED-P3 / CHAT_04 | P0 | sealed browser storageState type, lease fencing, human takeover; `CRED-T09` |
| CRED-P4 / CHAT_10 | P1 | master key operator input, backup rotation/migration/restore drill; `CRED-T11/15` |
| CRED-P5 / CHAT_08 | P1 | fresh-auth credentials UI, channel binding, error display; `CRED-T13` |
| CRED-P6 / CHAT_00+09 | P1 | actual AEAD algorithm/library and strict key storage policy by ADR + implementation spike |
| CRED-P7 / CHAT_11 | P1 | secret canary redaction CI with crash replay; test fixtures cannot use live credentials |

**Open decision:** Native KMS vs encrypted file+OS-secret mount, OAuth scopes/failure contract, support for multiple providers and lifecycle. These are proposals, not yet Locked. High-risk unresolved inputs keep Draft.

## 17. Sources (all inspected 2026-10-30 JST)

- [S1] **Code verified** OpenClaw `resolveSecretRefValue(s)`, `resolveSecretRefString`, provider caps, `src/secrets/resolve.ts`, `8b8c33ef334b3754f42afe2a2e212418abfa80f4`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/secrets/resolve.ts
- [S2] **Code verified, tests not run** `src/secrets/resolve.test.ts` same SHA: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/secrets/resolve.test.ts
- [S3] **Code verified** `src/secrets/model-egress.ts` `withConfiguredModelEgress`: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/secrets/model-egress.ts
- [S4] **Code verified, tests not run** `src/secrets/model-egress.test.ts` redacting stream and cancellation: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/secrets/model-egress.test.ts
- [S5] **Code verified** `src/logging/redact.ts` structured masking: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/logging/redact.ts
- [S6] **Code verified** Playwright `BrowserContext.storageState`: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/packages/playwright-core/src/server/browserContext.ts
- [S7] **Code verified, tests not run** https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/tests/library/browsercontext-storage-state.spec.ts
- [S8] **Code verified** Better Auth `getSession` and `session-api.test.ts`: https://github.com/better-auth/better-auth/blob/95d158b04cd8217cc17443e809c700f3b6969f97/packages/better-auth/src/api/routes/session.ts
- [S9] **Docs only** OWASP Cryptographic Storage: https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html ; Secrets Management: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- [S10] **Hypothesis** Broker/Vault, particular AEAD selection, default timeouts, schema and pricing are original proposed design **not externally verified**.
- [S11] **PR reports inspected** OpenClaw #178361 and #173635 (see appended upstream PR investigation) describe exact-value secret redaction and bounded refresh races; neither is integrated or product-verified here.

## Review gates

- [x] Scope/owner/alternatives; pinned implementation and test paths (OpenClaw, Playwright, Better Auth); proposed contract, algorithm, state, race, revocation, migration, API.
- [x] All MUST → TEST → SC and negative security checks.
- [ ] Actual product integration, benchmarks, key-management spike and unresolved canonical contracts. **Keep Draft.**

## Upstream PR investigation — Credential redaction and refresh races
- **[PR inspected, open/unmerged]** OpenClaw [#178361](https://github.com/openclaw/openclaw/pull/178361) describes a direct SecretRef resolver path where secret values with unrecognized shapes were not added to exact-value redaction registration; the proposed change affects `src/secrets/resolve.ts`/tests. It is **not evidence that upstream main already fixed this**. Add canary with irregular token shape to `CRED-T01`; our design does not expose plaintext to generic logs in the first place.
- **[PR inspected, open/unmerged]** OpenClaw [#173635](https://github.com/openclaw/openclaw/pull/173635) documents bounded refresh retries under concurrent credential changes, motivating `CRED-T07/T08` CAS/cancellation/unknown tests. PR description asserts tests but those were **not independently run** here.
- Source grade: PR report, **not product verification**. No upstream copy, especially arbitrary command-based SecretRef providers.
