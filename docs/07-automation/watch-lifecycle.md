# Watch Lifecycle — Detailed Implementation Specification

> Owner: CHAT_07 | Canonical: docs/07-automation/watch-lifecycle.md | Status: Draft / PROVISIONAL | Revision: 0.2 | Research: 2026-10-30 | Based on main b45d4e989998663a00ef09673e69ae1f4191f00d | Scenario: SC-02,07,08,09,11,12,16
>
> このrepositoryには実装はない。本書のTESTは全て設計であり実行していない。共有UUID/Task/Event/DB/REST/認可はDraft PR #2/#10/#8/#9が未Locked。承認と実装は別。

## 0. 結論と採用案

Watchを**保存されたユーザーの監視意図**として定義し、Task/Run/Conversationと別寿命にする。ユーザーが指定した「何を」「どの範囲から」「いつ」「どの条件で」「誰に通知するか」は認証済みのWatch Commandでしか変更できない。Schedulerは時刻、Observation Engineは確かな比較、Notification Outboxは送達を担う。Watch自体はLLMの判断で勝手に生成・修正・公開しない。

- 初回登録を**検証中ではなく永続commit後**にだけ成功応答。DB内でWatch、definition revision、Schedule参照、通知先binding、初期Command receiptを一貫して確定する。[PROVISIONAL: CHAT_10]
- Pauseは新規tick/外部Event由来のTriggerの**新規入場禁止**。既開始Runの副作用は必ずCHAT_02/03の取消境界に委譲。Resume時は新定義世代、次回未来slotを計算し過去停止時間の監視結果を捏造しない。
- Editは**recordVersion（CAS）**と**definitionRevision（意味的条件の世代）**を分離。条件、URL、schedule、通知binding、許可内容に変更があるとdefinitionRevisionを増やす。lastCheck等の実行更新でdefinitionRevisionを増やさない。
- Deleteは初期実装では監視の退役・tombstoneであり即時の履歴/Tool ledger物理削除を意味しない。ユーザーの消去要求と保持期限はCHAT_09/10に委譲する。
- Watch起点TaskにConversationは必須でない。新規TaskはWatchId+triggerKey+provenanceを参照し、CHAT_01に従いnullable conversationIdを許す（CHAT_10と要調整）。

## 1. 目的・責務・非責務・適用

| Watch Service owns | Does not own |
|---|---|
| create/pause/resume/edit/retire、authorization lineage、definitionRevision、対象と条件binding | Task/Run/Eventの共有Schema [CHAT_01]、承認/Side effect [03] |
| user intent、最短周期制約、通知同意binding参照、Trigger admission | Cron/TZ/DST/Occurrence [07-01]、実際の価格評価 [07-03] |
| 現在状態と監視状態のUI投影、revision競合、再起動時の復帰 | REST wire/Telegram [08]、tenant/owner mapping [09]、物理DB [10] |
| event-driven subscriptionとの共通Watch条件接続 | X event ingestion/actor handle解決 [13-01]、change snapshot diff [13-02]、priority ranking [13-03] |

SC-02の一意IDと通知、SC-07の取消/再配送、SC-08の越境/prompt注入、SC-09の監視コスト、SC-11のX新投稿起点、SC-12の差分更新、SC-16のユーザー明示フィードバックを対象。非目標はWebページを直接命令として扱うこと、任意の外部URLを無審査で巡回すること、未知商品探索の戦略（CHAT_12）、リアルタイム更新の無条件保証、購入/予約/投稿。

## 2. Source-code prior art（2026-10-30、固定commit）

