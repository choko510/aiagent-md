# Observations and Trigger Evaluation — Detailed Implementation Specification

> Owner: CHAT_07 | Path docs/07-automation/observations-and-trigger-evaluation.md | Status: Draft / provisional | Research 2026-10-30 | main b45d4e989998663a00ef09673e69ae1f4191f00d | SC-01,02,03,07,08,09,10,11,12,14,16
>
> **Code verified**は既存OSSのコード/テストの調査レベル。ここで設計する実装は存在せず全TESTは未実行。共有Observation/Evidence/Event型、物理DB、Source reliability/Entity matchingは正本Ownerの合意待ち。

## 0. 採用設計

「価格が45,000円以上」「前回より5%以上下がった」「Xの投稿を検証して条件成立」といったユーザー条件を、LLMの推測ではなく**純粋で再実行可能な決定的評価器**で判定する。入力はproviderから得た**成功した構造化Observation**だけで、失敗、未解析、古いサンプル、価格不明を「0」「変化なし」「条件不成立」に変換しない。

持続状態は、(a)immutable Observation envelope、(b)accepted baseline/reference、(c)predicate version/arming state、(d)unique Trigger Decision ledger。通知はここで直接送らず07-04へEvent intentをcommitする。Watch定義の変更は07-02のdefinitionRevisionで遮断し、結果のarrival orderとobservation source timestampの順序を別に扱う。

**厳守**: 観測がsource/published timeより前に「現在確認済み」と扱われない。キャッシュヒット、過去履歴、X投稿の噂、予測利益、画像からの低信頼抽出は**リアルタイム確認された価格**ではない。異なるSKU・容量・通貨・税・条件を同じseriesへ誤統合しない。

## 1. 目的/責務/非目標

| Owner内の責務 | 境界・非責務 |
|---|---|
| Observationの評価入力と品質検査、freshness、baseline選択、threshold/crossing/hysteresis | 共有Observation/Evidence正式型はCHAT_01、抽出はCHAT_04/05、画像はCHAT_15 |
| deterministic state machine、triggerKey/decisionKeyの一意性、unknown/partial status | Opportunity/リスク調整利益はCHAT_12、X/HTML diff正本はCHAT_13 |
| Trigger Intent→Task またはNotification Outboxへの許可済み引渡 | Scheduleの時間決定07-01、Watchの状態/権限07-02、配達07-04 |
| 時刻・単位・出典と警告のUI投影 | 物理DB CHAT_10、REST CHAT_08、tenant CHAT_09 |

SC-02価格Watch、SC-12構造化値の意味的差分、SC-11 X投稿のon-event評価、SC-10 cross-source SKU整合、SC-14 reliability、SC-16ユーザー優先ルールが主要対象。SC-01利益比較に必要な型番/送料/販売条件の確認も必須だが利益算出自体はCHAT_12-04。非目標は生HTML全文のAI判定を最終triggerの確定根拠にすること、広告の文字列diffを価格変動とすること、サイトアクセス規約の回避、送金/注文。

## 2. OSSコード調査（2026-10-30）

