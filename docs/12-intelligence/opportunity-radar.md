# Opportunity Radar — Detailed Implementation Specification (12-01)

> Owner CHAT_12 | docs/12-intelligence/opportunity-radar.md | Draft / PROVISIONAL rev 0.1 | research 2026-10-30 | Tests designed: 0 executed
> Dependencies: 12-02 Entity Resolution, 12-04 Decision Risk, 12-05 Reliability, CHAT_02 Task, CHAT_03 Tool Gateway, CHAT_05 providers, CHAT_07 Watch/Scheduler, CHAT_09 Policy, CHAT_10 persistence.

## 0. 結論・採用
Radarは「既知SKUの価格Watch」ではない。ユーザーが登録していない商品・ホテル・話題など、未発見候補の空間から、権限ある公開ソースの探索→発掘→正規化→同一性保留→検証→有限のリランキングを行う。defaultはREAD/RESEARCHのみ。実行ごとにseed queryではなくDiscoveryPlan（カテゴリ/Provider/探索スロット/上限/過去の未探索空間）を永続し、coverageと発見されなかった領域を開示する。Opportunityは「購入可能」や「利益保証」を意味しない。Watch登録/通知/購入はそれぞれ別Ownerの承認とコマンド。

## 1. 目的・非対象・SC
- ユーザー例「今どんな商品に利益機会がありそうか」「昨日と違う面白い新サービスは」「ホテル価格が落ちた地域は」。物販専用の実装はしない。ProviderごとのStrategy Pluginを抽象化し、通貨・モノ・文章とも入力可能。
- 「発掘」の定義: 既知の特定IDを照会するだけでなく、collection/range/category/keyword expansion/trending feedから初見SourceRecordを抽出すること。0件結果もcoverageと一緒に返す。ブラウザの無許可探索や全網羅保証はしない。
- 既存Watchの定義・閾値・通知・外部EventトリガーはCHAT_07/13。購入/予約/投稿の実行は禁止。SC-01,02,03,05,07,08,09,10,14,16。

## 2. OSS/APIコード比較
|Source|実際に読んだファイル/仕組み|採用と限界|
|---|---|---|
|Keepa公式API + Java client|keepacom/api_backend@683bfa83 DealRequest.java の page/domainId/priceTypes/includeCategories/currentRange/isLowest等、Deal.java のASIN/delta/avg/current/lastUpdate。Apache-2.0|category/deal探索をProvider対応戦略に。SDKオブジェクト=利用資格ではない|
|Keepa公式Docs|https://keepa.com/api-docs/deals.html: 1ページ最大150件・5 tokens、query全体最大10,000 ASIN、dealは更新直近12h対象。https://keepa.com/api-docs/plans-tokens.html token-bucket|探索分母/ページング/Tokenを推定し予算管理。API account利用可否は未検証|
|OpenClaw|openclaw/openclaw@a59a0504 src/cron/active-jobs.test.ts の run authority/fence と default-proactive-job.test.ts のACK loss/重複provision test。MIT|繰返し・所有権Fenceの負例から学ぶ。汎用探索戦略や商品候補採点そのものは当該テストが保証しない|
|全件LLM自律探索|探索方向を自由に発生できる|永久ループ/請求爆発/重複。却下|
|Watcher流用のみ|既知IDなら簡単|未発見候補のcoverageを出せない。却下|
|閲覧サイトの無制限クロール|広範|資格/ToS/SSRF/プライバシー違反。却下|

Keepa price/dropの「5tokens/150件」は現行Doc記述でありアカウント契約/実発見件数/為替/国内Marketplaceでの成立は実測していない。

## 3. 正本と権限
DiscoveryRunはAgent Runではない。CHAT_02 Task/Runを開始した上で、Radar内部の探索進捗（Plan/Frontier/Candidate/score receipt）の名前。Tool実行はCHAT_03 Gateway→CHAT_05 adapter→CHAT_09 credential+egress。Providerは探索可能カテゴリ・quota・ToSをManifestで宣言し、その自己宣言だけでは認可されずCHAT_09/05審査済 entitlementを参照。Observation/EvidenceはCHAT_01、物理DBは10、通知は07、検索UIは08。Keyとtenant-owner mapping未Lockは[PROVISIONAL]。

