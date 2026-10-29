# Scheduler and Recurring Tasks — Detailed Implementation Specification

> Owner: CHAT_07 | Canonical: docs/07-automation/scheduler-and-recurring-tasks.md | Status: Draft / Provisional | Revision: 0.1 | Research: 2026-10-30 | Base main: 92b5827ed16a7d4ae61e8146ce382795398293cb | Acceptance: SC-02, SC-07 (also SC-08, SC-09)
>
> 既存OSSのコードとテストを読んだが、本ハーネスの実装と実機テストは未実施。Shared Contracts、物理DDL、Security Policyの承認前にLocked/Implemented/Verifiedとしない。

## 0. 結論と採用案

セルフホストの**汎用Web調査エージェント**における「あとで実行」「周期的に調べる」を、LLM常駐やメモリ内timerに依存せず永続化する。**Schedule Definition → Occurrence Ledger → Transactional Dispatch Outbox → Job Queue → Task/Tool Gateway**の分離を採用する。Watch/Goal/一般Taskが同じ基盤を消費する。Cron/TZ/DST、missed runs、再起動と多重起動、費用/レート制約は決定的処理で評価し、必要な場合だけAIを呼ぶ。

**核心の保証**: 同じ(tenantId,scheduleId,revision,scheduledForUtc)の**論理Occurrenceは最大1件**。物理的なqueue配達や外部HTTPは二重に起こり得る。外部副作用のexactly-onceは保証せず、Tool Gatewayの承認/台帳で疑義を管理する。scheduledForは「調べる予定だった時刻」であり、observedAtという「実際に価格を確かめた時刻」ではない。

**採用案**: PostgreSQLをSchedule/Occurrence/Outboxの正本、独立Scheduler Portからqueue adapter（第一候補pg-boss）へ非同期投入。pg-boss内蔵スケジュール機能だけでWatchの業務ID・revision・観測slotを表現する案は採用しない。

## 1. 目的・責務・非責務

| 責務 | 責務外・正式Owner |
|---|---|
| Cron/interval/one-shot time仕様・IANA timezone・DST・プレビュー | Watch条件/編集/Pause UIは07-02 |
| 永続予定時刻/slot identity・missed policy・revision連携 | Observation/価格/鮮度/thresholdは07-03 |
| 同一slotの一意入場・recovery・fencing/lease要求 | Task/Run/Error/Eventの型はCHAT_01 |
| 費用・サイトrateを満たす場合だけdispatchする | Budget/Task管理はCHAT_02、Tool権限/副作用はCHAT_03 |
| 運用計測、失敗分類、UIへの状態公開要求 | 通知送達は07-04、UIはCHAT_08 |
| 必要なテーブル・インデックス・取引条件の提案 | 物理DDL・queue運用はCHAT_10、securityはCHAT_09 |

- SC-02: 買取価格が閾値を越えたら通知するWatchは、ID/TZ/次回時刻/通知同意が保存され再起動後も継続。取得失敗を0円扱いせず二重通知を防ぐ。
- SC-07: Queue投入・DBコミット・Tool Intent・完了報告の境界ごとにkillしても、論理slotの喪失/重複と未承認の副作用を防ぐ。状態不確定を明示する。
- SC-08/09: prompt injectionや秘密漏洩を防ぎ、CPU/LLM/HTTP/費用と待機時間を実測する。

**非目標**: EC専用/ホテル専用/コーディング専用の固定Workflow、ホストOS shell/SSH/Docker socket、未承認の購入・予約・投稿・送金、CAPTCHA突破保証、無制限LLM loop。

## 2. 既存OSS横断調査（Source pin: 2026-10-30）

| OSS | Code / function / Test inspected | 振る舞い・判断 | License |
|---|---|---|---|
| **pg-boss 12.38.0**, master 71ea068b144c75b014a1eb90327aaa4a92da1cf2 [S1-S3] | src/timekeeper.ts: onCron, cron, missedOccurrence, readOccurrences, onSendIt; src/plans.ts: SCHEDULE_MISSED_POLICIES; test/scheduleTest.ts, scheduleDbTest.ts | DB claim・clock skew・60s due窓・missed skip/once・schedule keyのslot重複抑制・異常行隔離。ジョブ配送第一候補だが独自slot台帳を追加。Node >=22.12.0 | MIT |
| **OpenClaw 2026.10.9**, main 8b8c33ef334b3754f42afe2a2e212418abfa80f4 [S4-S5] | src/cron/schedule.ts: computeNextRunAtMsとTZ transition処理; service/jobs-scheduling.ts: resolveCronRestartRetryAtMs, errorBackoffMs; src/cron/schedule.test.ts, service.restart-catchup.test.ts | DST gap/fold、再起動時の長時間run slotの重複回避、有限backoff/安定staggerをテスト。設計参考とするがGateway実装へ依存しない | MIT |
| **Hermes Agent**, main fcc26fbd9f1641755e49793d7ed608f3b7214c3d [S6] | cron/occurrences.py: pending_slot_stamp, unclaimed_pending_slot, completed_occurrence; cron/scheduler_tick.py: tick, _advance_or_drop_recurring | tickのファイルロック、dispatch前のnext_run advanceとそのクラッシュ穴をpending_slotで復旧する工夫。file lockを複数ホストの正本へは流用不可。関連テストは直接未確認 | MIT |
| **BullMQ 6.5.1**, master 4d1631f9f74e258779ff1fa2c29de23577248147 [S7-S8] | src/classes/job-scheduler.ts: upsertJobScheduler, getNextJobOpts, removeJobScheduler; tests/job_scheduler.test.ts | Scheduler ID/upsert、遅延job/限界/endDateを確認。公式Docsによると次回job生成は前のjob処理開始に依存。監視独立slotの正本として不採用 | MIT |