| OSS/pin/license | 読んだファイル/関数・テスト | 採否 |
|---|---|---|
| **Huginn** master 2b1eb20cb657b86ca52378c75fc4176a3815c524, MIT [S1,S2] | app/models/agents/trigger_agent.rb: receive/matches?/keep_event?、attribute_difference_agent.rb: handle/calculate_integer_difference/calculate_decimal_difference/calculate_percentage_change/last_value/update_memory、spec/models/agents/trigger_agent_spec.rb / attribute_difference_agent_spec.rb | eventをpredicateで選別し前回値と差分を比較する分離を参考。Agent memory一件だけではout-of-order/Revision/tenant/Observation鮮度保証が足りないため再実装 |
| **changedetection.io** master 11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b, Apache-2.0 [S3,S4] | changedetectionio/conditions/default_plugin.py: contains_regex、length_min/length_max等のoperator、notification_service.py: watch_will_send_content_changed_notification、notification diff formatting、tests/unit/test_conditions.py、tests/test_conditions.py | condition plugin・変更通知と表示を分離する参考。正規表現はReDoS対策、通知見出しから事実判定しない。価格型/単位は独自に厳密化 |
| **pg-boss** master 71ea068b144c75b014a1eb90327aaa4a92da1cf2, MIT [S5] | src/timekeeper.ts のmissedOccurrence、test/scheduleMissedTest.ts のskip/once、outage/last pass/slotテスト | time-based dueとObservationが別であることを確認。取得失敗と変化なしの意味論はここには無いため本Checkerが担当 |
| **OpenClaw** main 8b8c33ef334b3754f42afe2a2e212418abfa80f4, MIT [S6] | src/cron/service/notification-intents.ts / notification-dispatch.ts、source schedule/test | condition成立から配達に至る処理を別段にする参考。通知transportの結果は値の真偽を証明しない |

**比較**: A) 生ページ/自然言語を毎回LLM判定→確率的で費用・注入リスク高、**却下**。B) 受信順のlatest valueだけを比較→late replayで偽crossing、**却下**。C) Versioned immutable observation + quality gating + deterministic predicate state + unique trigger intent→**採用**。D) 変化検知をHTML diffのみに依存→広告/日時に弱く、SC-12ではCHAT_13構造化差分へ委譲。ライセンスを確認せずsourceをコピーしない。

## 3. 依存・正本

~~~text
Connector/Search/Browser CHAT_04/05, Image CHAT_15
  -> extraction with source evidence (CHAT_01/04/05)
  -> immutable Observation ingress [THIS validation, CHAT_01 schema]
  -> entityId/link version CHAT_12-02 + reliability CHAT_12-05
  -> Watch definition revision CHAT_07-02
  -> baseline + predicate state [THIS]
  -> deterministic Evaluator (no model call)
       -> trigger decision + outbox atomic [CHAT_10]
       -> optional bounded research Task [CHAT_02]
       -> notification intent [CHAT_07-04]
       -> ranking/digest [CHAT_13-03] (never delete required alert)
~~~

他OwnerのEntity/Observation/Event schemaは[PROVISIONAL: CHAT_01/12/13]。tenantIdとownerUserIdはCHAT_01/09が正本、workspace_id mappingはCHAT_10。Watch triggerKeyは07-02、Outbox送信先は07-04。Run.leaseEpochやTool approvalは本評価器の認可トークンではない。

## 4. Requirements

| REQ | Level | 検証可能な期待値 | SC |
|---|---|---|---|
| OBS-001 | MUST | 成功値、取得失敗、抽出不明、未確認、データ欠落を区別 | 02,12 |
| OBS-002 | MUST | ObservedAt/sourceAt/fetchedAt/scheduledFor/publishedAtを混同しない | 02,03 |
| OBS-003 | MUST | numeric decimal+currency+unit+condition/SKUが比較可能なときだけ比較 | 01,10 |
| OBS-004 | MUST | stale/expired/cache-only観測はfresh threshold confirmedを発火しない | 02,14 |
| OBS-005 | MUST | above/below/equals strict/inclusive の境界が純粋関数で再現可能 | 02 |
| OBS-006 | MUST | crossingは有効な前後の比較可能な値がある場合にだけ成立 | 02,12 |
| OBS-007 | MUST | hysteresisで閾値前後の微振動を多重発火させない | 02 |
| OBS-008 | MUST | unknown/failureではbaseline/armed stateを更新しない | 02,07 |
| OBS-009 | MUST | late/out-of-order Observationを新しいものとして上書きしない | 07,11 |
| OBS-010 | MUST | 同じObservation/definitionRevision/predicateのTrigger決定は一意 | 02,07 |
| OBS-011 | MUST | Watch pause/retire/revision変更/owner revoke後、新規通知/Tool 0 | 07,08 |
| OBS-012 | MUST | Evidence source URL + observedAt + extraction/identity confidenceを保持 | 01,02,14 |
| OBS-013 | MUST | 価格0と「取得できなかった」を混同しない | 02 |
| OBS-014 | MUST | 追加研究へ委譲する際、budget/approval/identityの範囲を超えない | 09,11 |
| OBS-015 | MUST | 多源データの矛盾はconfirmedとして融合しない | 10,14 |
| OBS-016 | MUST | 同一外部Event re-deliveryから通知Intent重複0 | 11,16 |
| OBS-017 | SHOULD | trigger理由、比較値、鮮度/信頼度、失効条件を表示 | 02 |
| OBS-018 | LATER | 複数期間比較とrolling aggregateをpolicy付きで導入 | 14 |
| OBS-019 | EXPERIMENT | Entity resolution/reliability重みの偽陽性テスト | 10,14 |

