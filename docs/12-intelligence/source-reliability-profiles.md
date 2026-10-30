# Source Reliability Profiles — Detailed Implementation Specification (12-05)

> CHAT_12 | docs/12-intelligence/source-reliability-profiles.md | Draft / PROVISIONAL rev 0.1 | research 2026-10-30 | Test execution 0
> Dependencies: CHAT_01 Observation/Evidence, CHAT_04/05 Fetch+Connector, CHAT_09 compliance/privacy, CHAT_10 metrics DB, 12-01/03/04 consumers.

## 0. 結論
Source Reliabilityは「情報源が真実か」を機械的に確定する格付けではない。同一Provider内のendpoint/category/field別の**観測された運用品質**（到達/欠損/遅延/訂正/裏取り一致/費用/利用資格）を時系列として保持し、タスク文脈での適合性を有限なconfidence bandで表示する。独立した真偽の証拠を欠く場合はVERIFICATION_UNKNOWN。速いAPIも嘘はつけるし、新規sourceがデータ不足というだけで偽と判定されない。

## 1. 範囲/SC
対象: API/HTTP/Browser/X等、source+method+geography+domain+field単位の稼働、更新間隔、価値の再検証率、訂正履歴、取得コスト、著作権/ToS承認の履歴。評価は取得先「正しさ」の認証や投稿主の人格評価ではない。ユーザーの主観的好き嫌いはCHAT_06/14のpreferenceで区別し、Reliability ground truthに混ぜない。SC-01/02/03/05/07/08/09/10/14/16。

## 2. OSS・アルゴリズム比較
|対象|source inspected|教訓/採否|
|---|---|---|
|Evidently Data Quality|evidentlyai/evidently@34771fe: src/evidently/metrics/dataset_statistics.py RowCount/DuplicatedRowCount、tests/metrics/data_quality/test_stability_metric.py; tests/calculations/test_data_quality.py|測定窓と欠損/重複/安定性は定量化可。真偽そのものではない。Apache-2.0、参考|
|Evidently Drift|同repo src/evidently/legacy/calculations/data_drift.py get_one_column_drift、tests/calculations/test_data_drift.py empty-column error|測定分布が変化した際にスコア固定を避ける。旧legacy/最新版API差に注意|
|River|online-ml/river@af064928 river/drift/adwin.py ADWIN.update、tests/drift/test_adwin_regression.py|逐次変化の検知は可能。偏り・真偽の直接判定は不可。BSD-3|
|一律星5/ドメイン単位真偽|初見ソースへの一般化が楽|証拠不足と誤判定。自動信頼/拒絶として却下|
|LLMで出典の「格」を推定|説明可能だが主観依存、攻撃対象|政策・事実の代用として却下|

## 3. Ownership
Source ProfileはCHAT12内部の派生メトリクス。生のObservation/EvidenceはCHAT01、Provider health/Entitlementと取得結果はCHAT05、ネットワーク/利用条件はCHAT09、Watchの有効性はCHAT07、実体のDBはCHAT10。Quality ProfileはTool Gateway/Authorizationに「許可」を与えない。禁止済みProviderを高スコアだから使うことは不可。source profileを閲覧する前にtenant/owner/purpose scope確認。[PROVISIONAL: CHAT_01] canonical SourceRef/field lineage mapping はowner合意待ち。

