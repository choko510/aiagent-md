# Trend Fusion — Detailed Implementation Specification (12-03)

> Owner: CHAT_12 | Canonical path: docs/12-intelligence/trend-fusion.md | Status: Draft / PROVISIONAL rev 0.1 | Research: 2026-10-30 | Production implementation: none; all tests designed/not executed.
> Dependencies: CHAT_01 Evidence/Observation/Event, CHAT_05 X/Keepa/buyback, CHAT_07 Observation/Watch, CHAT_09 policy, CHAT_10 storage, 12-02 entity identity, 12-04 risk, 12-05 source reliability.

## 0. 採用設計
同一対象の「X言及が増えた」「販売価格が変わった」「買取額が動いた」「サイト構造が更新された」を、**証拠と観測時刻の異なる系列**として扱う。入力をUTCのevent timeに正規化→dedupe/repost collapse→同一性versionと上流相関でグループ化→各sourceのsampling/lag/coverageを算出→有限windowで各系列の変化を検出→独立sourceからの確認を注記してTrendAssessmentを作る。投稿が増えたから値上がりした、という因果推論はしない。データ疎・sourceの遅延・情報品質が落ちた場合はUNKNOWN/LOW_COVERAGEのまま出す。高スコアだけで監視/通知/購入を自動開始しない。

## 1. 目的・適用/非目標
対象: 複数時間粒度のX posting, Keepa prices, buyback quotes, site change signals, independent official announcements, general Web topic signals。対象のEntityRefが異なる場合は意図して比較しない。変動が意味のある情報かを示すのが目的で、統計的な因果性・利益保証・投資判断・全X投稿の完全取得を目的としない。
SC-01/02/03/05/07/08/09/10/11/12/14/16。X Account Trigger自体のcursor/streamはCHAT_13、X取得はCHAT_05、HTML変更判定はCHAT_13-02の正本。Trendは正規化済みObservationのみ消費する。

## 2. 公式資料とOSS実ソース比較
|先行実装|実ソース/テスト|選択理由と制約|
|---|---|---|
|River ADWIN|online-ml/river@af064928 river/drift/adwin.py の ADWIN.update(AdaptiveWindowing backend)、tests/drift/test_adwin_regression.py の constant stream / rapid shift / multi-drift tests|online stream driftを検知できるが規則的な時刻/欠損や同一上流情報の独立性を保証しない。BSD-3; benchmark candidate|
|River PageHinkley|同commit river/drift/page_hinkley.py PageHinkley.update/_test_increase/_test_decrease|平均の片側/両側変動を検知。ADWINとの差を事前fixtureで評価。ドリフト判定を「真実」と扱わない|
|Evidently|evidentlyai/evidently@34771fe src/evidently/legacy/calculations/data_drift.py get_one_column_drift, tests/calculations/test_data_drift.py (empty column errors); metrics/dataset_statistics.py|reference vs current windowでdistributionを比較。legacy APIには注意、現行APIそのまま統合は保留。Apache-2.0|
|X公式 search/stream|https://docs.x.com/x-api/posts/search/introduction (recent search過去7日)、https://docs.x.com/x-api/posts/filtered-stream/introduction (matching public posts stream)|APIが返す範囲は契約・フィルタ・poll cadenceに依存。streamで見えないPostを0件とは証明できない|
|単純X投稿件数/販売価格差|実装容易|転載・欠測・検索クエリ変更で誤trend。却下|
|LLMで投稿と価格の因果を説明|一見分かりやすい|共通原因と観測biasを検証できない。因果的断定として却下|

2026-10-30静的調査。River issue #1914 はドリフト評価ハーネス拡張提案（Issue report）、検証結果ではない。Evidently旧Docsの0.6.7以前のAPIを現行として採用しない。公式Xの記述するレイテンシーは製品保証値ではなく、当利用者のentitlement/実測が必要。

