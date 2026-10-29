# Notification Outbox — Detailed Implementation Specification

> Owner: CHAT_07 | Path docs/07-automation/notification-outbox.md | Status: Draft / [PROVISIONAL] | Revision 0.2 | Research date 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-02,07,08,09,11,16
>
> 仕様リポジトリであり送信Workerは実装されていない。すべてのNOT-Tテストは**設計・未実行**。通知配送のexactly-onceはネットワーク越しに保証できない。

## 0. 結論・採用方式

ユーザーが明示的に同意した宛先に対してのみ、**Committed Trigger / Result → Immutable Notification Intent → Transactional Outbox → Channel Delivery Adapter → Delivery Attempt Ledger**を実装する。Outboxを入れたことはTelegram/メールが届いたことではない。「受付」「配送プロバイダ受理」「ユーザー既読」を別の事実にする。送信中にWorkerをkillして返答がなくなった場合、実際に受信済みか不明なので、transportが有効なidempotency key/照会APIを提供しない限り同文面を自動で再送しない。重複より通知欠落のリスクを優先するかはユーザーの重要度政策で異なり、システムは**UNKNOWNと明示**する。

通知OutboxはCHAT_07が**業務意図と送達ポリシー**を所有し、物理Outbox/Queue/LeaseのDDLとrelayはCHAT_10が正本。Telegram channel adapterとAPIはCHAT_08、メール/Connector資格はCHAT_05/09。CHAT_13-03の通知ランキング/ダイジェストは**このOutboxへ意図を提案**するだけで独立senderを作らない。

**一意性**: same (tenantId, ownerUserId, semanticEventKey, destinationBindingId, deliveryPurpose) → logical notification intent最大1件。同じイベントが再検出されても新しいoutbox行を作らない。formatやpolicyの改訂は送信済みイベントの再送権限にならない。異なるtriggerKeyであり実質同内容の連続イベントはquiet hours/episode/hysteresisと連携して抑制するが、必要な強制通知をランク付けで捨てない。

## 1. Scope/非責務と受入

責務: 通知同意・宛先binding参照・user opt-out、テンプレートレンダリングの出典と時刻、配送前認可の再照合、retry/unknown、送達receipt、重複event key、quiet hoursを受け入れる契約、送信成功と既読の区別、通知履歴の監査と削除境界。非責務: Trigger判定07-03、Watch状態07-02、Scheduler時刻07-01、ランキング/バッチのpolicy CHAT_13、Telegram入出力transport CHAT_08、物理DB CHAT_10、全体Event Envelope CHAT_01。

SC-02（買取価格+URL+観測日時）、SC-07（outbox lost ACKとreplay）、SC-08（secret/tenant/宛先誤送）、SC-09（費用・遅延）、SC-11（X event起点）、SC-16（quiet hour/priority/feedback）を網羅。購入/予約/投稿の無承認通知リンクを自動実行ボタンと混同しない。

## 2. OSSコード・テスト横断調査（2026-10-30）

| OSS/pin/license | 読んだ関数・テスト | 採用/不採用 |
|---|---|---|
| **Hermes** fcc26fbd9f1641755e49793d7ed608f3b7214c3d, MIT [S1,S2] | cron/delivery_queue.py: enqueue/claim_next/_finish/recover_abandoned、tests/cron/test_cron_delivery_redaction.py: platform_send/session_mirror/bot_chatのsecret masking | 永続配送受付・terminal tombstoneとUNKNOWNで二重再送を避ける方針を参考。SQLite/profile局所とprocess PID依存をmulti-tenant Postgresの正本へ流用しない |
| **OpenClaw** 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT [S3,S4] | src/cron/service/notification-intents.ts: cronNotificationJob, resolveCronNotificationQueueOwner; notification-dispatch.ts: dispatchCronNotification/transportFailureAlert/recordFailureAlertOutcome、service.failure-alert-notifications.test.tsのrouteとfailure通知 | 永続確定した「通知意図」とroutingを分ける。Gateway固有sessionKey/recipientの既定値を本ハーネスへ暗黙引継ぎしない |
| **Apprise** bae311ba19f92124ea1fa3ba794343e14bee68a1, BSD-2-Clause [S5] | tests/test_delivery_tracking_retry.py の test_retry_does_not_repeat_delivery、plugin delivery-tracker | 一つのsend中に複数service targetへ送った後の**target別再送抑制**が必要。プロセス内trackingだけではcrash/restartを跨げないため独立Postgres attempt ledgerを使用 |
| **changedetection.io** 11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b, Apache-2.0 [S6] | changedetectionio/notification_service.py の watch_will_send_content_changed_notification、NotificationContextData / FormattableDiff | 変更を検出したことと送達処理が別であるパターンを参考。値の確定と送信は別。テンプレート文字列に秘密/生HTMLを含めない |