## 5. 型/契約/validation（提案、共有型はCHAT_01）

~~~ts
type ObservationResult = {
  schemaVersion:"1.0"; observationId:string; tenantId:string;
  watchId:string; definitionRevision:string;
  origin:"scheduled"|"external-event"|"manual";
  sourceRef:string; evidenceRef:string|null; providerEventRef?:string;
  entityRef:string; entityMappingRevision:number|null;
  scheduledForUtc:string|null;
  fetchedAtUtc:string; observedAtUtc:string|null; sourceAtUtc:string|null;
  outcome:"valid"|"not_modified"|"unavailable"|"extraction_failed"|"access_denied"|"unknown";
  price?: {minorUnits:string; currency:string; unit:string; condition:string; taxBasis?:string};
  freshness:"current"|"stale"|"historical"|"unverified";
  confidence:{source:"verified"|"reported"|"unknown"; extraction:"verified"|"estimated"|"unknown"};
  contentHash:string|null; maxAgeMs:number;
};
type Predicate =
 | {kind:"price_at_least", thresholdMinorUnits:string, currency:string}
 | {kind:"price_at_most", thresholdMinorUnits:string, currency:string}
 | {kind:"cross_up"|"cross_down", thresholdMinorUnits:string, currency:string}
 | {kind:"band_hysteresis", triggerMinorUnits:string, rearmMinorUnits:string, direction:"up"|"down"}
 | {kind:"structured_change", fieldRef:string, operator:"changed"|"increased"|"decreased"};
type Decision =
 {kind:"fire"|"no_match"|"unknown"|"stale"|"suppressed"|"needs_research";
  reasonCode:string; observationRef:string; previousRef:string|null;
  definitionRevision:string; triggerKey:string|null; evidenceRefs:string[]};
~~~

正式Observation/Evidence envelopeとEventはCHAT_01の正本に合わせて変換する。価格は浮動小数のJSON numberでなく**正規化された整数最小単位文字列**を提案（JPY=整数、USD=cent等、ISO通貨exponentを検証）。異なる通貨は直接比較不可、換算が必要なら同一時刻の根拠あるFX観測を別Evidenceとして保存（ユーザーの条件が固定JPYなら勝手な換算禁止）。容量/色/型番/商品の状態/購入条件が異なれば別系列。Entity matchingの判断はCHAT_12のversioned mappingを参照し、unknown matchはconfirmed triggerを作らない。

**時間**: sourceAtは情報元の公称時刻、fetchedAtは取得、observedAtは実際に値を確かめた時刻（valid時のみ設定）、scheduledForはSchedulerの予定、publishedAtはEvent側の発信時刻。ソースが日時を示さないときsourceAt=nullとし、架空に補完しない。maxAgeMsは有限政策値、0<=observedAt<=DB現在+許容clock skew。未来source timestampや期限切れEvidenceはuncertain。