| OSS | 実際に読んだclass/function/test | 活用/制約 |
|---|---|---|
| **OpenClaw** [S1,S2] main 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT | src/cron/service/jobs-mutation.ts の cloneCronJobForMutation / finalizeUpdatedJob、src/cron/service.jobs.test.ts のdelivery patch、restart/catchup/revision test | 既存定義変更で残すfieldと消すfieldの条件を分ける必要を学ぶ。ただし本Harnessでは下流Agentに直接Scheduleを修正させず、公開revision CASが必要 |
| **Hermes** [S3,S4] main fcc26fbd9f1641755e49793d7ed608f3b7214c3d, MIT | cron/job_definition.py の merge_job_definition / import_job_definitions、tests/cron/test_atomic_paused_creation.py のpaused登録→resume | Pauseを**作成時に原子的に保存**するテストを転用。ローカルfile/profile stateでの所有権をPostgres多tenant正本へそのまま流用しない |
| **changedetection.io** [S5] main 11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b, Apache-2.0 | changedetectionio/model/Watch.py の validate_time_between_check_required, model.update, watch persistence、is_safe_valid_url | URL変更で以前の取得文脈を無条件に使い回さないことやチェック間隔検証を参考にする。Web変更監視専用モデルは一般Watch正本には不採用 |
| **pg-boss** [S6] master 71ea068b144c75b014a1eb90327aaa4a92da1cf2, MIT | src/timekeeper.ts の schedule/unscheduleに相当するschedule ownerパス、test/scheduleDbTest.ts のcommit/rollbackと同時登録 | schedule登録がDB transactionで永続確定することの参考。Watch状態/権限/revisionをpg-bossだけに保存しない |

**案比較**: (A) Watch=Taskで永続Runループ：コンテキスト/費用/承認の寿命が混同→却下。(B) 保存済Cron rowのpayloadへ条件を全部格納：変更と通知のCAS/監査が困難→却下。(C) Watch独立aggregateとVersioned Definition + Scheduler port + Task trigger：採用。変更時に多テーブル取引が必要だが監査できる。別SaaS特有モデルへForkしない。

## 3. 依存関係・正本とAuthority

~~~text
Authenticated human/API/Telegram -> (CHAT_08 transport/idem)
  -> Auth principal & binding (CHAT_09)
  -> WatchCommandService [THIS] -> Watch revision/target policy [THIS]
      -> SchedulerPort 07-01 -> Occurrence -> Task Manager CHAT_02
      -> ExternalEvent ingress CHAT_13 -> Trigger admission [THIS]
      -> Observation checker 07-03 -> Notification 07-04
      -> DB/transaction/outbox CHAT_10
~~~

Watch ownerUserId, tenantIdはCHAT_01/09から取得、DB workspace_idとのmappingは**[PROVISIONAL: CHAT_09/10]**。Watchの公開IDはCHAT_01提案UUIDv7、DBの gen_random_uuid()とは矛盾があるため、UUIDv7を作るtrusted adapterと永続一意制約をCHAT_01/10へ提案。**SHAによる重複キーはIDに代えない**。Watch起点TaskにConversationはnullを許す。Task.activeRunId、Run.leaseEpoch/goalRevisionはWatch Serviceが独自に更新しない。

## 4. 要件（MUST/SHOULD/LATER）

| REQ | レベル | Expected | SC |
|---|---|---|---|
| WAT-001 | MUST | 認可ユーザーが登録したWatchだけ永続化しcommit後にreceipt | 02,08 |
| WAT-002 | MUST | target、正規化済み条件、TZ/周期、通知bindingの不変版を保存 | 02,11 |
| WAT-003 | MUST | Pause commit後の旧未開始Triggerから新Tool開始0 | 02,07 |
| WAT-004 | MUST | Resumeで前回停止時間の観測捏造0、未来slot再計算 | 02,07 |
| WAT-005 | MUST | UpdateはIf-Match recordVersionをCAS、意味変更でdefinitionRevision+1 | 02,07 |
| WAT-006 | MUST | 同時update/disable/triggerで旧定義に基づく新規Run開始0 | 07 |
| WAT-007 | MUST | retire後に通知新規受付0、履歴は保持方針で参照可能 | 02,07 |
| WAT-008 | MUST | Watch triggerKey単位にTask生成は一意、重複queueはno-op | 02,07,11 |
| WAT-009 | MUST | invalid URL/cron/閾値/権限/宛先をrejectし副作用0 | 02,08 |
| WAT-010 | MUST | owner disable/tenant revokeで発火停止、logoutのみで永続Watch削除しない | 02,08 |
| WAT-011 | MUST | Watch定義は非信頼Web/X投稿によって変更できない | 08,11 |
| WAT-012 | MUST | 旧通知先binding・同意失効時に送信せず停止/再承認要求 | 02,16 |
| WAT-013 | MUST | restart後にstate/revision/nextDueをDBから読み、重複Task 0 | 02,07 |
| WAT-014 | MUST | Watch起点TaskはconversationId=null合法、UUID/tenant整合 | 02,11 |
| WAT-015 | MUST | 失敗と空/unknownの観測を価格0/条件成立としない | 02,12 |
| WAT-016 | MUST | Task/Provider/Notification予算と有限並列を超えるfireを禁止 | 09,11 |
| WAT-017 | SHOULD | WatchにlastChecked/lastObserved/nextDue/blockedReasonを明示 | 02 |
| WAT-018 | LATER | 複数Watchを認可されたグループに束ねて同時編集 | 16 |
| WAT-019 | EXPERIMENT | cron引数とイベント型triggerで一意性負荷比較 | 09 |

