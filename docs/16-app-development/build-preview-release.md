# DEV-05 — Build Toolchain, Preview Broker, CI/CD, Release
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_02/03/04/05/08/09/10/11 | 実装・PoC・テスト未実施。

## Goal
Web/API、GitHub既存ソースの改修、Android/desktop/iOSの段階的開発を扱う。ローカルコード実行は既存 [sandbox.exec](../02-runtime/sandbox-execution.md) を利用。BuilderからホストShell/SSH/Docker socket、実秘密情報、raw Internet/CONNECT、Browser CDPを触れさせない。

## Target capability
| Tier | Product | Build/Test |
|---|---|---|
| P1 | React/Next/Vue Web、Python/Node/Go/Rust API、既存GitHub repo | Cell内のpin済みSDK、unit/integration、Browser E2E、mock preview |
| P2 | Android/Kotlin/Flutter/RN, Electron/Tauri/Linux desktop | SDK image/Gradle/Android emulator/ABI適合、専用isolated runner。APK/AAB、desktop成果物 |
| P3 | Swift/Flutter/RN iOS | 別途認可されたmacOS/Xcode runner必須。Ubuntu単体のiOSネイティブビルド不可 |

## Build/Package Broker
依存取得は既存の `package.fetch` typed RPC を拡張して、固定registry origin/version/hash/license/archive安全性とロックファイルを検証し、Trusted Package Brokerがcontent-addressed staging。Cell内npm/pip/Gradleなどはoffline cache使用。postinstall、ビルドscript、テストコードは攻撃可能な任意コードなのでSandbox quotas/secretなし/外部networkなし。toolchain image+lock hash+source tree shaをArtifactに残す。未知の依存、checksum改変、image不一致はfail closed。ビルドがexit0でも機能成功とはしない。

## Project Preview Broker
開発中サーバーの `localhost:3000` はBrowser Workerのlocalhostではない。一般のprivate-network/SSRF防止を緩めたり、任意TCP forward/CONNECTを許可しない。
1. Serverは project/owner/tenant/run/lease/workspace revision/image digest/許可port/プロトコル/TTL を検証した時だけ `PreviewSession` を発行。Port指定はboundedでOperator policyに従う。
2. Untrusted VMの**指定された**Executionで起動したpreview processのみに、Operator構成の専用制御IPCと認証付きgatewayが接続。実行VMへ公衆WAN/LAN経路を追加しない。別port、別VM、別tenantへの横流し不可。
3. Browser Workerは短命opaque `previewSessionRef` の専用仮想originにアクセス。HTTP, WS, SSE/HMRを制限付き転送するが、redirect/Host/Origin/DNS/IPv6/ServiceWorker/iframe/WebSocket upgradeでも管理ホスト/metadata/任意IP接続は禁止。
4. Browser WorkerはPage/CDP唯一のowner。Viewerには期限付き閲覧権のみ、操作時のHuman takeoverは排他lease/認可。Secret、Cookie、CDPソケットをブラウザユーザー/Cellへ配布しない。Preview中のHTTP bodyは低信頼情報。
5. RunまたはPreview expiry/revoke/cancel/steer、VM restartでgenerationを更新して旧viewerと旧grantsを失効。Previewの常時外部公開はdeployment承認を別途要求。

```ts
type PreviewSessionV1 = { previewId:string; projectId:string; executionId:string;
  workspaceRevision:string; generation:string; runtimeImageDigest:string;
  boundPort:number; expiresAt:string; viewerRouteRef:string;
  state:"STARTING"|"READY"|"FAILED"|"EXPIRED"|"STOPPING" };
```
server-owned ID/owner/port/policyはモデルのProposalから生成せず、検査済みprojectから解決する。Preview長期稼働が必要なら独立した**有限**Execution/lease/budgetにする。

## GitHub / Release providers
GitProvider Brokerの `fetch/branch.create/commit/push/pr.create/check.read/release.create` は個々のtyped Tool Intent、TargetRepo/branch/commitSha/If-Match、tenant/owner/cost/side effect ledger、Sentinel/Transportで認可。remote push ACK不明は照会のみ。ブランチ保護・force-push・merge・公開release・production deployは同じ「開発許可」から自動で権限拡張しない。認可済みの明確なscope内ならcommit/PRを自動作成できるが公開・署名・実データ更新は必要な承認を要求。
Secrets/Signing Brokerは鍵を決してCell/Model/traceへ露出させず、署名済成果物のdigest/receiptだけを返す。CIのpassは同一SHAの実runが確認できた場合のみ。Provider unavailable/未実行をpassedと扱わない。リリース成果物にSBOM/ライセンス/テスト状態/rollback条件を含める。

## Build manifest / tests
Build Manifest: projectId, sourceSha, imageDigest, dependencyLockDigest, testReceipts, artifactSha256, toolchainInfo, previewRef, signedStatus。 
- DEV-BLD-T01 pinned toolchain offline buildと再現、hash mismatch reject。
- DEV-BLD-T02 scriptsがhost/secret/Internet/Dockerへ到達不可。
- DEV-BLD-T03 WebSocket/HMR/HTTPが許可したpreview routeのみ動作。
- DEV-BLD-T04 previewからSSRF/private/別tenant/CDP試行全DENY。
- DEV-BLD-T05 expiry/old generation/revokeで旧viewer・実行grant拒否。
- DEV-BLD-T06 push ACK loss→remote SHA再照会、重複releaseなし。
- DEV-BLD-T07 Android emulator/macOS runner unavailableはUNAVAILABLE表示、偽成功なし。
- DEV-BLD-T08 CI/署名鍵/利用者データがArtifact/Modelへ露出0。

## 2026-11-08 P0-01 — Preview Capability / Origin / Socket Isolation (normative proposal)