## 3. 権限とオーナー
- CHAT_01: Observation/Evidence/Event shared schema + attribution。12-02: entity relation revision。12-05: source quality axes。12-04: risk/profit計算。CHAT_05: X/price connectorsとactual rate/entitlement。CHAT_07: Watch/Notification、CHAT_13: event ingress and diff。CHAT_10: ledger/outbox。CHAT_09: tenant/owner/source policy。Trend内部のaggregation/method/seriesは本MDのロジックであり共有Eventを勝手に追加しない。
- Provider credentialを直接受け取らない。各Input Observationは事前のPolicy/Evidence validationを経て渡されるが、publish前に再度owner/goalRevision確認。

## 4. 安定したRequirements
|REQ|Priority|検証可能な契約|
|---|---|---|
|TRND-001|MUST|Inputにtenant/owner,sourceRef,evidenceRef,entityRevision,metric,unit,quality,source/observed/fetched timeを要求|
|TRND-002|MUST|series同一性をentityId+revision+metric+unit+scopeで決定し別容量/地域/通貨を混ぜない|
|TRND-003|MUST|dedupはorigin post ID、引用/リポスト/転載group、provider observation keyを別管理|
|TRND-004|MUST|event-timeとingest-timeとnotification-timeを分離、late/out-of-orderを安易に最新に上書きしない|
|TRND-005|MUST|sourceごとに可用/不可/偏ったsampling windowとcoverage/unknownを保持|
|TRND-006|MUST|系列密度不足またはbaseline不在ではtrend direction UNKNOWN、0値を作らない|
|TRND-007|MUST|時間粒度alignmentはUTC half-open bucket、time zone/freshness/lagをメタに残す|
|TRND-008|MUST|異常値検出前にtyped unit・price precision・消費者条件/conditionRevisionをvalidate|
|TRND-009|MUST|短期間の価格/言及変動を、それぞれ検出し上流独立性の欠如を明示|
|TRND-010|MUST|scoreの式・閾値・window・異常値処理・versionを保存し未校正ならscore UNKNOWN|
|TRND-011|MUST|相関を因果関係として示さず、独立source evidenceと反証を併記|
|TRND-012|MUST|Source Reliability/Entity mapping改訂時に下流AssessmentをSTALE・再計算|
|TRND-013|MUST|取得失敗、429、window漏れ、API範囲外をtrue zero postsとみなさない|
|TRND-014|MUST|遅延データのwatermark/revisionにより古い結果を上書きせず再計算履歴を維持|
|TRND-015|MUST|再送、worker crash、duplicate event、late correctionを冪等なreceipt/CASで扱う|
|TRND-016|MUST|cancel/Steer/revoke後、新集計/通知意図を出さない|
|TRND-017|MUST|Trend engineが直接投稿/購入/Watch登録、無許可Scrapingを実行しない|
|TRND-018|MUST|上限（window/buckets/sources/CPU/requests）を持ち、過負荷でbounded partial result|
|TRND-019|SHOULD|偽陽性率・drift・precision@alertをsource/category別に再校正|
|TRND-020|LATER|季節性/曜日変動と連続時系列モデルを十分なbackfill後に導入|

## 5. 内部型・Validation（共有契約追加は提案）
~~~ts
type Metric='post_count'|'price'|'buyback_price'|'availability'|'site_change_count'|'verified_announcement_count';
type PointV1={schemaVersion:'1.0'; tenantId:string; ownerUserId:string; sourceId:string; evidenceId:string; sourceObservationId:string; entityId:string; entityRevision:string;
 metric:Metric; unit:string; valueDecimal:string|null; quality:'OBSERVED'|'ESTIMATED'|'UNKNOWN'; originalId:string|null; upstreamRootId:string|null;
 sourceAt:string|null; observedAt:string|null; fetchedAt:string; validUntil:string|null; sourceProfileRevision:string|null; scopeDigest:string; sampling:{kind:'COMPLETE'|'FILTERED'|'SAMPLED'|'UNKNOWN'; denominator:number|null; queryRevision:string|null}};