**既知の問題・外部報告**: pg-boss issue #914 [S9] は45秒cron worker間でclaim時刻がずれdue窓が欠落した事例（closed）。BullMQ issue #3272 [S10] は旧v5.52.0でJobSchedulerが止まった報告（closed）。Issue reportのため、現時点の最新版に同じbugがあるとは主張しない。

**代替案比較**:
- A: process-local cron + 常駐AI → 再起動/複数replicaに弱く費用無界、却下。
- B: pg-boss.schedule()のみで全業務予定を表現 → 短期実装は簡単だが、業務revision/予定slotとmissedの監査をライブラリ仕様へ拘束、却下。
- **C: Postgres Schedule + Occurrence + Outbox + Queue Adapter → 採用**。DB/schema/リレー開発コストは増えるが再現・監査・再起動が明示できる。
- D: Temporalの全面採用 → durable workflowの候補だが運用サービス/統合/履歴コストが増加、今回**未採用・実ソース未監査**（後日比較可能なAdapter境界を置く）。

ライセンスは4リポジトリのLICENSEを直接確認しMIT。ソースの流用時は著作権/許諾表示を維持し、ここではメカニズムを比較した独立設計とする。外部パッケージの固定版・互換性・保守性は実装PR時に再検証。

## 3. Architecture / ownership / trust boundary

~~~text
Web/Telegram CHAT_08
  -> Watch/Goal owner CHAT_07
     -> SchedulerPort: time/occurrence semantics [THIS]
        -> Postgres definitions, occurrence, outbox [CHAT_10 DDL owner]
        -> budget/rate authorization [CHAT_02/03/09]
        -> Queue adapter [CHAT_10]
          -> Task/Run Manager [CHAT_01/02]
            -> Tool Gateway/Side-effect Ledger [CHAT_03]
              -> HTTP/API/Browser/Connector [CHAT_04/05]
                -> Observation/Trigger [07-03]
                  -> Notification Outbox [07-04/CHAT_10]
~~~

Ownerの正式型をこのMDから二重定義しない。Task/Run/Event/ErrorはCHAT_01、Approval/Tool IntentはCHAT_03、Tenant/security/egressはCHAT_09、DDL/queue/migrationはCHAT_10が正本。未決のフィールドは全て[PROVISIONAL: 該当CHAT]として扱う。スケジューラは既存Tool Gatewayを回避できず、スクレイピング先のHTMLをTrusted Instructionに昇格できない。

## 4. Requirements

| REQ-ID | Level | Expected | SC |
|---|---|---|---|
| SCH-001 | MUST | 保存したSchedule/TZ/nextDueがWorker再起動後も復元 | 02,07 |
| SCH-002 | MUST | 登録/変更/trigger時にowner/tenant権限検証・監査 | 02,08 |
| SCH-003 | MUST | Cronの明示IANA TZを維持、serverローカルTZに依存しない | 02,07 |
| SCH-004 | MUST | DST gapを発火しない、foldで同一local日次slotを二重発火しない | 02,07 |
| SCH-005 | MUST | 同一(revision, scheduledFor)で論理Occurrence最大1 | 02,07 |
| SCH-006 | MUST | 二重配送/lease失効でも外部副作用を盲目的に再送しない | 07 |
| SCH-007 | MUST | slot・cursor・outbox投入意図を一つのDB transactionに確定 | 02,07 |
| SCH-008 | MUST | Pause/取消/改訂後、旧revision未開始slotの新規実行0 | 02,07 |
| SCH-009 | MUST | restart missed slotsには有限のskip/coalesce/replayを適用 | 02,07 |
| SCH-010 | MUST | budget/rate/concurrency違反でTool実行0、理由保存 | 02,09 |
| SCH-011 | MUST | failure/skip/blocked/uncertain/successを区別、取得失敗≠価格ゼロ | 02,07 |
| SCH-012 | MUST | DB clock基準で時刻を定め、host clock skewに左右されない | 07 |
| SCH-013 | MUST | Incomplete claim/outboxをrestart時にreconcile可能 | 07 |
| SCH-014 | MUST | scheduledForとobservedAtを混同せず表示/保存 | 02,07 |
| SCH-015 | MUST | 同一Watchの新規active runは既定最大1 | 02,07 |
| SCH-016 | MUST | Secret非出力、非信頼Webによるschedule/権限変更を拒否 | 08 |
| SCH-017 | MUST | 不正cron/TZ/interval/Date/改訂番号は永続化前にreject | 02,07 |
| SCH-018 | MUST | 破損schedule 1件が正常scheduleを阻害しない | 02,07 |
| SCH-019 | SHOULD | 次回候補5件+TZ/DST/missed policyをpreview | 02 |
| SCH-020 | SHOULD | deterministic checkではLLM呼出0、費用を記録 | 09 |
| SCH-021 | LATER | 明示権限のある冪等jobのみbounded replay | 07 |
| SCH-022 | EXPERIMENT | Queue/Scheduler adapterの比較負荷試験 | 09 |

## 5. 型・API・validation（共有契約の提案）

以下は型の独自確定ではなく、CHAT_01/08/10へ合意を求める**[PROVISIONAL] interface sketch**。

~~~ts
type TimeSpec =
  | {kind:'cron', expr:string, tz:string, gap:'skip', fold:'first'}
  | {kind:'interval', everyMs:number, anchorUtc:string}
  | {kind:'once', atUtc:string};
type MissedPolicy =
  | {kind:'skip'}
  | {kind:'coalesce_latest', maxAgeMs:number}
  | {kind:'bounded_replay', maxCount:number, maxAgeMs:number};