**代替比較**: (A) Watch Triggerで直接Telegram API呼び出し：commit前に送信→再試行で重複、**却下**。(B) Redis/in-memory channel queue単独：再起動前の決定的なconsent/owner/event一意性が証明しにくい→**却下**。(C) Postgres Outbox + immutable consent-scoped intent + Transport Adapter + attempt ledger：**採用**。運用は増えるが疑義を記録できる。(D) SaaS通知基盤に全本文を渡す：個人データ保管/費用/資格管理/送達事実の継承が不透明→将来Adapterのみ、採用保証なし。上流ソースをライセンス抜きでコピーしない。

## 3. Trust boundary・正本図

~~~text
Watch/Observation/Agent result [07-02/03, CHAT_01/02]
 -> notification eligibility / consent refs [07-04]
 -> PG transaction: immutable intent + outbox + event [CHAT_10]
 -> outbox relay, lease [CHAT_10]
 -> delivery worker [07-04 protocol]
    -> resolve verified principal/tenant binding [CHAT_09, CHAT_08]
    -> ranking/quiet policy [CHAT_13-03]
    -> render safe summary with Evidence URLs/times [07-04]
    -> channel adapter [CHAT_08 Telegram; CHAT_05 email etc]
    -> provider receipt / UNKNOWN / permanent error
 -> REST/UI status [CHAT_08]
~~~

UserId/tenantId/workspace_idの変換はCHAT_01/09/10合意前で[PROVISIONAL]。公開通知IDはUUIDv7候補、dedup keyは安全なhash、Chat ID/Email/credentialは参照Bindingとしてのみ記録しwireへ露出しない。通知はTask/Conversationに必ず帰属するわけでなくWatchやEventからも起こる。Conversation ID nullのWatch起点Taskを合法とする提案を維持。

## 4. Requirements

| ID | Level | Expected | SC |
|---|---|---|---|
| NOT-001 | MUST | 無条件送信ではなく検証済み同意・binding scopeのNotification Intentだけ保存 | 02,08 |
| NOT-002 | MUST | 同じevent/destination/policyの論理通知Intent重複0 | 02,07,11 |
| NOT-003 | MUST | Event/Trigger commitとOutbox登録が単一DB transaction | 07 |
| NOT-004 | MUST | 送達receipt状態がqueued/attempted/provider_accepted/unknown/readを混同しない | 02 |
| NOT-005 | MUST | 再起動後outbox pendingを回復し既済みIntentを再生成しない | 07 |
| NOT-006 | MUST | 送信中のACK欠落は冪等証明なし自動再送せずUNKNOWN | 02,07 |
| NOT-007 | MUST | 一時的エラーと永久エラー/429を区別し有限backoffとDLQを持つ | 07,09 |
| NOT-008 | MUST | 配送前のowner/consent/bindingVersionを再検証、失効後新送信0 | 08,16 |
| NOT-009 | MUST | Watch pause/retire/definitionRevision更新で旧通知Intentを適切に抑止 | 02,07 |
| NOT-010 | MUST | 外部文章で宛先・同意・ranking優先条件を勝手に変更できない | 08 |
| NOT-011 | MUST | 通知に出典URL・observedAt・confidence/unknownを正直に明示 | 02,11,14 |
| NOT-012 | MUST | Quiet hoursで高優先を無断破棄せず延期/予約して再照合 | 16 |
| NOT-013 | MUST | unsubscribe/opt-outの受付後、対象bindingへの新規送信0 | 16 |
| NOT-014 | MUST | multi-destinationの部分成功と不確定をrecipient別に保存 | 07 |
| NOT-015 | MUST | log/trace/LLMへのchat ID・email・token漏出を防ぐ | 08 |
| NOT-016 | MUST | user read ackは認証済みclient操作のときのみ反映、送信成功と別扱い | 02,08 |
| NOT-017 | SHOULD | message/digestをユーザー設定に従い優先付け、dedup理由を可視化 | 16 |
| NOT-018 | LATER | provider問い合わせ可能なreceipt reconciliationを採用 | 07 |
| NOT-019 | EXPERIMENT | Transport別送達・重複/UNKNOWN率を統一fixtureで測定 | 09 |