## 4. 要件
|ID|Priority|要求|
|---|---|---|
|SRC-001|MUST|sourceをproviderId+method+endpoint-class+domain+field+regionで識別、個人/tenant特有metricsを混ぜない|
|SRC-002|MUST|acquisition attemptとsuccessful observationとverified claimを別母数に保持|
|SRC-003|MUST|成功/失敗/429/403/timeout/parser-invalidを区別しfailureを金額0にしない|
|SRC-004|MUST|observedAt/sourcePublishedAt/fetchedAt/correctedAt/validUntilを別々に保持|
|SRC-005|MUST|校正前・sample不足のqualityはUNKNOWN、偽の点数0や100を付けない|
|SRC-006|MUST|verified evidenceは独立した複数原ソースと、確認不能/否定証拠を区別|
|SRC-007|MUST|共通上流API/転載/同一文章/同系統アカウントは同一correlation clusterとして数え、水増し禁止|
|SRC-008|MUST|quality scoreにconfidence intervalとsample count/measurement windowを必須表示|
|SRC-009|MUST|契約/利用規約、robots/ライセンス、API entitlement、個人情報処理根拠を別のADMISSION状態に管理|
|SRC-010|MUST|profileVersion/metricVersion/calibrationVersionとsource policy epochを保存|
|SRC-011|MUST|訂正・削除・後からの反証で過去Profileを上書きせず新Revisionと下流invalidationを発行|
|SRC-012|MUST|Source新規/疎/ドリフト時にはscore suppressionか置信区間拡大、収集を無条件拒否しない|
|SRC-013|MUST|source scoreをpolicy overrideに流用不可、購買/投稿承認に流用不可|
|SRC-014|MUST|source原資料のsecret・Cookie・生の個人投稿本文をログに保持しない|
|SRC-015|MUST|同一Receipt再配達・crash・遅延訂正に対し統計を二重加算しない|
|SRC-016|MUST|Source qualityが劣化したらRadar/Trend/Decisionの該当結果をSTALEとして再評価|
|SRC-017|MUST|期限切れ/認可取消後にprofileを使って非公開Evidenceへアクセスできない|
|SRC-018|SHOULD|観測品質をUIで4軸（freshness,access,corroboration,cost）表示、総合値は任意|
|SRC-019|LATER|domain/fieldごとの統計校正器と人間ラベル検証を拡張|

## 5. 内部型/validation
~~~ts
type SourceKey={providerId:string; method:'api'|'http'|'browser'|'webhook'; endpointClass:string; domain:string; fieldName:string; region:string};
type Admission='ALLOWED'|'PENDING_REVIEW'|'BLOCKED'|'EXPIRED'|'UNKNOWN';
type AttemptOutcome='SUCCESS'|'NO_DATA'|'HTTP_429'|'HTTP_403'|'TIMEOUT'|'PARSER_ERROR'|'REVOKED'|'UNKNOWN';
type Receipt={schemaVersion:'1.0'; tenantId:string; ownerUserId:string; sourceKey:SourceKey; attemptId:string; evidenceIds:string[]; outcome:AttemptOutcome; fetchedAt:string; observedAt:string|null; sourcePublishedAt:string|null; correctedAt:string|null; bytesRead:number; providerTokens:string|null; upstreamCluster:string|null};
type ProfileV1={schemaVersion:'1.0'; sourceKey:SourceKey; tenantId:string; profileRevision:string; metricVersion:string; windowStart:string; windowEnd:string; attempts:number; successes:number; verifiedClaims:number; contradictedClaims:number; unverifiedClaims:number; correlationClusters:number; freshnessP50Sec:number|null; freshnessP95Sec:number|null; interval:{lowerBps:number;upperBps:number}|null; admission:Admission; evidenceRefs:string[]; reasons:string[]; status:'INSUFFICIENT_DATA'|'MEASURED'|'DEGRADED'|'STALE'};
~~~
Validate numeric count safe integer 0..2^53-1 before JSON serialize、prob interval 0..10000、timestamps UTC and monotonic window boundaries。Provider ID and endpoint field <=128 characters canonical lower、unknown major rejects。window最長30日提案、最低30 independent validated claim clusters未満は校正した真偽scoreを出さずINSUFFICIENT_DATA。Claimsが未確認ならverified=0ではなくcount UNKNOWNで根拠率を算定しない。

## 6. Sequenceと状態
~~~mermaid
sequenceDiagram
 participant C as Connector(05)
 participant E as Evidence(01)
 participant S as SourceProfiler(12)
 participant D as DB(10)
 participant R as Radar/Trend(12)
 C->>D: acquisition attempt receipt
 E->>S: independent corroboration/correction
 S->>D: dedupe attempt + update metrics CAS
 S->>S: group upstream roots, validate windows
 S->>D: new immutable profile revision + outbox
 D-->>R: versioned quality + stale invalidation