type BucketV1={startUtc:string; endUtc:string; count:number|null; sumDecimal:string|null; observedCoverageBps:number|null; missingReason:string|null; originClusters:number; points:number};
type TrendAssessmentV1={schemaVersion:'1.0'; tenantId:string; entityId:string; entityRevision:string; metricSet:Metric[];
 algorithmVersion:string; inputDigest:string; windowStart:string; windowEnd:string; watermark:string;
 status:'TREND'|'NO_CLEAR_CHANGE'|'LOW_COVERAGE'|'UNKNOWN'|'STALE'; direction:'UP'|'DOWN'|'FLAT'|'UNKNOWN';
 scoreBps:number|null; independence:'INDEPENDENT'|'CORRELATED'|'UNKNOWN'; sourceProfileRevisions:string[];
 evidenceIds:string[]; counterEvidenceIds:string[]; samples:number; missingBuckets:number; biasWarnings:string[]; computedAt:string};
~~~
valueDecimal: null on UNKNOWN; priceはdecimal string→通貨minor unitsをRisk/CHAT01の正本validatorで変換、IEEE floating money禁。input maximum 64KiB/record、time ISO UTC、unknown major reject、unit/currency mismatch reject、source IDs validated, ownership enforced。post Countは実際の取得範囲のcountであって全ネットワーク分母ではない。

## 6. シーケンスと状態遷移
~~~mermaid
sequenceDiagram
 participant C as Connectors/Events(05,13)
 participant O as Evidence(01)
 participant T as TrendFusion(12)
 participant S as Reliability(12-05)
 participant D as DB/outbox(10)
 C->>O: provenance + source timestamps
 O->>T: validated observations
 T->>D: dedup + bucket watermark checkpoint
 T->>S: reliability and upstream origin revision
 S-->>T: quality + dependence
 T->>T: align + change + confidence gate
 T->>D: assessment receipt CAS + stale history
 D-->>T: persisted version only
~~~
COLLECTING→ALIGNED→{LOW_COVERAGE,ANALYZING}→{TREND,NO_CLEAR_CHANGE,UNKNOWN}。late data/old source profile/new relation mapping→STALE→RECALCULATING→new immutable revision。cancel/revokeでCANCELLED、既存Event事実は保持し新しい通知を作らない。TRND statusはTask/Run状態の再定義でない。

## 7. 時間・重複・計算アルゴリズム
1. ScopeKey = hash(length-prefixed(tenant,entityId,entityRevision,metric,unit,region,condition,queryRevision))。同値でもquantity/condition/source windowが異なれば別。
2. Ingress pointはunique(tenant,sourceId,sourceObservationId)で冪等。X postsはroot post ID、retweet/quoteの引用数・原投稿数を区別。転載判定が曖昧なら独立に加算せずCORRELATED_UNKNOWN。元post削除は新tombstone event、過去値を静かに消さない。
3. UTC bucket [start,end)で時間格子を作り、24h windowの提案は1時間格子、7d windowは1日格子。sourceAtが不明なら観測時刻の代理代入をせず、observedAtのみのシリーズとして「取得系列」と注記。
4. Watermarkはsource別lastCompletedBucketEnd−configuredAllowedLateness、allowed lateness初期提案2h。window end > watermarkならINCOMPLETE。長期遅延は新revisionとしてbackfillし、過去の速報を上書きしない。
5. count data: 完全取得/分母妥当なbucketだけゼロを数値0として扱う。API結果0でもquery/filter/author entitlement・ページ完全性が欠けたらcount=null。価格は同一通貨/identity rev/条件でだけ差分計算し、線形補間で架空価格を作らない。
6. Source featureごとbaselineとcurrent両窓にmin3 valid buckets（運用提案）必要。計測窓のminimum coverage 80%は仮閾値。未達→LOW_COVERAGE。固定値の外れ値検知はmedian/MAD、MAD=0は別安定系列扱い。単発の買戻し価格0やbot repost burstだけでTRENDにしない。
7. 各Metricは方向とMagnitudeを別に推定。投稿率変化は (currentCount+alpha)/(historicalCount+alpha) with explicitly chosen smoothing parameter alpha=1だが、完全観測窓だけ。統計量はNumber floatを使用可能でも**金額加減算はBigInt/decimal**。
8. Probabilistic detectorはRiver ADWIN/PageHinkleyをベンチ対象。初期リリースはreplay可能なdeterministic robust delta + minimum supportを採用、校正済みconfigがない間scoreBps=null、TRENDではなくUNKNOWN/NO_CLEAR_CHANGEを選ぶ。Source quality低下時に高スコアを持ち越さない。
9. 複数独立ソースで同方向が支持されたならevidence set増加を示す。ただし共通API代理・ミラー・同一原文転載は1 upstreamClusterとして数える。2つの一致があるから因果成立とは書かない。
10. Score校正式はPRにバージョン固定され、held-out fixtureでfalse alert costと再現率の基準が決定されるまで公開主指標にしない。ランキングはconfidence tierとcoverage warningsを優先。Red flagsはsource discrepancy、outlier、X query drift、holiday/weekday、unobserved universe。