## 5. I/O型・API [PROVISIONAL: CHAT_01/08/10/13]

~~~ts
type NotificationIntent = {
  schemaVersion:"1.0"; notificationId:string; tenantId:string; ownerUserId:string;
  eventKey:string; sourceKind:"watch"|"task"|"external-event"|"system";
  sourceRef:string; evidenceRefs:string[]; watchDefinitionRevision?:number;
  destinationBindingRef:string; bindingVersion:number; consentVersion:number;
  policyRef:string; policyVersion:number; formatVersion:number;
  priorityHint:"critical"|"standard"|"low";  // server policy may override, never untrusted source
  requiredByUserRule:boolean; notBeforeUtc:string|null; expiresAtUtc:string|null;
  payloadRef:string; createdAtUtc:string;
};
type DeliveryAttempt = {
  notificationId:string; destinationBindingRef:string; attemptNo:number;
  sendToken:string; claimedLeaseEpoch:string; startedAtUtc:string;
  finishedAtUtc:string|null;
  disposition:"not_started"|"transport_rejected"|"provider_accepted"|"unknown"|"permanent_failure";
  providerReceiptRef?:string; errorCode?:string; nextRetryUtc?:string;
};
type NotificationPublicStatus =
 "queued"|"deferred_quiet_hours"|"sending"|"provider_accepted"|
 "delivery_unknown"|"failed"|"suppressed"|"read";
~~~

StatusはUI projectionの局所案でCHAT_01のEvent/Run状態を再定義しない。DeliveryAttemptのsendTokenはproviderが**明示的に冪等性キーを受理する場合だけ**外部へ送る。Telegramのようにprovider-side idempotent sendが確認できないTransportは、timeout後に**unknown**で停止しオペレーター照会/人間確認へ（同じSendを自動反復するとユーザーに2回届き得る）。provider acceptanceはユーザー端末への配信・既読を保証しない。

Validation: notificationId/owner/tenantはtrusted sourceから、ユーザー外部本文はpriorityHint、destinationBindingRef、consentを変更不可。SemanticEventKeyは(Watch trigger/ExternalEvent stable identity/Task result revision)をdomain-scope付きcanonical hashで生成する。**bindingVersion / policyVersion / formatVersionが更新されても同じ通知目的への既存イベントは再送しない**。新宛先への過去イベント再通知は独立した認証済み再送CommandとrequestIdを必須とし、監査して新intentを生成する。新たなperiodic eventは別semantic occurrence IDにする。旧通知を新しいDestinationに無断転送しない。

Delivery Adapterはsend(payload, bindingRef, optionalProviderIdempotency)→{accepted?, providerReceipt?, transportOutcomeEvidence, retryAfter?}を返すが、返答なしの成功はunknown。webhook send先はSSRF/redirect対応のTool Gateway、email SMTP応答は受理/最終配信を区別する。Telegram Chat bindingはCHAT_08/09の認可した本人channelに限定。

## 6. 状態遷移・sequence

~~~text
PREPARED(uncommitted)
   -> QUEUED(committed)
      -> DEFERRED_QUIET_HOURS -> QUEUED (due)
      -> SUPPRESSED (consent revoked, expired, source stale)
      -> SENDING (worker leased, irreversible boundary)
          -> PROVIDER_ACCEPTED (provider gave success)
          -> RETRY_WAIT (explicit proof no send, 429 before acceptance)
          -> PERMANENT_FAILED (bad binding/unrecoverable)
          -> UNKNOWN (timeout/crash/ack loss after send could have happened)
      -> DEAD_LETTER (retry budget exhausted)
READ is separate verified user-ack state, never implied by provider acceptance.
~~~

~~~text
Observation Trigger -> same DB TX commit: TriggerDecision + intent + Outbox
 -> relay discovers and leases -> verify consent/owner/quiet-rule
 -> materialize redacted message from Evidence refs
 -> record SENDING attempt+sendToken BEFORE network call
 -> send via authorized adapter
 -> record accepted/failed/unknown
 -> publish user-visible status Event
 -> authenticated user ack -> optional READ
