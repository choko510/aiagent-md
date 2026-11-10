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
4. `wss://`のTLS（証明書検証必須）＋短命接続tokenとnonce署名でproof-of-possession。tokenをURL queryに含めず、ログ/診断に残さない。再接続には新たなchallengeと単調なconnection generationを要求。**アカウント切替ではこのconnectionを使い回さない**（§4.1参照）。
5. `PAIRED_OFFLINE → ONLINE → STALE → OFFLINE`。`REVOKED`は新規接続・命令を拒否。Device再登録は新しいIdentityと明示承認が必要。Gatewayが落ちても`ONLINE`を推測しない。
6. 盗難/紛失デバイスの失効、キー回転、サーバーPITR時の`restoreIncarnation`増分、全セッションの閉鎖と再ペアリング条件を実装。旧接続の通信再開だけで権限を復元しない。

**技術選択**: rustlsを候補とするTLS、Tokio系async runtime、Serde + JSON Schemaを用いた厳格なwire検証。認証プロトコルや署名形式は暗号ライブラリの実績ある標準を使用し、独自暗号を作らない。ライブラリのバージョンと脆弱性・ライセンスは実装PRでpinする。

### 4.1 同じOSユーザーでCloudアカウントを切り替える場合（P2-03）

- Device Identityは物理PC単位の万能鍵ではなく、**`(OS user SID/UID, tenantId, ownerUserId, deviceIdentityId)`ごとに独立Key Pair**を発行し、OS Secure Storeでも分離する。Device登録状態・Local Policy・秘密設定・Grant/Journal/Artifact cacheのnamespaceを同じ組で分離し、アカウントBがAのDevice Key、実行履歴やArtifactを見られないようにする。アカウントBは別のCloud pairingと本人認証を必要とする。
- アカウントA→Bへの切替は`ACCOUNT_SWITCH_PREPARE → STOP_NEW_ACTIONS → RECONCILE_OR_UNKNOWN → REVOKE_A_TUNNEL_AND_GRANTS → ZEROIZE_A_ACTIVE_MEMORY → AUTHENTICATE_B → LOAD_B_NAMESPACE`。AのActive Run / desktop lease、Cua/Chrome子プロセス、pending grants、認証token、旧接続を遮断し、Bのsessionで再利用しない。OSデスクトップのユーザー切替ではロック/非対話状態を別途検査する。
- Aで既に始まった操作の完了可能性は消せない。AのReceiptは**Aの暗号化されたjournalに保存**し、後日A自身の再認証で旧Intentを照合可能にする。Aの未確定EffectをBへコピーしない。AのReceiptをBが提出/再解釈してBのTaskを完了させることは拒否する。Aのjournalをアカウント切替だけで消去せず、retention/明示削除規則を適用する。
- OSユーザーを共有する場合、同じOS権限を持つ別プロセスからの完全分離は保証できない。ファイルACL/鍵ストアの保護限界をUIへ提示し、アカウント切替時にOwner/tenant境界違反をfail closedにする。


## 5. 操作認可：CloudとLocalの二段階

クラウドのTool Gatewayは**唯一の承認とIntent正本**。ローカルはより厳しい条件で拒否できるが、クラウドで未承認の操作を「ローカルで許したから」で実行できない。

- Cloud: Authenticated user → Task/Run/Goal/Lease/HEAD proposal → Tool Registry schema → policy/budget → approval → Sentinel → Device Gateway delivery。
- Local: device identity/session/restore generation → signed, single-intent action grant → allowed adapter/op/target/args digest → interactive user policy/consent/lock state → Cua/Browser/Build各Adapter固有の制約 → execute。
- Action Grant（提案）に結合: `tenantId, ownerUserId, deviceId, deviceClaimEpoch, taskId, runId, goalRevision, leaseEpoch, restoreIncarnation, toolIntentId, attemptFence, operation, targetFingerprint, argsSha256, policyRevision, expiresAt, nonce`。**`deviceClaimEpoch`はPC横断Task移動専用の単調増加世代で、Runの`leaseEpoch`と独立**。Gatewayでの配送、Deviceの受信、Local Journalへの記録、**OS副作用の直前**にすべて照合する。値不一致/世代失効/オフライン/Grant期限切れは新規実行を拒否。grantと実引数の不一致も拒否。
- **直前オンライン認可（P1-01）**: `click/type_text/press_key/navigation/send/install/write/build-with-side-effects`等の状態変更は、Local Policy判定後、操作の直前にCloud Tool Gatewayへ`authorize_start(toolIntentId, deviceId, deviceClaimEpoch, leaseEpoch, attemptFence, argsSha256)`を認証済み接続経由で照会する。Cloudは現行Device Claim/Run/Goal/Approval/Sentinel/Effect状態と認可対象の同一性を**同じトランザクション境界または同等のCAS/予約**で確かめ、使用済みなら拒否する。Localは新しい`START_PERMIT`（当該Intent/Claim/Connection/対象/期限に限定した単回Permit）を得た場合だけ開始を認める。インターネット不達時に既存Grantだけで開始しない。端末側にもサーバー応答と時刻・対象を確認する実行直前ゲートを置く。読み取りも秘密情報の流出を伴う場合はオンライン再認可を要求する。
- 直前PermitとCloud側Handoff CASの競合は、Cloudが`inFlight`を予約したか`FENCED`にしたかを単一の順序で記録する。Permitを発行済みでもOS配送に至る前の切断/Cancelで停止する努力は必要だが、**Permitを取得した瞬間と物理入力の間を原子的にはできず、通信断後の即時失効は保証しない**。HandoffはinFlight/UNKNOWNを確認し、依存する新たな副作用を止める（§19.2/§9.1）。
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
- 同じ物理desktopへの操作は**global exclusive lock**。異なるCua session名でもキーボード/focusは共有。Chrome Adapterと同じChromeウィンドウへの操作は排他・経路選択を統合し、Chromeで拒否されたOriginをCua経由で迂回させない。**この文だけでは強制不能なので§6.1の実行側制御を必須とする。**
- 複数モニター、表示倍率、window移動、フォーカス、現在frameの鮮度を操作前後に検証。合成入力が対象以外のアプリに届いた兆候があれば以後停止。
- Cuaの終了/再起動でセッション・element tokenを破棄し、新しいDriver generationで再取得。子プロセスに秘匿済みでないTool引数をログ出力させない。

### 6.1 Desktop CuaとBrowserの強制分離（P1-04）