~~~
状態: NEW→ACCUMULATING→MEASURED→DEGRADED/STALE。権限ALLOWED/BLOCKEDは直交するPolicyStateでありQualityの状態がALLOWEDに変更しない。Revokeで新しいsource access不可、aggregationは許可された既存metadataのみに限定する。

## 7. 計算アルゴリズム
1. AttemptはsourceKey,tenant,attemptId uniqueでappend。出所が明示されない取込を拒否。Loss/ERRORはstatus別母数、確証済み観測とは別に数える。
2. Availability = successes/attempts（attempts=0時null）。freshness = fetchedAt - sourcePublishedAtでsourcePublishedAt不明ならnull、時計逆行はinvalid時刻として集計外。SuccessでもContent品質は別。
3. Corroboration: evidence claimを独立上流根拠clusterでgroup。転載リンクのコピーは新独立証拠ではない。確証したclaimの内、事後反証が付いた割合を別途記録。3つの同じAPIミラーを3 votesにしない。
4. 校正済区間はWilson区間等の2項信頼区間をraw successful/verified countsへ適用可能。ただしsample-selection biasとラベルの偏りがあるため「真実確率」と呼ばない。依存度/ドメイン/期間を必ず併記。n<30 independent claimsはquality rate UNKNOWN。
5. Cost = provider token/verified independent fact、0件の場合はnullでゼロコストと呼ばない。Reliabilityはaxesベクトルとして利用者に渡す。総合スカラー必須にしない。
6. Drift検知（River ADWIN等）は充分なwindowがある場合にのみ。Drift→DEGRADED or RETRAIN_REQUIRED、Policyは変更しない。旧結果へSTALEを発行。
7. Recalculationは新revisionを原子commitし、異なるmetricVersionを比較する場合はMigration/説明を必須にする。

## 8. Failure matrix
|event|action|terminal|
|---|---|---|
|n=0/new source|profile interval=null|INSUFFICIENT_DATA|
|30 identical reposts|one origin cluster|CORROBORATION_UNKNOWN|
|source timestamp absent|freshness unknown、更新遅延と決めつけない|PARTIAL|
|429/403|acquisition failure、政策はPolicy Owner再確認|DEGRADED|
|bot detection/ToS denied|alternate unauthorized scrapingなし|BLOCKED|
|late correction|new revision + dependent stale|MEASURED/STALE|
|duplicate retries|attemptId unique, no double counts|SAME_PROFILE|
|commit loss|idempotent read last revision|SAME_PROFILE|
|drift false alarm|relabel/holdout＋検知率監査|REVIEW|
|auth/tombstone|cached proof ref invalid|REVOKED|
|cancel/Steer|fresh goal/policy epoch check, no stale publish|CANCELLED|
Provider REST errorをSource責任でなく通信/認可/ProviderErrorの別原因に切り分ける。失敗率は正確な測定窓を伴う。

## 9. Security/Privacy
生のWeb文字列をreliabilityのcommandとして読まない。ユーザー固有ログを他tenantの公開スコアへ合算しない。Profileの全数をベースに利用者特定されないよう最少集計閾値、閲覧権限、集計粒度を制限。目的外継続収集禁止。標的個人の評判スコア生成禁止。ToS/権限のstatusは別OwnerのPolicyのみ。DELETEで生資料が消えたらprofile provenanceを隔離/再算定。

## 10. 有限資源・計測
1provider/tenant最大200k attempt metadata、aggregation window1-30日、同時4集計、1window最大100k rows、aggregation timeout15s、daily CPU予算はRun予算内、LLM 0。SLOは実測後決定。Metrics: uptime rolling, p50/p95 source lag, parser error, 403/429, independent confirmation ratio, contradiction ratio, source diversity, correction reversal, stale invalidation, time/cost per corroborated claim、calibration Brier/ECE（真偽ラベルありのみ）。low-sample false certainty count=0。

