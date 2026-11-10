# Rust Local Device Client + Cua Driver — 統合実装設計

> Status: **Draft / PROVISIONAL / DESIGN ONLY**（2026-11-10）。ユーザー決定: ローカルPCクライアントの実装言語はRust。本書はサーバー全体のRust移行ではない。**設計・契約・受入条件の提案であり、デバイスアプリ、Cloud Gateway、Cua連携、Chrome拡張、実機テストは未実装・未実施**。
>
> Dependencies: [ADR-036〜040 / ADR-041](../00-foundation/architecture-decisions.md), [Architecture Rules `E](../00-foundation/architecture-rules.md), [Tool Gateway](../03-tools/tool-execution-gateway.md), [Approval](../03-tools/tool-approval-and-policy.md), [Sentinel](../09-security/sentinel-authority.md), [Runtime Cell](../09-security/runtime-cell-isolation.md), [Browser Session](../04-web-browser/browser-session-manager.md), [Browser Live Observation](../08-interfaces/browser-live-observation.md), [DEV-05](build-preview-release.md), [DEV-06](autonomous-browser-debugger.md)。実装面では未マージの [PR #82](https://github.com/choko510/aiagent-md/pull/82) を参考にするが、存在をmainの実装済み契約とみなさない。

## 1. 採用判断と非目標

- **採用**: Rust per-user Device Agent（鍵/通信/ローカルPolicy/Execution Journal）＋Tauri 2 UI（接続状態、許可、停止、観測）＋別プロセスのCua Driver（GUI/アクセシビリティ/キャプチャ）。クラウドは既存のPython-first Runtime/Tool Gateway/Approval/Sentinelを維持。Android/Build/Chromeは分離Adapter。
- **Cuaとの接続**: 最初はローカル限定のMCP stdio子プロセス。Windows/Linuxは`cua-driver mcp`を管理。macOSはTCC帰属と署名済みホストを維持できる**アプリ所有のembedded daemon + private socket + MCP proxy**を選択する。UI、クラウド、AgentにCuaのraw socket/CDP/任意MCPを直接開示しない。
- **理由**: Cua Driver自身のSDKはRustだが、SDK crate `cua-driver-sdk` は現時点で`publish = false`。Rustソースの直接vendoringは大きなworkspace依存・更新結合を生む。初版はバージョン固定した公式バイナリを独立プロセスで実行し、安定したMCP契約をAdapterで吸収する。将来、公開・安定したRust APIと実機測定が成立したときだけ、Adapter内部のin-process切替をADRで再審査する。
- **非目標**: リモートデスクトップの全面開放、PC全体の常時録画、クラウド経由の任意Shell、管理者権限の自動取得、ユーザーのCookie/パスワード抽出、Cloud Browser Workerの置き換え、WindowsのSession 0でGUI操作するWindows Service化。
- **原則**: PCは「外部のユーザー所有デバイス」であってUntrusted Execution VMのCellではない。`sandbox.exec`と同一権限にしてはならない。BrowserMCP/Chrome拡張による既存Chrome操作は、Cuaの一般GUI操作と区別する。

### 1.1 選択肢の判定

| 案 | 長所 | 失敗しやすい点 | 結論 |
|---|---|---|---|
| RustからCua SDKを同一プロセスにリンク | 低いIPC overhead、型付きAPI | crate非公開publish、native資源/権限/プロセスクラッシュの結合、macOS TCC/overlay差 | **保留**（安定APIと計測後に再評価） |
| Rust Device Agent → Cua MCP子プロセス | 公式実行面、個別再起動・版固定・テスト差替え、macOS daemon経路を使用可能 | MCP schema適合、バイナリ配布/署名、プロセス監視が必要 | **初版採用** |
| RustでWin32/UIA等を全面再実装 | OSS依存が減る | OS/Toolkit別の入力・capture・検証が膨大 | 不採用 |
| 全MCP/CDPをクラウドへ公開 | 実装が簡単に見える | フル権限の逸脱、User/Task別認可と収束しない | 不採用 |

## 2. 配置図・信頼境界

```text
[Trusted Control VM]
 Agent Runtime (Python; Tool stub)
   → Tool Gateway (唯一のToolIntent/Approval/Effect Ledger正本)
   → Sentinel (対象Device/Operation/Grantを認可)
   → Device Gateway / Delivery Outbox (提案、別権限プロセス)
   → authenticated outbound-initiated WSS tunnel at Edge
══════════ Public network / tenant boundary ══════════
[User PC, current signed-in OS session]
 Rust Device Agent (User token, non-admin; private keys, local policy, journal)
   ├─ Cua Adapter → cua-driver mcp (Windows/Linux)
   │                / app-owned embedded daemon + proxy (macOS)
   ├─ Chrome Adapter → optional BrowserMCP local daemon + extension
   ├─ Build Adapter → isolated build runner / explicitly trusted workspace
   └─ Android Adapter → locally installed ADB, serial-bound operations
 Tauri 2 Desktop UI ↔ private owner-restricted local IPC ↔ Rust Device Agent
```

- クラウド側でDevice Gatewayを新設しても、**Runtime/Cellが直接PCと通信する通路を作らない**。Tool Gatewayのadmission、承認、Sentinel判定、Effect Ledgerを必ず経由する。通信路の恒常的な確立と、個々の副作用の許可は別。
- Device Agentが**外向き**にTLS/WSSで接続する。ユーザーPCへの着信ポート/UPnP/広域LAN公開を要求しない。認証されたEdge経路以外にバックドア用poll endpointを作らない。
- 既存Trusted Egressを迂回した別のインターネット出口にしない。Device Gatewayへの送信許可・Gatewayからの命令配送の両方を、Sentinelが認証済みの**実送信境界**で監査し、発行済みTool Intentと照合する。
- Device AgentはOSの**一般ユーザー権限**。同じOSユーザーで動く悪意あるプロセスからの完全な隔離は保証できない。ローカルIPCのACLや鍵保護を「同一UID/SIDからの絶対的防御」と宣言しない。

## 3. Rustクライアント構成・プロセス寿命

提案ディレクトリ（将来の実装先、今PRでは作成しない）:

```text
desktop/
  Cargo.toml               # Rust workspace, pinned Cargo.lock
  crates/
    device-agent/           # per-user tray/background process + Tokio
    device-protocol/        # generated schema-compatible wire types/validator
    device-transport/       # TLS WSS, authenticated enrollment, reconnect
    device-policy/          # local action admission, restrictions, revoke
    device-journal/         # durable local effect journal + crash recovery
    adapter-cua-mcp/        # stdio MCP lifecycle, schema, OS session lock
    adapter-chrome/         # optional BrowserMCP sidecar, origin/tab policy
    adapter-build/          # recipe executor and artifact broker
    adapter-android/        # serial/package-scoped ADB
  app/src-tauri/            # Tauri 2 desktop shell / tray / signed updater
  app/ui/                   # WebView presentation only
```

- Windows: ログイン済みユーザーのper-user agent（Startup/トレイ）。Session 0のsystem serviceでマウスを動かさない。GUI操作にはinteractive sessionが必要。端末ロック、UAC secure desktop、RDP切断、権限不足は`DEVICE_NOT_INTERACTIVE`等で拒否。
- macOS: 署名された責任元アプリからembedded daemonを直接起動し、Accessibility/Screen RecordingのTCC権限を明示取得。LaunchServicesによる不適切な再起動や独立daemonへの権限の横流しをしない。権限未許可ではfail closed。
- Linux: X11/XWayland/Wayland compositorの対応可否を実測し、未対応操作は`UNSUPPORTED_COMPOSITOR`。仮想入力の成功をすべてのWayland環境で保証しない。
- アプリ側WebViewにはデバイス秘密鍵、任意ローカルファイル、Shell、MCP/CDPポートを渡さない。TauriのcapabilityはUI露出抑制に使い、**Rust handler自身の認可**も必ず行う。
- ローカルUIを閉じてもトレイ常駐設定時はDevice Agentが稼働可能。ただしOSログアウト・画面ロック後のGUI操作は許可しない。終了・緊急停止・ユーザーの入力はAIの継続より優先。
- Rust AgentとTauri UIを別プロセス化する案を第一候補とし、IPCはWindows owner SID限定Named Pipe / Unix mode 0600 UDS。ユーザー別1 agent、同一画面1 controllerの排他Lease。

## 4. Device Enrollment、資格情報、接続

1. 未登録デバイスは`UNPAIRED`。ユーザー操作による「PCを接続」でDevice AgentがDevice Key Pairを生成。秘密鍵はWindows Credential Manager/DPAPI、macOS Keychain、Linuxの適切なSecret Storeを用い、暗号化できない場合は登録を拒否（平文設定への保存なし）。
2. Device AgentがTLS先のDevice Gatewayから短命のpairing requestを受信し、ブラウザで本人に既存アカウント認証を求める。Cloudのowner/tenantは**認証されたサーバーセッション**から解決する。表示コードとPC側コードを照合しユーザーが承認する。
3. サーバーはDevice public key/owner/tenant・失効世代・device capabilitiesの最小情報を保持。登録は鍵の所持証明でありTPM attestationを意味しない。主張されたOSやtoolchainをtrust proofとして扱わない。
4. `wss://`のTLS（証明書検証必須）＋短命接続tokenとnonce署名でproof-of-possession。tokenをURL queryに含めず、ログ/診断に残さない。再接続には新たなchallengeと単調なconnection generationを要求。
5. `PAIRED_OFFLINE → ONLINE → STALE → OFFLINE`。`REVOKED`は新規接続・命令を拒否。Device再登録は新しいIdentityと明示承認が必要。Gatewayが落ちても`ONLINE`を推測しない。
6. 盗難/紛失デバイスの失効、キー回転、サーバーPITR時の`restoreIncarnation`増分、全セッションの閉鎖と再ペアリング条件を実装。旧接続の通信再開だけで権限を復元しない。

**技術選択**: rustlsを候補とするTLS、Tokio系async runtime、Serde + JSON Schemaを用いた厳格なwire検証。認証プロトコルや署名形式は暗号ライブラリの実績ある標準を使用し、独自暗号を作らない。ライブラリのバージョンと脆弱性・ライセンスは実装PRでpinする。

## 5. 操作認可：CloudとLocalの二段階

クラウドのTool Gatewayは**唯一の承認とIntent正本**。ローカルはより厳しい条件で拒否できるが、クラウドで未承認の操作を「ローカルで許したから」で実行できない。

- Cloud: Authenticated user → Task/Run/Goal/Lease/HEAD proposal → Tool Registry schema → policy/budget → approval → Sentinel → Device Gateway delivery。
- Local: device identity/session/restore generation → signed, single-intent action grant → allowed adapter/op/target/args digest → interactive user policy/consent/lock state → Cua/Browser/Build各Adapter固有の制約 → execute。
- Action Grant（提案）に結合: `tenantId, ownerUserId, deviceId, taskId, runId, goalRevision, leaseEpoch, restoreIncarnation, toolIntentId, attemptFence, operation, targetFingerprint, argsSha256, policyRevision, expiresAt, nonce`。受信側は全て検証し、grantと実引数の不一致は拒否。
- `session`やタブ名、PID、URL文字列、ユーザー入力のownerIdは認可証明ではない。PID/window handle/tabIdは再利用されるため、実行直前にプロセス開始時刻・window generation・browser identity・tab document generation等を再照合。
- Cloud側Grantの有効範囲とLocal Policyの積集合のみ有効。GUI操作は`READ_ONLY`、`WINDOW_CONTROL`、`DESKTOP_CONTROL`、`BROWSER_SITE`、`BUILD_RECIPE`、`ANDROID_TEST`等のカテゴリを細分化。Cuaのbounded manifestは上限で、Task/Origin別grantの代替ではない。
- 画面全域のcapture、クリップボード、ファイルdialog、パスワード入力、UAC、OS設定、未知アプリ起動、任意コード/外部送信はデフォルト拒否。不可逆な外部操作は既存の個別approvalを要求。
- `Stop`またはLocal revokeは新規操作を遮断、通信を切断、driver actionのキャンセルを試みる。ただし既にOSへ配達した不可逆入力を巻き戻せないため、結果は`UNKNOWN`になり得る。

## 6. Cua MCP Adapter：具体的な実装契約

- 起動前に署名済み配布物のdigest・バージョン・ライセンスを検証。実行ファイルを`PATH`から無条件で拾わず、ユーザー認可したインストールを選択。外部`curl | shell`をアプリ起動時に自動実行しない。
- Windows/Linux: 子プロセス`cua-driver mcp`をstdioで起動、MCP `initialize` → `tools/list`の実際のschema確認。macOSはCuaのembedded host仕様に沿いprivate socketへproxy接続。
- MCP serverが宣言するToolをそのままCloudへforwardしない。**静的なAdapter allowlist**（例: `list_windows`、`get_window_state`、`click`、`type_text`、`scroll`、`press_key`）と実際のschema双方を検証。未知版・Tool・schema差分は`DRIVER_INCOMPATIBLE`。
- Cuaの`bounded` + capability manifestで許可範囲を減らす。`unrestricted`は使わない。`--grant existing-profile`を起動時一律に与えない。Chromeの既存プロフィールは`Chrome Adapter`側で明示ペアリングを基本とする。
- GUI action前にfresh snapshot取得、対象のPID/window generation + element tokenを確認。`delivery_mode: background`から開始し、`background_unavailable`時に勝手にforegroundへ昇格しない。ユーザーの前面操作許可があり、同じターゲットを再検証したときのみ再提案。
- 同じ物理desktopへの操作は**global exclusive lock**。異なるCua session名でもキーボード/focusは共有。Chrome Adapterと同じChromeウィンドウへの操作は排他・経路選択を統合し、Chromeで拒否されたOriginをCua経由で迂回させない。
- 複数モニター、表示倍率、window移動、フォーカス、現在frameの鮮度を操作前後に検証。合成入力が対象以外のアプリに届いた兆候があれば以後停止。
- Cuaの終了/再起動でセッション・element tokenを破棄し、新しいDriver generationで再取得。子プロセスに秘匿済みでないTool引数をログ出力させない。

## 7. Chrome拡張・サーバーブラウザとの責務分離

- ユーザーPCのログイン済みChromeは**オプションのBrowserMCP系ローカルAdapter**。初版はBrowserMCPのMCP daemon +拡張機能をローカルsidecarとして利用できるかPoCする（追加JS runtimeの同梱サイズ・アップデート検証を測定）。最初からRustでCDP Bridgeを再実装しない。
- BrowserMCPはローカル`127.0.0.1`のみ・拡張のペアリング/Stopを保持。Cloud Device Agentの登録とは別の同意。**クラウドにMCPの任意コマンド文字列やraw CDP WebSocketを露出しない**。
- Browser Toolは`browser.tab.inspect` / `browser.tab.click` / `browser.tab.type` / `browser.tab.navigate` / `browser.console.read`等の型付き小操作に変換。1操作ごとに`Origin + profile + tabId + documentGeneration + action + args hash`を再検証する。
- 既存ChromeのCookie、storageState、password manager、認証ヘッダー、秘密DOM/Console/Network値はモデルへ無条件に渡さない。危険な`Runtime.evaluate`/raw CDP capabilityは初版で公開しない。ページContentからのPrompt Injectionも権限拡張を引き起こせない。
- BrowserMCPの`chrome.debugger`は対象タブのdebugging bannerや競合を生み得る。ユーザーがChrome側でCancel/Stopしたら、モデルが自動再接続・再許可してはならない。
- **サーバーBrowser Worker**は既存`04-web-browser`正本のまま。Local Chromeは新しいDeviceAdapter/resource typeとして区別。`08-interfaces/browser-live-observation.md`のサーバーBrowserの映像権限をローカルChrome映像へ流用しない。

## 8. Build・デバッグ・USB実機

GUI操作とビルドは独立したAdapter/Policy境界とする。

| Operation案 | 対象 | 実行条件 |
|---|---|---|
| `device.build.run` | Cargo / Gradle / 指定Toolchain | allowlisted workspace ID、ユーザーが登録したrecipe、引数配列・cwd・envの固定、有限CPU/時間/出力 |
| `device.build.log.read` | ビルドログ | 同じExecution ID、secret redaction、bounded chunks |
| `device.artifact.inspect` | APK / Binary / Test result | SHA-256、workspace root、mime/size、read-only |
| `device.android.list` | ADB一覧 | 明示許可されたUSB/エミュレータのみ、serial再検証 |
| `device.android.install` | 指定APK/パッケージ | 本人が許可したserialとpackage ID、artifact digest、確認済みdebug profile |
| `device.android.test` | Instrumentation / logcat | 対象serial + package、ログの秘密除外、期限付き |
| `device.ios.*` | macOS + Xcode + 署名済みDevice | 後期追加、ユーザーのApple signing keyをクラウドへ渡さない |

**Build Recipeの罠**: Cargo build.rs、Gradle plugin/task、npm lifecycle script等はコンパイルの一部として任意コードを実行できる。`argv allowlist`だけでは安全なビルドとはいえない。クラウド生成/未信頼のソースはローカルVM/隔離環境でビルドし、明示的に信頼したworkspaceでのみUser権限のネイティブbuildを許可する。隔離不可なら`BLOCKED_UNSAFE_BUILD`、ユーザーの明示的なTrusted Workspace昇格なしに実行しない。Docker daemonの無条件公開も禁止。
- デバッグ用デバイス接続はBuild Toolから直接USB全権を渡さず、`serial/package/session`で限定したADB Brokerへ。アプリインストール・データ削除・debugger attachは別権限。USBデバイスやLocalhostサーバーをCloud Runから自由に探索させない。
- 成果物はハッシュ固定のArtifact Brokerを通して返し、ローカルファイルパスの絶対値やHome/Secretをログへ出さない。ネットワーク越えのuploadはCloudの承認＋ローカル転送Policyを満たすこと。

## 9. 実行ジャーナル・通信断・再試行

- Device Agentの暗号化/アクセス制限されたlocal SQLite journalに`(deviceId, toolIntentId, requestSha256, attemptFence, restoreIncarnation)`、`RECEIVED → ADMITTED → STARTED → COMPLETED/FAILED/UNKNOWN`、対応するdriver generation、receipt digest、許可記録を保存（生の入力文字列・秘密画像は保存しない）。
- `STARTED`記録のDurable commit後に副作用を送る。切断やクラッシュの発生箇所によって「操作済みか不明」が残るため**exactly-onceの保証はしない**。その場合は`UNKNOWN`をCloud Ledgerへ伝え、書き込み操作を自動再試行しない。
- `RECEIVED`かつ実行前と証明できる操作は再配送可。`COMPLETED`/`FAILED`は同じintentionを再実行せず保存済みreceiptを返す。`toolIntentId`同一・request digest違いは`INTENT_CONFLICT`。読み取りで安全と証明できる操作だけ再実行を許可する。
- Cloud側`tool_gateway.py`のEffect Ledger/Unknown/Reconcileを正本にし、Local SQLiteは重複実行防止の補助。Local成功をCloudの`CONFIRMED`へ反映する前に、Cloud側の現行run lease/goal/revocationを再検証。
- Clientのreconnect/backoff/jitter/heartbeatは必要だが、接続復活だけで旧grantを復活させない。新しいconnection generation＋run fence＋restore incarnationを照合。長期offline時は保留操作を実行せず期限切れとして返す。
- Ctrl+CやStopは新規dispatch停止と子プロセス終了を優先するが、OSが既に受け取ったinputやAPK installをロールバックできない。partial/unknownを成功扱いしない。

## 10. APIとワイヤ契約（正式所有者の承認前）

**正本追加の提案**であり既存`contracts/domain/v1`やMigrationをこのPRでは編集しない。命名・型は共有Schemaと既存int64 codecへ統合してから確定する。

| Proposed endpoint | 認証者 | 意味 |
|---|---|---|
| `POST /v1/devices/enroll/start` | owner user | short-lived pairing initiation |
| `POST /v1/devices/enroll/complete` | owner + device proof | public key登録・確認 |
| `GET /v1/devices` | owner user | tenant/owner scope一覧 |
| `POST /v1/devices/{id}/revoke` | owner user | revoke + epoch bump +接続停止 |
| `GET /v1/devices/{id}/capabilities` | owner user | attestした操作能力のRead model |
| `POST /internal/device-dispatch` | trusted Tool Gatewayのみ | admitted intentに対する配送 |
| `WSS /internal/device-channel` | registered device proof | authenticated outbound-initiated channel |

提案Envelopeの概念:

```json
{
  "schemaVersion": "device-action/1",
  "messageId": "uuid",
  "deviceId": "uuid",
  "toolIntentId": "uuid",
  "operation": "device.ui.invoke",
  "targetRef": "opaque-window-capability",
  "arguments": { "elementRef": "fresh-element-ref" },
  "argsSha256": "sha256-digest",
  "attemptFence": "1",
  "connectionGeneration": "9",
  "expiresAt": "2026-11-10T14:00:00Z",
  "grant": "short-lived-signed-envelope"
}
```

- 値は例示であり有効な署名や実UUIDではない。`grant`はServer発行の認証済みGrantを表す概念。生のowner/tenantは受信ペイロードの自己申告だけでは認証にならない。
- 型・`int64`・versionは`contracts/domain/v1`が正本、Python/Pydantic・Rust/Serde・WebViewの型を生成する。未知major/余分なfield/負数・オーバーフロー・期限・schema driftはfail closed。Device-specific enumを生成型へ重複手書きしない。
- 新規DB表はDeviceIdentity/Pairing/Connection/Grants/Revoke/DispatchReceipt/ArtifactAccess等の**提案のみ**。CHAT_10が既存migrationに重複・衝突しないexpand/backfill/contract案を採用し、RLS/owner分離、同じ`tool_intents`/`side_effect_records`と整合させる。
- Log/Observability: deviceId（必要なら匿名化）、operation class、intentId、duration bucket、result code、redacted receipt digestだけ。画面内容、パスワード、クリップボード、キー入力、Full DOM、full URL queryはログ不可。

## 11. UXとプライバシー

Tauri UI: 初回pairing、接続状態、登録済みデバイス、許可中のアプリ/サイト/Workspace、動作中Runと現在の操作、短命の承認ダイアログ、操作停止、Cloud側のDevice Revoke、ログインユーザーの明示切替。

- `観測`と`操作`と`外部転送`は別Consent。PC画面は初期状態で共有しない。ローカルスクリーンショットをCloudへ送るには明示された対象window・時間・目的・redactionの確認。秘密を確実に除去できない画面は全面遮断。
- キャプチャした静止画・アクセシビリティtree・ブラウザDOMの長期保存はオプトイン別契約。生データをDevice Agentの診断ファイルへ残さない。Clipboardを介する入力は元の内容を復元できない場合があり、標準手段にしない。
- 実操作中はトレイ/overlayで現在の制御元と停止手段を見えるようにする。利用者が他の作業を始めたら操作を一時停止する選択肢を提供する。foregroundへ勝手に切り替えない。

## 12. リリース・ライセンス・依存

- Rustクライアントは署名済みインストーラ、Tauri更新署名、SBOM、アーキ別成果物（Windows x86_64を初期対象）、署名とSHA-256を検証したCuaバイナリを別管理。Cua DriverのMIT部分とoptional perception等の非MIT部分を区別する。後者は初版に含めない。
- Cua公式CLI/SDKのversion・MCP tool schema・起動引数・TCC帰属は固定した版でWindows実機/ macOS実機に対して確認。forkから自動ビルド/配布するならLICENSE/NOTICE、第三者ライセンスを監査。
- BrowserMCPはApache-2.0。フォーク配布時には名称・アイコン・ライセンスと変更履歴を別管理。追加sidecarの依存更新はCua更新とは独立。
- 初版の対応範囲はWindows 11 user-session + Cua。Linux/macOS、Chrome、Build、Androidは順次拡張。未対応機能をUIで有効なボタンとして表示しない。

## 13. 実装順序（1つの巨大PRにしない）

| Stage | Delivery | Exit gate |
|---|---|---|
| LDC-0 | 本設計/ADR/契約とThreat Model合意 | ownerレビュー、PR #82契約とのdiff解消 |
| LDC-1 | Rust Agent + Tauri pairing/status/Stop、Mock Device Gateway | device cross-owner denial、revoke/restore epoch、TLS/reconnect負試験 |
| LDC-2 | Cua MCP Adapter、Windows 11でRead-only window inspection | Cua版/Tool schema固定、no raw MCP、local policy refusal |
| LDC-3 | 特定ウィンドウの許可付きclick/type/scroll | foreground/background排他、Stop、unknown副作用 |
| LDC-4 | Device Gateway ↔ Tool Gateway/Sentinel/ledger E2E | 本番同等トポロジーでadmitted intentからreceiptまでの証跡 |
| LDC-5 | Chrome拡張Adapter（別PoC）、Build/ADB Adapter | Origin別権限、ビルド隔離、serial/package fencing |
| LDC-6 | macOS signed app-owned daemon、Linux compositor matrix、配布 | 実機OS permission/署名、再起動・update/rollback試験 |

LDC-1〜4が揃うまで、ユーザーの実PCに対する本番無人操作を公開しない。PR #82のKVM実機検証未了が残る場合、ローカルDevice経路の合格でその不足を相殺しない。

## 14. 受入テスト / REQ → TEST → 既存SC

下記はすべて**DESIGNED / NOT RUN**。SCは既存仕様との関連であり、新しいグローバルSC-IDの採番はCHAT_11の審査対象。

| Requirement | Test | 確認する事実 / Failure oracle | Related SC |
|---|---|---|---|
| LDC-REQ-01 device identity | LDC-T01 | 別owner/tenantでdevice列挙・接続拒否、コード期限切れ、鍵取替不可 | SC-06/08 |
| LDC-REQ-02 transport | LDC-T02 | 自己署名TLS、無効token、nonce replay、pin不正、device revokeは0操作 | SC-08/21 |
| LDC-REQ-03 Sentinel admission | LDC-T03 | Tool Gateway/承認/Sentinelを迂回した命令は0操作・0receipt | SC-08/19 |
| LDC-REQ-04 fencing | LDC-T04 | old run/goal/lease/restore incarnation/connection世代を拒否 | SC-07/21 |
| LDC-REQ-05 effect journal | LDC-T05 | 応答欠落・process kill・再配達後の二重write 0、未確定はUNKNOWN | SC-07/21 |
| LDC-REQ-06 Cua schema | LDC-T06 | Tool未登録、schema差異、driver再起動時の古いelement token拒否 | SC-08/24 |
| LDC-REQ-07 background policy | LDC-T07 | background失敗時にforegroundへ自動昇格せず入力漏れ0 | SC-08/24 |
| LDC-REQ-08 desktop lease | LDC-T08 | 複数Run/Cua＋Chrome同一画面でfocus競合0、排他のフェンス | SC-07/24 |
| LDC-REQ-09 Chrome privilege | LDC-T09 | 未許可Origin、別tab、cookie/storage/raw CDP拒否、ユーザーStop尊重 | SC-06/23/24 |
| LDC-REQ-10 build safety | LDC-T10 | 悪意あるbuild scriptでhost・秘密・ネットワークへ到達できない | SC-17/18/24 |
| LDC-REQ-11 ADB scope | LDC-T11 | 別serial/別packageへinstall/debugできず、device抜差しで再確認 | SC-24/31 |
| LDC-REQ-12 screen privacy | LDC-T12 | password画面・敏感windowでcapture/export/log禁止 | SC-08/23 |
| LDC-REQ-13 interactive session | LDC-T13 | lock screen/RDP切断/別session/ UAC secure desktopでGUI拒否 | SC-08/24 |
| LDC-REQ-14 release | LDC-T14 | 改ざんCua binary/署名不一致/update rollback/既存設定を検証 | SC-18/24 |
| LDC-REQ-15 reconnect | LDC-T15 | Network partition→reconnectで旧grant再実行0、read-onlyなら安全に復旧 | SC-07/21 |
| LDC-REQ-16 negative egress | LDC-T16 | Local deviceに公開Listenなし、Client発の任意LAN/SSRF/自律elevation不可 | SC-08/19 |

追加のLDC-SC01〜LDC-SC05（ローカル提案）: ①未登録PCへの操作拒否、②ユーザーが許可したウィンドウだけ操作、③強制切断中に起きた不確定副作用の照会、④ユーザーChromeをSite単位に制限、⑤許可済みAndroid端末に検証済みAPKを入れてログ取得。global SCへの昇格はQA Ownerレビュー後。

## 15. 監査すべき上流実装と制約（2026-11-10調査）

- Cua: [driver README](https://github.com/trycua/cua/blob/main/libs/cua-driver/README.md)、[SDK Cargo.toml](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/crates/cua-driver-sdk/Cargo.toml)（`publish = false`）、[Windows delivery](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/crates/platform-windows/src/input/delivery.rs)、[Windows click](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/crates/platform-windows/src/tools/click.rs)、[Authorization](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/crates/cua-driver-core/src/authorization.rs)、[macOS embedding](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/Skills/cua-driver/EMBEDDING.md)。
- BrowserMCP: [Relay](https://github.com/BrowserMCP/mcp/blob/main/packages/mcp/src/relay/server.ts)、[CDP policy](https://github.com/BrowserMCP/mcp/blob/main/packages/extension-core/src/background/cdp-policy.ts)、[pairing](https://github.com/BrowserMCP/mcp/blob/main/packages/extension-core/src/background/pairing.ts)、[command engine](https://github.com/BrowserMCP/mcp/blob/main/packages/mcp/src/commands/engine.ts)。
- Tauri 2: [capabilities](https://tauri.app/security/capabilities/)はWebViewに権限を絞るが、Rust handlerの安全性そのものを証明しない。
- Microsoft UFO³: [device connection](https://github.com/microsoft/UFO/blob/main/galaxy/client/components/connection_manager.py)、[Android MCP](https://github.com/microsoft/UFO/blob/main/ufo/client/mcp/http_servers/mobile_mcp_server.py)。Device Gatewayを直接移植せずプロトコル・故障モードを参考にする。
- ライセンス・CLI/API・Platformサポートは実装時点で再監査。READMEや計画書にある機能を動作実証済みと混同しない。

## 16. 未確定事項 / owner handoff

1. CHAT_00: ADR-041（PC側RustはBackend言語変更ではない）承認。
2. CHAT_01/03/09/10: DeviceIdentity・ActionGrant・Receipt・Operation risk class・DB/RLS・Sentinel配送の共有契約確定。PR #82のheadに合わせ、実装前にschema/migration差分を再検証。
3. CHAT_04/08: ローカルChromeとサーバーBrowserの別resource型、Tauri権限UI、観測の秘密判定。
4. CHAT_05/16: Build recipeと実機ADB/iOSの隔離・署名鍵の扱いを決定。
5. CHAT_11: LDC-T01..16をWindows/macOS/Linux実機で実測し、`DESIGNED`から`TESTED`へ更新。

**Release gate**: リモートDevice操作の未承認0・クロステナント0・生秘密露出0・旧世代操作0の否定試験、真正な実デバイスを使うGUI/Build/E2E、Cua互換性、installer署名、障害復旧の証跡がそろうまで`NOT PRODUCTION READY`。