~~~

**Critical**: intentのeventKeyやcontentHashは必要条件であり、毎回同じ本文ならすべて同一Event扱いにはしない。ユーザーが別日に「毎朝報告」と指定した場合はscheduled occurrence identityで区別する。Quiet hoursのダイジェストは複数sourceEventKeyを保持し、送達済み/未知状態の各Intentを黙って消してはならない（CHAT_13 policy）。

## 7. 擬似コード：Commit/Claim/Send/Recover

~~~text
acceptNotificationIntent(tenant, trigger, deliveryPolicy):
  tx:
    assertWatch/Task/Event is committed and owner scoped
    binding := lookupVerifiedBindingForUpdate(tenant,owner,trigger.destinationRef)
    assert binding.currentConsent && policyAllows
    key := canonicalHash(tenant, owner, trigger.semanticEventKey, binding.id, deliveryPurpose)
    existing := findByUniqueKey(key)
    if existing: return existing
    insert Intent with immutable payloadRef+evidenceRefs+sourceRevision
    insert Outbox(eventKey=key, notificationId) ON CONFLICT DO NOTHING
    commit
  return committed notificationId

deliver(outboxRow):
  tx:
    lock notification+binding/consent by tenant
    if revoked/disabled/expired/stale definition -> SUPPRESSED
    if quiet hours -> deferredUntilUtc <= finite max, exit
    if user ranking asks for suppression but user required critical -> DO NOT DROP
    if prior attempt in SENDING/UNKNOWN -> RECONCILE_REQUIRED, do not send
    lease with monotonically increasing deliveryEpoch
    insert DeliveryAttempt(SENDING) with unique (notification,target,attemptNo)
    commit
  message := renderSafe(Evidence refs, escape markup, hard byte limit)
  try: receipt := adapter.send(message, authorized binding, provider idemToken if supported)
  catch:
    if transport proves nothing submitted: safe finite retry
    else: UNKNOWN; require reconciliation
  tx:
    settle by deliveryEpoch CAS
    store providerAcceptedAt only if receipt proves acceptance
    write status Event+Outbox (not another send)
    commit

recover():
  scan pending/RETRY_WAIT and expired SENDING in finite batches
  if expired SENDING:
    if provider supports status query: query once within budget
    else mark UNKNOWN (avoid automatic second outbound send)
  never promote UNKNOWN to delivered/failed without evidence
  redrive only attempts with proven not-submitted or provider-idempotent guarantee
~~~

ACK lost後に再試行する場合、**二重API呼び出しを許しても配信重複が起きないことがProvider契約で確認された時のみ**。同一targetの複数Attemptがあり得るのは「未送信が証明された」または「provider idem」がある場合。違うtarget宛の送達は独立行、部分成功targetの再送を避ける。

## 8. Failure matrix

| Failure | Recovery | Visible state |
|---|---|---|
| Intent commit前kill | 全rollback、同eventKey再受付 | 未受付/Unknown creation |
| Intent commit後relay kill | Pending Outboxから再送準備 | queued |
| Queue duplicate | unique intent key+lease CAS | logical1 |
| Bot accepted/ACK lost | provider照会可能なら問い合わせ、不可なら再送しない | delivery_unknown |
| 429 before accepted | Retry-After + jitter、max retries・budget | retry_wait |
| SMTP 5xx explicit reject | durable error; provider policyでsafe retry | retry/failed |
| email accepted but bounce | acceptanceとbouncedを別Event、bounce evidence要確認 | accepted/bounced |
| token revoked/Telegram unbound | send前停止、再同意待ち | suppressed/needs_input |
| quiet hours跨ぎ、timezone DST | intended local quiet windowをIANA TZで計算、ambiguous境界は安全な延期 | deferred |
| Watch retire after intent | user policyとsource revision再照合、criticalでも未認可ならSTOP | suppressed |
| same trigger replay | unique key/episode | one notification |
| user reports unseen | receipt accepted!=read、再送はuser explicit command + requestId | unread/unconfirmed |
| cancel during SENDING | transport cancellationを届けても外部受理は不確か | unknown if indeterminate |
| outbound secret in template | renderer redact/escape fail-closed、no call | suppressed_error |
| old leaseEpoch writes after successor | CAS rejected; delivery might have happened → reconcile | unknown |

## 9. Security / privacy / consent

