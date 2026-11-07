# Browser Live Observation — サーバーブラウザのリアルタイム閲覧

> Companion specification to `04-03 Browser Session Manager`, `08-01 REST API`, `08-05 Approval & Browser Takeover UI`. Status: **Draft / PROVISIONAL** (2026-11-07). 本リポジトリは仕様書のみで、実装・テスト・性能測定は未実施。既存88モジュールの外に正本を増やさず、共同設計の補足とする。Scenario: SC-06/07/08/09/18/23。

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

## 8. Requirements / tests / scenarios（全て設計・NOT RUN）

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

Fixture: 複数owner/tenant、popup+tab switch、login/payment+iframe/canvas、late frames、Worker kill、Cancel、revoke、Handoff race、slow viewer、invalid Origin/ICE、mobile/hidden tab、network loss。BLO-T01〜11の失敗/未実施はrelease blocker。安全性を性能目標達成で相殺できない。

## 9. 関連仕様と未決

- [BrowserSession](../04-web-browser/browser-session-manager.md), [Browser Action](../04-web-browser/browser-action-engine.md), [REST](rest-api.md), [SSE](realtime-events.md), [Takeover](approval-and-browser-takeover-ui.md), [Dashboard](task-and-watch-dashboard.md)。
- [Auth](../09-security/authentication-and-user-isolation.md), [Privacy](../09-security/privacy-audit-and-retention.md), [VM isolation](../09-security/runtime-cell-isolation.md), [Docker](../10-infrastructure/docker-deployment.md), [QA](../11-qa/end-to-end-acceptance.md)。
- 未決: capture方式、秘密判定の実効性、WebRTC/TURNの接続方式、実測fps/レイテンシ、mediaのOpenAPI契約。ADR-029は`PROPOSED`。`HITL-014`の旧EXPERIMENT案を、プライバシー防御を前提とした読み取り専用`MUST`へ移す提案。安全に実装できなければ公開をブロックする。