## 5. API/型/validation — [PROVISIONAL: CHAT_01/08/10]

~~~ts
type WatchCommand =
 | {kind:"create", idempotencyKey:string, spec:CreateWatch}
 | {kind:"patch", watchId:string, expectedRecordVersion:number, patch:WatchPatch}
 | {kind:"pause"|"resume"|"retire", watchId:string, expectedRecordVersion:number, idempotencyKey:string};
type WatchTarget = {kind:"provider-entity"|"url"|"query"|"external-event", reference:string, normalizedIdentityRef?:string};
type PredicateRef = {kind:"price-threshold"|"change"|"structured-expression", version:number, canonicalConditionRef:string};
type CreateWatch = {target:WatchTarget, predicate:PredicateRef, timeSpecRef?:string, eventSubscriptionRef?:string, destinationBindingRef:string, budgetRef:string, consentRef:string};
type WatchProjection = {watchId:string, recordVersion:number, definitionRevision:number, status:"active"|"paused"|"retired"|"blocked", target:WatchTarget, nextDueUtc:string|null, lastObservedAtUtc?:string|null, blockedReason?:string, notificationSummary:string};
~~~

上記は**所有範囲を示す局所proposal**でありCHAT_01の共有Event/StateやCHAT_08のREST DTOを確定しない。recordVersionは共有record CAS（1開始をCHAT_01が提案）、definitionRevisionはWatch条件の版、Task.goalRevisionはTask目標、Run.leaseEpochはRun実行所有権。互いに代用禁止。value/stringのJSON wireではunsigned safe integerを超えるversionはrejectまたは共有string bigint codec決定待ち。

Validation: target URLはnetwork/egr​​ess/SSRF認可済みprovider gatewayで確認、redirect/DNS rebindingの初回・実行時再検証必須。外部イベントはCHAT_13 Subscription ID/安定Actor IDの照合が必須、SNS handleだけをIdentityにしない。価格閾値はfinite decimal + ISO currency、条件比較は07-03へ、無限大/NaN/negative invalid。宛先は自分の検証済binding参照であり任意email/chat ID直入力は禁止。Cron/TZの検証は07-01、最低周期/rate/quotaは契約値に従う。

HTTP [PROVISIONAL: CHAT_08]: POST /api/v1/watches、GET /watches/:id、PATCH /watches/:id、POST /watches/:id/commands。変更にIf-Match必須（欠落428、不一致412、違法遷移409）、POSTのIdempotency-Keyと指示body fingerprint。成功時は永続Watch ID+次回予定時刻を返す。削除はcancelとは別、watch retire commandとして扱い物理DELETEは独立プライバシー経路。

JSON例（架空の公開投影、実IDではない）:

~~~json
{"watchId":"01956e32-58c5-7a22-8b7c-913aa11c44c1","recordVersion":4,"definitionRevision":2,"status":"paused","target":{"kind":"provider-entity","reference":"provider:sku-a"},"nextDueUtc":null,"lastObservedAtUtc":"2026-10-29T04:00:00.000Z","notificationSummary":"Telegram verified"}
~~~

## 6. State transitions / シーケンス