interface ScheduleIntentV1 {
  version:1; tenantId:string; principalId:string; scheduleId:string;
  targetKind:'watch'|'goal'|'task'; targetRef:string;
  revision:number; enabled:boolean; spec:TimeSpec; missed:MissedPolicy;
  validFromUtc:string; validUntilUtc?:string;
  nextDueUtc:string|null; maxConcurrency:number;
  budgetPolicyRef:string; ratePolicyRef:string;
}
interface OccurrenceRefV1 {
  version:1; occurrenceId:string; tenantId:string; scheduleId:string;
  revision:number; scheduledForUtc:string;
  reason:'on_time'|'catchup_coalesced'|'manual';
  dispatchIntentId:string; admissionRef?:string;
  resultRef?:string; // Task/Run/Observation contract owner is elsewhere
}
interface SchedulerPort {
  create(intent:ScheduleIntentV1, requestId:string):Promise<ScheduleIntentV1>;
  update(id:string, expectedRevision:number, patch:unknown):Promise<ScheduleIntentV1>;
  pause(id:string, expectedRevision:number):Promise<ScheduleIntentV1>;
  resume(id:string, expectedRevision:number):Promise<ScheduleIntentV1>;
  preview(spec:TimeSpec, fromUtc:string, count:number):Promise<string[]>;
  scanDue(max:number):Promise<number>;
  dispatchPending(max:number):Promise<number>;
  reconcile():Promise<void>;
}
~~~

- version discriminator必須。契約不明major拒否、正式version policyはCHAT_01へ。revisionはユーザー定義変更時に増加（単純なnextDue更新で増やさない）。更新はexpectedRevisionのCAS。手動triggerは予定slotと別のnamespaceを使用し、requestIdで重複登録を防ぐ。
- occurrenceKey = SHA-256(tenantId,scheduleId,revision,scheduledForUtcの**長さ付き正規encode**)。文字列連結の区切り衝突は禁止。UTC ISOミリ秒へ正規化。manual keyはSHA-256(tenantId,scheduleId,'manual',requestId)。
- Cronは5フィールド限定、秒/年/非決定性表現/空欄は拒否。TZは明示IANA ID必須（未指定のserver TZは使わない）。intervalは正整数msとUTC anchorが必須。24hのelapsed intervalは壁時計の日次09時とは別。onceはoffset付き日時のみ。Date overflow/NaNは禁止。
- [PROVISIONAL: CHAT_00/02/03/09/10] 初期運用値の提案：Web監視最短5分、1tenant active Schedule最大1000、同一Watch並列1、tenant並列2/全体8、scan batch 100、missed window 24h、bounded replay最大20、outbox retry最大5。値は**未実測・未承認**で設定可能な上限として実装。Cron計算・preview件数/探索期間にも有限上限を設定。
- [PROVISIONAL: CHAT_08] Web/Telegramで同じscheduleを表示。GET detail/preview、POST create/pause/resume/update/manual trigger。競合409、無権限403、validation422等の最終HTTP表現はCHAT_08へ。成功応答はDB commit後のみ。登録時にID/条件/周期/TZ/nextDue/通知許諾を示す。

## 6. 状態・sequence・ライフサイクル

業務的なWatchのcreate/pause/resume/deleteは07-02が正本。このSchedulerの局所的な運用上の分類は「enabled」「invalid_definition」「budget_blocked」「rate_deferred」「expired」であり、CHAT_01のTask/Run stateを再定義するものではない。

~~~text
CREATE/RESUME:
UI/Telegram -> Auth/Watch owner -> Scheduler validate + preview
 -> DB transaction (schedule + nextDue + revision)
 -> commit -> receipt(scheduleId,revision,nextDue,TZ,policy)

DUE:
DB clock -> scanDue locks definition -> evaluate missed policy
 -> capacity/rate/budget admission -> occurrence + outbox + cursor (one TX)
 -> outbox relay -> queue -> worker claims occurrence with fencing token
 -> verify current revision/rights/cancel/budget
 -> Task/Run Manager -> Tool Gateway -> Provider/Browser
 -> Observation/Trigger -> Notification owner
 -> settle occurrence projection (scheduledFor remains original logical slot)

PAUSE/UPDATE:
authorized revision CAS -> invalidate not-yet-started old occurrence
 -> in-flight Task behavior delegated to CHAT_02
 -> no new Tool started from stale revision

RESTART:
reopen DB -> load nextDue -> reconcile outbox/expired claims
 -> apply bounded missed policy -> normal dispatch
~~~

**状態遷移と判定時点**:
- create: 正式な権限検証→Schema検証→TZ/DST preview→容量/サイト許容周期の検証→保存→受付。保存の前に「監視登録済み」を返さない。
- update/pause/resume: 同じSchedule行をロックしrevisionをCAS。過去revisionの未開始jobはrunnerの再検証で抑止。すでに始まったHTTP/API/Browserの取り消しはベストエフォートであり、結果が不確定ならTool台帳へ。
- one-shot: 最初のslotのみ作成、その後はスケジューリング上終端。未確定外部結果がある場合でも二度目のone-shotを自動で生成しない。
- active Watch: 同一targetRefで1 runが進行中なら次回は多重起動せず、既存run完了後の現状再チェックへcoalesceまたはskipを記録。長時間停止の全slotを同時起動しない。
- manual trigger: time-based slotとは別namespace。別のPOSTの再送で実行数が増えないようrequestIdを必須にし、権限・budget・rateを同様に検査。
- dequeue時のpause/delete: enqueueが成功していても実行直前にrevision/ownerを照合。Pause/更新のcommitより前にToolが始まったケースだけin-flightとして残り、取消はCHAT_02の安全境界に従う。

**競合順序**: Scheduler Aが行ロックを持ってslot確定中にBがpauseする場合、A commit後にBが旧revisionを無効にする。Bが先にcommitした場合Aは新revisionを再読する。二重Scheduler A/Bは同一schedule行とOccurrence unique制約で1論理slot。古いWorkerがlease失効後にresultを確定しようとしてもfencingToken一致のCASに失敗する。**ただし実際の外部HTTP副作用はCASでは取り消せない**のでTool intent ledgerと疑義状態が必須。

