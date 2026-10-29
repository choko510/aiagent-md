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