## 4. 要件
|ID|Priority|検証可能な要求|
|---|---|---|
|RAD-001|MUST|DiscoveryGoalをユーザー許可範囲/除外条件/時間/予算/地域/用途とともに固定|
|RAD-002|MUST|Planは有限frontierとstrategy/version/capability/estimated costを含み、実施探索と未探索のcoverageを記録|
|RAD-003|MUST|既存SKU監視だけではないcategory/collection/deal/broad searchを最小1つ有効化、候補0件でも説明|
|RAD-004|MUST|各Providerはapproved purpose、method、entitlement/ToS、quota、effective cost/budget leaseがない場合実行しない|
|RAD-005|MUST|CandidateはSourceRecord/Evidence/sourceAt/fetchedAt/observedAt/validUntilと抽出版を保持|
|RAD-006|MUST|候補重複はsource raw ID+revision/normalized key+entityRevisionで抑止し、誤マージを防ぐ|
|RAD-007|MUST|identity UNRESOLVEDなら利益比較に進めずreview queueへ置く|
|RAD-008|MUST|条件と不明の伝播をRisk/Source reliabilityへ委譲、利益とScoreを捏造しない|
|RAD-009|MUST|ユーザー除外条件/利益閾値/新規性/鮮度を deterministic filter→順位の順で適用|
|RAD-010|MUST|scoreの各項（freshness,margin,cost,confidence,coverage penalty）は欠測を0に置かずmissingとして表示|
|RAD-011|MUST|LLM、API tokens、取得回数、ページ、worker同時数、CPU/時間に有限上限を設ける|
|RAD-012|MUST|cancel/Steer/disable/revokeで次の探索dispatch停止、結果公開前にgoalRevision再検証|
|RAD-013|MUST|再送/途中クラッシュ/レスポンスACK欠落に対してfrontier slotとCandidate insertが冪等|
|RAD-014|MUST|rate-limited/ToS禁止/Provider失効時は別ソースへ無条件迂回せず部分coverageを明示|
|RAD-015|MUST|探索が偏った場合topic/source/categoryに偏りがあることを表示し「市場全体」を断定しない|
|RAD-016|MUST|Watchへのpromotionは本人の明示操作とWatchOwnerの保存receiptが必要|
|RAD-017|MUST|notification/外部購入/予約/投稿への直結経路を持たない|
|RAD-018|MUST|0 candidates、0 verified candidates、partial Provider、full budget exhaustedを別結果|
|RAD-019|SHOULD|重複と負例を使って次回seed allocationを改善。ただし明示除外は優先|
|RAD-020|LATER|校正済探索バンディットを追加（探索偏りと安全性実測後）|

## 5. 内部Port・Schema
~~~ts
type DiscoveryStatus='QUEUED'|'PLANNED'|'RUNNING'|'VERIFYING'|'PARTIAL'|'COMPLETED'|'EXHAUSTED'|'CANCELLED'|'FAILED';
type DiscoveryPlanV1={schemaVersion:'1.0'; tenantId:string; ownerUserId:string; taskId:string; runId:string; goalRevision:string; planId:string; revision:string;
 targetClasses:('product'|'hotel'|'service'|'topic')[]; countryCodes:string[]; excludedSources:string[]; excludedCategories:string[];
 strategyVersion:string; slots:DiscoverySlot[]; budgets:{maxRequests:number; maxPages:number; maxCandidates:number; maxProviderTokens:string; maxLLMTokens:number; deadlineAt:string}; createdAt:string};
type DiscoverySlot={slotKey:string; providerId:string; strategy:'category'|'deal_feed'|'search_expansion'|'changed_collection'; queryDigest:string; sourceScope:string; pageCursor:string|null; admissionRef:string; costUpperBound:string};
type CandidateV1={candidateKey:string; planId:string; slotKey:string; sourceRef:string; providerItemRef:string; evidenceRef:string; identityRevision:string|null; observedAt:string|null; freshness:'fresh'|'stale'|'unknown'; stage:'SEEN'|'NEEDS_IDENTITY'|'VERIFYING'|'VERIFIED'|'REJECTED'; riskDecisionRef:string|null; reliabilityRef:string|null; features:Record<string,string|null>};
type DiscoveryOutcome={status:DiscoveryStatus; planId:string; seen:number; unique:number; verified:number; unresolved:number; rejected:number; uncoveredSlots:string[]; incompleteReasons:string[]; candidateIds:string[]};
~~~
slotKey=SHA256(length-prefixed(tenant,planId,strategyVersion,provider,normalizedQuery,cursor))。candidateKey=SHA256(length-prefixed(tenant,provider,marketplace,listingID,extractionRevision))。DigestにCredential/Secret/URL query tokenを混ぜない。Schema unknown major reject。max slots提案<=100、cursor bytes<=2048、query<=512、1 source record<=64KiB、raw Artifact別管理。DateはUTC ISO。cost stringはprovider token小数でなく非負整数・上限ガード。