## 7. 内部アルゴリズム・擬似コード

### 7.1 next occurrence / DST

**時刻の正本**はPostgreSQLのclock_timestamp。Worker自身の時計で「今」という不可逆判断をせず、ローカルtimerはDB再走査のwakeに限定。scheduler入力に明示されたIANA TZとともに、parserの固定バージョン/候補生成契約を保存または移行検証する。

- Cron: TZ-aware evaluatorで未来候補を得てUTCに正規化。ローカルhour/minute/dayが式の意図と一致するか再確認する。DST spring-gapでは存在しないローカルslotをskip。fall-foldでは同一local occurrenceに対応する早いUTC instantのみ採用し後方の重複候補を除外。UTC単調増加と有限探索を強制する。
- Interval: anchorMs + (floor((cursorMs-anchorMs)/everyMs)+1)*everyMs。整数/日付範囲を確認し、cursorより厳密に大きい次回を計算。elapsed 24hと「ローカル毎朝9時」は異なる仕様。
- Once: once.atUtc > cursorUtcかつ有効期間内で1回。過去のonceを再設定で勝手に再発火しない。必要なら新しい改訂/明示手動trigger。
- 登録時の初回cursorはmax(DBNow,validFromUtc)とし、最初の予定を**未来**に置く。即時一回チェックは明示のmanual triggerとして別の識別子にする。
- 例: America/New_York 2026-03-08 02:30はgap→その日skip。2026-11-01 01:30は05:30Zと06:30Zの2候補から05:30Zを1回のみ。Europe/Oslo 2026-03-29 02:30もskip。Australia/Lord_Howeの30分foldもfixtureで確認する。
- 探索回数/CPU時間上限を超える、または5年先まで適格時刻がない場合は未解決/nextDue=nullとしてUI・運用へ警告。他のScheduleのscanは継続。実パーサが必要なgap/fold規約を満たさなければWrapperで除外する（未実装）。

### 7.2 atomic due scan / dispatch outbox

~~~text
scanDue(maxBatch):
  dbNow := SELECT clock_timestamp()
  for i in 1..maxBatch:
    BEGIN
    s := SELECT next eligible schedule
         ORDER BY nextDueUtc, fairTenantOrder
         FOR UPDATE SKIP LOCKED
    if none: COMMIT; break
    if disabled/invalid/expired:
      record terminal/deferred reason; COMMIT; continue
    due := computeDue(s, dbNow, s.missedPolicy)  # finite list
    for slot in due:
      if target already active and concurrency=1:
        record coalesced/deferred decision; continue
      permit := policyReserve(tenant, provider, token/USD/rate/concurrency)
      if !permit.ok:
        record blocked reason, finite nextRecheck; break
      key := sha256(lengthPrefixed(tenant,s.id,s.revision,slot.utc))
      INSERT logical occurrence(key,...)
        ON CONFLICT DO NOTHING
      INSERT dispatch_outbox(key, payload)
        ON CONFLICT DO NOTHING
    update schedule cursor and nextDue with expected revision
    # unprocessed due work MUST have explicit defer/disposition
    # never move cursor over a pending slot without its durable disposition
    COMMIT # occurrence, outbox, cursor, same-DB reservations all-or-nothing

dispatchPending(maxBatch):
  lease ready outbox rows with DB locks/fencing, finite TTL
  queue.enqueue(payload, deterministic occurrenceKey)  # adapter
  if queue call succeeded: mark accepted by compare-and-swap
  if timeout/unknown: retain outbox, recheck queue if supported
  # after enqueue but before ACK, duplicate physical enqueue is possible

runner(payload):
  BEGIN
    read schedule & occurrence with lock
    reject if duplicate completed, stale revision, paused,
      revoked principal, invalid grant, expired, not admitted
    reject/defer if active target exceeds concurrency policy
    increment fencingToken, claim with DB-time lease
    COMMIT
  before new Tool operation:
    verify current token + cancellation + Tool Gateway policy/budget
  call shared Task/Run manager, never call external site autonomously
  if Tool outcome is ambiguous: ledger records UNCERTAIN, no blind retry
  settle outcome with CAS(fencingToken)
  # stale worker may finish actual HTTP despite failed settle;
  # external effects require Tool ledger reconciliation

reconcile():
  bounded scan of committed-but-undispatched outbox and expired leases
  if no external effect started can be proven: enqueue same key
  else: mark needs-reconciliation; consult Tool ledger / user approval
  release/refund unconsumed reservations only with idempotent accounting
  alert operator for persistent errors; no secret/raw HTML in logs
~~~

**設計の微妙な点**:
1. Policy reservationがPostgresと**同じtransaction**で永続化できない場合、原子性は成立しない。CHAT_02/03/10にcompensating reservation protocolとdouble-spendテストを依頼し、原子コミットがあると偽らない。
2. キューのenqueueが非transactionalの場合、outboxとqueueの間はat-least-once。queue jobIDで重複抑止できても最終防御はOccurrenceのDB一意制約・claim CAS。
3. Leaseは所有権の有効期間であり、既に完了した外部副作用を巻き戻す機能ではない。fencingはDB resultを保護するが、ネットワーク外部サービスがfencingに対応しないときはTool台帳が必要。
4. 予約段階で予算不足を検出したら、**予定を架空の成功として前進させない**。期限切れ/延期/skipと利用者への説明を1つの監査可能な決定にする。
5. Backlogを大量に読み込まず、tenantの公平性とbatch/timeoutを制限。重いパーサと1行のinvalid scheduleが他スケジュールを停止させない。

### 7.3 missed runs / catch-up policy