**Baseline比較**: 同じ(tenant,Watch,definitionRevision,entityRef,mappingRevision,currency,unit,condition,field)の有効系列に限定。out-of-order到着はfetchedAt/DB ingest sequenceとsource orderを分け、単なる遅延Responseで最新baselineを書き換えない。revision変更直後は旧baselineを自動で新条件へ継承せず、明示migration/seed policy（初回crossingは発火しないのが既定）を採用。

## 6. シーケンスと状態遷移

~~~text
Provider/Browser -> immutable Observation (content hash & provenance)
 -> validate outcome+unit+freshness+owner+revision+entity mapping
 -> DB transaction: lock current predicate cursor
       -> load prior comparable accepted baseline
       -> evaluate threshold/crossing/hysteresis
       -> persist decision + (if valid) new accepted baseline / armed state
       -> if fire, insert unique Trigger Intent & Notification/Research Outbox
    commit -> downstream Worker rechecks Watch/owner/consent/approval
~~~

Predicateの状態（正式共有Stateではなく内部局所状態）:
- NO_BASELINE: まだ有効比較値がない。初回valid Observationはbaseline seed。単純level判定は条件によりfire可能、crossingは不可。
- ARMED: 逆方向のrearm条件を満たし次のcrossingを待機。
- FIRED_UNARMED: 同じ条件が続く間は再通知しない。hysteresisのrearm境界を正しく越えたときだけARMEDへ。
- SUPPRESSED_STALE / UNKNOWN: 判断を保留する**評価結果**でありbaseline/arming stateを変えない。
- BLOCKED_POLICY: Watchが停止/認可失効・budget不足ならnotificationを生成しない。取得自体のToolは上流policyで遮断。

## 7. 決定的アルゴリズム/擬似コード

~~~text
evaluate(obs, watchDef, expectedRecordVersion):
  validateObservationEnvelope(obs)
  begin transaction
  w := lock Watch(tenantId,watchId)
  if w.status != ACTIVE or w.definitionRevision != obs.definitionRevision:
    record SUPPRESSED_STALE_DEFINITION; commit; return
  if owner revoked or consent invalid:
    record BLOCKED_POLICY; commit; return
  if observation decision uniqueKey already exists:
    return persisted decision with exact same triggerKey
  if obs.outcome != valid or obs.price absent:
    insert Decision(unknown or failure, reason); commit; return
  if stale(obs.observedAtUtc, policy.maxAge, sourceAt, evidence):
    insert Decision(stale); commit; return
  if !sameIdentityAndUnits(obs,w.expectedEntity) or !sourceTrustAllowsConfirmed(obs):
    insert Decision(needs_research); commit; return

  cursor := loadAndLockAcceptedBaseline(seriesKey)
  if obs sequence <= cursor.lastAcceptedSequence:
    insert Decision(suppressed, OUT_OF_ORDER); commit; return

  predicate := loadImmutablePredicate(watchId,definitionRevision)
  if predicate.kind = price_at_least:
    matched := obs.value >= predicate.threshold  # exact integer arithmetic
    # Repeat policy: level every sample OR once-per-episode, user-configured.
  else if predicate.kind = cross_up:
    matched := cursor.value exists and cursor.value < threshold and obs.value >= threshold
  else if predicate.kind = band_hysteresis:
    if cursor.armed and obs.value >= trigger: matched=true; armed=false
    else if !cursor.armed and obs.value <= rearm: armed=true
  else if structured_change:
    matched := validated semantic field change (CHAT_13 structured diff)
  record decision, update baseline+armed on accepted VALID
  if matched and admission/owner/consent permits:
    triggerKey := stableHash(tenant,watchId,definitionRevision,predicateId,obs.id)
    INSERT TriggerDecision ON CONFLICT DO NOTHING
    INSERT Event/Notification/Research outbox same DB TX
  commit; return decision
~~~