## 6. Sequenceと状態遷移
~~~mermaid
sequenceDiagram
 participant U as User
 participant T as Task(02)
 participant R as Radar(12)
 participant G as Tool Gateway(03)
 participant P as Provider(05)
 participant E as Entity/Risk(12)
 U->>T: goal, budget, constraints
 T->>R: authorized plan
 R->>R: bounded frontier + slot receipts
 loop admitted slot until budget/deadline
 R->>G: immutable read-only intent+budget
 G->>P: provider-scoped fetch
 P-->>R: records + Evidence + cost
 R->>R: dedupe and frontier checkpoint
 R->>E: identity -> risk -> quality
 end
 R-->>T: candidate report + coverage + unverified
 U->>T: optional create Watch command
 T->>U: Watch ID only after CHAT07 commit
~~~
QUEUED→PLANNED→RUNNING→VERIFYING→COMPLETED/PARTIAL/EXHAUSTED。cancelは全状態からCANCELLEDへ（確定済み独立Receipt残る）。RUNNING→PARTIALはProvider cap/permission loss、RUNNING→EXHAUSTEDは予算枯渇、VERIFYING→COMPLETEDは評価終了（verified 0でもあり得る）。外部ToolのUNKNOWN効果はCHAT_03台帳の判断、Radarは結果を捏造しない。

## 7. 決定的探索アルゴリズム
~~~ts
async function discover(plan:DiscoveryPlanV1){
 assertPermissionAndGoalRevision(plan);
 const frontier=await loadUnfinishedSlots(plan.planId);
 while(frontier.length && hasBudget() && !deadline() && !cancelled()){
   const slot=pickFairSlot(frontier, {maxSharePerProvider:0.5});
   if(!await providerEntitled(slot)) { await markSkipped(slot,'NOT_ENTITLED'); continue; }
   const reservation=await budgetReserve(slot.costUpperBound);
   if(!reservation){await markSkipped(slot,'BUDGET'); continue;}
   const outcome=await toolGateway.read(slot, reservation); // ToolLedger, safe retry semantics owned by 03
   await checkpointSlotAndCandidatesAtomic(slot, outcome); // 10 outbox proposal
   if(outcome.kind==='UNKNOWN') break; // no invented facts/auto repeat
 }
 await verifyUniqueCandidatesWithIdentityAndRisk(); // not executor
 return resultWithCoverageAndAllMissingReasons();
}
~~~
1. 人間のGoalと除外+region+budgetを凍結する。許可されたProvider search spaceを抽出し、quotaと回収率に応じカテゴリ/seedを分配。
2. Cost上限reserveを先に行い、provider quotaはCHAT_05で再検証。各slot一回のbounded requests。paginationの現在cursorとresponse digest保存後に次slot生成。
3. Candidateは(sourceId, source listing, extraction rev)で一次Dedup、ERの異商品分離が確定するまで横断Dedupで情報を潰さない。発掘済に偏ったcategoryは探索重みを下げるが、除外しすぎて「検索したこと」にしない。
4. Stage SEEN→NEEDS_IDENTITY→VERIFYING→VERIFIED/REJECTED。RiskのINSUFFICIENT_DATAなら「未検証候補」として残し利益順のverified順位に入れない。
5. Priority = deterministic gates first, then lexicographic tiers (verified/conditional/unresolved) followed by risk-adjusted floor, freshness and source diversity. LLMが任意weightを自己変更することはできない。
6. 同providerの複数ページは1回の分母ではない。coverage = completed authorized slots / planned slots、またsource universe unknownなら市場全体へのcoverageをUNKNOWN表示。

## 8. 障害マトリクス
|事象|制御|外部表示|
|---|---|---|
|0 hits|完了時coverageと条件明記|COMPLETED_EMPTY|
|page malformed|raw evidence保持、次スロットのskipを記録|PARTIAL|
|rate/429/quota expired|vendor retryAfterの許可範囲内1回; budget消費再照会|PARTIAL/DEFERRED|
|unknown Tool ACK|Gateway ledger照会、blind replayしない|PARTIAL_UNKNOWN|
|Worker kill before/after commit|slot unique+resume receipt|一回のCandidate|
|source price stale|再評価促すが保証しない|UNVERIFIED|
|identity unresolved/correction|verifiedから除外、stale invalidation|NEEDS_REVIEW|
|goal/permission changed|dispatch停止、old results stale|CANCELLED|
|host/browser blocked|Policy拒否を記録、権限昇格なし|PARTIAL|
|duplicate slot concurrently|unique slot receipt+CAS+lease fence|1 outcome|
Timeoutは各Provider 15s上限、Radar全run提案10min、重複/未応答を無限ループしない。