**Preview != public Browser navigation.** Security authority: CHAT_09; Broker admission/effect is CHAT_03, Browser Worker/session ownership CHAT_04, DB atomic lease/retention CHAT_10. The following server-minted types are **proposals to be reconciled with CHAT_01 and CHAT_09**, not permission that a model can create:

```ts
type PreviewCapabilityV2 = Readonly<{
  previewId:string; tenantId:string; ownerUserId:string; projectId:string;
  taskId:string; runId:string; runLeaseEpoch:string;
  executionId:string; executionGeneration:string; workspaceRevision:string;
  boundPort:number; protocol:"http"|"https"; originId:string; originGeneration:string;
  browserSessionId:string; browserLeaseEpoch:string; capabilityId:string;
  capabilityVersion:string; policyEpoch:string; identityVersion:string;
  issuedAt:string; expiresAt:string; allowedMethods:readonly ("GET"|"HEAD"|"POST")[];
  allowedModes:readonly ("observe"|"demo_interact"|"debug_synthetic")[];
  maxBytes:string; maxConcurrentConnections:number; maxLifetimeSeconds:number;
}>;
type PreviewConnectionAdmissionV2 = {
  capabilityId:string; operationId:string; requestDigest:string;
  expectedRunLeaseEpoch:string; expectedBrowserLeaseEpoch:string;
  expectedPolicyEpoch:string; expectedOriginGeneration:string;
  targetProtocol:"http"|"https"|"ws"|"wss"; targetPath:string;
  mode:"observe"|"demo_interact"|"debug_synthetic";
};
```
The Capability token itself is server-only, opaque and non-exportable; the UI receives **only** a scope-reduced viewer/session reference. `PreviewCapabilityV2` is persisted in trusted control; never hand its raw fields to Cell JS or browser page. Tenant/owner/project/task/run/execution/port/origin/session/lease/identity/policy epochs must be resolved afresh from DB, not accepted from page, request Host header, AI or URL. **Fail CLOSED** if any binding missing or revoked. Each individual stream admission and every redirect/WS upgrade is independently reauthorized with current generation. Hold a short-lived revocable stream-lease on the protected transport; Stop/revoke/session change closes established HTTP streaming/WS/HMR sockets as well as preventing new ones. Lost revocation channel → bounded lease expiry + fail closed; no socket survives a stale lease grace period.

**Origin security:** Allocate cryptographically unpredictable project+session scoped origin IDs, isolated per-project top-level site and BrowserContext; never reuse origin on a different project or generation. Scope Cookie, localStorage, IndexedDB, CacheStorage and Service Worker to the project session context; destroy/partition storage on reuse, after revocation and on worker crash. Service Workers are disabled by default for preview/debug unless explicitly tested, scope-limited, and scrubbed. Block cross-project navigation, `window.open`, iframe cross-origin embeds, cross-context messaging, top-level opener access, CORS wildcard abuse, credentialed fetch to internal services, and untrusted page initiated protected Browser Worker operations. Render preview with no owner login, Vault token, bridge APIs, or browser extensions. Headers: strict frame sandbox, no ambient session cookies, CSP/connect-src restricted to scoped origin, Referrer-Policy=no-referrer. Browser sandbox stays enabled. Treat malicious JS in the preview as adversarial; origin partition is defense-in-depth, not authority.

**Binding actual process:** on each connection, verify VM attestation, guest identity, Execution PID namespace/handle, process generation, selected bound port, kernel socket owner and lease via trusted supervisor; a port number or model claim alone cannot bind a listening process. Allow only the intended process's HTTP listener, not the guest's arbitrary TCP services. Connection to a different PID/port/execution instance or resurrected PID rejects even if the URL string is identical. No generic CONNECT tunnel, arbitrary DNS lookup, proxy environment override, raw TCP/UDP, internal host route, or redirect to new target.

**HTTP/WS matrix:** normalize scheme, host, path, headers before policy; reject absolute-form proxy requests, CONNECT, TRACE, cross-scheme redirect, redirect to other virtual origin, malformed Host/Origin, DNS rebinding, IPv4/IPv6 literals, IP-zone identifiers, userinfo, CRLF/smuggling and WebSocket subprotocol surprises. Upgrade is a **new** authenticated admission with connection deadline, token limits, message size/rate cap and periodic current-epoch checks; Server-Sent Events/long polling similarly. If the preview service issues a redirect, check the next hop before following; never let Browser navigate an unscoped destination through Preview. The Browser Worker remains sole CDP owner. Browser Live Observation #71, if separately merged, is read-only and never grants `demo_interact`.

### P0-01 negative and fault verification
- **DEV-BLD-T09**: two projects with same port/cookie/Service Worker → zero cross-context state leakage; a reused origin must be rejected.
- **DEV-BLD-T10**: stolen/expired/revoked capability, mismatched tenant/Task/Run/Execution/browser lease/Policy epoch/owner → reject at admission.
- **DEV-BLD-T11**: concurrent Stop/Worker restart/revoke while WS/HMR/SSE already established → old sockets forcibly close and no further bytes admitted.
- **DEV-BLD-T12**: redirect/Upgrade/Host/Origin/IPv6/DNS rebinding/cross-origin iframe/opener attempts → no internal LAN, metadata, admin, other project, or raw CDP reachability.
- **DEV-BLD-T13**: attacker-controlled JS + fake Browser Action instructions → no privileged cookie, secret or Broker capability exfiltration; no executable callback to Trusted Plane.
- **DEV-BLD-T14**: reused PID, old Execution generation, port hijack and wrong VM attestation → preview denied despite matching port.
All **NOT RUN** until trusted-VM, browser, network and WebSocket fault tests execute.