**比較規約例**:
- 45,000 JPY level-at-least: 44,999→45,000で成立（>=）、45,000→45,000はユーザーがrepeat-every-observationを明示しない限り一度のepisode。
- crossing-up 45,000: 初回45,001は前値不明なので**未確定crossing**。44,000→45,000で成立。unknown→45,000ではunknownがbaselineを変えないため前の有効44,000からcross可能（ただしmax gap/age policyが許す場合のみ）。
- hysteresis up trigger 45,000 rearm 44,500: 45,100で1発→45,010でなし→44,600でもなし→44,500でrearm→45,000で2発。trigger>rearmの検証必須。
- 逆方向cross_downでは above previous > threshold and now <= threshold、hysteresis downは trigger<rearm。初回baselineのseed政策を明示する。
- 取得失敗→値なし：**baselineもrearmも変えない**。明確な価格0のvalid Observationは外部根拠があり0円を許容するProvider fieldに限って値として評価。通常買取価格の0はvalidity policyで再確認扱いでもよいが、その判断を黙って失敗へ転換しない。
- event/snapshot late arrivals: older baselineを上書きしないが監査用Evidenceは記録する。sourceAtが新しいか不明な場合はfetch sequence+source-specific ordering policyを明記し、強い時系列保証のないsourceに「crossing確定」を許さない。

## 8. 障害とRecovery

| Fault | Detector/handling | User result |
|---|---|---|
| HTTP timeout/403/429 | outcome unavailable/access_denied、finite retryはProvider ownerへ | failure/blocked、値0扱いなし |
| parse extractor returns null/NaN/negative unsupported | extraction_failed/unknown、前baseline不変 | unknown+reason |
| stale cached sample | freshness guard、必要なら新fetch予約 | stale、confirmed不発 |
| source timestamp future or missing | clock/time provenance validation、unknown | unverifiable |
| provider returns different currency/SKU | entity/unit fail closed、manual review or CHAT_12 resolver | needs_research |
| old observation delivered after new | watermark/cursor CAS、decision記録、baseline不変 | out_of_order |
| same observation processed twice | unique decision key + TX outbox | fire 1 / notification intent1 |
| crash after decision+before outbox | same transaction rollback/retry same key | loss/dup logical0 |
| outbox ACK lost | relay retry、notification owner dedupe | exactly-once delivery主張しない |
| Watch update during evaluation | lock + definitionRevision recheck | old result新閾値に転用0 |
| owner revoke/consent withdraw after commit | send-time policy recheck 07-04 | suppressed/uncertain |
| daily cost depleted | no additional research invocation; record budget blocked | partial/blocked |
| regex catastrophically slow | fixed parser/resource budget、kill/suppress offending condition | invalid predicate |

## 9. Security/Privacy

取得物のHTML/X投稿/添付PDFは非信頼Data。LLMが「閾値を引き上げる」「認可された出力先へ転送する」など提案してもWrite Commandにはならない。TypeGuard/Validatorとowner/tenant/revisionを送信先DBで再照合。SSRF/redirect/DNS lock、API limits、CredentialのマスクはCHAT_04/05/09の正本。Condition regexは入力最大長/計算budgetを設定、無限ループと任意scriptを禁止。外部Providerの「金額」や「商品同一性」信頼度は出典・抽出・最終確認を別fieldにして、根拠がないものはconfirmedとしない。秘密・個人情報をObservation raw payloadに不必要保存しない。

## 10. 計測・負荷と費用

Metrics: obs_ingested_total{outcome}, obs_stale_total, obs_invalid_total{reason}, obs_compare_mismatch_total, obs_out_of_order_total, trigger_fired_total{kind}, trigger_suppressed_total{reason}, trigger_duplicate_prevented_total、latency ingest→decision、LLM calls per accepted observation、USD/watch/day。ラベルに生URL/商品名/WatchIDは入れない。TraceはObservationId、sourceRef、definitionRevision、baselineRef、decisionId、triggerKeyHash、Task/notification outbox refを辿れるようにする。