~~~text
create(valid+consented) -> ACTIVE | PAUSED(initial choice)
ACTIVE --pause--> PAUSED --resume(new definition generation)--> ACTIVE
ACTIVE/PAUSED --patch(expected version, new validated spec)--> same status + revision
ACTIVE/PAUSED/BLOCKED --retire--> RETIRED (terminal)
ACTIVE --owner revoke / policy block--> BLOCKED
BLOCKED --reauthorize+validate--> ACTIVE (explicit safe command)
~~~

許可されない遷移（RETIRED→ACTIVE、競合old If-Match、blocked ownerを別principalでresume）は拒否し、既存履歴や旧通知を破壊しない。状態名はWATCHローカルの案、共有state正本を上書きしない。

~~~text
User -> API(Auth+Idempotency) -> Watch service validates target/consent/limits
    -> DB TX: insert Watch+Definition+Schedule intent+command receipt
    -> commit -> return watchId + nextDue
Scheduler/ExternalEvent -> verify Watch ACTIVE+current definitionRevision
    -> evaluate trigger identity, admission reserve, unique Task intent+outbox in 1 TX
    -> worker reverify ownerVersion/Watch state/Run lease; execute via Tool Gateway
    -> Result+Observation -> predicate -> Notification Outbox (not direct sender)
~~~

## 7. 具体アルゴリズム / race / retry

~~~text
applyWatchCommand(auth, idemKey, cmd, expectedVersion):
  principal := verify(auth)
  begin transaction
  receipt := idempotency.lockOrCreate(principal, idemKey, hash(cmd))
  if receipt.completed: return exact previous response
  w := tenantScopedWatchForUpdate(principal, cmd.watchId)
  assertOwnerCapability(principal,w)
  if expectedVersion != w.recordVersion: throw PreconditionFailed412
  if cmd is pause/retire: set inert state; expire pending admission grants
  if cmd is resume: check owner active+destination consent+site rates
       set ACTIVE; increment definitionRevision; scheduler nextDue=future
  if cmd is patch: validate full merged immutable definition, not partial stale field
       increment definitionRevision; conditionBaselineRef=unset or explicit migration plan
       invalidate all not-yet-started old revision intents
  bump recordVersion (recordVersion initial from shared contract)
  write event+audit+outbox+receipt in same TX
  commit
  return receipt

admitTriggeredWatch(watchId, triggerKey, expectedDefinitionRevision):
  begin transaction
  w := lock scoped Watch(watchId)
  if w.status != ACTIVE or w.definitionRevision != expected: skip(reason)
  recheck owner/tenant identityVersion + consent + budget + provider rate
  existing := findTaskBy(tenant,watchId,triggerKey)
  if existing: return existing
  insert Task with conversationId=null, createdFromWatchId=watchId,
    triggerKey, immutable goalRevisionRef, outbox intent
  commit -> worker must recheck Watch/Run/Tool authority
~~~

重要: 本アルゴリズムのTask/Outbox原子性とunique indexはCHAT_10への提案で、既存Draft DBがそのまま実装済みとは言えない。Watch update時、前条件の成功した古いTask履歴は残す。すでに外部HTTP送信されたToolは取消不能な場合がありUNKNOWNはTool ledgerへ。Receiverのack喪失は同じidempotencyKey/triggerKeyで照会して新しいTaskを増やさない。

## 8. 障害マトリクス

| 故障 | 判断と回復 | ユーザー可視 |
|---|---|---|
| create DB commit直前kill | 同じIdempotency-KeyでDB結果照会、rollbackなら同key再投入 | 登録状態未確認/成功を分離 |
| commit直後ack消失 | receiptを再読、同じwatchIdを返す | 重複Watch 0 |
| pauseとscheduler claim競合 | Schedule/Watch行を同一lock orderでCAS、workerはdispatch前再照合 | 旧slotはskipped、既開始は終了待ち |
| patchと評価結果到着が競合 | Observationは旧revisionに紐付け、旧条件から新通知を生成しない | 更新直後のfalse positive 0 |
| reauthorize時token失効 | frozen/blocked、credentialを要求 | 明示Needs input |
| external-event二重配達 | providerEventIdをtriggerKeyへcanonical map、同じwatchで一Task | 重複通知0（outbox境界も別途） |
| destination unbind時 | 実配達前にbinding version/consentを確認し抑止 | consent revoked |
| worker crashまたはDB再起動 | 時刻+Watch定義を正本から再走査、未確定副作用はreconcile | partial/uncertainを表示 |
| watch retire while notification queued | 発送前の不要な通知停止の政策を再検証。既送達は戻せない | 既送達/停止後禁止を区別 |
| provider 429 / budget exhausted | Watch削除せず有限再評価、hard capでTool呼出0 | blocked reason/nextRetry |
| malicious page instruction | 非信頼内容として条件変更不能 | 監査、秘密漏洩0 |