## 8. 障害マトリクス
|fault|検出/復旧|result|
|---|---|---|
|X search filter changed/query revision|別scope、新baselineが必要|UNKNOWN|
|partial 429/403/expired plan|no zero、source quotaを検証|LOW_COVERAGE|
|late event>watermark|retroactive recalculation new revision|STALE→new|
|out-of-order duplicate|unique identity + digest receipt|no double-count|
|same-source repost storm|upstream origin correlation cluster|not independent|
|price wrong currency/capacity|reject series merge/ER re-evaluate|UNKNOWN|
|source reliability downgrade|dependency invalidation|STALE|
|kill before DB commit|rollback+slot replay|single new receipt|
|commit after ACK lost|read-by-inputDigest|single receipt|
|stale worker lease|CAS fence reject|no overwrite|
|cancel/Steer|owner+goalRevision validation before publish|no new publish|
|source deletion/revoke|tombstone, deny evidence ref, recalc|STALE|
|excess source count/window|truncate explicitly with PARTIAL reason|LOW_COVERAGE|
|prompt injection in X post|untrusted content only; no tool escalation|safe|

## 9. Security / Compliance
収集権限はCHAT05/09、Tool Gatewayは03。未承認X streaming/検索費用やアカウント/Token取得なし。外部投稿の文面は操作命令でなく値。Source trust/reputationはアクセス資格を発行しない。本人固有X検索や個人名の分析は目的を限定し、センシティブな誤同定は避ける。非公開記事の引用/学習、Secret/personal dataの保存は09 retentionとEvidence原本の許可に従う。テキストから自動投稿/購入/予約しない。

## 10. 上限・Metrics/Benchmark
実装初期提案: 1 Task window最大30d、max1000 buckets/metric、max8 metrics、max20 source keys、max100k points/window/tenant、concurrent fusion 2/tenant・global8、job timeout30s、memory capはworker設定で強制、LLM 0、external API calls 0 (既に取得済Observationを使用)。再計算は1対象1時点最大2回並行（古いrevision破棄）。observability: align_ms, missing_bucket_ratio, source_skew_sec, watermark_lag, duplicate_rate, correlated-origin_ratio, false_alert_rate, drift detection precision/recall, signal disagreement, invalidation_latency, lost receipt count。Benchmark: time series regular/irregular/offline 100–100k, X repost burst, known change point, nonchange seasonality; baseline false positives/false negatives separately。X費用/latencyは本人プランで実測されるまで未知。

## 11. DB・移行 [PROVISIONAL: CHAT_10]
提案: trend_points keyed (tenant,sourceObservationId,scopeDigest)、trend_bucket_snapshots(scope,metric,window,watermark,inputDigest,revision)、trend_assessments(tenant,entity,relationRevision,algorithmVersion,profileRevision,inputDigest,status,metrics)、trend_invalidations(assessmentId,reason,originEventId)。PointはCHAT01 Observationを参照して重複原本を正本化しない。Aggregateはappend-only revision+active CAS head。source revisionやmapping改訂はsame txnでinvalidations/outboxを確定。過去版は旧modelVersionのまま読み、新versionへ移行時はshadow-recompute、freshness基準なしで旧値を高信頼と扱わない。DB migration/unique/FKはCHAT10に提案だけを提出する。

## 12. UI/API [PROVISIONAL: CHAT_08]
Trend方向/series chartには元の観測時点、最新取得時刻、データ欠落、取得範囲、source数と独立上流数を分けて表示。X件数推移と買取価格推移が同時に上昇しても「相関が観測された」であり因果でない。score不明や低coverageなら空白と注記し、勝手に0/100にしない。GET /trends/:idはauthorized read projection、Watch登録やNotifyはCHAT07の別Command。同時に到着した古いSSEが新しいstateを上書きしないようrevision付き。

