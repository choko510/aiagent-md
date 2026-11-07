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
- T02 scriptsがhost/secret/Internet/Dockerへ到達不可。
- T03 WebSocket/HMR/HTTPが許可したpreview routeのみ動作。
- T04 previewからSSRF/private/別tenant/CDP試行全DENY。
- T05 expiry/old generation/revokeで旧viewer・実行grant拒否。
- T06 push ACK loss→remote SHA再照会、重複releaseなし。
- T07 Android emulator/macOS runner unavailableはUNAVAILABLE表示、偽成功なし。
- T08 CI/署名鍵/利用者データがArtifact/Modelへ露出0。