初期暫定上限: maxAgeは監視周期ごとの有界値・Owner設定依存。正規表現pattern 512文字、入力抽出本文64KiB、1watch連続履歴のホットbaselineは1件（全履歴はDBに別保存）、evaluation CPU 100ms/観測、Research retry最大1回。数値は未実測で[PROVISIONAL: CHAT_00/11]。Benchmarkは1k Watch×100観測、10% late/429/unknown、複数Worker、p95 CPU/DB/false-positiveとUSDを記録し合格閾値をADRで固定。

## 11. 永続化/Transaction/Migration [PROVISIONAL: CHAT_10]

必要論理概念: immutable_observation(id,tenant,watch,definitionRevision,entityVersion,sourceRef,evidenceRef,sourceAt,observedAt,fetchedAt,outcome,value/unit/confidence)、accepted_baseline(tenant,watch,revision,seriesKey,lastObservation,lastSequence,armed,recordVersion)、trigger_decision(tenant,watch,revision,observationId,predicateId,decision,reasonCode,triggerKey)、outbox(notification/research intent)。同じObservationId+PredicateRevisionにunique制約、同じtriggerKeyは1件。baseline CAS、decisionとoutbox insertの1 DB TX。DBの物理索引/partition/retentionはCHAT_10の正本、Source reputationはCHAT_12-05が正本で重複表を作らない。

Migration時、以前の無単位float価格から暗黙にcents換算せず、通貨/単位が確認できない旧履歴はunverified。旧lastValueを新predicateへのcrossing baselineとして転用禁止。新baselineをseedし表示に「再監視開始」を示す。Canaryで比較精度、rollbackで旧notification workerと二重稼働を防ぐ。

## 12. API/UI契約提案 [PROVISIONAL: CHAT_08]

Watch detailのlastCheckedAtとlastValidObservedAtを分離、最後に成立したtriggerのsource URL/date、previous/current value、通貨/条件、hysteresis armed stateはprivacy-safe summaryで表示。Data unknown、stale、failed、reliability低、sourceAt不明を区別する。crossingで初回baselineしかないときに「条件成立」と断言しない。通知は07-04がdelivery idと明確な送達Statusを返す。

## 13. 実装PRフェーズ

OBS-PR1: exact money/time/unit validators + baseline pure evaluator + fixture。OBS-PR2: immutable Observation ingestion・series identityとtransactional baseline/decision/outbox、migration。OBS-PR3: 07-02 Watch revision/07-01 occurrence/CHAT_13 event bridge、CHAT_12 identity/reliability adapter。OBS-PR4: UI/notification subscription、Integration/Fault/Security tests。OBS-PR5: benchmark/canary/rollback。予算上限は導入前に合意。

## 14. TEST設計（全て未実施、Mock）