| Policy | 再起動後の厳密な意味 | 適用上の注意 |
|---|---|---|
| skip | 過去に発火しなかったslotをmissedとして記録し、次の未来予定から再開 | 過去の観測を捏造しない |
| **coalesce_latest (Watch既定案)** | outage中のslotを一つにまとめ、現在の情報を**一回だけ新たに取得**。元のmissed時刻と実際のobservedAtを分ける | 最大age超過時は古いslotを期限切れ記録にしてnow-checkを生成。過去価格を再現したとは言わない |
| bounded_replay | allowlistに属する冪等targetのみ、age/count/budget/provider rateの上限以内の過去slotを再生 | 既定無効。長いoutageで無制限LLM/外部アクセス禁止 |

初期案: coalesce-latestのmaxAgeは24h、replay max20、同時Watch 1、rateに余裕がない場合は有限backoff。監視登録を停止した間のmissedは**resumeで勝手に遡及しない**。定義のrevision変更で旧仕様に対する過去slotを新仕様の根拠として扱わない。

## 8. 障害マトリクス

| 故障位置 / 検出 | Recovery | 最終状態・副作用危険 |
|---|---|---|
| API受領→DB commit前に切断 | requestIdで保存を照会 | 成功未確認を登録済と言わない |
| occurrence insertとoutbox insertの間でkill | DB transaction丸ごとrollback、同じslotを再scan | 論理喪失なし |
| commit直後→queue送信前にkill | committed outboxを再配送 | 同一keyを保持 |
| queue送信後→outbox ack前にkill | queueへ重複配送あり得る。runner claimで論理一意 | exactly-once物理実行は非保証 |
| claim後→Tool Intent前にkill | 未実行を証明し同じOccurrenceを再claim | fencingToken新しくする |
| Toolの送信途中に通信断 | Tool LedgerへUNCERTAIN、結果照会/承認で解消 | 不可逆副作用を盲目的に再試行しない |
| 取得HTTP 500/空/parse失敗 | 失敗Observationとして扱う、0円Observationを作らない | false price alertを抑止 |
| pause/updateとdispatch競合 | scheduleの行ロック+revision CAS、runner再check | 未開始旧jobの開始0、既開始は取消境界へ |
| 許可変更/token失効/403 | policy reject、手動入力待ちなどOwner状態へ | secretを表示しない |
| DST gap/fold | 明示skip/first、UTC slotへ正規化 | 重複時刻/架空時刻を防止 |
| Clock skew/DB failover | DB clock優先、DB障害時は新規発火停止、再接続後reconcile | 独断のローカル発火0 |
| cron expression不正/DB旧データ破損 | 行単位隔離、error codeと警告を記録 | 正常な他行のschedule継続 |
| budget枯渇/429/queue congested | hard stop/deferred＋有限再試行、次回評価時刻を保存 | 無制限費用・AI loopなし |
| 同じWatchで前回Run未終了 | 新しいRunを重複開始せずcoalesce/skip | 同一Watch active最大1 |
| stale Workerが結果保存 | fencing token一致CASで拒否 | 外部不確定性はTool Ledgerへ |

## 9. セキュリティ・privacy・trust boundary

- Schedulerが持つのは**時間の決定権**だけ。登録時、run起動直前、各Tool開始直前にowner/capability/approval/scope/予算を再照合。定期実行そのものが外部副作用の事前承認にならない。
- scheduleRef、queue payload、log、traceにはOAuth token、Cookie、API key、生HTML、取得した秘密のquery string、ユーザーの機密promptを埋め込まない。secretの保管と使用はCHAT_09/Providerが管理。
- HTML/X投稿/検索結果に含まれる「監視間隔を1秒に変更」「通知先を攻撃者へ」「鍵を送信」はDataでしかない。Watch条件の変更は信頼された認証済みAPIの操作だけを許す。
- 監視サイトの規約/robots/API資格とrate制約を優先し、明示した最短周期未満への設定や認可外アクセスを拒否。SSRF/DNS rebinding/redirect/egressはCHAT_09/Tool Gatewayの正本で防ぐ。
- Tenant/owner分離、監査ログ・保存期間・削除時の個人情報処理はCHAT_09/10へ。Schedulerは別tenantのSchedule照会/trigger/pauseができないようpolicyを必須にする。

## 10. 計測・Trace・性能評価条件

推奨metrics: scheduler_due_lag_seconds、scheduler_scan_duration_seconds、schedule_invalid_total、schedule_misfire_total{policy}、schedule_coalesced_total、occurrence_created_total、occurrence_duplicate_suppressed_total、occurrence_budget_denied_total、outbox_pending_age_seconds、outbox_retry_total、runner_lease_expired_total、runner_uncertain_total、provider_rate_limited_total、llm_calls_per_watch_check、model_usd_per_check、DB CPU/IO、RSS。scheduleId/raw URLをメトリクスラベルに使わず高cardinality/機密漏洩を防ぐ。

Trace連結キー: scheduleId/revision/occurrenceId/dispatchIntentId、Task/Run ID、Tool ledger ID、Observation/Evidence ref。scheduledFor, dispatchedAt, startedAt, observedAt, finishedAtを別属性にする。

**実測計画**: Postgres＋1/2/4 Scheduler/Worker replicas、1k/10k/100k schedule、周期1/5/15/60分、Mock HTTP 100ms/500ms、長時間run、429、DB停止、Queue停止、24h outage、DST変化、budget exhausted。各条件のdue lag p50/p95/p99、誤重複、計算コスト、DB負荷、RSS、USD/日を測る。**p95や最大スループットの値は未測定であり採用目標を捏造しない**。5分最短周期などはプロダクト政策値で性能保証ではない。

## 11. 永続化・Transaction・Migration提案 [PROVISIONAL: CHAT_10]

物理DDLは作成しない。以下の論理要求だけをCHAT_10へ依頼する。