## 11. 永続化/Migration提案 [PROVISIONAL: CHAT_10]
source_attempt_receipts unique(tenant,attemptId,sourceKey)、source_claim_validation(tenant,claim,originCluster,verifiedOutcome,revision)、source_profiles(tenant,sourceKey,window,metricVersion,revision,status,metricsJson,evidenceRefs)、source_profile_invalidation_outbox。Raw Evidenceは01の正本をreferential link。Counterのread-modify-writeはSERIALIZABLE/CASまたはaggregate deterministically from immutable events、double count不可。初版集計→新metricVersionはshadow rebuild+dual-read注記で移行。

## 12. UI/API proposal [PROVISIONAL: CHAT_08]
Sourceごと「接続成功率」「最終確認」「情報の裏取り状況」「取得コスト」「利用条件」「参照期間・件数」を別表示。スコア欄に『信頼度95%』など校正不明確率を出さない。GET profileは所有者に許可されたAggregatesのみで元Evidenceへのアクセスは別途再認可。Sourceを拒否する操作はSource profileではなくCHAT09/05 policy edit、CSRF/If-Match付き。

## 13. Codex実装PR
SRC-PR1 attempt/provenance validators+fixtures。SRC-PR2 window aggregation + null/missing handling。SRC-PR3 origin correlation and contradiction evidence。SRC-PR4 revisioned invalidation/DB (owner承認後)。SRC-PR5 UI+2-user privacy+drift benchmarks。導入順にmalicious source tests必須。

## 14. テスト（DESIGNED、未実行）
|TEST|setup/action/expected|
|---|---|
|SRC-T01|attempt vs verified claim 100:5→それぞれ別母数|
|SRC-T02|0 attempts / 2 claims→unknown interval not 0/100|
|SRC-T03|30 repost same root→ independentCluster=1|
|SRC-T04|sourcePublishedAt null/clock skew→freshness null|
|SRC-T05|expired policy/403→ADMISSION blocked、スコアで解除不可|
|SRC-T06|late correction/contradiction→new revision and stale dependent|
|SRC-T07|same receipt100 replays→count=1|
|SRC-T08|commit before/after Kill→no double metric|
|SRC-T09|tenantA profile + tenantB lookup→private data 0|
|SRC-T10|raw X injection in source→tool privileges unchanged|
|SRC-T11|model drift/low-N→degraded or unknown, source not assumed false|
|SRC-T12|source facts with stale origin + 2 independent claims→profile changes deterministically|
|SRC-T13|no data vs parser failure→outcome separate|
|SRC-T14|30d window 100k events→budget/CPU metrics, timeout fail bounded|
|SRC-T15|retention delete + export→redact provenance and deny deref|
|SRC-T16|SC-14 degradation→trend reliability update triggers stale|
## 15. MUST requirement → TEST → SC 追跡（全ID・省略表記なし）

|Requirement|Designed TEST (未実行)|Acceptance Scenario|確認可能な合格条件|
|---|---|---|---|
|SRC-001|SRC-T01, SRC-T09|SC-08, SC-14|sourceKey粒度・tenant隔離|
|SRC-002|SRC-T01|SC-01, SC-14|3母数が独立|
|SRC-003|SRC-T13|SC-02, SC-14|失敗をzero priceとしない|
|SRC-004|SRC-T04|SC-03, SC-14|時刻誤同一化なし|
|SRC-005|SRC-T02|SC-14|sample不足unknown|
|SRC-006|SRC-T03, SRC-T12|SC-03, SC-14|根拠と反証を分離|
|SRC-007|SRC-T03|SC-03, SC-14|転載は独立支持0|
|SRC-008|SRC-T02, SRC-T11|SC-09, SC-14|区間/件数を明示|
|SRC-009|SRC-T05|SC-08, SC-14|policyとquality独立|
|SRC-010|SRC-T06|SC-07, SC-14|schema/revision履歴|
|SRC-011|SRC-T06|SC-07, SC-14|訂正で新revision|
|SRC-012|SRC-T11|SC-09, SC-14|source低Nから偽の確定0|
|SRC-013|SRC-T05, SRC-T10|SC-08, SC-14|高scoreでも権限拡大0|
|SRC-014|SRC-T09, SRC-T15|SC-08|秘密ログ0|
|SRC-015|SRC-T07, SRC-T08|SC-07|再送統計加算1回|
|SRC-016|SRC-T16|SC-10, SC-14|下流STALE再計算|
|SRC-017|SRC-T09, SRC-T15|SC-08|撤回後非公開Evidenceへの参照拒否|