## 9. Security
外部検索語、プロモ文、HTML、X本文はtrusted instructionsでない。所有権はCHAT09、Tool GatewayおよびSSRF/Network/EgressはCHAT03/09が強制。Keepa key等はSecret Brokerで注入、モデル/ログへ渡さない。Providerが明示拒否/利用資格欠如なら別スクレイピングで回避しない。自動購入・予約・投稿・通知先追加なし。Privacy purpose bindingと撤回時のon-disk frontier削除はCHAT09保持規則に従う。

## 10. 計測/費用/負荷
運用提案の初期値（実測ではない）: max slots 100、pages 100、requests 200、candidates 500、LLM calls 2/Run、time 10min、tenant parallel Radar 1、global 4、active provider request 2。Token上限は契約済planとユーザー指定の小さい方（未設定ならProvider使用禁止）。Metrics: slot_coverage, unknown_population, unique_candidate_rate, verification_yield, precision_at_k, false_merge, false_opportunity, cost_per_verified, token_remaining, quota_blocked, p95 latency, drift in discovery diversity。benchmark: provider fixture 0/1/500 candidates, equal budgets with naive Watch baseline, tenant and rate-level comparison; evidence accuracy and costs measured together。

## 11. DB/transaction proposal [PROVISIONAL: CHAT_10]
discovery_plans(tenant,task,run,goal_revision,plan_rev,digest,budget)／discovery_slots(tenant,plan,slot_key,state,attempt_ref,checkpoint,provider_cursor,cost)／source_candidates(tenant,candidate_key,source_ref,extraction_rev,stage,evidence_ref,relation_ref)／rank_receipts。Uniq(tenant,slotKey) and (tenant,candidateKey,revision)、CAS on run lease, commit slot progress+candidate references+event/outbox atomically。Blobは01 Artifact。移行時、strategy v1→v2は既存slotをそのまま旧versionで再開、切替は新Planとして保存。保留状態を0件に改変しない。

## 12. UI/API影響 [PROVISIONAL: CHAT_08]
作成条件、実際に見たカテゴリ/Provider、到達/未到達、発掘件数/検証件数/候補0件理由、参照URL/観測時刻/保留理由、予算消費を表示。「推奨候補」「未検証」「除外」の3タブ。Watch登録には別ボタン+Preview→CHAT07 receipt。POST /discoveryにidempotency+budget・CSRF・owner/goal rev validationが必要。Chat UIの自然言語返信だけを保存成功とみなさない。

## 13. Codex実装PR分割
RAD-PR1 bounded plan/frontier/strategy interface and zero-result fixtures。RAD-PR2 Provider admission+Keepa deals adapter via CHAT05（独立調整）。RAD-PR3 dedup+Entity+Risk coordination。RAD-PR4 durable lease/slot receipts/outbox after CHAT10 contract。RAD-PR5 coverage UI、0件/partial/fault E2E、precision@k benchmark。各phaseでauthorization/limits tests。

## 14. すべて未実行のテスト設計
|ID|setup/action/expected|
|---|---|
|RAD-T01|未知candidateだけ含むcategory feedを投入→既知SKU Watchなしでも発掘|
|RAD-T02|0件/Provider制限→0を正しく表示しcoverageを分離|
|RAD-T03|同product別ASIN/容量/condition→ERが曖昧情報を潰さない|
|RAD-T04|source time/validUntil欠損→verifiedに昇格しない|
|RAD-T05|Tool quota 10 tokens/不足→reserve拒否、超過0|
|RAD-T06|200 requests+500 candidate limit→先読み・追加dispatch 0|
|RAD-T07|禁制source/secret無効→実ネットワーク0、後続scraping迂回なし|
|RAD-T08|同slot100回配信→1 receipt/candidate unique|
|RAD-T09|commit前Kill/ACK loss→再起動後確定0または1|
|RAD-T10|Steer/cancel/revoke/goalRevision変更→新dispatch 0|
|RAD-T11|利益Unknown/identity ambiguous→「確認済み利益候補」に0|
|RAD-T12|偏ったsource population→coverage unknown/偏り警告|
|RAD-T13|Watch promotionなし→Watch record 0、明示操作後のみ1|
|RAD-T14|HTML injection/social instructions→credential漏洩0/policy変更0|
|RAD-T15|SC-10 E2E:混合サイズと0件探索→誤マージなし・出典保持|
|RAD-T16|latency/CPU/memory/token benchmarks with fixed fixture→metrics recorded|
|RAD-T17|source retract after candidate verified→stale、通知なし|
## 15. MUST requirement → TEST → SC 追跡（全ID・省略表記なし）