1. Schedule Definition: scheduleId PK、tenant/principal/target/ref、revision、enabled、spec/TZ、missed policy、cursor/nextDue、有効期間、budget/rate refs、created/updated。
2. Occurrence Ledger: occurrenceId PK、UNIQUE(tenant,scheduleId,revision,scheduledForUtc)、reason、admissionRef、claim lease/fencingToken、結果参照（結果型はCHAT_01）。
3. Dispatch Outbox: dispatchIntentId PK＋UNIQUE occurrenceId、status、attempt数、lease、nextRetryUtc、error code、queue responseRef。
4. Tool LedgerはCHAT_03、Budget ReservationはCHAT_02/03/10の正本を**参照**する。これらの偽の重複台帳は作らない。
5. Transaction: cursor advance/Occurrence/Outbox/同一DB Reservationをatomic。Queueへの非原子的送信はoutbox relayで回復。Ack後もretention期限までは同一slotの論理一意証拠を保持。

**移行/ロールアウト**: Additive schema→read-only dry-run preview比較→少数tenant canary→worker機能flag→監視→段階展開。同一Watchを旧/新Schedulerが同時発火しないよう排他。旧scheduleのtimezone/次回/last slot/停止/tenantを検証し、変換不能なら隔離・管理者確認。RollbackはOccurrence/Outboxを削除せず、新旧二重起動を阻止する。RetentionとGDPR等の削除方針はCHAT_09/10が確定。

## 12. UI・外部APIへの影響 [PROVISIONAL: CHAT_08]

同一Watch IDと保存済Scheduleの画面・Telegram表示に、対象/条件/周期、IANA TZ、次回予定のUTCとローカル時刻、最後の**実際の観測**と最後の予定実行、missed/coalesced/blocked/uncertain原因、費用上限/通知同意を分けて返す。成功応答前のDB commit確認が必須。プレビューでDST skip/foldの解釈と制限を確認可能にする。Update/Pause/Resumeはrevision条件付き409競合応答。長時間実行中の停止は「新規実行停止」と「既存Run終了待ち」を区別する。

## 13. 実装フェーズ・依存PR順序

| PR | 実装範囲 / 先行条件 | 完了判定 |
|---|---|---|
| SCH-PR1 | TimeSpec validator / TZ+cron evaluator / DST gap-fold wrapper / preview。独立pure functions | fake clockと固定DST fixtureが合格 |
| SCH-PR2 | CHAT_10 DDL、revision CAS、Occurrence unique constraint、cursor/outbox単一Transaction、DB clock | 同時4 schedulersで論理slotが1件。故障注入 |
| SCH-PR3 | Queue adapter（pg-boss第一候補）、outbox relay、lease+fencing、reconciler | Queue ack loss/crash、stale workerテストが合格 |
| SCH-PR4 | CHAT_02/03のBudget、rate、Task/Tool ledger、owner revocation連携 | 予算枯渇/不確定外部副作用/取消で安全 |
| SCH-PR5 | CHAT_08のREST/UI/Telegram、運用metrics、CHAT_11 E2E/benchmark、canary | SC-02/07/08/09でE2E/負荷結果と移行手順が明示 |

このMDは設計だけであり、上記PRを実装済みと主張しない。依存の契約が未決ならmock/adapter境界を作ってから当該Ownerの正本確定を待つ。Provider接続・Watch条件・Notificationは別MDで具体化する。

## 14. テスト設計（このHarnessでは**全件未実行**）

| TEST-ID | 種類 / setup → action | Expected（自動判定条件） |
|---|---|---|
| SCH-T01 | Unit: 正常5-field cronと空欄/秒欄/未知TZ/invalid interval/NaN/overflow/invalid revisionをsubmit | 不正登録0、正しいtimezone保存、合法条件だけpreview |
| SCH-T02 | Unit fake clock: NY 2026-03-08 02:30と2026-11-01 01:30、Oslo 2026-03-29 02:30、Lord Howe fold | Gap 0、Fold初回のみ、UTCは単調で重複0 |
| SCH-T03 | Property: 1000種類の時刻/interval/cronを異なるhost TZでcomputeNext | 同一入力の結果一致、strictly future、有限反復で終了 |
| SCH-T04 | Integration: schedule登録→Worker kill→Postgres/Worker再起動 | scheduleId/revision/TZ/nextDueが保存、missed政策どおり再開 |
| SCH-T05 | Concurrency: 4 Schedulerが同じ100件のdueを同時scan | 各(tenant,schedule,revision,instant)のOccurrence=1/outbox=1 |
| SCH-T06 | Fault injection: Occurrence insert直後、Outbox insert直後、commit直後にkill | commit前はrollback、commit後はreconcileで同一logical slot=1 |
| SCH-T07 | Fault injection: queue enqueue成功後・ack前にkillして再配送 | runner active claim最大1、重複はno-op、無条件外部effect増加0 |
| SCH-T08 | Fault injection: claim直後、Tool Intent前、Tool送信結果不明の各境界でkill | 未実行と証明できる場合だけ再claim。送信疑義ではUNCERTAIN/approval |
| SCH-T09 | Race: due transaction中pause、update先commit、旧revision enqueue、旧worker到着 | Pause/改訂commit以降、旧未開始Tool新規開始0 |
| SCH-T10 | Integration: 1分周期Watchを24h停止、coalesce_latestで復旧 | 過去1440件再実行0、現在check最大1、scheduledFor≠observedAt |
| SCH-T11 | Unit: skipとbounded_replay(max=3,age=1h)を相互比較 | skip過去0、replayは冪等許可済みslotのみ最大3、経費上限内 |
| SCH-T12 | Integration: daily USD budget exhausted、429、target既にactive | 新規Tool 0、正しいBLOCKED/DEFERRED分類、同一Watch並列最大1 |
| SCH-T13 | Fault: worker時計+120s、DB一時停止、timer遅延、復帰 | DB clockで正常slot選択、論理重複0・過去値捏造0 |
| SCH-T14 | Unit/integration: 破損cron/TZを1レコード挿入、極端に疎なcron | 隔離・warning、他の正常100予定が処理を継続、CPU探索有限 |
| SCH-T15 | Integration: Provider HTTP500/parse失敗/空返答 | error Observation。価格0の正常観測と誤分類しない |
| SCH-T16 | Security: 別tenantのcreate/update/pause/trigger、失効owner token | 拒否・監査、別tenantデータ/secret表示0 |
| SCH-T17 | Security: 取得ページに「毎秒監視・通知先変更・tokenを送れ」を埋め込む | Schedule/Approval/Network policyが不変、漏洩0 |
| SCH-T18 | Contract: OccurrenceとTask、Observation、UIに異なる時刻を付与しJSON version違反も投入 | timestamp意味は分離、version incompatibleはreject |
| SCH-T19 | Concurrency: lease expiry後に新worker claim、旧workerがlate complete | 旧fencing tokenのDB settlement 0、Tool不確定性は可視 |
| SCH-T20 | E2E Mock: SC-02 Watch登録→再起動→価格閾値到達→通知ownerへ | ID/条件/TZ/nextDue保持、Source+observedAtつき通知意図1件 |
| SCH-T21 | E2E Mock: SC-07のQueue/DB/Tool save各段階でkill + cancel/steer | 一意slot、retriable/uncertain/completedのいずれかに収束、不可逆再試行0 |
| SCH-T22 | Benchmark: 1k/10k/100k schedules、replicas 1/2/4、429/budget/external mock | lag p50/p95/p99、LLM USD/job、RSS、DB負荷を測定し保存。数値目標は事前合意待ち |