- **二重承認**: Watch/Resultの通知要請時にConsent binding検証、実送信直前にもowner/tenant/bindingVersion/consentVersion/identityVersionを照合。Logoutは以前に有効化されたWatch自体を消さないがopt-out/user disable/revokeは新規送信を停止。
- 宛先はVerified Channel Binding、raw URL/email/chat IDをLLMや未信頼HTMLから直接指定できない。Webhook宛先はSSRF/egressで制御し、リダイレクト後も制限。Personal Messageを他Tenantへ誤送する事案を防ぐ。
- Message rendering: EvidenceのURL scheme allowlist、HTML/Markdown escape、文字数/byte上限、シークレット検出redactionが失敗したら**送らない**。ログへmail addresses、Telegram chat IDs、access tokens、raw HTML、message全文を保存しない。監査はhash/codeのみ。
- Unsubscribeはdestination-scoped version/consentを更新し、既Queuedも送信前に再照合。通知本文を誤って削除要求の対象外に残さないようretention/data exportはCHAT_09/10 owner。

## 10. Metrics / cost / SLA measurement

notification_intent_created_total, notification_duplicate_suppressed_total, notification_delayed_quiet_total, notification_consent_blocked_total, delivery_attempts_total{channel,disposition}, delivery_unknown_total, delivery_reconciled_total, outbox_lag_seconds, provider_retry_after_seconds, user_ack_lag_seconds, delivery_cost_usd_total。注意: provider accepted!=human delivered/seen。数値SLAは実験前に約束せず、仮測定条件は1k/10k intents・2/4 Worker・429/timeout/killを混ぜてp95 outbox wait/duplicate attempts/unknown/false deliveries、channel API費用を記録。個人宛先/IDをmetric labelsに入れない。

初期提案[PROVISIONAL]: max retries=5、送信timeout=10秒、retry backoff 30秒→60秒→5分→15分→1時間上限、outbox batch100、message<=4KiB per channel cap、delivery worker parallel global8/tenant2、quiet deferral最大30日（実際のpolicyはCHAT_13/00へ）。永続UNKNOWNは自動消去/送達済み処理しない。実値はProvider rate制約やSLO測定後に確定する。

## 11. 永続化・Transaction・Migration [PROVISIONAL: CHAT_10]

必要論理概念: notification_intent(public UUIDv7 candidate,tenant,owner,sourceRef,eventKey,bindingRef,bindingVersion,consentVersion,policy/format version,payload/evidence refs)、notification_attempt(notificationId,target,attemptNo,deliveryEpoch,started/finishedAt,transportReceiptRef,errorCode,outcome)、notification_outbox(unique intentId,notBeforeUtc,lease,attemptLimit)、user_ack(notificationId,userId,readAt,version)、binding consent version（正本CHAT_09/08）。Unique(tenant,owner,semanticEventKey,destinationBindingId,deliveryPurpose)と物理unique per notification+recipientを提案する。**bindingVersion, policyVersion, formatVersionは検証・監査属性であり、重複検知キーに含めない**。それらの変更で同一イベントが再配信されることを防ぐ。別日の定期報告はscheduled occurrence IDをsemanticEventKeyの一部にする。Watch Trigger→Intent→Outboxを同一DB transaction。Provider APIは当然TX外。

Retention: Notification/attempt auditとuserデータ削除を分離。terminal tombstone（最小hash/intent ID）をTTL内保持し、その間の再配達で二重を防ぐ。削除要求の法令/Consent漏洩はCHAT_09/10へ。Migrationは通知先identifierからVerified Bindingを手動再確認し、不確かな旧destinationはsend禁止。送信中の旧新Worker排他・rollback時の二重送信を検証。

## 12. API/UI/Telegramへの提案 [PROVISIONAL: CHAT_08/13]

通知一覧に acceptedAt, queuedAt, sentAttemptAt, providerAcceptedAt, deliveryUnknownAt, readAtを別表示し、「届いた」「既読」はreceipt/本人操作なしに主張しない。Unsubscribe、mute、再送要求はIdempotency-Key+If-Matchと本人確認で受付。quiet hours/priority/digestはCHAT_13-03が決めるが、強制高優先Triggerを黙って破棄しない。Email/Telegramの配達失敗で監視そのものを取消さず、解消が必要なblocked stateを表示。

## 13. 実装PR分割