|Requirement|Designed TEST (未実行)|Acceptance Scenario|確認可能な合格条件|
|---|---|---|---|
|RAD-001|RAD-T01|SC-05, SC-10|goal条件凍結|
|RAD-002|RAD-T02, RAD-T12|SC-05, SC-10|有限探索slotとcoverage|
|RAD-003|RAD-T01|SC-10|未知ID候補発見|
|RAD-004|RAD-T05, RAD-T07|SC-08, SC-09|未許可Provider呼出0|
|RAD-005|RAD-T04|SC-01, SC-10|候補の時間・証拠保持|
|RAD-006|RAD-T03, RAD-T08|SC-10|二重登録/誤結合0|
|RAD-007|RAD-T03, RAD-T11|SC-01, SC-10|未解決identity利益に昇格しない|
|RAD-008|RAD-T11|SC-10, SC-14|条件/未知をRiskへ委譲|
|RAD-009|RAD-T11, RAD-T12|SC-10, SC-16|除外条件と閾値を優先|
|RAD-010|RAD-T12|SC-10, SC-14|欠測をゼロ点にしない|
|RAD-011|RAD-T05, RAD-T06|SC-09|予算/回数上限遵守|
|RAD-012|RAD-T10|SC-07|cancel後dispatch0|
|RAD-013|RAD-T08, RAD-T09|SC-07|再配達でslotは一意|
|RAD-014|RAD-T02, RAD-T07|SC-08, SC-10|ブロック理由と未探索表示|
|RAD-015|RAD-T12|SC-10, SC-14|sampling bias可視|
|RAD-016|RAD-T13|SC-02, SC-16|明示Watch receiptのみ昇格|
|RAD-017|RAD-T13, RAD-T14|SC-08|購入/投稿/無断通知0|
|RAD-018|RAD-T02, RAD-T11|SC-10|0/partial/exhaustedを区別|


## 16. Cross-team Proposals / Unresolved
P0-RAD-01 [PROVISIONAL: CHAT_02/10] DiscoveryPlan/slot checkpointのRun fencing、goalRevision、DB atomic receipt、nullable Task.conversationId。P0-RAD-02 [PROVISIONAL: CHAT_05/09] searchable provider Manifest、token bounds、license/ToS/purpose、API credential handling。P0-RAD-03 [PROVISIONAL: CHAT_01] Candidate Evidence relation IDs and schemaVersion。P0-RAD-04 [PROVISIONAL: CHAT_12-02/12-04] verified identity and exact amount before score。P1-RAD-05 [PROVISIONAL: CHAT_07/08/11] Watch promotion API/coverage UI/E2E。過去のUUIDv7とDB default、tenantId vs workspace mapping、RecordVersion/CAS等の調停は未Lock。Keepa plan/marketplace利用資格は未確認。全件Draft維持。

## 17. Sources / grades (inspected 2026-10-30)
[S1] https://github.com/keepacom/api_backend/blob/683bfa839b84d172a156085bda0b29ce8c3eee45/src/main/java/com/keepa/api/backend/structs/DealRequest.java ; https://github.com/keepacom/api_backend/blob/683bfa839b84d172a156085bda0b29ce8c3eee45/src/main/java/com/keepa/api/backend/structs/Deal.java ; LICENSE Apache-2.0 (Code verified, no client tests for this behavior inspected).
[S2] https://keepa.com/api-docs/deals.html , https://keepa.com/api-docs/plans-tokens.html , https://keepa.com/api-docs/request-basics.html (Docs verified; present API cost/endpoint, live entitlement UNVERIFIED).
[S3] https://github.com/openclaw/openclaw/blob/a59a05040adca02defaa0af3f791d068687a7039/src/cron/active-jobs.test.ts ; https://github.com/openclaw/openclaw/blob/a59a05040adca02defaa0af3f791d068687a7039/src/cron/default-proactive-job.test.ts (Code verified, MIT; ownership/ack behaviors only, not Radar implementation).