## 13. Codex実装フェーズ
TRND-PR1 typed Point/Source scope validators + UTC align + zero-vs-missing fixtures。
TRND-PR2 dedup/repost/source correlation + quality-profiles binding。
TRND-PR3 deterministic robust detector + calibration pipeline + classifier tests。
TRND-PR4 durable revision CAS/watermark/late correction outbox owner承認後。
TRND-PR5 UI/read API + replay and 2-tenant fault and SC14 E2E。
各phaseのgateは先行preconditions/expected testsが緑になってから次へ（このrepoは仕様のみ）。

## 14. Unit/Contract/E2E/Fault/Bench test design: ALL NOT EXECUTED
|TEST|setup → action → expected|
|---|---|
|TRND-T01 Unit|X count sourceAt/fetchedAt境界と23:59/00:00 → half-open UTC window correct|
|TRND-T02 Unit|same post 100 reposts + 2 reworded copies → origin clusters 1 or unknown, not 102 independent|
|TRND-T03 Contract|different entityRevision/currency/capacity → refused aggregation|
|TRND-T04 Unit|X API incomplete one bucket, 0 posts → null/LOW_COVERAGE not zero|
|TRND-T05 Unit|fresh vs late correction after watermark → new assessment rev, prior still historical|
|TRND-T06 Unit|two prices drift but no X and unknown buyback → no cross-source confirmation claim|
|TRND-T07 Unit|source roots same upstream → independence CORRELATED|
|TRND-T08 Unit|constant stream vs sudden change→ detector distinguishes and exposes thresholds/version|
|TRND-T09 Unit|outlier single bot burst and weekday effect → no unsupported trend claim|
|TRND-T10 Integration|source reliability degrades + re-evaluate → old trend STALE|
|TRND-T11 Integration|mapping split old entity to two → old trend STALE, outputs separated|
|TRND-T12 Fault|two worker duplicates / kill before commit → one assessment receipt|
|TRND-T13 Fault|commit after ACK lost + replay → new revision 0 duplicates|
|TRND-T14 Security|tenant B tries evidenceRef/source profile A → zero disclosure|
|TRND-T15 Security|injected X text requests host shell/credentials → no Tool execution|
|TRND-T16 Lifecycle|Steer/cancel/expiry after calculation before publish → no stale publication|
|TRND-T17 E2E|SC-14 X/price/buyback different sampling intervals→ sparse coverage, source warning & false positives suppressed|
|TRND-T18 Benchmark|100k irregular observations; measure CPU/RSS/lag/error at same hardware| 
|TRND-T19 Contract|unknown major version / malformed time / NaN money → reject|
|TRND-T20 Integration|late retraction/delete post → revised provenance and stale derived series|

## 15. MUST requirement → TEST → SC 追跡（全ID・省略表記なし）

|Requirement|Designed TEST (未実行)|Acceptance Scenario|確認可能な合格条件|
|---|---|---|---|
|TRND-001|TRND-T19|SC-03, SC-14|provenance/time入力検証|
|TRND-002|TRND-T03|SC-01, SC-10|別商品/地域/通貨非混合|
|TRND-003|TRND-T02|SC-03, SC-14|repost source dedup|
|TRND-004|TRND-T01, TRND-T05|SC-03, SC-14|late時刻で最新汚染0|
|TRND-005|TRND-T04|SC-03, SC-14|sampling欠測保持|
|TRND-006|TRND-T04, TRND-T17|SC-14|薄いデータtrend unknown|
|TRND-007|TRND-T01|SC-03, SC-14|UTC half-open bucket|
|TRND-008|TRND-T03, TRND-T19|SC-01, SC-14|unitとcondition不正拒否|
|TRND-009|TRND-T06, TRND-T08|SC-14|別系列の独立変化を明示|
|TRND-010|TRND-T08, TRND-T09|SC-09, SC-14|校正なしscore null|
|TRND-011|TRND-T07, TRND-T09|SC-14|相関≠因果・反証表示|
|TRND-012|TRND-T10, TRND-T11|SC-10, SC-14|依存revision変化でSTALE|
|TRND-013|TRND-T04, TRND-T17|SC-11, SC-14|429や取得失敗が0件に化けない|
|TRND-014|TRND-T05, TRND-T20|SC-07, SC-14|watermark後は新version|
|TRND-015|TRND-T12, TRND-T13|SC-07, SC-14|duplicate/crash receipt 1|
|TRND-016|TRND-T16|SC-07, SC-08|cancel後publish0|
|TRND-017|TRND-T15|SC-08, SC-11|外部副作用なし|
|TRND-018|TRND-T18|SC-09|有限予算内終了・部分説明|