NOT-PR1: immutable intent/eventKey/Verified Binding validation + safe renderer + tests。NOT-PR2: CHAT_10のOutbox/Attempt/Consent atomic transactionとmigration。NOT-PR3: Telegram/Email transport adapter + explicit receipt contract + leases + unknown recovery。NOT-PR4: quiet hours/CH13 rankingとの合流、unsubscribe/read ack/API。NOT-PR5: fault injection、multitenant security、benchmark、canary。実サービスへの勝手な通知テストは禁止。

## 14. Unit/Contract/Integration/Fault/E2E/Benchmark tests（**DESIGNED, NOT RUN**）

| ID | Setup/action | Expected |
|---|---|---|
| NOT-T01 | consentあり/なし、別user destinationでIntent作成 | validだけ保存、誤送0 |
| NOT-T02 | 同じTrigger/eventを100回再送、2 workers | Intent1/outbox1、定義したepisodeの同一通知1 |
| NOT-T03 | Trigger decisionとIntent commit前/後にkill | all rollback or both committed、片方だけ0 |
| NOT-T04 | Provider受理、user read未実行 | provider_acceptedだがread=false |
| NOT-T05 | Pending OutboxをWorker再起動でrecover | loss0、同じintentId |
| NOT-T06 | send中にtimeout/ACK loss、API idem不可 | UNKNOWN、新API call0 |
| NOT-T07 | 429 Retry-After、明確な未送信、max5回 | backoff finite+DLQ、cap超過0 |
| NOT-T08 | Queue後にopt-out/owner revoke/Chat binding change | send前再検証でprovider call0 |
| NOT-T09 | Trigger後Watch改訂/retire、queued pre-revision通知 | policyに従いsuppress、旧sourceが新条件と誤結合0 |
| NOT-T10 | X post/HTMLに「宛先変更・critical化」命令 | consent/priority unmodified |
| NOT-T11 | evidence with URL/time/confidence/unknown & secret injection | source label accurate、secret出力0 |
| NOT-T12 | Quiet hrs 22-07 Asia/Tokyo・DST NY transition・required critical | permitted deferred配信、user ruleの無断drop0 |
| NOT-T13 | unsubscribe/consent version CASと並列delivery | revocation commit後新send0 |
| NOT-T14 | 2 recipients: one accepted, one explicit reject, late retry | accepted target2送信0、失敗targetのみ条件付き再送 |
| NOT-T15 | token/ChatID in title+body / redactor crashes | all outward lanes secret0、send suppressed |
| NOT-T16 | Forged read receipt from other tenant | readAt不変、他user detail0 |
| NOT-T17 | duplicate provider events/unknown then explicit resend command | 無断二重送信0、user commandは監査 |
| NOT-T18 | Fault: old deliveryEpoch settle after new worker reconcile | stale write拒否、unknown保持 |
| NOT-T19 | Benchmark 10k intents/4 workers mixed 429/timeouts | p95/unknown/duplicate measured、値未設定 |

## 15. Requirement → Test → SC traceability

| MUST | TEST | SC | Expected |
|---|---|---|---|
| 001 | T01 | 02,08 | Consentなし保存/送信0 |
| 002 | T02,T17 | 02,07,11 | Logical intent一意 |
| 003 | T03 | 07 | atomic commit |
| 004 | T04,T16 | 02 | accepted≠read |
| 005 | T05 | 07 | pending復旧 |
| 006 | T06,T18 | 02,07 | ambiguous再送0 |
| 007 | T07 | 07,09 | 有界retry |
| 008 | T08 | 08,16 | revoke新送信0 |
| 009 | T09 | 02,07 | old revision抑止 |
| 010 | T10 | 08 | malicious content変更0 |
| 011 | T11 | 02,11,14 | Evidence/時刻/unknown |
| 012 | T12 | 16 | priority/quiet尊重 |
| 013 | T13 | 16 | opt-out後0 |
| 014 | T14 | 07 | partial target分離 |
| 015 | T15 | 08 | secret漏洩0 |
| 016 | T16 | 02,08 | 本人readのみ |

## 16. 未解決・Cross-team proposals