## 9. Security / privacy

AuthZは**登録・command・Trigger Admission・Task Start・Tool Begin・通知送信**の各段階で照合。Session logoutは即Watch停止と同義でないがownerUserId disable/tenant revoke/Watch retireは以降の新しい操作を禁止する。Authenticated principalからtenantを導出、Watch payload内のowner/tenantを信用しない。任意のURL/template/regexは入力サイズとタイムアウト制限、regexのReDoSはsafe engine/時間上限で防ぐ。未承認の購入・予約・投稿なし。機密URL query/tokenはログとLLM文脈へ出さない。削除時の観測/通知履歴retentionはCHAT_09が決定。

## 10. 観測性・予算と性能試験

watch_commands_total{kind,accepted}, watch_concurrency_conflict_total, watch_revision_conflict_total, watch_revoke_block_total, watch_due_lag_seconds, watch_task_duplicate_suppressed_total, watch_permission_rejected_total、blocked_duration_seconds を採取。Watch ID/URL/Telegram chatIdはmetric labelにしない。TraceでwatchId、recordVersion、definitionRevision、triggerKey hash、taskId、sourceRefと日付を関連付けるがsecretを記録しない。

初期政策案[PROVISIONAL]: Watch最短5分、同一Watch active Task 1、1 tenant最大1000 Watch、create payload上限64KiB、1日USD hard capを持ち、数値の正本をCHAT_00/02/09で合意。性能は1k/10k Watch、2/4 worker、pause burst 100 command、site429とDB切断でp95 register latency/DB lock time/重複Task数を測定する。実測未実施。

## 11. 永続化・Transaction/Migration [PROVISIONAL: CHAT_10]

提案論理表: watch(tenant,owner,watchId,status,recordVersion,currentDefinitionRevision,created/updated/tombstonedAt)、watch_definition(tenant,watchId,definitionRevision,immutable target/predicate/schedule/destinationBindingRef,policyVersion)、watch_trigger(tenant,watchId,definitionRevision,triggerKey,sourceEventRef,scheduledFor,TaskId)、WatchCommandReceipt/Outbox、Consent binding参照。Unique(tenant,watchId,definitionRevision)とUnique(tenant,watchId,triggerKey)。TaskのcreatedFromWatchIdはFK、conversationIdはnullable提案。Trigger fingerprintはcurrentDefinitionRevisionを含めて再利用を阻止。どのカラムもこの文書ではDDL確定せず、保存時のCAS/unique/外部Outbox atomicの要求をCHAT_10へ提出。

Migration: 旧Cron設定だけのWatchはローカルTZ/owner/target/通知先を検証し、不確定ならPAUSED+BLOC​​KED reasonで手動review。既存UUID v4をv7へ勝手に置換し参照を切らない。revision初期値の変換は明示Adapter。Rollout=additive tables→read-only shadow validation→canary→new worker exclusive→old scheduler shutdown→rollback rehearsal。

## 12. REST/UI/Telegram提案 [PROVISIONAL: CHAT_08]

POST createはIdempotency-Key必須、成功201のみWatch実体とverified destination summary/nextDue/ETagを返す。PATCH/commandsはIf-Match必須、HTTP 412/428/409を使い分ける。GETはlastCheckedが失敗した時刻とlastObservedAt（値あり）を別表示。Pause、Resume、Retireには確認とすでに始まったRunの説明を明示。Telegram認可bindingはCHAT_08/09が正本。