- **Desktop Cua runtime**は`bounded` + **非ブラウザApp Identityのpositive allowlist** + `desktop.display: false` +必要最小限のUI toolを使用。Windowsなら署名・実行ファイルpath/Process Identity/StartTime/Window handle generation、macOSならbundle ID等を実行前・直前に照合。Chrome/Edge/Brave/Firefox/Chromium、ブラウザ内蔵WebView、ブラウザとして扱うElectron等は**Desktop入力からデフォルト除外**。未知App/Identity混在のウィンドウも拒否。Cua manifestのApp resourceが実際の制限を表現できない場合、そのAppをDesktop操作に許可しない。
- Desktop Cuaからの汎用`click/type_text/press_key/drag`、座標入力、フォーカス変更、App launch、global shortcut、Address barへの入力を使って**ブラウザへ到達する経路をすべて拒否**。Window ID/Proc変化後や画面重複の疑いでは一旦停止。デスクトップ全体へのクリックと`get_desktop_state`は初版無効。Tool manifestだけに依存せず、Rust Adapterがtarget-classifier/Window ownership/Actionの二重検証を強制する。分類できない場合は`BROWSER_BOUNDARY_UNKNOWN`でfail closed。
- **Browser runtime**はDesktop Cuaと**別プロセス・別MCP session・別Manifest/Grant**とし、Origin/Tab/Documentを束縛する**typed browser toolsのみ**を公開。ユーザーの既存Chromeは原則BrowserMCP local Adapterに限定し、Cuaのbrowser-only runtimeを採用する場合も同じOrigin/Tab/Document Policyを満たしたときだけ許可。`--grant existing-profile`をDesktop runtimeへ渡さない。
- Cua公式のPermission仕様では`resources.browser.origins`と汎用Desktop入力/観測Toolは分離が必要（[Cua permissions](https://cua.ai/docs/cua-driver/guides/permissions)）。**同一runtimeにBrowser Origin制限と汎用Desktop入力を併存させず**、あるAdapterで拒否されたOriginを別Adapter/ショートカットへ再提案しても拒否する。守れないOS/アプリでは初版のブラウザ併用を無効化する。


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

- Device AgentのOwner/Tenant分離された暗号化/アクセス制限local SQLite journalに`(tenantId, ownerUserId, deviceIdentityId, deviceId, deviceClaimEpoch, toolIntentId, requestSha256, attemptFence, restoreIncarnation)`、`RECEIVED → ADMITTED → STARTED → COMPLETED/FAILED/UNKNOWN`、対応するdriver/connection generation、receipt digest、許可記録を保存（生の入力文字列・秘密画像は保存しない）。`deviceClaimEpoch`や署名/Intent/Request digest不一致では重複実行を許可しない。
- `STARTED`記録のDurable commit後に副作用を送る。切断やクラッシュの発生箇所によって「操作済みか不明」が残るため**exactly-onceの保証はしない**。その場合は`UNKNOWN`をCloud Ledgerへ伝え、書き込み操作を自動再試行しない。
- `RECEIVED`かつ実行前と証明できる操作は再配送可。`COMPLETED`/`FAILED`は同じintentionを再実行せず保存済みreceiptを返す。`toolIntentId`同一・request digest違いは`INTENT_CONFLICT`。読み取りで安全と証明できる操作だけ再実行を許可する。
- Cloud側`tool_gateway.py`のEffect Ledger/Unknown/Reconcileを正本にし、Local SQLiteは重複実行防止の補助。**実行開始の認可と、過去に始まったIntentのReceipt照合は別規則**。新規開始のみ現在のRun Lease/Goal/Device Claim/Revocationを要求する。既存Intentの正当なReceiptは、Run Cancel/Lease変更/Device Handoff後でも`toolIntentId, owner/tenant/device identity, original deviceClaimEpoch, attemptFence, request digest, signer, execution record, receipt digest`を照合し、Cloudの元IntentのEffect Ledgerへ`CONFIRMED/FAILED/UNKNOWN`として歴史的に記録できる。旧RunのReceiptを新Runの`RUN_COMPLETED`や新Goalの成功判定へ転用してはならない。
- **Receipt専用経路（P1-05）**: `reconcile_existing_intent`は新規Tool dispatchと別API/権限。元Owner namespaceから署名されたReceiptだけ受け入れ、元Intentが当時の認可対象だったこと・実行Journalと一致することを検証。Receipt受信時の現行Run Lease/Grant失効**だけを理由に捨てない**。競合更新はCAS、重複Receiptはidempotent、矛盾Receiptは`RECEIPT_CONFLICT`で監査へ。
- Clientのreconnect/backoff/jitter/heartbeatは必要だが、接続復活だけで旧grantを復活させない。新しいconnection generation＋run fence＋`deviceClaimEpoch`＋restore incarnationを照合。長期offline時は保留操作を実行せず期限切れとして返す。
- Ctrl+CやStopは新規dispatch停止と子プロセス終了を優先するが、OSが既に受け取ったinputやAPK installをロールバックできない。partial/unknownを成功扱いしない。


### 9.1 異なるIntent ID間の意味的重複防止（P1-02）

- `toolIntentId`による重複防止は**同じID内のみ**。Cloud Gatewayは`semanticEffectKey`（例: `tenantId + project/task goalRevision + external target identity + action kind + normalized payload/idempotency scope`）と依存Step/resourceのEffect Barrierを発行・保持する。鍵生成はTool Gatewayが検証済み引数と認可された対象から行い、LLMやユーザーが任意の文字列で衝突回避/権限拡大できない。外部対象を正規化できないときも同一resourceの保守的なBarrierを使う。
- 同一の外部対象/意味的に同じ送信・決済・投稿・install・delete等について、Aの`SENDING/STARTED/UNKNOWN`が残るとき、**別Run/別PC/新しい`toolIntentId`であっても`WAITING_RECONCILIATION`を優先**。Task全体を止める必要はなく、依存Stepと競合resource上の新規副作用を封鎖し、独立read-only/証跡検索・安全な別resource作業だけ進める。
- Barrier解除は(1)元Intentの**信頼できるReceipt/外部照会**による`CONFIRMED`または`PROVEN_NOT_APPLIED`、あるいは(2)結果未確定であることと二重送信リスクを**人間が対象とペイロードを確認**し、既存Approval/Sentinelを経た独立の`RISK_ACCEPTED_RETRY`決定だけ。後者は自動解除ではなく、新Intent/新Permitで別の試行として監査し、元`UNKNOWN`は確定済みと書き換えない。元アクションが本当に取り消せた保証はない。
- 例: PC Aで`送信`クリック後に切断→`UNKNOWN`。PC Bの新Runはメール/メッセージ送信の再実行不可で、送信済み確認を先行。通信断中のAに同じ送信を繰返させない。受信先/内容の違う独立メッセージは別Effectキーでも、同一スレッドへの順序依存がある場合はresource barrierで待つ。
- **本規則はTool Gateway/Effect Ledgerの共通仕様への追加提案**。PR #82の`reconcile()`を複製してRust側に新しい正本を作らない。正式なDB unique/lock key、外部照会Providerとリスク分類はCHAT_03/09/10審査を要する。

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
  "deviceClaimEpoch": "12",
  "connectionGeneration": "9",
  "expiresAt": "2026-11-10T14:00:00Z",
  "grant": "short-lived-signed-envelope"
}
```

- 値は例示であり有効な署名や実UUIDではない。`grant`はServer発行の認証済みGrantを表す概念。`deviceClaimEpoch`はEnvelopeと署名付きGrant、Local Journal、Cloud Device Claim CASの**全てで同一**でなければならない。状態変更では操作直前に`START_PERMIT`を取得する（§5）。生のowner/tenantは受信ペイロードの自己申告だけでは認証にならない。
- 型・`int64`・versionは`contracts/domain/v1`が正本、Python/Pydantic・Rust/Serde・WebViewの型を生成する。未知major/余分なfield/負数・オーバーフロー・期限・schema driftはfail closed。Device-specific enumを生成型へ重複手書きしない。
- 新規DB表はDeviceIdentity/Pairing/Connection/Grants/Revoke/DispatchReceipt/ArtifactAccess等の**提案のみ**。CHAT_10が既存migrationに重複・衝突しないexpand/backfill/contract案を採用し、RLS/owner分離、同じ`tool_intents`/`side_effect_records`と整合させる。
- Log/Observability: deviceId（必要なら匿名化）、operation class、intentId、duration bucket、result code、redacted receipt digestだけ。画面内容、パスワード、クリップボード、キー入力、Full DOM、full URL queryはログ不可。
- `START_PERMIT`は内部Delivery Envelopeと同じ`deviceClaimEpoch`/Operation/Intent/Targetを結び、Cloud Tool Gatewayが一度だけ生成する短命の開始証明。署名されたRecipientであるDevice Agentだけが受け取り、別Device/別Owner/後継Runへ再使用不可。CloudがClaimを進めた後の遅延Permit生成はCASで拒否し、既発行PermitによるinFlight/UNKNOWNは§19の照合バリアに残す。

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
| LDC-REQ-04 fencing | LDC-T04 | old run/goal/lease/restore incarnation/connection/`deviceClaimEpoch`を拒否、in-flight OS入力はUNKNOWN分類し即時失効を誇張しない | SC-07/21 |
| LDC-REQ-05 effect journal | LDC-T05 | 応答欠落・process kill・再配達後の同一/別Intent二重write 0、別Runの依存StepはWAITING_RECONCILIATION。明示RISK_ACCEPTED_RETRYは例外として別記 | SC-07/21 |
| LDC-REQ-06 Cua schema | LDC-T06 | Tool未登録、schema差異、driver再起動時の古いelement token拒否 | SC-08/24 |
| LDC-REQ-07 background policy | LDC-T07 | background失敗時にforegroundへ自動昇格せず入力漏れ0 | SC-08/24 |
| LDC-REQ-08 desktop lease | LDC-T08 | 複数Run/Cua＋Chrome同一画面でfocus競合0、Desktop runtimeから禁止OriginへGUI迂回0、ブラウザ対象未知時は拒否 | SC-07/24 |
| LDC-REQ-09 Chrome privilege | LDC-T09 | 未許可Origin、別tab、cookie/storage/raw CDPを拒否。Cua Desktop click/type/hotkeyでChrome/Edge/Braveへ迂回0 | SC-06/23/24 |
| LDC-REQ-10 build safety | LDC-T10 | 悪意あるbuild scriptでhost・秘密・ネットワークへ到達できない | SC-17/18/24 |
| LDC-REQ-11 ADB scope | LDC-T11 | 別serial/別packageへinstall/debugできず、device抜差しで再確認 | SC-24/31 |
| LDC-REQ-12 screen privacy | LDC-T12 | password画面・敏感windowでcapture/export/log禁止、Cuaの既定screenshot無効、UIA treeのROI外送信0 | SC-08/23 |
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


## 17. Desktop固有機能：追加要求と全体の連携（2026-11-10、ユーザー要望）

本節の4機能は本仕様のLDC-0〜6を**拡張する設計提案**であり、PR #82をマージ済み/実装済みとはみなさない。すべて**DESIGNED / NOT RUN**。既存のDEV-01 Project/Workspace/Git、DEV-05 Build、DEV-06 Browser Debugger、DEV-12 Reproduction Lab、DEV-09 UI、Task/Run/Tool Gateway/Approval/Sentinel/Artifactの正本を再定義しない。

| ID | 機能 | 必要な新規責務 | 最初の実装目標 |
|---|---|---|---|
| LDC-F01 | **Local/Cloud自動振り分け** | Capability Catalog / Placement Planner / Local Data Boundary | OCR・アクセシビリティ・許可済みBuildはLocal、計画・推論・修正案はCloudへ |
| LDC-F02 | **PC横断Task移動** | Handoff Coordinator / Device Claim Fence / Workspace Transfer | デスクトップのTaskを別の登録済みノートPCで再開 |
| LDC-F03 | **ローカルプロジェクト自動認識** | Scoped Discovery / Toolchain Probe / Project Binding | Cargo・Gradle・Git・SDK・ADB等を検出し操作候補を提示 |
| LDC-F04 | **範囲選択 → 修正 → 自動デバッグ → 実機テスト** | Annotated Bug Report / Repro Run / Build/Test Verification | 画面の気になる領域から修正前後と端末実行結果まで追跡 |

**統合経路**: ユーザーが範囲選択 → LDC-F03が対象Project/Window/Deviceを候補提示 → LDC-F01がcapture・OCR・build等を許可済みPCへ配置 → Cloud DEV-06/12が再現・原因調査・修正 → LDC-F04がBuild/ADB/UI検証 → 別PCが必要な場合はLDC-F02で安全に再開。4機能は「1個の無制限Agentモード」ではなく、**有限Task/Run + 個別Tool Intent + 明示的デバイス権限**の組合せ。

### 17.1 公開能力とUI原則

- クラウド接続なしでもローカルで「登録済みProject一覧・SDK検出結果・範囲選択の未送信Preview」を表示できる。**Cloud推論が必要な操作をOffline中に無断実行しない**。
- Tauri UIのトップには`現在の実行先（Local/Cloud）`・対象PC・対象Project・共有する情報の種類・現在のRun・承認待ち・停止を表示。
- 自動判定が不可能な場合は不確実な状態を明示し、推測でPCを選んだり許可を拡張したりしない。費用削減や速度最適化は承認・機密境界より優先しない。

## 18. LDC-F01 — Local/Cloud自動振り分け

### 18.1 分割方針

**Local first for device/OS-bound operations; Cloud for reasoning/coordination.** 推論は基本的に既存のPython Agent Runtime / Model Gatewayに残し、Rust Device Agentを第二のAgent Loopにしない。

| 処理 | 初期配置 | fallback / 特記事項 |
|---|---|---|
| OS Window/UIA tree・マウス/キーボード・キャプチャ | 対象Local DeviceのCua | GUI対象をCloudへ移動不可。Offline時は操作停止 |
| OCR・画面の簡易前処理・画面解像度/DPI補正 | 対象Local Device（OS API候補: Windows OCR / Apple Vision / Linux OCR Adapter） | OS APIの対応・ライセンス・品質はPoC評価。未対応時は**ユーザー同意したredacted画像**をCloudへ送る選択肢のみ |
| プロジェクト検出・SDK/USB認識 | 対象Local Device | ローカル探索結果を勝手に全量アップロードしない |
| Rust/Cargo・Android/Gradle等のビルド | 明示許可されたLocal隔離Runner、もしくは既存Untrusted VM Cell | 未信頼ビルドはLocalの通常ユーザー権限で実行しない。隔離不可→BLOCKED、勝手にCloud/Hostへ昇格しない |
| Goal分解・Planning・レビュー・原因仮説・パッチ提案 | Cloud Agent Runtime / Model Gateway | 機密原本を送る前にUser/Local Policy検証。Cloud不可時は停止・限定的にローカル保存 |
| Browser自動デバッグ | 既存Cloud Browser Worker **または**許可済みLocal Chrome Adapter | この2つは権限・セッション・Originが異なる。無言で相互に切替不可 |
| Unit/E2E/実機テスト | 現行テストレシピとデバイスに合う許可済み環境 | ユーザーの物理USB端末は移せない。物理端末テストは接続先PCを選ぶ |

### 18.2 Placement Planner 契約案

Cloud側`PlacementPlannerPort`は各Task Stepの実行先を**候補として選ぶ**。Tool Gatewayが最後の権限判断をするため、Plannerの候補をGrantとみなさない。入力は最小化した`CapabilitySnapshot`、実際のユーザー承認、必要なArtifact所在、ToolchainVersion、DeviceOnlineState、cost/budget、Projectのデータ所在、機密性ラベル、Run lease等。

- 能力広告は`deviceId, clientVersion, adapterVersion, osFamily, interactiveSessionState, supportedOperations, toolchainVersionDigests, observedAt, capabilityGeneration, policyRevision, resourcePressureBucket`を基本とする。**OS/SDKがインストール済みという自己申告は能力証明ではない**。実行前にLocal Deviceの能力とPolicyを再評価する。
- 実行候補は`REQUIRE_LOCAL_DEVICE` / `PREFER_LOCAL` / `PREFER_CLOUD_CELL` / `CLOUD_ONLY_REASONING` / `USER_CHOICE_REQUIRED` / `NO_ELIGIBLE_TARGET`。決定順は(1)権限/データ所在、(2)必須環境・端末、(3)隔離条件、(4)期限・リソース、(5)推定時間/転送量/費用。料金比較だけでデータ境界を緩めない。
- `Plan = (taskId, runId, goalRevision, stepId, projectRevision, sourceSha, selectedDeviceId?, adapter, operation, reasonCode, capabilityGeneration, policyRevision, inputArtifactRefs, outputArtifactPolicy, fallbackRules, expiry)`。必要ならrecordVersion/CAS。型正本はCHAT_01/03承認待ち。
- 自動再配置は**読み取り・再計算可能なStep**に限定。GUI click、書き込み、Push、APK install、外部送信は同じ副作用を繰り返す危険があるため、移動前に元デバイスのEffect状態を`CONFIRMED / NOT_STARTED / UNKNOWN`へ照会。
- デバイス側はPlan単位の実行を直接受け取らず、既存の**typed Tool Intent + signed action grant**のみ受け取る。Cloud Plannerが選んだdeviceでもLocalがより厳しく拒否できる。
- 画面画像/DOM/Console/ファイル内容をCloud推論へ送る場合はUser Policyの`dataClass + sourceApp + projectId + purpose + maxBytes + retention + destination`を検査。Secret redactionが不確実なら全文送信しない。
- Local OCR結果は位置情報と信頼度、元フレームのDigest、Window/Display Generationを付与。OCRテキストをUIA要素のIDと混同せず、低確信の場合は再観測か人手確認。
- リソース圧迫時は低優先OCRやテストを遅延できるが、重いCloudモデルへ画像を自動エスカレーションしない。ユーザーに「別環境を使う理由」を表示。

### 18.3 測定と障害

ローカル/クラウドへの配置理由、実行場所、権限拒否、queue wait、tool execution time、画像転送量、Cloud推論時間、電力/CPU負荷の概算を**機密を含まない指標**で記録。固定fixtureで`Local preferred`と`Cloud preferred`を比較し、速度・費用・成功率を測る。失敗時の許可範囲拡大をfallbackと呼ばない。

## 19. LDC-F02 — PC横断Task移動（Handoff）

### 19.1 「Task移動」の正確な意味

**移すのはTaskの継続に必要な検証済み状態と実行先の権限であり、動作中プロセス・ウィンドウ・GUIセッションそのものは移動しない。**

- Task/Run/GoalRevision/Checkpoint HEAD/Tool Intent/Effect Ledgerの正本はCloud側に維持。別PCは新しいDevice Claimを取得してから、持ち込めるArtifact/Project Snapshotを開いて続きを実行する。
- 継承可能: 明示的に共有を許可したrepoのcommit SHA、承認済みworkspace snapshot、redacted log/test result、再現手順、選択したUI領域（redacted）、build recipe digest、目的と未完ToDo。
- 自動継承禁止: OSパス、開いたウィンドウやPID、Clipboard、USB端末の接続、Chrome Cookie/プロフィール、Keychain/資格情報、署名鍵、環境変数、実機のprivate data、進行中のShell/GradleプロセスやCua element token。
- **端末Aのローカルのみの変更（未保存ファイル、Git dirty diff、untracked files）はデフォルトで移送しない**。安全なProject Artifact Snapshot化をユーザーが選び、secret scan・サイズ上限・ファイル単位の送信同意を通したものだけ別PCへ転送。Aがオフラインなら未取得の変更を「転送済み」と表示しない。
- 同一Git repoでも端末Bのワークスペースを勝手に上書きしない。`sourceSha/workspaceRevision/expectedHeadSha`が一致しない場合はDiff確認、競合解決、別worktree作成の選択が必要。
- 署名に必要なiOS/macOS credentials、ADB実機serialの再同意は移動先BのLocal Policyで評価。未対応toolchainや物理端末不足は`BLOCKED_TARGET_CAPABILITY`で停止。

### 19.2 中断・移動・再開の原子的手順

1. **PREPARE**: UIから移動先Bを指定、または能力不足で推奨。CloudがOwner/Project/Task、Bのオンライン状態・Capability・必要なdata classを確認。まだ旧PC Aの権限をBへコピーしない。
2. **QUIESCE**: Aの対象Runについて新規操作受付を止め、active actionを完了/失敗/UNKNOWNとして確定できる限り照会。GUIの途中操作があるならStop結果を明示し、Cloud Effect Ledgerで不確定を保持。
3. **SNAPSHOT**: Cloud canonical HEADと同じrevisionのProject Snapshot / Test Manifest / Artifact digestを凍結。Aに残るローカル状態は分類して本人に確認。秘密を含む差分は共有不可。
4. **FENCE**: CloudでCASの成立を条件に`deviceClaimEpoch`を進め、Aのデバイス別Action Grant/Start Permitを失効。復旧世代、Run lease、Effect Intentを再検証。**ACKやオフライン通知に依存せずサーバー配送時に旧世代を拒否**し、Local側でも実行直前のOnline認可と当該epoch照合で旧命令を拒否。すでに開始された副作用は`IN_FLIGHT/UNKNOWN`として別の照合バリアへ（§9.1）。
5. **STAGE**: B側で許可済みProject binding/worktreeとArtifactを整合確認。ステージング失敗ならBへActive権限を与えず、明示的`HANDOFF_BLOCKED`として残す。
6. **ACTIVATE**: Bの登録済みDevice Key・Local Policy・ユーザー同意・必要SDK/USB対象を確認し、**新しいTask segment / Run claim**で再開。Aのraw session/approvalを横流ししない。**§9.1の`WAITING_RECONCILIATION`にある依存Stepへの新規状態変更IntentはBからも発行不可**。独立read-only作業のみ進行可。
7. **RECONCILE**: Aが遅れて再接続しても旧claimを再開不可。Aからの旧receiptは**元Intentへの歴史的Effect照合**として取り込み、勝手に二重実行せず新Runの成功扱いもしない。Cloudが状態確定できない副作用は`UNKNOWN`でUser/Verifierへ提示し、関連対象の再送を止める。

**線形化境界**はCloudのdevice claim CAS とTool Gatewayのdispatched-intent admission。旧PCにあらかじめ投入済みの命令の停止はネットワーク断中に物理的には保証できないため、短命Grant、Local expiry、通信不達で新規高リスク操作を拒否するPolicy、接続時の再検証を重ねる。`exactly-once`または通信断後の即時取消しを保証しない。

### 19.3 UX・競合

Tauriの「このTaskを別PCで続ける」からBの候補（オンライン、必要Toolchain、SDK、接続端末、必要Artifactの有無）を表示。`引き継ぐ: Task/Commit/テスト証跡`、`引き継がない: ログイン状態/USB/実行中プロセス`を明示。`移動先での再承認待ち`を正規状態にする。複数PCが同時に「引き継ぎ」を要求した場合はCloud CASの勝者一台だけがActive。PC Aへの復帰も同じHandoff手順を行う。

## 20. LDC-F03 — ローカルプロジェクト自動認識

### 20.1 発見範囲と信頼

プロジェクト検出は**登録済みルート（ワークスペースフォルダ）とユーザーが明示した対象のみ**。ホームディレクトリ全体・ネットワーク共有・全ドライブ・ブラウザ履歴の無断走査は行わない。初回は候補表示の`DISCOVERED_UNBOUND`、ユーザーが選択した後に`PROJECT_BOUND`。**検出 = 操作権限ではない**。

- ファイル一覧: 登録済みroot以下の`Cargo.toml`、`Cargo.lock`、`settings.gradle[.kts]`、`build.gradle[.kts]`、`gradlew`、`.git`、`package.json`、`pyproject.toml`、`pubspec.yaml`、`*.xcodeproj`などを**限定深度/件数/時間/総bytes**で調査。Dotfiles・秘密ファイルの中身をCloudへ自動送信しない。
- Git: **検出段階では未信頼Repoに対して`git status`/`git rev-parse`等を直接起動しない**。Git実行ファイルの絶対path/署名だけではRepoが読ませる`.git/config`や`core.fsmonitor`などの外部コマンド起動を防げない。初回Probeは`git`子プロセスを使わず、Rustの限定的なread-only parserで`.git`の形（worktreeのgitdir fileを含む）、HEAD/refs/packed-refs、object IDと必要なmetadataだけを**boundedに解析**する。malformed/untrusted config、submodules、worktree外へ向くgitdirは`UNTRUSTED_GIT_METADATA`として停止または部分情報に限定。raw remote URLやcredentialをCloudへ自動送信しない。dirty summaryは安全に取得できない場合`UNKNOWN_DIRTY_STATE`と表示。
- **任意コード起動の遮断（P1-03）**: 特に`core.fsmonitor`（`git status`でhook command実行）、`core.hooksPath`、外部filter/`filter.*.process`、Git aliases、`include.path/includeIf`、config-driven program、`GIT_CONFIG_*`/`GIT_*_COMMAND`環境変数、`PATH` hijack、external diff、credential helper、repository内の`git` shadow binaryを敵対入力とする。外部Gitコマンドがどうしても必要な後段では、OSプロセス生成・通信・任意書込みを**強制的に禁止する隔離Worker**内だけで、Git system/global/local configを無効化し、空の明示的config/env、`core.fsmonitor=false`、無効hook/filter/credential helper、固定argv/cwd/timeout/resource limitsで起動する。**OS側で子プロセス起動を確実に遮断できない環境では、この後段Probe自体を提供しない**。`-c core.fsmonitor=false`単独では十分なSandboxではない。ユーザーが明示的に信頼しないRepoでBuild/Hookを実行しない。
- `P1-03`の一次資料: [Git git-config: core.fsmonitor](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsmonitor)、[githooks: fsmonitor-watchman](https://git-scm.com/docs/githooks#_fsmonitor_watchman)。Gitの挙動はGit版で異なるため、対象OS/版の実際の負例試験が完了するまではGitコマンドベースのProbeを解禁しない。
- Toolchain: `rustc/cargo`、JDK/Gradle、Android SDK/ADB、Xcode/xcrun（macOS）、Flutter、Node等の存在・対応版を**独立Probe Adapter**で取得。`--version`や`adb devices`も外部プロセスの実行なので、登録済みバイナリの署名/絶対パス、出力上限、環境変数制限、timeoutを用いる。未信頼ワークスペースの同名実行ファイルをPATH優先で起動しない。
- 接続端末: ADBは権限付きProbeだけでUSB機種/serialを最小表示。OSやADBが拒否した場合は`UNKNOWN_DEVICE_ACCESS`。機器固有ID・ユーザー保存データをCloudへ無条件に転送しない。
- Repo Identity: Projectの正本は既存DEV-01 ProjectRef。PC間の一致候補には認証済みGit provider repo ID、git remote/commit SHA、workspace manifest digestを利用し、絶対パス・同じディレクトリ名・ユーザーの自己申告だけで同一Projectと判定しない。ローカル`bindingId`はDevice毎に別。
- symlink/junction/reparse pointを解決して許可rootを逸脱しない。見つけたProjectのREADME/コメント/Gradle Script等は**非信頼データ**であり、AI Toolの許可やCloudへの送信命令を含んでも実行しない。
- 検出結果は`pathless summary`（Project候補、framework、Git snapshot、SDK適合、available test adapters、observedAt、probeResult、warnings）をTauri UIへ表示。Cloudへの同期はプロジェクトを選んだ時点で`metadata-only`を既定とし、詳細内容送信は別許可。

### 20.2 Capability Discovery → Tool Suggestion

`ProjectCapabilityManifest`（提案）を`projectBindingId, deviceId, sourceRevision, gitHeadSha?, dirtyState, kindCandidates[], selectedKind?, toolchainDigests[], detectedDeviceRefs[], allowedRecipes[], probeVersion, observedAt, freshnessTTL, errors[]`として作成。安全な判定のみで次を提示:

| 検出 | ユーザーに示すTool候補 | 注意 |
|---|---|---|
| `Cargo.toml` + Rust toolchain | cargo check/test/build、Rust desktop debug | build.rsを含むので信頼/隔離確認が必要 |
| `settings.gradle.kts` + Android SDK | Gradle debug build、Android実機起動/ログ | APKインストール先serialとpackageを独立承認 |
| Web `package.json` +許可済みpreview recipe | dev preview、Browser UI debug | npm scriptsは任意コード、元ファイルそのまま実行しない |
| `.git` +認証済みRepo | 差分レビュー、test selection、PR生成 | git remoteに埋まる資格情報はredact |
| Xcode project + macOS署名環境 | iOS Simulator、署名可能なら実機テスト | Windows上でiOSネイティブbuild可能と誤表示しない |
| SDK未導入/版不一致 | 必要SDKの提案・セットアップ案内 | 無断インストール・管理者昇格は禁止 |

ファイルやGit HEAD変化時は**bounded debounceで再Probe**し、stale manifestを`CAPABILITY_STALE`として再確認。WatcherのOSイベント漏れを想定し、build開始時は必ず実ファイル/Toolchainを再照合。`detected`を`permission granted`として使用しない。

## 21. LDC-F04 — 画面を囲んで修正 → 自動デバッグ → 実機テスト

### 21.1 ユーザー体験と安全境界

1. ショートカットまたはTauriの「ここを修正」から**一時的な範囲選択Overlay**を表示。ユーザーがウィンドウ/モニターと領域を選び、「何がおかしいか」をテキスト（任意で声入力）で説明。
2. Rust Agentは選択されたWindow/ROIを識別し、**意図的に選択した範囲の画像と最小化された情報だけを永続化・Cloud送信の候補にする**。OS APIが一時的にWindow全体のFrame Bufferを取得することはあり得るため、「OS内部でも選択範囲以外を一切読まない」とは保証しない。全FrameはLocal processの短命メモリに保持し、ROI抽出後ただちに破棄して他のプロセス・ログ・ディスクへ残さない。初期Capture段階ではCuaの`get_window_state`を呼ぶなら必ず`include_screenshot: false`、`get_desktop_state`は禁止。**UIA treeにもROI外の文字列が含まれ得る**ため初期段階では取得しないか、利用者の別同意後に空間bboxと要素属性を検証してROI内の必要な項目のみ抽出・Local Filteringし、生のTreeをCloudに送らない。Secure desktop、password manager、credential dialog、機密画面、不明感度は全面遮断。オーバーレイ全画面のスキャン・自動常時録画はしない。
3. 画像とユーザーの意図をローカルPreviewで確認。Cloudへ送るデータ（crop、説明、OCR、必要なログ）と保持期間を表示し、**送信前に明示承認**。Previewは**redaction/encoding後の実際の送信予定バイト列**から表示し、`consentPayloadSha256`（画像+OCR+個別添付Manifestのcanonical byte digest、owner/task/privacy revision/送信先/期限）を保存。Upload時に完全一致するDigest・添付件数/サイズ・Policy・Ownerを再検証し、再Capture/再OCR/再Encode等で1 byteでも変化したら承認を失効し新しいPreviewを提示。Stream途中で同意取消しなら中断し、未送信を「送信済み」としない。画像を端末内でしか扱わない「添付しない」選択も可。
4. LDC-F03から対象Project候補を提示。Windowプロセス名/タイトルだけで自動結び付けず、Project Binding・起動プロセス/preview session・source Sha/署名等の根拠を確認。曖昧ならユーザーが選ぶ。
5. Cloud側はDEV-06/DEV-12のReproCaseを作り、元SHAの症状を`REPRODUCED / NOT_REPRODUCED / FLAKY / INCONCLUSIVE`で判定する。スクショだけで「バグ確定」としない。自動診断ではConsole/Network/Trace/Build logs/必要なADB logcatを最小限で取得。
6. 既存のDeveloper Builderが**隔離された作業環境**で修正し、immutable base SHAからpatch/reviewを作成。ローカルPCの任意ディレクトリを勝手に書き換えない。修正patchをローカルへ適用する場合はGit/workspace CASと許可。
7. LDC-F01がBuildを適正配置。Localが適する場合も**未信頼build.rs/Gradle scriptsは隔離**。必要なAPK/バイナリは署名・ハッシュ検証後、ユーザーが許可したADB serial/package（または別OSのテストRunner）だけに配布。
8. Cua/BrowserMCP/ADBで元の再現操作を**権限とデバイスleaseの範囲内**で再実行。テスト前後に対象process/window/package/revisionの一致を確認。GUI操作が許可されない場合は実行不能を明示。
9. 元の失敗recipe・同じinputで修正後のテストを行い、スクショのROI比較（DPI/解像度/色/環境を正規化）、UIA tree/DOMの意味検査、Console/Network/ADB logs、回帰テストを照合。同じ問題が消えても他の必須テストが未実施なら`PARTIAL`。最新source SHAとbuild artifact digestに結合した証拠が揃ったときだけ`FIXED_VERIFIED`。
10. TauriのDebug Studioで、赤枠の元画像・再現手順・変更diff・修正後画像・実機結果・検証できなかった項目を表示。「適用/PR作成/再試行」は既存Git/Tool Gatewayの個別許可を利用。**本番公開/外部送信/アプリ削除/本番データ破壊の許可を自動付与しない**。

### 21.2 画像座標と再現の厳格な扱い

`ScreenRegionIssue`（提案）の必須項目: `issueId, deviceId, userSessionGeneration, displayId, displayTopologyGeneration, windowRef, windowGeneration, rectanglePhysicalPx, captureScale, frameDigest, capturedAt, userDescription, sanitizedCropArtifactRef?, consentPayloadSha256?, sanitizedAccessibilityAnchors[], projectBindingId?, candidateProjectRefs[], baseSha?, privacyPolicyRevision, retention, sourceEvidenceRefs[]`。

- ROIは**物理ピクセル**で保存し、UIのDIP/論理座標との換算を明記。高DPI、マルチモニター、負座標、ウィンドウ移動、再描画後のフレーム再利用を誤認しない。
- 元スクショ上の座標を永続的なクリック対象にしない。再現時はWindow/Element/DOMのsemantic anchorを優先し、新鮮なsnapshotが取れないなら`STALE_TARGET`で停止。画像座標だけで画面外の操作を推定しない。
- Capture許可と操作許可は別。Frame取得→Local crop→PII/Secret分類→mask→encode→同意Preview→Digest固定→送信を明示的に分離する。画面に未許可アプリが入るときはcropのみ/マスクでも秘匿を保証できない場合はcapture全体を拒否。Cua`get_window_state`の既定screenshotは取得しない（`include_screenshot: false`）。UIAのWindow全体のtext/valuesやWindowタイトルも機密候補であり、ROI外のデータはPreview/Artifact/Cloudに残さない。
- **P2-01のメモリ境界**: 全Frame/Raw UIA情報はLocal IPC/クラウド輸送に渡さず短命の明示的Bufferに閉じる。Crash dump/telemetry/ログから除外し、可能な範囲でrelease時にzeroize。OSがフレーム全体へのアクセス自体を提供しない選択Crop APIを使える場合は最小範囲APIを優先。表示Previewと実送信`consentPayloadSha256`の不一致・TOCTOU・Window世代更新時は`CONSENT_STALE`で拒否。
- Debug log/HAR/stacktraceは機密・非信頼入力。Agent Promptに命令として昇格させない。ログ・画面・OCRと実コードの対応はEvidenceとして記録し、根拠不明の推論は`HYPOTHESIS`。

### 21.3 Step DAG・分岐・停止条件

`CAPTURE → CONSENT → BIND_PROJECT → CREATE_REPRO → BASELINE_FAIL → PLAN_FIX → PATCH → REVIEW → BUILD → DEPLOY_TEST_TARGET → REPLAY → VERIFY → REPORT`。各段は有限のTask/Run/Execution; Cloud checkpointを正本とし、画像やログをContextに無限追加しない。

- `BASELINE_FAIL`が未再現なら修正を強行せず`INCONCLUSIVE`またはUser確認へ。再現が必須でない静的UI修正案は`UNVERIFIED_FIX_PROPOSAL`として明確に区別。
- 予算: 最大修正ループ回数、時間、Cloud token/費用、Local CPU/電力、Device操作回数、収集log/画像サイズをTask開始時に提示し、超過時`WAITING_BUDGET / BLOCKED`。
- Android端末が接続されていない場合: emulatorでの代替を**別環境の結果**として示し、物理端末PASSと偽らない。端末Bへ移動するならLDC-F02 handoffを実行し、ADB serialとpackageの再承認が必要。
- 修正前/修正後で異なるApp version、device、orientation、network fixture、permissionsがあるときは`ENV_MISMATCH`で結果を区別。単なる画像差分の減少では成功とせず意味論Oracleを優先。
- Crash、USB抜去、Device disconnect、ユーザーStop、権限失効で既存Runの状態を保守的に処理し、未確定なinstall/入力はEffect Ledger`UNKNOWN`へ。サイレントリトライは禁止。

### 21.4 GUIの提案

`Bug Capture Overlay`（赤枠/範囲/説明/送信内容確認）、`Project Link Panel`（候補Project・SDK/ADB能力）、`Task Execution Map`（Local/Cloud/実行先PC・費用理由）、`Debug Studio`（before/after、再現、修正diff、実機ログ、認可、再開/停止）、`Handoff Picker`（次のPC、未移送状態警告）。Tauri側でプロジェクト選択・送信・実行を承認し、Cloud UIの`Observe-only`をローカルデスクトップ操作の許可に変換しない。

## 22. 追加REQ / TEST / 受入・実装計画

本節は既存LDC-REQ-01..16/LDC-T01..16を拡張。すべて**DESIGNED / NOT RUN**。SC-24〜40等のグローバルシナリオとの正式対応はCHAT_11承認後。

| Requirement | Test | 判定（肯定＋必須否定試験） | 関連正本 |
|---|---|---|---|
| LDC-REQ-17 Placement | LDC-T17 | デバイス必須StepはLocal、推論はCloud。権限・能力・データ所在に違反する自動fallbackは0 | DEV-05 / Tool GW |
| LDC-REQ-18 Local OCR | LDC-T18 | 許可したROI内OCR・座標対応、未許可画面はCloud送信0、OCR未対応で勝手にCloud転送0 | CHAT_09 |
| LDC-REQ-19 Device capability freshness | LDC-T19 | SDK削除/変更・電源/接続失効で古いPlanを拒否、read-only候補のみ再計画 | CHAT_01/09 |
| LDC-REQ-20 Handoff CAS | LDC-T20 | A→B移動時のActive claim 1、Action Grant/Envelope/Journal/StartPermitのdeviceClaimEpoch一致、遅延開始0、既にOSへ配達した操作はUNKNOWN | CHAT_02/03/10 |
| LDC-REQ-21 Workspace fidelity | LDC-T21 | 同一ProjectのSHA/Tree/dirtyを照合し、未保存変更・Secret・ローカルパスを暗黙転送しない | DEV-01 |
| LDC-REQ-22 Cross-device effects | LDC-T22 | Aの送信後ACK不明→UNKNOWN。Bの別toolIntentId/新Runでも同一semanticEffectKeyの新規送信0、WAITING_RECONCILIATION。認証済み旧Receiptは元Intentの履歴へ取込 | Effect Ledger |
| LDC-REQ-23 Safe discovery | LDC-T23 | 未登録root、symlink逸脱、無断全ドライブ探索、build/hook実行0 | CHAT_09 |
| LDC-REQ-24 Project identification | LDC-T24 | 異なるPCの同名folderを別Project、認証済みrepo IDとsource SHAが一致した場合のみ候補結合 | DEV-01 |
| LDC-REQ-25 Probe isolation | LDC-T25 | 悪意ある`.git/config`の`core.fsmonitor`、config include、外部filter/helper、偽`git`/cargo/gradleによる外部process起動・通信・漏洩0。隔離不可時dirty UNKNOWN | CHAT_09 |
| LDC-REQ-26 Targeted capture | LDC-T26 | DPI/マルチモニター/負座標で指定ROI再現、`include_screenshot:false`でUIA初期撮影なし、ROI外tree/Frame送信0、PreviewDigestと送信bytesの不一致拒否 | Browser / QA |
| LDC-REQ-27 BugProject binding | LDC-T27 | 複数Projectが同じ画面に関連する場合は人手選択、誤repoへ修正適用0 | DEV-01 / DEV-09 |
| LDC-REQ-28 Debug loop | LDC-T28 | baseline元SHAでFAIL、修正SHAでPASS + 必須回帰 + 同じ実機/fixtureで初めてFIXED_VERIFIED | DEV-06 / DEV-12 |
| LDC-REQ-29 Device test | LDC-T29 | adb serial/package誤り、USB抜差し、未接続、別PCでの端末不足をPASSにしない | DEV-05 / LDC-T11 |
| LDC-REQ-30 Unified Stop/Budget | LDC-T30 | Cloud/Local/別PCでUser Stop・Budget・Device Revokeを尊重し新規副作用0、取消後でも真正な旧Receiptを旧Effectへ記録 | CHAT_02/03/09 |
| LDC-REQ-31 Privacy/evidence | LDC-T31 | ROI/ログ/diagnostic/ArtifactにSecret残存0、Cloud送信前のconsentPayloadSha256固定、同意取消/owner切替でbyte不一致の転送0 | CHAT_09/11 |
| LDC-REQ-32 Offline semantics | LDC-T32 | offline中のLocal表示は可、Cloud推論・遠隔UI操作を勝手に実行しない。Owner切替で古いconnection/Grant/JournalへBがアクセス0 | CHAT_02/09 |

**新しいE2Eシナリオ（ローカル提案・未採番）**

- LDC-SC06: Windows PC AのCargo repoを発見、source SHA検出、Cloudレビュー、隔離されたLocal Build、検証結果Artifactを報告。
- LDC-SC07: PC AでTask開始 → PC Bへ安全に移動 → 異なるローカル絶対パスでも同一Projectを確認 → Resume。Aを強制オフラインにして旧write拒否とUNKNOWN検証。
- LDC-SC08: 高DPIマルチモニターでUI領域を囲む → Project結合 → 元SHAで再現 → 修正/ビルド → ユーザー指定Android実機へinstall → 同じrecipe +回帰テスト → before/afterの根拠付き完了報告。
- LDC-SC09: Android端末がAにしかない場合にBへの移動を提案。Bでは`BLOCKED_TARGET_CAPABILITY`、端末がないまま成功報告ゼロ。
- LDC-SC10: 誤ってCredential画面を囲む、Source内のPrompt Injection、送信同意取消、悪意あるBuild Script、古いRunの再配送を同時注入。未承認送信/書込みゼロ。

**実装の独立スライス（元のLDC-0..6に追加）**

1. **LDC-2A: Scoped Project Discovery** — Rust read-only Probe + Tauri候補一覧。Node/SDK/ADB検出は許可後。Read-onlyで先行可能。
2. **LDC-2B: Placement Planner** — Cloud Python側の能力照合とLocal/OCR PoC。実行先候補を示すだけでTool Grant発行しない。
3. **LDC-3A: User Region Capture** — Windows選択Overlayと高DPI座標・ローカル赤枠/Mask、送信同意ダイアログ。初めは画像のCloud送信なし。
4. **LDC-4A: Cross-device Handoff** — Cloud Task/Run HEAD/Device Lease/Artifactの連携をLDC-4完了後に実装。UI操作中のlive transferは禁止。
5. **LDC-5A: Debug-to-Device Loop** — DEV-06/12の再現とLDC-F04の範囲選択、Build/ADB Adapterを縦につなぎ、元SHA失敗→新SHA成功を検証。実AndroidとChromeの各権限は個別に導入。
6. **LDC-6A: Regression and rollout** — 他PC/OS、高DPI、Task移動競合、USB切断、メモリ圧、偽Toolchain、機密画面の故障注入を実測。段階的にenable。

**新しい正本/API/DBの所有者**: CHAT_01が`CapabilitySnapshot/PlacementPlan/DeviceClaim/ScreenRegionIssue`型、CHAT_02/03がPlacement admission・handoff fence・Effect Ledger、CHAT_09がLocalPolicy・画面/ファイル送信・elevation禁止、CHAT_10がDevice Claim CAS・Artifact retention、CHAT_08がTauri/Cloud UI、CHAT_05/16がBuild/ADB Recipe、CHAT_11がTest/SCの正本を審査する。これらは本PR内で**契約案に留め、未承認Schema/テーブル/エンドポイントを実装済みと主張しない**。

## 23. PR #83 監査指摘の対処追跡（2026-11-10）

> 8件とも**設計修正のみ**。ここでいう「修正」は静的な仕様更新を意味し、Rust Client/Device Gateway/OSテストでの機能実証ではない。正式なOwner合意、schema/protocol lock、OS実機受入は引き続き未実施。

| 指摘 | 仕様上の解消先 | 必須の失敗注入・検査 | 状態 |
|---|---|---|---|
| P1-01 | `5, `9, `10, `19.2: deviceClaimEpoch + START_PERMIT | LDC-T04/20: Aで旧Claim Permit受信→CAS移動→OS未配送の旧開始拒否・OS配達済みはUNKNOWN | SPEC_UPDATED / NOT RUN |
| P1-02 | `9.1, `19.2: semanticEffectKey + WAITING_RECONCILIATION | LDC-T05/22: Aの送信UNKNOWN後、Bが新Intent IDで同一送信を試みても拒否 | SPEC_UPDATED / NOT RUN |
| P1-03 | `20.1: Rust read-only Git Probe、隔離不可ならGitコマンド不使用 | LDC-T25: core.fsmonitor + Git include/filter helperでプロセス0 | SPEC_UPDATED / NOT RUN |
| P1-04 | `6.1, `7: Cua Desktop positive allowlist + Browser runtime独立 | LDC-T08/09: Browser Origin拒否後のDesktop click/type/hotkey迂回を拒否 | SPEC_UPDATED / NOT RUN |
| P1-05 | `9, `19.2: original-intent reconcile とnew-dispatch admission分離 | LDC-T22/30: Cancel後の真正Receiptは元Ledgerに記録、新Runは完了しない | SPEC_UPDATED / NOT RUN |
| P2-01 | `21.1/21.2: Screenshot flag、ROI UIA最小化、送信bytes digest | LDC-T12/26/31: ROI外転送0、Preview差替え拒否 | SPEC_UPDATED / NOT RUN |
| P2-02 | COMP-02 + spec-provenance manifest + validate_spec_migration | LDC静的監査: REQ/T 01..32の連続性と1対1追跡、Manifest blob整合をCI | SPEC_UPDATED / CI確認待ち |
| P2-03 | `4.1: Owner/Tenant/OS User namespace、Account Switch Barrier | LDC-T01/30/32: A→B切替でAのTunnel/Grant/Journals混入0 | SPEC_UPDATED / NOT RUN |

追加の否定試験: `LDC-NT01` stale Claim + delayed Permit、`LDC-NT02` unknown semantic duplicate、`LDC-NT03` malicious git config/fsmonitor、`LDC-NT04` Cua/browser boundary crossing、`LDC-NT05` stale-run authenticated Receipt reconciliation、`LDC-NT06` ROI full-frame/UIA/Preview bytes mismatch、`LDC-NT07` account switch namespace separation。すべて**設計のみ・未実行**。