| ID | Owner | Priority/required changes |
|---|---|---|
| NOT-P01 | CHAT_01/10 | P0 canonical Notification/Event envelope + UUIDv7 ID, outbox/Attempt unique and immutable receipt schema、sourceEventKey collisions T02/T03 |
| NOT-P02 | CHAT_09/08 | P0 consentVersion/Verified Binding正本、ownerUserId+tenantId vs workspace_id、revocation after queue、manual read/unsubscribe T08,T13,T16 |
| NOT-P03 | CHAT_08/05 | P0 Telegram/Mailで送信結果とidempotency/照会サポートの実API確認、UNKNOWN policy T06,T14 |
| NOT-P04 | CHAT_13 | P1 Notification Intelligence ranking/digestが既存intentを勝手に消さない、quiet hoursのIANA TZに一貫性 T12 |
| NOT-P05 | CHAT_07 07-02/03 | P1 Watch retire/revision とTrigger決定のLinearization、source ref/evidence valid T09/T11 |
| NOT-P06 | CHAT_10/11 | P0 Attempt fencing+Queue duplicate recovery/transactional intent+outbox + multi-target fault tests |
| NOT-P07 | CHAT_00 | P1 delivered/accepted/read UX、ユーザー緊急性別UNKNOWNと再送のプロダクトADR |

## 17. Sources / evidence grades（閲覧2026-10-30）

- [S1] **Code verified**, Hermes Agent commit fcc26fbd9f1641755e49793d7ed608f3b7214c3d, MIT: cron/delivery_queue.py enqueue, claim_next, _finish, recover_abandoned https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/cron/delivery_queue.py
- [S2] **Code verified (test inspected, not run)** Hermes secret redaction tests https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/cron/test_cron_delivery_redaction.py ; license https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/LICENSE
- [S3] **Code verified**, OpenClaw main 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT: src/cron/service/notification-intents.ts https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/notification-intents.ts ; notification-dispatch.ts https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/notification-dispatch.ts
- [S4] **Code verified (test inspected, not run)** OpenClaw src/cron/service.failure-alert-notifications.test.ts https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service.failure-alert-notifications.test.ts ; MIT https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/LICENSE
- [S5] **Code verified**, Apprise master bae311ba19f92124ea1fa3ba794343e14bee68a1, BSD-2-Clause: tests/test_delivery_tracking_retry.py test_retry_does_not_repeat_delivery https://github.com/caronc/apprise/blob/bae311ba19f92124ea1fa3ba794343e14bee68a1/tests/test_delivery_tracking_retry.py ; license https://github.com/caronc/apprise/blob/bae311ba19f92124ea1fa3ba794343e14bee68a1/LICENSE
- [S6] **Code verified**, changedetection.io master 11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b, Apache-2.0: changedetectionio/notification_service.py https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/changedetectionio/notification_service.py ; license https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/LICENSE
- [S7] **Issue report**, Hermes cron issue #97230: adapterの明示的ambiguous write後にfallbackが部分的に再配信し重複するとの報告（open、現行HEADで再現確認したわけではない）: https://github.com/NousResearch/hermes-agent/issues/97230
- [S8] **Issue report**, OpenClaw cron delivery pressure/restart/notification design discussion #138348: https://github.com/openclaw/openclaw/issues/138348 。requirementsの反証材料であり現行実装保証ではない。
- [S9] **PR report**, OpenClaw #168615 (conversation-resultと通知経路の分離): https://github.com/openclaw/openclaw/pull/168615 。マージ/実装状態は本MDで保証しない。
- [S10] **Hypothesis**: Postgres DeliveryAttempt/UNKNOWN state and canonical dedup policy unique to this harness, not yet validated in implementation.
- [S11] **Unverified**: current Telegram/email provider-specific end-to-end receipt semantics, cost/rate limits; need provider adapter investigation, no delivery guarantee claimed.

### Review Gate
- [x] scope/owner, multiple OSS code+test+license, states, pseudocode, security/races, DB/REST, all16 MUST traceability, failure matrix
- [ ] source provider receipt test; P0 agreement; actual unit/integration/fault/E2E; independent review (Draft)

### Round 2 focused audit: event semantic identity

**不変条件**: Notificationの一意性は「ある出来事を誰へ何の目的で通知するか」で定義する。Policyや表示形式のversion、単なる宛先bindingのcredential rotationは**同じ出来事をもう一度届ける新しいイベントではない**。Versionを一意キーに含めた旧案はFormat変更で重複通知を生むため撤回した。新しい通知先へ過去イベントを再送したければ明示的なCommandに基づく新semantic resendEventKeyを発行する。