## 13. 実装PR分割

WAT-PR1: Watch domain validation+immutable definition & CAS（shared IDs/versionとstorage adapter待ち）。WAT-PR2: Watch CRUD/commands、consent lookup、idempotent receipts、migration。WAT-PR3: scheduler admission＋EventIngress bridge＋Task trigger unique/outbox。WAT-PR4: security/restart/revoke/fault suite。WAT-PR5: REST/Telegram UIとE2E/benchmark。旧新Workerは同時同一Watchを発火しない。

## 14. 検証テスト（全件DESIGNED、未実行）

| TEST | Setup/action | Expected |
|---|---|---|
| WAT-T01 | Auth ownerでCreate + DB commit/ack | 1 Watch、ID/TZ/条件/宛先確認、201後のみ存在 |
| WAT-T02 | 同key同body並行create / 異body | 同body同receipt、異body409、Watch1件 |
| WAT-T03 | Pauseとdue scanをlock順別に100反復 | commit後旧未開始Tool 0 |
| WAT-T04 | 休止24h後Resume | 過去観測0、未来nextDueと新definitionRevision |
| WAT-T05 | 2 client同じIf-MatchでPATCH | 片方200、片方412、lost update 0 |
| WAT-T06 | old revisionのObservation到着後patch/通知評価 | 新定義で旧結果がthreshold alertにならない |
| WAT-T07 | Retire→restart→旧outbox replay | 新しいTask/notification 0、過去履歴残存 |
| WAT-T08 | queue二重送達、同じEvent idとTimer slot | triggerKeyごとTask 1 |
| WAT-T09 | invalid URL/TZ/currency/negative amount/unbound destination | reject、保存0、outbound 0 |
| WAT-T10 | owner revoke中queued Watch + logoutのみの比較 | revoke後Tool 0、logoutのみでWatchが無断削除されない |
| WAT-T11 | X post本文に通知先/周期変更命令 | Watch definition/owner unchanged |
| WAT-T12 | Telegram consent revokeと配達race | send前再検証でnew send 0、uncertainは可視 |
| WAT-T13 | DB・Worker kill→reconcile | exact WatchID/revisionsを復元、重複Task0 |
| WAT-T14 | UUIDv7 verifier / null Conversation Task / workspace mapping | Owner/tenant一致、ConversationなしTask受理 |
| WAT-T15 | Provider 500/null/emptyとprice0を区別 | false trigger 0、failure status可視 |
| WAT-T16 | Rate429/dailyUSD cap/target concurrency | 新Tool0、次回再試行有限、Watch生存 |
| WAT-T17 | UI/Telegram projection/404 auth/412 conflict | 他tenant情報0、nextDue/lastObserved意味分離 |
| WAT-T18 | Benchmark 1k/10k Watch + 4 Worker | p95 due and update latency/duplicate countを記録（閾値後決） |

## 15. MUST traceability / acceptance

| REQ | TEST | SC | Expected |
|---|---|---|---|
| 001 | T01,T02 | 02,08 | 永続後だけ成功 |
| 002 | T01,T09 | 02,11 | 完全条件/binding |
| 003 | T03,T07 | 02,07 | Pause後新Tool 0 |
| 004 | T04 | 02,07 | 観測捏造なし |
| 005 | T05,T06 | 02,07 | CASと定義世代 |
| 006 | T03,T06 | 07 | stale fire 0 |
| 007 | T07 | 02,07 | retire後新通知0 |
| 008 | T08 | 02,11 | 同trigger1Task |
| 009 | T09 | 02,08 | invalid保存0 |
| 010 | T10 | 02,08 | revoked停止 |
| 011 | T11 | 08,11 | injectionはData |
| 012 | T12 | 02,16 | consent revoke停止 |
| 013 | T13 | 02,07 | restart回復 |
| 014 | T14 | 02,11 | UUID/nullable対応 |
| 015 | T15 | 02,12 | failure≠0 |
| 016 | T16,T18 | 09,11 | budget/rate上限 |

## 16. Unresolved / Cross-team proposals

