# Browser Live Observation — サーバーブラウザのリアルタイム閲覧

> Companion specification to `04-03 Browser Session Manager`, `08-01 REST API`, `08-05 Approval & Browser Takeover UI`. Status: **Draft / PROVISIONAL** (2026-11-07). 本リポジトリは仕様書のみで、実装・テスト・性能測定は未実施。既存88モジュールの外に正本を増やさず、共同設計の補足とする。Scenario: SC-06/SC-07/SC-08/SC-09/SC-18/**SC-23**。

## 1. 利用者ができること

WebダッシュボードのTask詳細から「ブラウザをライブ表示」を開く。AIがサーバー上の隔離Browser Workerで実行中のページを、スクロール・クリック・移動に応じて継続更新し、閲覧できる。複数タブがあれば対象Taskの許可されたタブに切り替えられる。Browserを使用していないTaskは「ブラウザ未使用」と表示する。

**Observe（見る）とHuman Takeover（操作）は権限・状態とも完全に別。** Observeを開始してもAgentは停止しない。Observe画面からのクリック、キーボード、clipboard、file upload、DevTools、JavaScript、CDP操作は禁止。「操作を引き継ぐ」ボタンをユーザーが明示して初めて、既存08-05の本人確認・短命Handoff ticket・AGENT/HUMAN排他leaseを使う。Takeoverが外部購入/投稿/予約の独立Approvalを兼ねることはない。

UIはライブ映像＋安全なhost表示、閲覧対象タブ、最終フレーム時刻、`CONNECTING / LIVE / STALE / PRIVACY_BLOCKED / DISCONNECTED / ENDED`、閲覧停止、再接続、拡大/全画面。`LIVE`でもTask成功を示すわけではない。表示画面は一時的な観測でありEvidenceの真正原本ではない。画面が止まったときは最後のframeを消し、LIVEと誤表示しない。小画面・キーボード・スクリーンリーダーに対応。

## 2. 分離・データフロー

`Browser Worker (隔離された唯一の BrowserContext/Page/CDP owner)
  └─ Worker内部の read-only frame capture (pageGeneration fenced)
      └─ 機密画面判定 / redaction-or-full-block (配信前)
          └─ Trusted Browser Broker (揮発・bounded queue)
              └─ Authenticated Observation Relay (one-way WebRTC video / binary WSS fallback)
                  └─ 同一オリジンのWeb管理画面`

04-03のBrowser WorkerだけがBrowserContext/Page/CDPを所有する。Observerのために追加CDP owner、VNC、public remote debugging portを作らない。Agent、Untrusted Runtime Cell、LLM、外部Webクライアントへraw CDP URL、storageState、Cookie、Socket、任意Action APIを返さない。

Observationの認可scopeは`principal(user/tenant) + taskId + sessionId + sessionGeneration + pageRef + subscriptionId`。同Tenant別ユーザーでも元Taskの所有権がなければ閲覧不可。サーバー側で照合し、クライアントが渡すownerIdやtenantIdは信頼しない。閲覧開始はBrowserのAGENT leaseEpochを変えず、画面取得の重さが操作に影響するときはframeを破棄してAgentを優先する。

各frameは現行ownerとsubscription権限、BrowserSession generation、Page generation/selected Tab、revoke epoch、frame sequenceを検証し、旧Worker・旧Tab・旧権限・遅延Queue由来のframeを廃棄。Takeover/Task Cancel/Logout/権限失効/Worker killでsubscriptionを失効させ、新しいframe送信を止める。既に視聴端末が受信した情報を取り消せるとは言わない。

## 3. メディア配信方式

**優先案:** 一方向のWebRTC映像（音声・双方向DataChannel・入力を禁止）。既存HTTPS入口・認証済みSessionの下でSignalingを行い、必要なら期限付きTURNを使用する。LAN/NAT/TURN条件によって接続性が変わるので外出先から必ず見られるとは保証しない。不要な外部STUN/TURNへの接続は禁止。動画はDTLS/SRTPで暗号化。

**Fallback:** same-origin `wss`でbinary JPEG/WebP frameを配送するread-only stream。Origin/Host/CSRF相当・認証・session stateを初回だけでなく配信中に検査。安易な非TLS/VNC/CDP直結は不可。動画frameを既存のTask SSEに混ぜない（SSEはdurable進捗・metadata専用）。

Browser Worker内部のChromium capture API / Playwright screenshot / virtual display encoderの比較は実装PoCで判断。Workerの唯一所有権を崩さず、特にCDP screencastの互換性・切断・性能を確認する。MVPは**映像のみ、録画・音声なし**。保存を希望する拡張は別途同意/retention/ACL設計が必要。

Frame queueは有界（初期案2 frame、slow viewerでは古いframeをdrop）、視聴者なし・非表示タブ・閉じた画面ではcapture停止。接続断でAgentのRunをCancelしない。再接続は最新Session/Tab generationで新規subscriptionを認可し、古い画像を再表示しない。

## 4. API案（08-01/04-03/09が共同レビュー後固定）

| Method / endpoint | Scope / semantics |
|---|---|
| `GET /api/v1/browser-sessions/{sessionId}` | 既存read DTOへ`observationAvailable`と公開可能な`observationBlockReason`を提案。credential/CDPなし |
| `POST /api/v1/browser-sessions/{sessionId}/observations` | `browser:observe`権限・owner/task/session/page照合、CSRF/Originと`Idempotency-Key`、短命subscription作成。opaque ID・期限・対応transportのみ応答 |
| `GET /api/v1/browser-sessions/{sessionId}/observations/{id}` | subscription状態、選択Tab、最終frame時刻、expiresAt。認可再評価 |
| `POST /api/v1/browser-sessions/{sessionId}/observations/{id}/offer` | 有界SDP/ICE、同一session権限に束縛したWebRTC Signaling。別Task/再利用は拒否 |
| `wss://same-origin/api/v1/browser-sessions/{sessionId}/observations/{id}/frames` | same-originの認証済みbinary fallback。URL queryにtoken禁止 |
| `POST /api/v1/browser-sessions/{sessionId}/observations/{id}/close` | `If-Match` + `Idempotency-Key`で当該閲覧のみ終了。BrowserSession/Runは終了しない |

Session IDや観測IDだけでは権限にならない。ObserverにはHuman Takeover ticketもApprovalも渡さない。全HTTP+relayはTLS、`Cache-Control: private, no-store`、厳格CORS/Origin、失効検査、rate/concurrency/bandwidth上限。HTTP 401 unauth、404 other owner/not visible、409 generation/page/mode conflict、410 subscription expiry、429 capacity、503 relay unavailableを分ける。詳細DTOとmedia protocolはPROVISIONAL。

## 5. 状態と障害

Subscription: `REQUESTED → CONNECTING → LIVE → {STALE, PRIVACY_BLOCKED, DISCONNECTED} → ENDED`。機密表示を離れて再開する場合にもページ世代/権限/感度を再判定する。ENDEDを古いgrantから再有効化しない。

- Page navigate/Tab switch: 旧Page generation frameをdropし、最新の画面を安全確認後に表示。
- Worker kill/restart: 旧session generationは失効。再開には新たなviewer認可が必要。別Task初期タブへのfallthroughを禁止。
- Takeover開始: 旧Observe streamを切って、別の短命・排他操作relayへ切替。Agentが止まるのはTakeover時のみ。
- Relay/Capture失敗: 画面`DISCONNECTED`、TaskのBrowser actionは継続。
- Logout/revoke/Task cancel/Tab closed: buffered frameを破棄し、以後送信を停止。ブラウザ画面に旧frameが居残らない。

## 6. 安全・プライバシー

**画素は秘密になり得る。** password、二要素コード、カード番号、個人情報、canvas、動画、iframeにはDOMのinput maskが効く保証がない。**機密ページまたは感度不明ページではframe全体をブロック**して、最後の画像もUIから消す。安全と証明できないmaskを唯一の防御として採用しない。必要なログインは既存のHuman Handoffで本人が行う。

閲覧はユーザーが画面を開いた時だけ起動し、`observe_enabled=false`を初期値とする。`recording_enabled=false`固定。Raw frameをDB/Artifact/Evidence/Task SSE/log/trace/crash dump/backup/Analytics/CDNへ保存・転送しない。Bufferは揮発かつ上限付きで、View停止/失効時に消去。監査ログは`actorId, tenant/task/sessionRef, observationId, startedAt, stoppedAt, blockReason, bytes, frameCount`等の安全なmetadataに限定する。UIは外部サイトをiframeで直接公開しない。ネットワークやブラウザconsoleへframeを流さない。

画面を見せる相手の権限、端末の信頼性、セッションのfresh authにも配慮する。既受信frameのスクリーンショットや視聴者端末の保存はサーバーだけでは完全に防げない。これをセキュリティ仕様に明記する。

## 7. MVP・性能・導入フェーズ

MVP候補は1 BrowserSessionにつき1視聴者（設定で有限の上限）、720p、WebRTC 5〜15fps、WSS fallback 1〜5fps、LAN p95 2秒以下を**開発目標**とする。未測定でありSLAではない。CPU/RAM、frame bandwidth、Agent Browser action p95への影響、LAN/WAN差、同時Sessionを負荷試験して上限を決める。視聴OFF時とON時の費用・性能を比較し、Agent処理を優先。

1. 04-03 Browser Worker内のread-only Captureとgeneration fence、機密判定、buffer制限。
2. 09/10 ブラウザ隔離を崩さないrelay、AuthN/AuthZ、revoke、same-origin TLS/WebRTC/WS経路。
3. 08-01/08-05/08-04 Subscription API、ライブUI、Handoffと別権限の明確化。
4. 11 QA/実機PoCで画面漏えい・競合・Worker kill・ネットワーク不良・性能を検証する。仕様通過だけでVerifiedと言わない。

## 8. Requirements / tests / scenarios（BLO-001〜017、全て設計・NOT RUN）

| REQ | Level | Oracle | TEST | SC |
|---|---|---|---|---|
| BLO-001 | MUST | 別Tenant/同Tenant別ownerへframe 0 byte | BLO-T01 | 06/08/23 |
| BLO-002 | MUST | Observe中もAgent action継続、AGENT lease不変 | BLO-T02 | 06/07/23 |
| BLO-003 | MUST | CDP/cookie/token/storageState/DevToolsをviewerに渡さない | BLO-T03 | 08/18/23 |
| BLO-004 | MUST | Observe→Takeover/Approval/inputへ無権限昇格0 | BLO-T04 | 06/08/23 |
| BLO-005 | MUST | login/payment/secret/不明画面のframe漏えい0、旧frame残留0 | BLO-T05 | 08/23 |
| BLO-006 | MUST | revoke/cancel/worker restart/takeover後の新規旧世代frame 0 | BLO-T06 | 07/08/23 |
| BLO-007 | MUST | relay障害でRun cancel 0、再接続時他Task/旧frame 0 | BLO-T07 | 07/23 |
| BLO-008 | MUST | raw frameの永続化/Task SSE/Backup混入0 | BLO-T08 | 08/23 |
| BLO-009 | MUST | 同時数/帯域/frame queue有界、視聴停止でproducer停止 | BLO-T09 | 09/23 |
| BLO-010 | MUST | LIVE/STALE/PRIVACY_BLOCKED/ENDEDが正しい | BLO-T10 | 06/23 |
| BLO-011 | MUST | signaling/WS/ICE/Auth/Origin/CSRF/revokeの保護 | BLO-T11 | 08/23 |
| BLO-012 | SHOULD | 目標fps/遅延/資源影響を計測し未達なら開示 | BLO-T12 | 09/23 |
| BLO-013 | MUST (Viewer enabled) | HUMAN/HANDOFF_PENDING/RECLAIMING中のCreate/Reconnect/Offer/Tab/Sendは0。再引継後も新規Subscriptionだけ | BLO-T13 | 06/07/23 |
| BLO-014 | MUST (Viewer enabled) | Revoke送信Barrier後の新規Frame送出0、Track/Peer/Queue停止とClient wipeを別判定 | BLO-T14 | 07/08/23 |
| BLO-015 | MUST (Viewer enabled) | Allowlist/DOM/iframe/canvas/SPA感度不明時は全画面ブロック。Capture前後で再検証 | BLO-T15 | 08/23 |
| BLO-016 | MUST (Viewer enabled) | PITR後の旧restoreIncarnation/旧MediaGrantからの送出0 | BLO-T16 | 07/08/23 |
| BLO-017 | MUST (Viewer disabled) | observe_enabled=falseでMedia API/Socket非公開、通常Agent Runは妨害しない | BLO-OFF-T01〜03 | 09/23 |

Fixture: 複数owner/tenant、popup+tab switch、login/payment+iframe/canvas、late frames、Worker kill、Cancel、revoke、Handoff race、slow viewer、invalid Origin/ICE、mobile/hidden tab、network loss。BLO-T01〜11は **Live Viewer を有効にするリリースの必須ゲート**。無効の通常リリースではBLO-OFF-T01〜03だけを実行し、全media route/socket/offerが閉じていることを検証する。安全性を性能目標達成で相殺できない。

## 9. 関連仕様と未決

- [BrowserSession](../04-web-browser/browser-session-manager.md), [Browser Action](../04-web-browser/browser-action-engine.md), [REST](rest-api.md), [SSE](realtime-events.md), [Takeover](approval-and-browser-takeover-ui.md), [Dashboard](task-and-watch-dashboard.md)。
- [Auth](../09-security/authentication-and-user-isolation.md), [Privacy](../09-security/privacy-audit-and-retention.md), [VM isolation](../09-security/runtime-cell-isolation.md), [Docker](../10-infrastructure/docker-deployment.md), [QA](../11-qa/end-to-end-acceptance.md)。
- 未決: capture方式、秘密判定の実効性、WebRTC/TURNの接続方式、実測fps/レイテンシ、mediaのOpenAPI契約。ADR-029は`PROPOSED`。`HITL-014`の旧EXPERIMENT案を、プライバシー防御を前提とした読み取り専用`MUST`へ移す提案。安全に実装できなければ公開をブロックする。


## 10. PR #71 再監査修正：閲覧権限・送信境界・感度判定の正本案

> この節は第2〜8節の未確定部分を具体化する規範であり、矛盾する古い記述より優先する。**仕様の合意提案であって実装・実機検証済みではない**。BLO-T01〜16、BLO-OFF-T01〜03、QA-E2E-T23P/N/Fに対応。

### 10.1 TakeoverとObserveの排他Admission（P1-01）

BrowserSessionの正本が`mode=AGENT`かつ`takeoverPhase=NONE`、Taskがactive、Viewerが`browser:observe`を現在持ち、対象`BrowserSession/Page`の世代が一致する場合に限り、**新規Create・既存Subscriptionの再接続・WebRTC Offer/ICE・WS Upgrade・Tab切替・各frame送出**を許可する。`mode=HUMAN`、`HANDOFF_REQUESTED`/`HANDOFF_PENDING`、Handoff ticket発行済み、Release未確定、`RECLAIMING`、Task WAITING_INPUTで機密操作を待機中、Browser Worker unknown/stale、復旧QUARANTINEDの場合は即拒否。閲覧はTakeover前の未確定時間帯にも拒否する。Cookie/IDの所持は許可にならない。

Handoff intentを保存する短いCAS transactionで`observeAdmissionEpoch`を単調増加し`observeBlocked=true`を先に記録する。新規サブスクリプション受付は、この正本を同じ整合境界で読む。既存Relayへ`freeze + drain + revoke`を送る。Handoffの排他操作ticketは**先行Observer失効の確定を検証後**にだけ有効化する。失効に失敗したらHandoffは`WAITING_SAFE_HANDOFF`のまま失敗停止し、既存Mediaを流し続けて操作へ進むことを禁止する。

Release要求のACK≠release完了。`HUMAN→RECLAIMING→AGENT`がCASとBrowser Workerの最新Owner/Lease・Page/Session generationの再観測で完了するまでObserveを許可しない。Release後も古いsubscriptionは復活させず、**新しいAuth・MediaSession・最新世代**で作成する。Handoff中のObserve試行は404（対象不可視）または409（本人の競合）で拒否し、秘密状態を詳述しない。

### 10.2 実送信と権限失効の線形化境界（P1-02）

Capture開始/終了時、Encode投入前、Encoder結果取り出し時、Relay送信待ち行列投入時、そして**最後の送信境界で** current `(tenant,owner,task,sessionGeneration,pageGeneration,restoreIncarnation,observationEpoch,mediaGeneration,authorizationEpoch,mode)` を評価する。Capture/Encode時の許可は送信許可を意味しない。`revocationCounter`が変化したらqueued/in-flight encode結果は全廃棄し、relayに古いbufferを残さない。実送信Gateをフレーム単位で再検査できないMedia SDKなら、短命の媒体セッションを終了し、世代ごとの独立Track/PeerConnectionで再作成する方式以外は承認しない。

**サーバー側の失効確定点**は、Auth/Owner/Takeover取消 intentが安全に記録され、該当MediaSessionの送信Gateがfenceされ、Track.stop・RTCRtpSender停止/置換不可化・PeerConnection.close またはWS close、Encoder/Relay queueの廃棄が確認された時点（`REVOKED_BARRIER_COMMITTED`）とする。失効の受付ACKをこのBarrier完了と混同しない。MediaSession停止ACKの受信なし・Relay応答不明・他ノード障害では`REVOKE_PENDING / BLOCKED_UNSAFE`、新規配信/Takeoverを停止する。遅延PacketがOS/NIC/Internetで既に送出されている場合、取消確定前に送出した内容の回収は不可能であり、**「失効確定後に新たに送信境界から送出したFrame 0件」**と**「クライアントがいつ旧画像を消すか」**を別Oracleとする。

Clientは`REVOKED`制御通知またはローカルAuth失効検知で`video.srcObject=null`/canvas clear/URL object破棄を即行う。通知が届かなくても`signed frame freshness heartbeat`期限切れで必ず`STALE`に遷移して黒画面化する。Server revokeの最中に既存frameがClientへ遅延到着することはあり得るため、replay受容を`mediaGeneration`とfreshnessによって拒否する。クライアントを攻撃者が改変したケースや端末のスクリーンショット削除は保証しない。実機証拠で停止遅延の上限を決める。

### 10.3 Capture許可の明示条件とPrivacy Barrier（P1-03）

**デフォルトは配信禁止。** `SensitivityPermit`は`(tenant,owner,task,session,pageGeneration,frameTreeGeneration,route/origin allowlist revision, sensitivityPolicyEpoch, restoreIncarnation)`に結び、Trusted Browser Worker内の検証器だけが発行する。許可条件は以下の**全て**：
1. Ownerが当該Taskで閲覧を明示開始し、サイトoriginとrouteが組織・利用者のread-only allowlistに該当。認証/決済/OTP/個人情報等の機密用途のページはdeny。Allowlist未登録はdeny。初期MVPは公開された合成/デモ画面のみ許可し、一般Webページを無条件に「安全」とは判定しない。
2. 全frame tree（cross-origin iframe, shadow DOM, popup, nested content）に有効な感度評価がある。評価不能なiframeやcanvas/video/WebGL/DRM、file input/credential field、password manager overlay、スクリーン共有など保護できない内容が存在する場合は原則全面`PRIVACY_BLOCKED`。部分maskを根拠に許可しない。
3. WorkerのNavigation commit/redirect/History API/SPA route変化、DOM Mutation、focus/form表示、iframe追加・遷移、canvas/video更新、Tab選択、viewport変更・visibility変更を、配信前に同期可能な**Privacy Barrier**へ結合する。感度観測とcaptureの間に未検証の描画が入る場合はframeを遮断する。非同期DOM監視が間に合わない環境や不可視の描画更新を検知できない環境は許可不可。許可はframeごとに消費される短命な証明であり、過去のallowを永続cacheしない。
4. `captureBefore`のpolicy/DOM/route/pageGenerationと`captureAfter`の再観測が一致し、エンコード直前と送出直前にも最新Permitの適用対象を確認。変化時はcapture結果を破棄。検証器のcrash/timeout/unknownなら`PRIVACY_BLOCKED`とし、最後のClient frameも消去。

**重要な実装上の制限:** 任意の外部サイトで秘密を完全に検出する一般解を保証しない。安全判定不能のサイトはライブ画面を提供できない（機能は限定サイトだけ動く）。通常サイト向け画面を広げる前に、同一originの明示的に安全なsnapshot/projectionやユーザー別の閲覧許可設計などを別途審査する。いかなる場合も判定失敗時にraw videoを送信しない。

### 10.4 PITR・復旧世代の隔離（P1-10、PR #72 連携）

`restoreIncarnation`はDB PITRで巻き戻らないTrusted Operator管理の独立単調世代で、**PR #72 の復旧フェンス提案と同じ概念**を参照する。観測Subscription、MediaSession、WebRTC/WS送信Grant、Tab選択、Producer IPCすべてに`clusterId + restoreIncarnation`を結び、trusted relayの実送信境界で独立Storeの現世代と一致検証する。PITR開始前に既存Grant/PeerConnection/Queueを遮断し`QUARANTINED`へ遷移。復旧後は過去のSubscriptionや観測historyを再使用せず、新しいAuthとPage/Session世代で作成する。復旧世代Storeに到達できなければ fail closed。単なるDB `leaseEpoch`比較・MemoryCacheだけでは旧Workerを拒否できない。PR #72とはManifest更新を競合解消して再検証するまで両PR同時Lockedとしない。

### 10.5 媒体の状態遷移・安全な停止・資源制御（P2）

| From | Trigger | To | 送信条件 |
|---|---|---|---|
| REQUESTED | Auth/Mode/Privacy all approved | CONNECTING | signalingだけ、frame不可 |
| CONNECTING | SDP/Media generation/last-send gate valid | LIVE | safe frameのみ |
| LIVE | navigation, change, no fresh permit, late frame | PRIVACY_BLOCKED / STALE | frame停止+旧画像消去 |
| LIVE / CONNECTING | relay/ICE/WS失敗 | DISCONNECTED | frame停止・Agentは継続 |
| DISCONNECTED | user再接続、新しいAuth+mediaGeneration | CONNECTING | 旧Peer/Queue再利用不可 |
| PRIVACY_BLOCKED | 新route+frame treeで新Permit、mode AGENT | CONNECTING | 再審査後のみ |
| any nonterminal | revoke/Takeover/Logout/Cancel/PITR | REVOKE_PENDING | 送信Gate閉鎖、queue drain |
| REVOKE_PENDING | media停止Barrier完了 | ENDED | 新規送信0、reopen不可 |
| any | auth/relay fail unsafe | BLOCKED_UNSAFE | 永続deny、Operator介入 |

`ENDED`/`BLOCKED_UNSAFE`の既存IDへの再開禁止。障害中の媒体だけが失敗し、Agentは本来のBrowser leaseを保つ（Takeoverのための停止要求は除く）。Producer・Encoder・Relay・Track・Peer・Clientの停止は別々にメトリクス/成功状態を記録する。視聴OFF/Viewerゼロの時はCapture/Encodeを停止。同時subscription・媒体フレームサイズ/FPS/帯域/CPU/メモリ/キューに有限上限を設け、上限超過で古いframeを捨てBrowser操作を優先する。各制御APIの権限/Media stateはUIキャッシュよりサーバー正本優先。

### 10.6 Raw Frameの非保存（運用条件）

Capture/Encode/RelayのbufferはRAM限定、無関係なCrash/Core Dump/Heap dump/diagnostic attachmentへの映像流入を防ぐ。Core dumpは無効、診断時のメモリsnapshotも本番では原則無効。Swap・host debug/tracing、reverse proxy request/response body logging、ブラウザHTTP Cache、Analytics、CDN cache、screen recordingは無効であることを運用テストで検証。映像の書出しを含むDebug機能は本番で有効化不可。安全なAuditはframe内容を保持せず世代/停止理由/Byte量と匿名化されたtrace IDだけに限定する。

### 10.7 追加の設計テスト

- `BLO-T13`: Handoff requested→Human→reclaim前→Release後のCreate/Reconnect/Offer/WS/Tab/Sendを競合実行。HUMAN等では新規frame 0、Release後新Mediaでのみ再開。
- `BLO-T14`: Encoder未送信queue中revoke・Worker crash・媒体Packet遅延をinject。Barrier committed以降の新規Relay send=0、旧Track/Peer closed、Client frameクリアは別観測。
- `BLO-T15`: SPA、cross-origin iframe、canvas、password form、login overlay、DOM変化/検出器故障をinject。unknown=全面`PRIVACY_BLOCKED`、旧frame消去。
- `BLO-T16`: PITRでDB Epoch巻戻し＋旧Worker/旧Peer再接続。旧`restoreIncarnation`の送信0、trusted Store未到達で0。
- `BLO-OFF-T01〜03`: `observe_enabled=false`で観測API全拒否、media/ICE/WS外部公開なし、Observe無効でも通常Task/Takeoverの受入シナリオを妨げない。

**Release gates:** Live Viewer有効版のみ`BLO-T01〜11 + T13〜16 + QA-E2E-T23P/N/F`をHard Gateにする。Live Viewer無効版は`BLO-OFF-T01〜03`と既存Agent基盤試験を必須にする。未実装の本リポジトリはどちらも実機PASSを主張しない。