| ID | Setup/action | Expected |
|---|---|---|
| OBS-T01 | HTTP500→valid45000→unknown | failure/nullとvalid値の区別、failed=0への変換0 |
| OBS-T02 | sourceAt, fetchedAt, observedAt, scheduledForを別値に設定 | 公開出典時刻が観測/予定と混同しない |
| OBS-T03 | JPY/USD、容量128/256GB、条件新品/中古 | 異なるseries比較・confirmed0 |
| OBS-T04 | TTL超過cache sample/future sourceAt | stale/unknown、fire 0 |
| OBS-T05 | 44999,45000,45001、inclusive/strictを単体テスト | 境界で仕様通りFire |
| OBS-T06 | no baseline→45001、44000→45000 | 初回cross 0、後者1 |
| OBS-T07 | 45100→45010→44500→45000（rearm44500） | hysteresis fires正確2 |
| OBS-T08 | valid44000→error→valid45000 | baseline errorで不変、cross判定1 |
| OBS-T09 | source newer先到着→older遅延 | baseline最新維持、遅延fire 0 |
| OBS-T10 | 4 Worker同一Observationが同時評価 | 1 decision、1 outbox、同一triggerKey |
| OBS-T11 | Watch pause/revision/owner revoke between compare+outbox | 新通知/Tool 0 |
| OBS-T12 | no origin URL/sourceAt, old reliability score | confirmedに昇格しない、evidence label |
| OBS-T13 | invalid price NaN / genuine valid0 fixture | unknownとvalid0区別 |
| OBS-T14 | provider data contains malicious command and requests extra browsing | policy変更0、無認可Tool0 |
| OBS-T15 | 2 sources conflicting same entity, late/unverified mapping | confirmed profit/event alert0 |
| OBS-T16 | 5x provider event replay & duplicate queue ACK loss | logical Notification intent1 |
| OBS-T17 | UI preview/detail conditions/results | unknown/stale/failed/confirmedを正しく表示 |
| OBS-T18 | fault injection TX crash before/after decision+outbox | all-or-nothing、duplicate0 |
| OBS-T19 | benchmark 100k Observation, 10% late/failure across 4 Workers | latency/false alert/DB CPUとUSDを計測、達成値未定 |

## 15. REQ→TEST→SC traceability

| MUST | TEST | SC | 判定 |
|---|---|---|---|
| 001 | T01,T13 | 02,12 | 失敗と値ゼロ分離 |
| 002 | T02 | 02,03 | 時刻欄混同0 |
| 003 | T03 | 01,10 | 異単位/異SKU誤比較0 |
| 004 | T04 | 02,14 | stale確定fire0 |
| 005 | T05 | 02 | 境界一致 |
| 006 | T06,T08 | 02,12 | crossing前有効値必要 |
| 007 | T07 | 02 | episode抑止 |
| 008 | T08 | 02,07 | 失敗はbaseline不変 |
| 009 | T09 | 07,11 | old到着書換0 |
| 010 | T10,T18 | 02,07 | 1 decision/outbox |
| 011 | T11 | 07,08 | 旧/停止したWatch fire0 |
| 012 | T02,T12 | 01,14 | 根拠/時刻保持 |
| 013 | T13 | 02 | no-value≠zero |
| 014 | T14 | 09,11 | 無認可追加研究0 |
| 015 | T15 | 10,14 | conflictを事実に昇格0 |
| 016 | T16 | 11,16 | replay重複0 |

## 16. 未決事項 / Cross-team proposals

| ID | Owner | Severity / proposal and verification |
|---|---|---|
| OBS-P01 | CHAT_01/10 | P0 shared Observation/Evidence/Eventとnullable time/status、immutable id + unique decision DDL。T01/T10/T18 |
| OBS-P02 | CHAT_12 | P0 entityId+mappingRevision、reliability/independent sources、不一致時のconfirmed閾値。T03/T15 |
| OBS-P03 | CHAT_13 | P1 structured HTML change fieldRef、external Event dedupとTriggerKey、sourceAt vs receivedAt。T16 |
| OBS-P04 | CHAT_02/03/09 | P0 bounded Research Task authority/rate/budget、Tool security、owner recheck、reconciliation。T11/T14 |
| OBS-P05 | CHAT_07 07-02/04 | P0 definitionRevision更新時baseline invalidation + Notification intent共通dedupe。T07/T11 |
| OBS-P06 | CHAT_08 | P1 stale/unknown/observedAt/valid price値のUI表示、source link、安全なHTML |
| OBS-P07 | CHAT_00/11 | P1 hysteresis/TTL/CPU/runtime初期上限とproperty/fault/accuracy fixture、baseline migrationのADR |

## 17. Sources — pinned and graded (2026-10-30)