## 16. Cross-team proposals
P0-SRC-01 [PROVISIONAL: CHAT_01] SourceKey, Claim Verification, provenance relation, source version invalidationをEvidence/Observation契約に追加提案。P0-SRC-02 [PROVISIONAL: CHAT_05/09] ProviderEntitlementとSourceQualityを完全分離; policy epochs/ToS evidence required。P0-SRC-03 [PROVISIONAL: CHAT_10] idempotent attempt receiptsとaggregates、tenant unique index＋retention。P1-SRC-04 [PROVISIONAL: CHAT_07/08/11] drift中のWatch/Trend再計算と説明UI＋calibration E2E。Unresolved: truth labels source, n threshold, multi-tenant data sharing entitlement、IP/licenses; Locked不可。

## 17. Primary sources (code/tests inspected 2026-10-30; Harness tests 0)
[S1] https://github.com/evidentlyai/evidently/blob/34771fe41c08ca074d96a52aaf4a45cd640cbaf5/src/evidently/metrics/dataset_statistics.py ; https://github.com/evidentlyai/evidently/blob/34771fe41c08ca074d96a52aaf4a45cd640cbaf5/tests/metrics/data_quality/test_stability_metric.py ; https://github.com/evidentlyai/evidently/blob/34771fe41c08ca074d96a52aaf4a45cd640cbaf5/src/evidently/legacy/calculations/data_drift.py ; https://github.com/evidentlyai/evidently/blob/34771fe41c08ca074d96a52aaf4a45cd640cbaf5/tests/calculations/test_data_drift.py ; LICENSE Apache-2.0. (Code verified, legacy API note)
[S2] https://github.com/online-ml/river/blob/af06492837f29d786f70ac9b47116db24cf047c0/river/drift/adwin.py ; https://github.com/online-ml/river/blob/af06492837f29d786f70ac9b47116db24cf047c0/tests/drift/test_adwin_regression.py ; LICENSE BSD-3. (Code verified; detector does not establish veracity)
[S3] https://docs-old.evidentlyai.com/reference/data-drift-algorithm (old API, <=0.6.7; not current API evidence) and https://github.com/online-ml/river/issues/1914 (Issue report/proposal).

## 18. Round 4 — Source attempt lineage and profile revision
CHAT_10's proposed `source_attempt_receipts` is immutable per (tenant,sourceKey,providerAttemptKey), with `outcome_code`, `coverage_code`, source/fetch/published timestamps, actor owner and Evidence/Artifact FK. `source_profile_revisions` stores bounded-time metrics and revision; unknown provider cost, failed or restricted call is NOT success with price 0. Profile's reliability score is empirical retrieval/completeness and independent verification, **never permission or statement truth**.
Trend/Radar/Risk decisions must link to a specific profile revision and accepted SourceReceipt; a profile change or revoked provider entitlement invalidates derived confidence, not historical Evidence. Source correlation clusters mirror/retweets to one upstream origin; sample denominators unknown => coverage unknown, not zero.
Tests SRC-R4-T01 same provider attempt key duplicate→one receipt; T02 partial coverage→no confidence 100%; T03 three mirrors same upstream→one independent source; T04 revoked provider still high historic score→denied Tool; T05 profile version old→consumer rejects stale claim. SC-01/10/14. DRAFT.