| ID | Owner | Severity / 判定条件 |
|---|---|---|
| WAT-P01 | CHAT_01/10 | P0 UUIDv7公開WatchId vs gen_random_uuid()、recordVersion 1 vs DB revision0。migrationとValidator/T14合意 |
| WAT-P02 | CHAT_01/10/08 | P0 nullable Task.conversationId、Watch起点TaskのFK/DTO/T14 |
| WAT-P03 | CHAT_02/03/10 | P0 trigger→Task+Outbox atomic、Run leaseEpoch/goalRevision、旧定義Tool阻止T03/T06 |
| WAT-P04 | CHAT_09/10 | P0 ownerUserId/tenantId ↔ workspace_id、IdentityVersion/consent revokeと保存場所T10/T12 |
| WAT-P05 | CHAT_08 | P1 If-Match 412/428、Watch cancel vs retire、projection/ETag |
| WAT-P06 | CHAT_13 | P1 SC-11 X EventSubscriptionのactorId/event identity、SC-12 diff、external eventとschedule triggerKey namespace T08 |
| WAT-P07 | CHAT_12/14 | P1 EntityRefの同一性と明示feedbackがWatch条件を**暗黙変更しない**規則 |
| WAT-P08 | CHAT_00/11 | P1 最短周期/数値上限、new/old scheduler排他の統合E2E、canary policy |

Draftの理由: P0 contract調停未了、実装・実テスト未実施。

## 17. Sources / evidence classification

調査日 2026-10-30。固定SHA、テストは読んだだけで実行していない。

- [S1] Code verified, OpenClaw main 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT, cloneCronJobForMutation/finalizeUpdatedJob: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/jobs-mutation.ts
- [S2] Code verified, OpenClaw cron mutation tests: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service.jobs.test.ts ; restart: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service.restart-catchup.test.ts
- [S3] Code verified, Hermes commit fcc26fbd9f1641755e49793d7ed608f3b7214c3d, MIT, merge_job_definition/import_job_definitions: https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/cron/job_definition.py
- [S4] Code verified, Hermes test paused creation: https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tests/cron/test_atomic_paused_creation.py
- [S5] Code verified, changedetection.io commit 11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b, Apache-2.0, model/Watch.py: https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/changedetectionio/model/Watch.py ; license https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/LICENSE
- [S6] Code verified, pg-boss test transaction: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/scheduleDbTest.ts ; source https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/timekeeper.ts ; MIT https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/LICENSE
- [S7] Hypothesis: WATCH command/definitionRevision/triggerKey/consent revalidationは本Harness独自案で未検証。
- [S8] Unverified: Watch定義の実DDL/REST bindingおよびOpenClawとHermesの現在branch挙動が同時実装で競合しないこと。

### Author review gate

- [x] Scope、Owner、実コード/テストとLicense、代替案、契約/状態/シーケンス/疑似コード、race/権限/費用/移行、16 MUST traceability
- [ ] Cross-team P0の合意・実装側のテスト・独立レビュー（全部未実施）


### Round 2 evidence-driven audit: ownership generation and pause

- **[S9] Issue report**, Hermes cron/session recovery #88688: exact ownership generationを用いた復旧の整合性の議論。現行headで同一不具合が存在するという証明ではない。https://github.com/NousResearch/hermes-agent/issues/88688
- **[S10] PR report**, OpenClaw #167081: recurring commandのrecovery ownerとrunning maintenanceを分離する提案・修正。PRのmerge状態とは独立に、pauseでin-flightを「終了済み」と誤認しない設計の反例として参照。https://github.com/openclaw/openclaw/pull/167081

**追加要件 WAT-020 MUST**: 既存Task/Runを止めずWatchのみPauseしたとき、以降の新規Triggerを禁止しながら、in-flight Runのcompletion/UNKNOWNを**旧definitionRevisionへ帰属**して保存する。Run leaseEpochとの不一致では旧Workerの書込を拒否し、通知先bindingを新revへ勝手に移さない。**WAT-T19 → SC-02/07**: PauseとRun送信中/確認中の3境界を100回故障注入→旧revの結果は正しい履歴に残り、Pause以降の新規Toolは0、結果不明はunknownのまま。テスト未実施。
