# Telegram Channel Adapter — Authenticated Commands, Shared Tasks & Notifications

> Owner CHAT_08 | docs/08-interfaces/channel-adapters-telegram.md | Status Draft / [PROVISIONAL CHAT_01/02/03/07/09/10/13] | Research 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-01,02,03,04,05,06,07,08,09,10,11,14,16

## 0. 結論

**TelegramはWeb UIと同一のTask/Conversation/Watchに対する「認証済みチャネルAdapter」であり、2つ目のAgent Runtime/DB/Approval判定器ではない。** Telegramから自然言語のTask作成・状態照会・Pause/Cancel/Steer・Watch登録確認・通知受信を扱うが、必ずAuth-bound principal、RESTと同一のCommand Service（Idempotency, CAS, Policy, Budget）を通る。Webhook/Pollingの重複配信、Telegram API送信失敗、手動Web操作との競合にも耐える。**通知の外部配信exactly-onceは一般には保証できない**ため、UIはunknown deliveryを区別し、delivery/outboxの正本はCHAT_07へ委譲。

Primary UX: Webで起動した商品/ホテル/一般Web調査をTelegramで確認・Steer。逆も同様。監視通知には観測値・URL・時刻・条件を含め、X特定アカウントの新規投稿発火ならTask/trigger/eventへリンクする。Telegram message contentやcallback_dataは権限付与ではなく非信頼入力。

## 1. Scope / non-goals / ownership

Scope: bot account setup、Webhook/polling ingress、pairing/login link、Telegram user ID identity verification、DM/thread boundaries、command router、update dedup/outbox delivery、reply/edit、callback approve/deny handoff、channel cross-Task context、rate/quiet-hour presentation、audit and disable/revoke。

Non-goals: independent LLM loop, username-based identity, group-global admin shell, Telegram bot command as permission to purchase/book/post, arbitrary external URL/MCP execute, own Watch evaluator or notification outbox, direct long-lived secrets in logs/prompt, unauthorized account scraping. X-provider streaming semantics belong CHAT_05/13, UI only consumes Task and notification.

Canonical owners: CHAT_01 Task/Run/Conversation/ID/Event/Version; CHAT_02 task commands/safe steer; CHAT_03 Tool Gateway + Approval; CHAT_07 watch and notification outbox; CHAT_09 AuthN/identity/session/secret policy; CHAT_10 channel bindings/outbox/idempotency physical table; CHAT_13 external event ingestion; CHAT_08 adapter/REST/SSE/Web interface.

**Known conflicts:** public IDs UUIDv7 vs DB gen_random_uuid(); Task.conversationId nullable; recordVersion 1 vs DB revision 0; leaseEpoch is internal fenced bigint not Telegram-supplied; GoalRevision/activeRunId persisted by other owners; tenantId↔workspace_id mapping must be explicit. Each remains [PROVISIONAL] until PR #8 reconciliation + owner PRs are merged.

## 2. OSS code/test audit (2026-10-30)