**追加テスト NOT-T20**: 送信済みEventに対しFormat/Policy/Binding credential Versionをそれぞれ更新→同じsource EventをリプレイしてもIntent/Outbox/配送呼出は増えない。新たな翌日Scheduled occurrenceは別semantic Eventとして1件生成される。**NOT-020 MUST → NOT-T20 → SC-02/07/16**。未実施。


### Round 2 concurrency correction: opt-out vs send admission (P0)

「送信直前に同意確認した」だけでは、確認完了と外部send開始の間にopt-outがcommitし得る。**同意解除完了後の新規送信0**を約束するなら、次の状態・線形化が必要。共有認可/DB契約は [PROVISIONAL: CHAT_09/10/08]。

1. opt-out Commandは認証後にVerified Bindingをtenant scopeの行ロックで**STOPPING**へCAS更新し、新規delivery admissionを直ちに禁止する（受付receiptは「解除処理中」であり、完了通知ではない）。
2. Workerは同じBindingを直列化するgateで、consentVersion・stopping・leaseEpochを検査してからsendを開始する。gateの維持中は当該Bindingの送信を直列化し、Provider RPCのtimeoutを有限化する。**DB TransactionをネットワークI/O中保持してよいかはCHAT_10の決定事項**であり、単なる短い事前DB照合を完全な排他とみなさない。
3. 解除サービスはSTOPPING前に既に始まった配送を観測し、すべてsettledまたはUNKNOWNとして隔離した後のみ**REVOKED**を確定し「解除完了」を返す。未終了/不明の送信が残る間はPendingRevocationを明示し、完了したと主張しない。実送信済みメッセージのProvider内部配送・到着は取り消せない。
4. Worker/解除の片方がcrashした場合は永続SEND_RESERVED/SENDING attemptとfencingを再照合する。解消不能なSENDINGはUNKNOWNへ移すが、ネットワーク呼出がなお継続し得る旧Workerをリース切れだけで「物理送信終了」と推定しない。必要ならChannel Adapter ownerからの停止確認/worker process fencingを要求し、完了不能なら解除の保証を明確に限定する。
5. したがって既存のNOT-008/013とNOT-T08/13の**厳密な期待値**は「STOPPING確定以降の新規配送権限の発行0。REVOKED完了通知後の新規Provider send開始0（事前in-flightを収束した場合のみ）。既存in-flightのProvider配信結果はUNKNOWNあり」とする。

**NOT-021 MUST / NOT-T21 / SC-07,08,16**: Delivery Workerを同意確認直後・Provider送信前のbarrierで停止し、同時にopt-out開始、次にWorkerを再開するFault Test。受信側で無効Bindingの送信開始が0、最終解除ackが未収束のin-flightを隠さない、かつ旧worker leaseEpochのDB settlementが0であること。実テスト未実行。Cross-team proposal: CHAT_09/10に配送開始の厳密な線形化とBinding revoke barrierの所有権・phase/state/DDL、CHAT_08にPendingRevocationのUI契約を提案する。

**注意**: 強いrevocation保証が初期実装で成立しなければ、仕様を弱めて「最善努力」だと曖昧に隠すのではなく、個別Channelで完了確認できないことを明示してP0 blockerとして残す。

## 18. Round 4 — Digest coverage accepted by the only sender
CHAT_13's digest is a ranking/layout step, **not** a new delivery path. CHAT_10 proposes `notification_digest_envelopes` / `notification_digest_members`; CHAT_07 is sole owner of provider send, attempt ledger, reconciliation and notification status. Before sending, CAS-reserve digest + member original intents with fresh owner/consent/Watch/policy/revision checks and semantic Event dedupe.
`reserved` members cannot be sent individually; `provider_accepted` aggregate maps to `covered_accepted` **not delivered/read**; `UNKNOWN` maps to `coverage_unknown` and blocks blind individual resend. After a *proven pre-send reject*, release member reservations atomically. If digest disabled until verified, use original individual delayed sends only. No UI result may claim every member delivered just because one digest was generated.
Tests NOT-R4-T01 concurrent digest vs individual send→one transport intent; T02 ACK lost→UNKNOWN all included and zero automatic resend; T03 revoke between reservation and send→0 new dispatch; T04 digest rejected before send→members released; T05 cross-tenant membership/recipient mismatch rejected. SC-02/07/16. **Design only, awaiting real worker tests.**