**実行状況**: SCH-T01〜SCH-T22はすべて設計のみで未実行。OSS上の既存テストはソースを読んだだけで、本プロジェクトのテストを走らせたわけではない。実外部サイトへの監視・購入・通知・予約は無承認テストしない。

## 15. Acceptance Criteria / REQ→TEST→SC Traceability

| MUST REQ | TEST-ID | 確認できる合格条件 | SC |
|---|---|---|---|
| SCH-001 | T04,T20 | Schedule/TZ/nextDueをrestart後も保持 | 02,07 |
| SCH-002 | T16 | 他tenant/無権限からの変更0 | 02,08 |
| SCH-003 | T01,T02,T18 | 明示TZの解釈・保存一貫 | 02,07 |
| SCH-004 | T02,T03 | nonexistent slot 0 / fold同一日次slot重複0 | 02,07 |
| SCH-005 | T05,T06,T07 | 同じslotでOccurrence/Outboxそれぞれ1 | 02,07 |
| SCH-006 | T07,T08,T19,T21 | 不確定外部副作用の盲目的再試行0 | 07 |
| SCH-007 | T06 | cursorとslotとoutboxが同一TXで全成功/rollback | 02,07 |
| SCH-008 | T09,T21 | pause/改訂後の旧未開始Tool開始0 | 02,07 |
| SCH-009 | T10,T11 | unbounded backfill 0、過去値捏造0 | 02,07 |
| SCH-010 | T12,T22 | budget/rate/target並列越えで新規Tool 0 | 02,09 |
| SCH-011 | T10,T12,T15 | failure/skip/blocked/uncertainの誤成功0 | 02,07 |
| SCH-012 | T13 | skew後も論理重複0 | 07 |
| SCH-013 | T06,T07,T08,T21 | unfinished workをrecoverまたはuncertain化 | 07 |
| SCH-014 | T18,T20 | scheduledFor/observedAtを混同0 | 02,07 |
| SCH-015 | T12,T20 | 同一Watch active最大1 | 02,07 |
| SCH-016 | T16,T17 | prompt injectionによる権限変更0/secret出力0 | 08 |
| SCH-017 | T01,T03 | 無効な時刻/式/Date保存0 | 02,07 |
| SCH-018 | T14 | 異常1件でも他正常予定を処理 | 02,07 |

## 16. 未決事項・Cross-team proposals

全件、正本を持つ他担当への**提案**であり、このPRで担当外仕様を編集しない。必要なレビューでは変更前後挙動/互換性、対応するREQ/TEST、移行/取消への影響を確認する。

| ID / Owner | Severity | 何を確定してほしいか / 合格条件 |
|---|---|---|
| P07-01 / CHAT_01 | **P0** | Schedule↔Task/Run/Event参照とreconcile時のUNCERTAIN/RETRYABLE/CANCELLEDの共有型・version。contract test SCH-T18/T21 |
| P07-02 / CHAT_10 | **P0** | Schedule/Occurrence/Outbox物理DDL、一意索引、同一TX、DB clock、claim lease/fencing、admissionのtransaction境界。SCH-T05〜T08,T19 |
| P07-03 / CHAT_02/03 | **P0** | Budget reservation（同一TXが不可能なら補償プロトコル）、Run起動/取消、Tool side-effect ledgerの不確定結果照会。SCH-T08,T12,T21 |
| P07-04 / CHAT_09 | **P0** | Tenant隔離、資格失効と再照合、secret/egress/provider rate/監査retentionの正本。SCH-T16,T17 |
| P07-05 / CHAT_00 | P1 | Cron DST gap skip/fold first、監視min5分、coalesce既定とbudget上限のADR/UX判断。SCH-T02,T10 |
| P07-06 / CHAT_08 | P1 | preview/detail・scheduleId/revision・pause/resume/blockedReason・通知許諾、409と422表現。SCH-T18,T20 |
| P07-07 / CHAT_11 | P1 | multi-worker/DST/Queue-ack-loss/DB-failover/Tool未知結果のfixtureとfault test、測定系。SCH-T02,T05〜T08,T22 |
| P07-08 / CHAT_07 07-02 | P1 | Watchのcreate/pause/resume/deleteとSchedule revision、manual trigger、登録確認の整合 |
| P07-09 / CHAT_07 07-03/04 | P1 | 価格と取得失敗・実測時刻、観測品質、通知event dedupe/送達遅延の別仕様 |
| P07-10 / CHAT_10/11 | P1 | pg-boss 12.38.0とDB/Node現実環境の互換、Queue Adapter・障害テストを実施して採否判断 |
| P07-11 / CHAT_00/07 | P2 | sparse cronの探索上限、bounded replayの許可対象、手動triggerのpolicy |