| Source, pinned commit | Actual source and test inspected | Adopt / reject |
|---|---|---|
| **OpenClaw** [S1–S6] 8b8c33ef (MIT) | \`extensions/telegram/src/bot-handlers.inbound-authorization.ts\` \`createTelegramHandlerAuthorization\` / tests, \`bot-handlers.inbound-pipeline.ts\` \`createTelegramInboundPipeline\` (authorize→route→buffer), \`bot-handlers.inbound-pipeline.test.ts\` scoped debounce key, \`approval-callback-data.ts\` 64-byte callback encode/parse + test, \`approval-handler.runtime.test.ts\` terminal receipt/message update and origin channel safety | Adopt admission before dispatch, origin account/topic binding, small typed callback, terminal message edit, replay dedupe. **Reject** OpenClaw host command approval and broad shared-secret operator model; no host execution in this product. |
| **grammY** [S7–S10] e8b3b1be (MIT) | \`src/composer.ts\` middleware \`run\`/\`Composer\`, \`src/bot.ts\` \`Bot\` update/polling and lastTriedUpdateId, \`src/core/api.ts\` webhook/getUpdates/answerCallbackQuery docs and parameters, \`test/composer.test.ts\` middleware chaining, error and next-once tests | Adopt transport adapter, middleware sequencing/error boundaries, Telegram API integration with scoped bot token. **Reject** in-process offset as authoritative business dedupe (DB must persist update IDs+command receipts). |
| **Mastra** [S11–S12] ec3ba11d (Apache-2.0 standard/ee exceptions) | \`packages/server/src/server/handlers/schedules.ts\` schedule state and \`handlers/schedules.test.ts\` pause/resume idempotency tests | Compare channel command mapping with canonical Watch lifecycle, not adopt Mastra schedule schema. |

Source/test files read; **no upstream tests or Harness code run**. No code copied. grammY bot \`setWebhook\` secret-token feature is **a header shared-secret comparison, not a signed body/HMAC**. Telegram usernames, display names, chat titles, forwarded author fields can change or be spoofed and are not identity.

Alternatives: A. bot independently runs Agent (cheap prototype; DB truth forks)→reject. B. Telegram user numeric ID bound to an already-authenticated Web account via one-time authenticated exchange and central Command service→adopt. C. open group command by default→reject. D. Telegram callbacks execute Tool Gateway functions directly→reject; callbacks must create ordinary authorization-checked decision Commands.

## 3. Components / dependency sequence

~~~text
Telegram update (bot webhook / bounded polling)
    -> Ingress verifier (secret_token / TLS + allowed_updates)
    -> Dedup inbox (bot account + update_id; DB [CHAT_10])
    -> Identity binding (telegram_user_id ↔ tenant/user) [CHAT_09]
    -> Policy / rate / topic allowlist [CHAT_09]
    -> Parser (Command or natural-language user goal; untrusted)
    -> Chat_08 CommandAdapter
             └→ central Task/Watch/Approval command service
                  ├→ Domain/Run CHAT_01/02
                  ├→ Tool Gateway CHAT_03
                  └→ Watch/Notification CHAT_07
    -> reply projection (redacted Task status/URL/observedAt)
    -> notification outbox relay (CHAT_07/10) -> Telegram send/edit
                       └→ delivery result/unknown, bounded retry
~~~

Proposed implementation files (not in spec repo): \`apps/api/src/channels/telegram/{ingress,auth-binding,update-inbox,command-router,callback-codec,renderer,delivery-adapter}.ts\`, \`packages/interfaces/src/channel-command-port.ts\`, \`apps/api/test/telegram-*.test.ts\`. Store Telegram Bot Token through CHAT_09 secret resolver; only secretRef in DB, never frontend env or agent prompts.

## 4. Requirements

| ID | Level | Observable acceptance | SC |
|---|---|---|---|
| TG-001 | MUST | inbound update verified and bound to authorized Telegram numeric user, bot account, tenant; unknown/unpaired denied before Task creation | 06,08 |
| TG-002 | MUST | pairing is one-time, short-lived, web-authenticated, bound to Telegram numeric user and intended account; unpair/revoke immediate | 06,08 |
| TG-003 | MUST | Telegram/Web actions use identical Task/Run/Watch/Conversation and versioned Command services; no shadow Task store | 02,07,11 |
| TG-004 | MUST | update_id and callbacks deduplicated across retry/crash/parallel Bot worker without duplicate Task/approval commands | 02,07 |
| TG-005 | MUST | messages and edit/reply have tenant+chat/topic authorization, no cross-chat/group data leakage | 06,08 |
| TG-006 | MUST | approval button identifies immutable server Approval, applies fresh permission/If-Match/expiry/goalRevision, refuses stale callback | 04,06,07,08 |
| TG-007 | MUST | notification receives actual observation/source/observedAt/TZ, delivery unknown distinguished, no fake realtime/0-yen fallback | 02,03,11,14 |
| TG-008 | MUST | Web↔Telegram concurrent Steer/Cancel/Resume respects recordVersion CAS and safe Runtime boundary | 07 |
| TG-009 | MUST | all updates and outbound messages have finite rate/size/retry/budget/queue depth and backoff; stop on disable | 09 |
| TG-010 | MUST | bot token, webhook secret, login code, approval proof, cookie and unsafe source HTML absent from LLM/log/URL | 08 |
| TG-011 | MUST | account/chat binding lifecycle survives restart and preserves operator audit, protects bot account impersonation | 06,07 |
| TG-012 | SHOULD | rich formatted replies and safe navigation to Web Task/Approval detail, graceful long-text splitting/editing | 05,09 |
| TG-013 | SHOULD | user-configured quiet hours/priority via CHAT_07/13, without suppressing mandatory security warnings | 02,16 |
| TG-014 | LATER | explicitly opt-in groups/forum topics with scoped principals and restricted commands | 08 |
| TG-015 | EXPERIMENT | inline summaries with streaming edits after rate/length measurements | 09 |

## 5. Ingress, Bot trust, identity pairing

**Webhook:** endpoint \`POST /api/channels/telegram/{botRef}/webhook\` is not a generic REST user API; bind exact configured BotRef. HTTPS, request body max 1MiB initial cap, content type JSON, Telegram \`X-Telegram-Bot-Api-Secret-Token\` constant-time compare (secret store only). **Not an HMAC body signature**: it authenticates the expected shared secret at trusted ingress but relies on TLS + correct secret confidentiality. Optional provider IP allowlist is defense in depth and must be checked against current Telegram documentation; proxies must have trusted forwarding chain. Reject unknown botRef/invalid secret before parsing expensive JSON; malformed/oversized returns 4xx, no state change.

**Long polling fallback:** only one fenced consumer per bot with persisted offset = last *durably processed* update+1, not last received. Startup must check webhook disabled and exclusive lease. Never run webhook and polling simultaneously for one bot. Use bounded \`getUpdates\` timeout, retries, \`allowed_updates\` allowlist. If the provider redelivers, DB dedup still governs. Update sequence wraps/offset assumptions are Telegram-provider-specific, do not use as globally monotonic Event sequence.

**Pairing protocol:**
1. User logs into Web UI and selects Connect Telegram. Authenticated server generates random one-time pairing challenge, stores only hash with tenantId,userId,identityVersion,botRef,issuedAt,expiry and state. Display QR or \`t.me/{bot_username}?start={short_code}\` deep link; code is **temporary proof of intent** not login authorization and should be masked/no long retention. HTTP Referer/log exposure reviewed (nonce expiry short).
2. User starts Bot in private DM. Ingress verifies bot secret and Telegram \`from.id\` numeric ID, \`chat.type=private\`, rejects forwarded/replayed claims. User sends \`/start CODE\` (or secure Web confirmation).
3. Server validates challenge and authenticated Web owner confirmation. One-use atomic consume binds \`botRef + telegramUserId + privateChatId + tenant/user + identityVersion\`. If bot/user already paired to different tenant, reject/require explicit Web owner review. No username lookup authority. Bot answer says confirmation exists without echoing code.
4. On unpair/disable/revoked owner, increment binding epoch, cancel pending delivery/access for old binding; do not delete underlying Task/Watch unless explicit Task control. Any old callback must fail current bindingVersion check.

Pairing limits proposal: code expiry 5 min, max 3 failed attempts per challenge, max 5 outstanding per user, max 1 active primary DM binding per bot+Telegram user (multi-device Telegram maps same numeric user). All values configurable pending CHAT_09 review. Store bot token in central Secret Store, with rotation requiring webhook secret+binding continuity tests.

**Forbidden by default:** all groups, supergroups, channels, users not paired, forwarded posts; group activation needs explicit allowlisted admin/tenant binding and mention/thread policy, never reuse DM authority automatically. Telegram account ID, \`message_thread_id\`, chatId are notification routing details, not shared domain User IDs.

## 6. Canonical inbound Command DTOs [PROVISIONAL]

~~~typescript
type VerifiedTelegramActor = {
  botRef: string; telegramUserId: string; // decimal string, provider-specific
  privateChatId: string; topicId: string|null;
  boundPrincipalRef: string; bindingVersion: string; // server side only
};
type TelegramInboundRecord = {
  botRef:string; updateId:string; receivedAt:string; payloadHash:string;
  lifecycle:"received"|"processed"|"rejected"|"dead_letter"; // local adapter only
  commandId:string|null; replyIntentId:string|null;
};
type TelegramCommand =
  | {kind:"create_task"; goal:string; conversationId:string|null; timezone:string}
  | {kind:"task_status"; taskId:string}
  | {kind:"control_task"; taskId:string; action:"pause"|"resume"|"cancel"|"steer"; instruction?:string}
  | {kind:"watch_status"; watchId:string}
  | {kind:"approval_decision"; approvalId:string; decision:"approve"|"deny"};
type TelegramPublicReply = {
  kind:"task_ack"|"task_status"|"watch_status"|"approval_status"|"delivery_error";
  canonicalResourceId:string|null; shortText:string;
  url:string|null; lastObservedAt:string|null; sourceUrl:string|null;
};
~~~

These are channel-adapter DTOs, **not canonical domain definitions**. Parser and server validation enforce UUIDv7 for public Task/Watch/Approval IDs. The user may input \`/task <uuid>\`, \`/watch <uuid>\`, \`/pause <uuid>\`, \`/resume <uuid>\`, \`/cancel <uuid>\`, \`/steer <uuid> <instructions>\`, \`/help\`, \`/unlink\`, or freeform text; all unknown/ambiguous requests must ask for clarification or open Web link, never assume admin privilege. In group context only explicitly allowed safe commands. Task create can use \`conversationId:null\` when no Web Conversation is selected; optionally binding maps Telegram DM to a canonical Conversation/Thread created by authenticated service, no implicit cross-user joins.

**Idempotency key**: stable from \`HMAC(serverKey, botRef + update_id + operation_index + normalized actionType)\` length-prefixed canonical fields; not from message text (same text might represent two intentional separate Tasks). Duplicate callback IDs and duplicate same update must map to same persisted intent. For callback decision, server refetches approval and checks current recordVersion and owner; \`callback_data\` is a *locator*, not proof of authorization.

Callbacks **64 UTF-8 bytes** Telegram API upper bound confirmed in OpenClaw implementation/test; encoder uses server-issued short opaque key referencing (approvalId, actionDigest, issuedAt, bindingVersion, expiry) in a scoped DB cache. If callback data too large, omit buttons and use Web link, not truncate into a new approval ID. Include no bot token or raw URL in callback_data. For stale/expired callback call \`answerCallbackQuery\` with generic notice; don't accept old authorization.

## 7. Inbound lifecycle / pseudo-code

~~~text
Telegram update → validate webhook/botRef
  → DB UNIQUE(botRef,updateId) dedupe/pending claim
  → verify numeric from.id + privateChatId + binding epoch
  → authorize principal/resource and check quota
  → parse safe bounded user command / pure text
  → call shared CommandService with persistent idempotency key
  → write canonical ack/resourceId + reply outbox intent atomically
  → acknowledge provider 200 (or polling offset advance after commit)
  → outbound relay sends short redacted reply (at-least-once)
  → Web Task & Telegram see same durable Task/Run/GoalRevision
~~~

~~~ts
async function onTelegramUpdate(raw, transport) {
 const bot = await verifyIngressSecretAndBot(raw,transport);
 const update = strictTelegramUpdateSchema.parseBounded(raw);
 const key = [bot.ref, decimalString(update.update_id)];
 return db.transaction(async tx => {
   const receipt = await tx.inbound.tryClaimOrReadUnique(key, hashCanonical(update));
   if(receipt.committed) return receipt.originalAck;  // no extra command
   const actor = await bindings.resolveExact(bot.ref,update.from.id,update.chat.id);
   if(!actor || !actor.enabled) return tx.inbound.rejectAndAck(key,"UNPAIRED");
   await auth.requireCurrentBindingAndCapability(actor,update);
   const parsed = parser.parseAllowed(update);        // bounded and untrusted
   const commandId = deriveDurableIntentId(key,parsed.operationOrdinal);
   const response = await commandPort.acceptInTransaction(tx,actor.principal,parsed,commandId);
   await tx.telegramReplyOutbox.addUnique(commandId,renderSafeReplyIntent(response));
   return tx.inbound.commit(key,response);
 });
}
~~~

**Important boundary:** incoming Telegram API HTTP webhook cannot safely create a Task after ack then "hope" to save update ID; receipt + domain Command + reply outbox should share a DB commit or equivalent transactional contract [PROVISIONAL CHAT_10]. If provider timeout before commit, redelivery retries safely. If two replicas compete, unique claim+fence ensures one logical Command. If old lease worker later completes after new binding epoch, authorization and write CAS rejects it.

**Cross-channel Steer:**
- Web sends Steer with If-Match recordVersion N, Telegram sends Steer using server-read version N. If race, only one writes new immutable GoalRevision. Losing command returns conflict to Telegram with safe "conditions changed, review in Web"; never silently replay using N+1 because this can overwrite the latest user constraint.
- Task may be active under Run with old inputGoalRevision; server records accepted goal revision and runtime safe boundary applies. Telegram reply must distinguish "変更要求受付" from "適用済み". Existing external action with uncertain result is not auto repeated.
- Cancel an event-created Task does not cancel the underlying Watch or subscription unless explicit command; Pause Watch differs from Pause Task.

## 8. Notification send / edit / delivery semantics

CHAT_07 Notification Outbox owns trigger identity/dedup and user preferences. Telegram adapter is one *delivery transport*:
1. Outbox item includes notificationId, bindingRef, tenant, priority, structured safe payload, origin Task/Watch/Observation, observedAt, source URL, condition, timezones. Resolve current binding before every delivery; revoked/unpaired destination must be blocked.
2. Render text with Telegram-safe escaping (MarkdownV2 or HTML as supported, never direct raw web HTML). Source URLs validated; avoid automatic forwarding of privacy/credential query strings. Truncate long excerpts and link to authorized Web detail.
3. Send via Bot API with bounded retry on 429 Retry-After, 5xx/network. On successful response persist telegram message_id/chatId and receipt; later updates may edit same message rather than send new.
4. On timeout after network write, **delivery result is UNKNOWN**, not definitely failed or safely retryable. Telegram Bot API message send lacks a universal idempotency key guaranteeing exactly-once; duplicate notification can occur in ambiguous cases. Prefer delaying re-send, explicit reconciliation/manual resend, content-group dedup and existing message edit when message ID known. Do **not** claim perfect duplicate suppression. Audit risk.
5. Telegram edits (progress/status) are rate bounded and optional. If edit returns "message not found"/forbidden, fallback to new message only if policy and duplicate suppression permit; permanent failures mark degraded but do not erase Watch.
6. Quiet hours, escalation and priority are computed by CHAT_07/13. Telegram sender does not bypass them. A user's explicit high-priority threshold may override quiet hours only by canonical policy, not adapter guesses.

Payload summary for price: \`price: {amount:"45000",currency:"JPY"}\`, sourceUrl, observedAt, condition (> or >=), acquisition success state, WatchId. Missing or failed source is **unknown, never 0 yen**. For X named account event: verified actor stable ID + post link/time + downstream TaskId; no guarantee of instant X API detection, outbound delivery time separately shown.

## 9. Error/fault matrix

| Failure | Detection | Recovery/user message |
|---|---|---|
| Webhook bad secret | 401/403, audit rate limited | no processing, secret never logged |
| Unknown/private chat mismatch | pairing check | generic "Webから連携してください"; no Task |
| Group message unapproved | scope/mention policy | deny quietly, no global history exposure |
| Duplicate update / concurrent webhook | DB unique inbox | replay original Command Ack; 1 Task |
| Bot API 429 | Retry-After | bounded outbox retry, no new Task |
| Bot API timeout after send | UNKNOWN | cautious reconcile, possible duplicate clearly noted |
| Web and Telegram Steer race | CAS 412 | show latest in Web; no automatic override |
| Old approval button | expiry/goalRevision/bindingVersion/CAS | callback answered "期限切れ/解決済み", no effect |
| Auth/session revoked | binding epoch/identityVersion | stop new commands/delivery; existing Task state preserved per policy |
| Bot token rotated | ingress fails until configuration in sync | alert admin, block unauthenticated requests |
| Provider updates too many | rate/concurrency/queue depth | reject/throttle, preserve bounded backlog |
| restart after receipt committed | durable inbox/outbox | dedup and retransmit safe receipt without new task |
| malformed PDF/link/HTML text | strict parser/escape | safe error/no script execution |
| Source X unavailable | API error from provider owner | mark delayed/unverified, no invented post |

## 10. Security + resource limits

- Telegram numeric user ID is identity anchor **only after Web owner has completed pairing**. Never rely on username/phone number, message.forward_from, display name, or group admins to authenticate a resource request.
- Bot Token and webhook secret resolved through CHAT_09 server-only vault. Redact from URL/log/model/tool; bot identity explicit, no "use default bot" fallback on invalid BotRef.
- Webhook secret header verification must not be confused with HMAC signature. TLS, CSP, CSRF for Web pairing, Origin; Signed Web verification and DB replay are independent.
- Actor/tenant/resource authorization on every command, callback, reply. Channel-bound request cannot grant arbitrary Tool Gateway powers. No raw host shell, Docker socket, unapproved booking/purchase/post or money transfer; prompt injection from Telegram text and X posts remains untrusted user/source data.
- Proposed hard bounds: webhook body max 1 MiB, text command max 8 KiB, callback 64 UTF-8 bytes, per-user 30 inbound commands/min and 5 Task creations/hour, per-bot 8 in-flight DB handlers, outbound 1 message/s per chat, max 5 retries with exponential jitter; all **initial configuration hypotheses** pending rate/plan and load tests. Reject unlimited AI loops.
- Delivery privacy: account/user notification target is readable only as redacted summary in Web UI; Bot DM chatId isn't exposed to models. Logs contain update_id hash, commandId, scope decision, delivery state, never user text by default. Disable bridge must purge active adapter session cache and not accidentally remove canonical Tasks.
- Metrics: webhook accepted/rejected, unpaired commands, dedupe hits, queue lag, 429, notification UNKNOWN count, cross-channel conflict count, silent replay attempts (target0), delivery delay by timezone, per-bot spend. No fabricated success rate.

## 11. DB / API / UI integration and migration proposals

- [PROVISIONAL CHAT_09+10] \`telegram_bindings(botRef, telegramUserId, privateChatId, tenantId, ownerUserId, bindingEpoch, status, verifiedAt)\`; unique active mapping. No storing actual bot token. tenant↔workspace mapping must be explicit.
- [PROVISIONAL CHAT_10] \`telegram_inbox(botRef,updateId,payloadHash,status,commandId,acceptedAt)\` unique, and outbox delivery attempts with sender binding + message_id + UNKNOWN outcome.
- [PROVISIONAL CHAT_01] canonical Conversation/Task/Run/Event/Watch IDs and Task nullable conversation, GoalRevision, recordVersion; no "TelegramTask" domain record.
- [PROVISIONAL CHAT_07/13] Notification Outbox and priority/quiet hours, external X named-actor trigger Subscription. Telegram adapts notification dispatch only and may render subscription control in Web link.
- [PROVISIONAL CHAT_03] immutable Approval decisions CAS+GoalRevision; callback cannot auto-approve after expiry.
- [PROVISIONAL CHAT_02] Steer/Cancel safe boundary, same Run identity and input goal revision; lease epoch invisible.
- [PROVISIONAL CHAT_08 REST] Telegram calls same server **application CommandService** as Web REST. Internal service call doesn't need to literally issue HTTP over loopback but must share AuthZ/idempotency, and expose identical observable semantics.
- Version migration: pairing V1 challenge -> binding epoch and token rotation. Old callback code schema must fail closed; old sessions cannot claim other Tenant. Dual writers webhook/polling rejected until one owner elected; no destructive migration resetting offset to zero silently.

## 12. Implementation PR order

TG-P1 bot/webhook verification + owner pairing/unpair + security tests; TG-P2 durable inbox/offset/command adapter + race tests; TG-P3 safe outbound outbox transport/UNKNOWN delivery + 429 backoff; TG-P4 callback decisions and cross-channel Steer + browser handoff Web link; TG-P5 notifications/quiet hours/X triggers integration, E2E crash/chaos and operational rollback. Do not ship Telegram before invalid token revocation and tenant isolation are tested.

## 13. Test plan (designed only, not run)

| TEST | Setup/action | Expected | SC |
|---|---|---|---|
| TG-T01 | webhook forged secret/unpaired numeric user/other botRef | 0 Task, no user data, 401/403 or generic denial | 06,08 |
| TG-T02 | web pairing challenge success/expiry/mismatch/reuse/unpair | exactly one binding, old code/revoke denied | 06,08 |
| TG-T03 | create via Telegram then Web GET/Steer; Watch Task without Conversation | same canonical ID, nullable Conversation accepted, no shadow state | 02,07,11 |
| TG-T04 | 100 duplicate updates concurrent/worker restart before/after commit | exactly one Task/Approval command, one durable receipt | 02,07 |
| TG-T05 | 2 private chats/2 tenants and a forum topic injection | 0 cross chat content/approval leak | 06,08 |
| TG-T06 | expired/Steer changed/old callback 64 bytes vs 65 bytes | expired denied, no external effect, oversized callback omitted | 04,07,08 |
| TG-T07 | failed data acquisition, source stale, X trigger arrived late | unknown not ¥0; actual observedAt and source URL, delivery time separate | 02,03,11,14 |
| TG-T08 | Web and Telegram Steer same Task ETag, Cancel while tool UNKNOWN | exactly one accepted revision, old conflicts and no unsafe replay | 07 |
| TG-T09 | 429, 5xx, provider timeout after send, disabled binding | bounded retries, UNKNOWN accurately surfaced, no false exactly-once claim | 02,09 |
| TG-T10 | X/HTML/Telegram malicious instructions, bot token in fixture, bad URL | credentials/secret absent in output/log, permission unchanged | 08 |
| TG-T11 | crash/restart, rotate token, old binding version update | only current owner/pairing accepted and audited | 06,07 |
| TG-T12 | send 10,000 updates test with quota, 100 notification intents | finite backlog, measured memory/cost/latency; no unproven SLO | 09 |
| TG-T13 | notification preference quiet hours/excluded retailer, manual override | user policy respected, original evidence unchanged | 16 |

**REQ→TEST→SC trace:** TG-001→T01; 002→T02; 003→T03; 004→T04; 005→T05; 006→T06; 007→T07; 008→T08; 009→T09,T12; 010→T10; 011→T11. No tests actually run; fixtures only. Do not send real unapproved Telegram messages, purchases or booking requests in tests.

## 14. Blockers / cross-team proposals

| ID | Owner | Severity | Acceptance |
|---|---|---|---|
| TG-P01 | CHAT_09 | P0 | binding identity principal/session/tenant-workspace and revocation |
| TG-P02 | CHAT_10 | P0 | inbox+command+outbox atomicity; delivery unknown/outbox TTL; bot offset fence |
| TG-P03 | CHAT_07 | P0 | notification delivery record and retry/unavailable/quiet hours semantics |
| TG-P04 | CHAT_01/02 | P0 | nullable Task conversation, recordVersion starts 1, GoalRevision/activeRun persisted, command state |
| TG-P05 | CHAT_03 | P0 | approval digest/expiry/CAS, no cross-channel duplicate execution |
| TG-P06 | CHAT_13/05 | P1 | X named-account trigger event with actor ID, entitlement/rate freshness |
| TG-P07 | CHAT_08 REST | P1 | canonical shared CommandService with web/Telegram parity |
| TG-P08 | CHAT_11 | P1 | provider duplicate/timeout/429 and chaos fixture |

## 15. Source evidence (2026-10-30, precise commit, code read not run)

[S1] **Code verified** OpenClaw \`createTelegramHandlerAuthorization\`, pinned 8b8c33ef (MIT): https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/bot-handlers.inbound-authorization.ts  
[S2] **Code verified/test inspected** authorization: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/bot-handlers.inbound-authorization.test.ts  
[S3] **Code verified** inbound pipeline: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/bot-handlers.inbound-pipeline.ts and https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/bot-handlers.inbound-pipeline.test.ts  
[S4] **Code verified/test inspected** callback 64 bytes: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/approval-callback-data.ts and https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/approval-callback-data.test.ts  
[S5] **Code verified/test inspected** approval delivery: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/src/approval-handler.runtime.test.ts  
[S6] **Code verified** OpenClaw ingress is distinct from authorization, plugin README: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/extensions/telegram/README.md  
[S7] **Code verified** grammY \`Composer\` middleware \`run\`, pinned e8b3b1be (MIT): https://github.com/grammyjs/grammY/blob/e8b3b1bec0ecb0cd76af7f3996354027ba94c303/src/composer.ts  
[S8] **Code verified** grammY Bot/update/polling: https://github.com/grammyjs/grammY/blob/e8b3b1bec0ecb0cd76af7f3996354027ba94c303/src/bot.ts  
[S9] **Code verified** grammY webhook/getUpdates API: https://github.com/grammyjs/grammY/blob/e8b3b1bec0ecb0cd76af7f3996354027ba94c303/src/core/api.ts  
[S10] **Code verified test inspected** grammY middleware: https://github.com/grammyjs/grammY/blob/e8b3b1bec0ecb0cd76af7f3996354027ba94c303/test/composer.test.ts ; [MIT license](https://github.com/grammyjs/grammY/blob/e8b3b1bec0ecb0cd76af7f3996354027ba94c303/LICENSE).  
[S11] **Code verified** Mastra schedules: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/schedules.ts  
[S12] **Code verified/test inspected** Mastra pause/resume: https://github.com/mastra-ai/mastra/blob/ec3ba11dad428caeb3b4afae77d1669462e8c129/packages/server/src/server/handlers/schedules.test.ts  
**Unverified:** Integration with Telegram API in this product, actual Telegram policy entitlement, throughput benchmark, final webhook deployment/pairing UX, source repo exact release tag compatibility. Design proposals/limits are hypotheses, not guarantees.