## 16. Cross-team proposals / blocked contracts
P0-TRND-01 [PROVISIONAL: CHAT_01] ObservationV1拡張ではなくlineage extension: originalPostId/upstreamRootId, sourceAt, sampling completeness, entityRevision, source profile revision; event revision semantics owned 01。P0-TRND-02 [PROVISIONAL: CHAT_05/13] X actor ID/quote/repost taxonomy、search query revision、pagination and post deletion handling; upstream source coverage cannot be promised. P0-TRND-03 [PROVISIONAL: CHAT_10] append-only metric revision, bucket version CAS, source invalidation/outbox, tenant indexes, ID UUIDv7 consistency. P0-TRND-04 [PROVISIONAL: CHAT_09] data tenancy/owner scope, source permission revocation and retention. P1-TRND-05 [PROVISIONAL: CHAT_07/08/11] Watch alert oracle, dashboard bias warnings, held-out false positive metrics. CHAT01/10 common UUIDv7 vs random uuid, Task conversation nullable, recordVersion CAS, tenant/workspace mapping unresolved until coordinator decision. This draft must not become Locked automatically.
Unknowns: X account entitlement, rate, cost, latency under user's actual plan; robust drift calibration thresholds; source correlation oracle; independence of vendor feeds. Verify with fixtures+source logs+upstream plan docs before release.

## 17. Sources (read 2026-10-30; Code verified unless marked; never executed upstream tests)
[S1] https://github.com/online-ml/river/blob/af06492837f29d786f70ac9b47116db24cf047c0/river/drift/adwin.py ; https://github.com/online-ml/river/blob/af06492837f29d786f70ac9b47116db24cf047c0/river/drift/page_hinkley.py ; https://github.com/online-ml/river/blob/af06492837f29d786f70ac9b47116db24cf047c0/tests/drift/test_adwin_regression.py ; LICENSE BSD-3. Issue https://github.com/online-ml/river/issues/1914 (Issue report, no reproduction).
[S2] https://github.com/evidentlyai/evidently/blob/34771fe41c08ca074d96a52aaf4a45cd640cbaf5/src/evidently/legacy/calculations/data_drift.py ; https://github.com/evidentlyai/evidently/blob/34771fe41c08ca074d96a52aaf4a45cd640cbaf5/tests/calculations/test_data_drift.py ; Apache-2.0 (legacy API verified; no current SDK version guarantee).
[S3] https://docs.x.com/x-api/posts/search/introduction and https://docs.x.com/x-api/posts/filtered-stream/introduction (official Docs verified; access/cost/observed performance in actual integration unverified).

[S4] River [PR #1963](https://github.com/online-ml/river/pull/1963) is OPEN and UNMERGED on 2026-10-30; it proposes drift detector evaluation metrics, explicitly described as a prototype needing API review (PR metadata verified, not stable library API). River [issue #1914](https://github.com/online-ml/river/issues/1914) is a roadmap, not benchmark proof. Hence the Harness uses a standalone fixed-fixture false-positive/false-negative harness rather than importing an unfinished PR. Official [X Search](https://docs.x.com/x-api/posts/search/introduction) and [Filtered Stream](https://docs.x.com/x-api/posts/filtered-stream/introduction) pages were directly opened 2026-10-30. Search docs distinguish recent 7-day and full-archive access; streaming docs describe near real-time delivery, but actual coverage/latency/cost and plan entitlement remain UNVERIFIED (Docs verified, no account live test).