- [S1] **Code verified**, Huginn master 2b1eb20cb657b86ca52378c75fc4176a3815c524, TriggerAgent receive/matches? https://github.com/huginn/huginn/blob/2b1eb20cb657b86ca52378c75fc4176a3815c524/app/models/agents/trigger_agent.rb ; AttributeDifferenceAgent calculate_decimal_difference/last_value/update_memory https://github.com/huginn/huginn/blob/2b1eb20cb657b86ca52378c75fc4176a3815c524/app/models/agents/attribute_difference_agent.rb
- [S2] **Code verified, test cases inspected** Huginn tests: https://github.com/huginn/huginn/blob/2b1eb20cb657b86ca52378c75fc4176a3815c524/spec/models/agents/trigger_agent_spec.rb ; https://github.com/huginn/huginn/blob/2b1eb20cb657b86ca52378c75fc4176a3815c524/spec/models/agents/attribute_difference_agent_spec.rb ; MIT https://github.com/huginn/huginn/blob/2b1eb20cb657b86ca52378c75fc4176a3815c524/LICENSE
- [S3] **Code verified**, changedetection.io 11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b, conditions operators https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/changedetectionio/conditions/default_plugin.py ; notification filtering https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/changedetectionio/notification_service.py
- [S4] **Code verified, test cases inspected**, changedetection.io tests https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/changedetectionio/tests/unit/test_conditions.py ; license Apache-2.0 https://github.com/dgtlmoon/changedetection.io/blob/11c7ca8719bfc59a4ffa8cd06dd5d0c5d090a82b/LICENSE
- [S5] **Code verified**, pg-boss 71ea068b144c75b014a1eb90327aaa4a92da1cf2, schedule missed tests https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/test/scheduleMissedTest.ts ; algorithm https://github.com/timgit/pg-boss/blob/71ea068b144c75b014a1eb90327aaa4a92da1cf2/src/timekeeper.ts ; MIT
- [S6] **Code verified**, OpenClaw 8b8c33ef334b3754f42afe2a2e212418abfa80f4, notification intent contract https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/notification-intents.ts ; delivery https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/cron/service/notification-dispatch.ts ; MIT
- [S7] **Hypothesis**: 本ハーネスのdecimal baseline/hysteresis/receipt/out-of-order/triggerKey仕様は独立提案でありテスト未実施。
- [S8] **Unverified**: Providerの取得費用/権限/TTL、基準価格の算定、変更履歴の保存性能は測定・確認待ち。

### Review-gate check

- [x] 16 MUSTにTEST/SCを対応づけた。source level、alternative、failure/restart/race、quota、prompt-injection、DDL/REST proposalとmigration含む。
- [ ] 共有Contract Lock、実テスト・Benchmark、独立レビュー。Draft維持。


### Round 2 negative evidence: false changes and requested thresholds

- **[S9] Issue report**, changedetection.io #879 (closed、旧版ユーザーの要望): 単なるテキスト変更では数値価格が閾値を超えたか判定できないとの具体的ユースケース。最新版で未対応と断言する根拠ではない。https://github.com/dgtlmoon/changedetection.io/issues/879
- **[S10] Issue report**, changedetection.io #962 (closed、旧版): 同一内容でも空diffの通知が発火したとの報告。比較品質とnotification outboxを分ける反例。https://github.com/dgtlmoon/changedetection.io/issues/962
- **[S11] PR report**, changedetection.io #3824: タグごとのrestock settingsをAPIから設定する変更提案。正式な型や現在の動作保証ではなく設定世代とgroupingの考慮事項。https://github.com/dgtlmoon/changedetection.io/pull/3824

**追加要件 OBS-020 MUST**: 正規化された構造化値が等しいのに、HTML広告/日時/DOM差分だけの理由でprice threshold triggerを発火しない。**OBS-T20 → SC-02/12**: fixture A/BでHTML timestamp/rotating adだけ変更しprice=45,000を維持→meaningful price change=false、episode中の再通知0。なお「毎日同条件なら知らせる」といったユーザー明示ルールは07-04/13のpolicy別評価。テスト未実施。