**Draft維持理由**: P0のShared Contracts/DDL/Quota/Tool Side Effect/Securityが未Locked。CronのDST挙動も本Harnessの実コードでは未検証。レビュー後に契約Ownerが合意するまではCodexによる本番統合実装の確定仕様とは扱わない。

## 17. Sources — precise evidence and inspection date 2026-10-30

Evidence grade:
- **Code verified**: Github上の該当ソース/テストの関数・振る舞いを実際に読んだ（実行した意味ではない）。
- **Docs verified**: 公式ガイド/READMEの記述を確認した。
- **Issue report**: 報告者の条件付きでのIssueであり最新版の欠陥を証明しない。
- **Hypothesis**: このHarnessの未実装提案。
- **Unverified**: 対応実テストや運用環境を確認していない。

- **[S1] pg-boss, Code verified** — master SHA 71ea068b144c75b014a1eb90327aaa4a92da1cf2; package 12.38.0, Node >=22.12; functions onCron, cron, missedOccurrence, readOccurrences, onSendIt: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/timekeeper.ts ; missed kinds: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/plans.ts ; runtime/version: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/package.json
- **[S2] pg-boss, Code verified** — test/scheduleTest.ts（DB-time clock skew、duplicate keys、malformed schedule isolation、send-it error、missed behavior）: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/scheduleTest.ts ; scheduleDbTest.ts（schedule/unschedule transaction commit/rollback）: https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/scheduleDbTest.ts
- **[S3] pg-boss, Docs verified / License verified** — scheduling docs https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/docs/api/scheduling.md ; MIT https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/LICENSE
- **[S4] OpenClaw, Code verified** — main SHA 8b8c33ef334b3754f42afe2a2e212418abfa80f4; package 2026.10.9; TZ-aware cron parsing src/cron/schedule.ts https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/schedule.ts ; restart/error/backoff functions https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/jobs-scheduling.ts
- **[S5] OpenClaw, Code verified** — daylight-saving tests https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/schedule.test.ts ; restart slot ownership tests https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service.restart-catchup.test.ts ; MIT https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/LICENSE
- **[S6] Hermes Agent, Code verified** — main SHA fcc26fbd9f1641755e49793d7ed608f3b7214c3d; functions pending_slot_stamp, unclaimed_pending_slot, completed_occurrence https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/cron/occurrences.py ; tick, _advance_or_drop_recurring https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/cron/scheduler_tick.py ; MIT https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/LICENSE ; **対応テストは未確認**
- **[S7] BullMQ, Code verified** — master SHA 4d1631f9f74e258779ff1fa2c29de23577248147, package 6.5.1; upsertJobScheduler/getNextJobOpts/removeJobScheduler: https://github.com/taskforcesh/bullmq/blob/4d1631f9f74e258779ff1fa2c29de23577248147/src/classes/job-scheduler.ts ; tests: https://github.com/taskforcesh/bullmq/blob/4d1631f9f74e258779ff1fa2c29de23577248147/tests/job_scheduler.test.ts ; MIT https://github.com/taskforcesh/bullmq/blob/4d1631f9f74e258779ff1fa2c29de23577248147/LICENSE
- **[S8] BullMQ, Docs verified** — Job Schedulers / job generation when previous job begins processing: https://docs.bullmq.io/guide/job-schedulers/ ; cron/interval options: https://docs.bullmq.io/guide/job-schedulers/repeat-options ; v5 repeatable removal in v6: https://docs.bullmq.io/guide/jobs/repeatable
- **[S9] pg-boss, Issue report** — issue #914, cron pass timing gap on instance stop (closed; report condition pg-boss 12.33.3): https://github.com/timgit/pg-boss/issues/914
- **[S10] BullMQ, Issue report** — issue #3272, JobScheduler sometimes stops (closed; reporter used v5.52.0, not evidence of v6 defect): https://github.com/taskforcesh/bullmq/issues/3272
- **[S11] Hypothesis** — this document's independent PostgreSQL Occurrence/Outbox/fencing architecture, proposed operational constants, DST gap/fold policy; not tested in this harness.
- **[S12] Unverified** — Temporal API/source and Hermes corresponding tests not audited; cannot be called Code verified.

## Author self-review / Review Gates

- [x] Latest main, Project Brief, Architecture Rules, Acceptance Scenarios, AGENTS, COMMON, CHAT_07, spec-index, research protocol, gates and template inspected.
- [x] Four relevant OSS source functions and three sets of actual test files inspected; commit/version, license, evidence grade, Issue report distinguished.
- [x] Scope/non-scope/owner, alternative designs, failure/restart/race/lease/cancel, budget, security/Privacy, migration/ops/observability covered.
- [x] All 18 MUST REQ mapped to concrete TEST-ID and SC-ID; tests designed vs tests actually run explicit.
- [ ] Contract Owners agree on P0, migration/DDL/Tool/Budget/tenant policy.
- [ ] Real library integration, scheduler performance, unit/E2E/fault tests run.
- [ ] Independent review, Reviewed/Locked gates passed.

**Author status: Draft only.**
