# Entity Resolution — Detailed Implementation Specification (12-02)

> Owner CHAT_12 | Path docs/12-intelligence/entity-resolution.md | Draft / PROVISIONAL rev 0.1 | 2026-10-30 | Requirements ER-001..ER-018 | Tests designed, 0 executed
> Depends: CHAT_01 core-domain-models + evidence-artifact-contracts, CHAT_05 connector-framework/hotel-research, CHAT_09 authentication, CHAT_10 database, CHAT_11 fixtures. Canonical shared fields are NOT defined here.

## 0. 結論と採用案
複数Providerのlistingは即時統合しない。局所的なSourceRecordを原本参照とともに保存し、(1)クラス別blocking、(2)互換性のhard-veto、(3)特徴量比較、(4)バージョン固定の確率校正、(5)Review/承認、(6)versioned Canonical Mappingで照合する。誤統合の損失を未統合より重く設定する。確率・LLM判断は価格/空室/本人同一性を保証しない。確証不足ならUNRESOLVED。ERを通過しない買取・購入価格は同一商品として結合しない。

## 1. 目的・対象・非対象
対象は商品（JAN/EAN/GTIN/ASIN/メーカー型番/容量/色/世代/新品中古/セット数/地域）、ホテル（正式名/住所/緯経度/建物/客室プラン別）、企業（法人識別子/名称/所在地）、人物（公開人物の明示識別子と曖昧同名）をソース横断で照合すること。住所等のPIIは権限のある目的に限定し、個人属性推定や人間の身元自動断定をしない。画像類似度だけで自動統合しない。価格が近い、文章が似ている、同じホテルチェーンという理由だけでは同一性なし。SC-01/04/05/07/08/09/10/14。

## 2. 既存実装を読んだ比較
|方式|調査した実装・テスト（pin）|利点|弱点/採用|
|---|---|---|---|
|Splinkのblockingとaccuracy|moj-analytical-services/splink@5b719296: splink/internals/blocking.py の BlockingRule.create_blocked_pairs_sql; accuracy.py の truth_space_table_from_labels_with_predictions_sqls; tests/test_accuracy.py|候補数削減、blocking漏れを評価する構造|SQL依存、確率だけで型番差分を無視不可。思想を参考、直接実行はLATER|
|Python recordlinkage|J535D165/recordlinkage@b93d976: recordlinkage/base.py BaseIndex.index/BaseCompare.compute; tests/test_indexing.py|透明なindex→compare段階|クラス別否定制約は別途必要。ベースライン比較に採用|
|dedupe|dedupeio/dedupe@3f61e791: dedupe/api.py DedupeMatching.pairs/partition, RecordLinkMatching.pairs; tests/test_api.py|教師ラベルとblocking|初期ラベル不足・誤統合時cluster cascading。実験候補のみ|
|全件fuzzy/embedding|自然言語名だけで上位候補を結合|未知の別表記拾い上げ|誤結合と説明不能。自動mergeとして却下|
|完全exact match|GTIN正規化完全一致のみ|再現性|ホテル/企業/型番欠損で未検出。高信頼seedに採用|

[S1] SplinkのIssue #793 (blocking外のtrue matchの評価) と #2059 (accuracy集計の報告) を確認。Issueは報告でありHarnessで再現した事実ではない。2026-09 Splink 5 Releaseの記事が存在するため、以前のv4 API例を現行と混同しない。License: Splink MIT, recordlinkage BSD-3条項、dedupe MIT（各LICENSE原文を確認）。

## 3. 正本・権限・依存
- CHAT_05は取得とSourceRecord draft、CHAT_01はEntity/Evidence/Observationの共有意味・UUIDv7、CHAT_10は物理DB、CHAT_09はtenant/owner隔離とPII、CHAT_08はレビューUI。ER内部の候補・判定・マッピングは本MDの論理責務。
- CanonicalEntityIdはあくまで本機能の提案IDで、Task/Observationを置き換えない。[PROVISIONAL: CHAT_01] 共有ID type、Evidence relation、version/undo eventを新設するまで、内部独立IDをエクスポートしない。
- 各lookupはtenant + ownerの利用目的+resource ACLを再検証。SourceRecord間に別tenant情報を跨がせない。HTML/Xはデータであり命令ではない。

## 4. 規範要求
|ID|優先|受入可能な要件|
|---|---|---|
|ER-001|MUST|Schema/tenant/source ID/時刻が不正なレコードを拒否し、原本と抽出版を保持|
|ER-002|MUST|domain別正規化でメーカー、GTIN checksum、ASINの市場区別、型番suffix、容量単位、色、condition、bundleを保持|
|ER-003|MUST|候補blockingを複数キーのunionで実行し、全件Cartesian積は使わず1入力上限200候補|
|ER-004|MUST|異容量/色/世代/condition/セット数量/GTIN衝突など明確な否定条件では自動merge禁止|
|ER-005|MUST|ホテルの同一建物と同一販売プランを別レベルで照合、住所同名だけで客室価格を結合しない|
|ER-006|MUST|企業・人物は公的/Provider stable IDが欠ける時自動人物同定禁止|
|ER-007|MUST|match/nonmatch/reviewの三値判定、特徴量・理由・モデル校正版を保存|
|ER-008|MUST|自動match閾値は類型ごとにpositive/negative labeled holdoutで校正、初期は自動match無効|
|ER-009|MUST|人間のreviewは2候補・差分・原証拠・否定要因を表示、強制mergeに二段確認|
|ER-010|MUST|mappingはrevision/CAS付き、merge・split・undoで過去IDの履歴と依存を残す|
|ER-011|MUST|旧revisionを使う価格/利益/Watch判断は新revision時にstaleとして再評価|
|ER-012|MUST|同一(input IDs, source rev, model rev)の判定は冪等、並行mergeで循環/二重canonicalを防止|
|ER-013|MUST|cancel/期限/再起動時に未確定候補を確定扱いにしない|
|ER-014|MUST|誤統合率、未統合率、blocking recall、review率をクラス別に独立測定|
|ER-015|MUST|高信頼識別子でも誤記/偽造を想定、ソース証拠と整合性を保持|
|ER-016|MUST|アクセス権消失/削除tombstone後に旧matchから原資料を引き出せない|
|ER-017|SHOULD|calibration drift後は自動matchからreview-onlyに戻す|
|ER-018|LATER|多言語住所/法人DBのlicensed resolverをPluginとして追加|

## 5. 内部型・バリデーション（共有型改定ではない）
~~~ts
type Domain = 'product'|'hotel_property'|'hotel_rate'|'organization'|'public_person';
type Decision = 'MATCH'|'DISTINCT'|'REVIEW'|'UNRESOLVED';
type RelationV1 = {
 schemaVersion:'1.0'; tenantId:string; ownerUserId:string;
 left:{sourceId:string; listingId:string; extractionRevision:string; evidenceId:string};
 right:{sourceId:string; listingId:string; extractionRevision:string; evidenceId:string};
 domain:Domain; decision:Decision; ruleVersion:string; modelVersion:string|null;
 features:Record<string,string|number|null>;
 vetoCodes:string[]; probabilityBps:number|null; // 0..10000, calibration以外はnull
 reasons:string[]; evaluatedAt:string; mappingRevision:string|null;
};
type MappingCommand =
 | {op:'propose'; key:string; relation:RelationV1}
 | {op:'confirm'|'reject'; relationId:string; expectedRevision:string; actorId:string}
 | {op:'split'|'undo'; canonicalId:string; expectedRevision:string; actorId:string; reason:string};
~~~
Schema: unknown majorはreject、nullを0に変換しない。listingIdとexternal IDの正規化後長さ1..256、名称1..1024、PIIは暗号化/最小化。GTINは8/12/13/14桁のchecksumが一致するものだけkeyとして使用。ASINは10英数字でもGTINと同等の世界一意IDにはしない。型番は元表記とNFKC normalizedを並記、suffixやハイフンを安全に一括削除しない。型番・数量・条件は必要に応じ明示unknownとする。ホテル座標の閾値は地理的近接の候補取得用で同一性の証明にはしない。

## 6. Sequence / lifecycle
~~~mermaid
sequenceDiagram
 participant C as Connector(05)
 participant R as Resolver(12)
 participant D as Store(10)
 participant H as Human(08)
 C->>R: source record + Evidence ref + principal
 R->>D: authorization + source revision validation
 R->>D: blocking candidate query
 R->>R: normalize / hard-veto / features / calibrated score
 alt safe and calibrated
  R->>D: atomic mapping CAS + decision receipt
 else ambiguous
  R->>D: persist review request
  H->>R: explicit decision + expectedRevision
  R->>D: reauthorize + evidence check + CAS
 end
 R-->>C: versioned relation or unresolved
~~~
内部状態: INGESTED→NORMALIZED→BLOCKED→{DISTINCT, REVIEW, MATCH, UNRESOLVED}; REVIEW→{MATCH,DISTINCT,EXPIRED}; MATCH→SPLIT_PENDING→SPLIT、MATCH→STALE→REVALIDATE。terminalに見えるMATCHでも恒久的な不変同定ではなくversioned relation。review期限後はEXPIREDで通知なし。

## 7. アルゴリズム/競合
1. 入力のtenant、所有権、Evidence validity、schemaVersion、source lineageを検証。欠損のcritical attributeをunknown。
2. 同domainのみblocking。製品は正規化GTIN、メーカー+完全型番、brand+family+capacity等のOR。ホテルはgeo-cell+住所、公式property ID等。候補200件で切り、超過はTOO_BROADでreview-only（最上位200件だけを全探索と呼ばない）。
3. 各候補のhard veto: product容量差、確定した色差/世代差、単品 vs セット、condition class相違、矛盾する有効GTIN（例外は別Evidenceで個別レビュー）、ホテル物件と料金プランの混同。否定情報が欠測ならvetoなしだが強い肯定にはしない。
4. exact相性 + token similarity + address similarity等を特徴量化。校正済みmodelVersionが未設定ならprobabilityBps=null。自動matchは domain-specific precision holdoutが設定目標を満たし、hard vetoなし、複数独立識別子が整合する場合のみ。初期default REVIEW。Score基準値は検証後の設定、データなしで恣意的に0.95固定禁止。
5. Store transaction: SELECT canonical grouping FOR UPDATE（ordered IDs）→expectedRevisionのCAS→関係とversioned history+outboxを原子的保存。競合はRETRYABLE_CONFLICTで再取得し再判定、最大2再試行。
6. Split: 新canonical IDを発行し元関係をsupersede、依存する価格/Decision/Trend/Watchに再計算フラグを立てる。履歴を物理削除/旧ID再利用しない。過去Decisionはhistorical relationVersionを保持。

## 8. 失敗マトリクス
|故障|処置|最終表現|
|---|---|---|
|source 404/secret revoked|再取得を勝手に繰返さず入力拒否|UNRESOLVED|
|block候補過多/CPU上限|bounded stop + 人間確認待ち|REVIEW/TOO_BROAD|
|モデルが未校正|自動match禁止|REVIEW|
|二重入力/queue再送|hash receipt lookup|同じdecision|
|並行human merge/split|recordVersion CAS、後勝ちしない|CONFLICT|
|crash between draft and commit|Transaction rollback、再処理|UNRESOLVED→再計算|
|mapping commit後ACK loss|receiptを取得、二重mergeなし|同じrevision|
|withdraw/delete/revoke|deny + downstream invalidation|STALE/REVOKED|
|Steer/cancel|次境界で停止、進行中の書き込みCASでgoalRevision再検証|CANCELLED|
外部副作用を自身で実行しない。Tool lost ACKはCHAT_03台帳に帰属。

## 9. Security
Tenant/owner/purpose scopeを候補左右両側で確認。Cross-tenant embeddings/近傍indexを禁止。外部文に埋められた権限指示は正規化文字列でありpolicy入力にならない。公的人物名の誤同定、住所の収集目的外利用、PIIログ漏洩、provider fingerprintの再識別を防止。Operator auditに必要最小限のID/hashだけ。権限変更・tombstone後キャッシュ無効化。利用権限・ToSレビューと削除保持は09に委譲。

## 10. 計測/有限上限
初期運用提案: 1input最大200 candidates、2s compute timeout、1tenant同時resolve 2、1run最大1000 input、review queue 500/tenant、model calls 0が既定。候補数/CPU/RSS/p95/invalid-veto/false-positive/false-negative/blocking-recall/calibration ECE/review SLAをclass別に評価。accuracy ground truthはダブルアノテーションし人間のlabel不一致率も公開。閾値の成功は実測してから決定。全数exact検索との比較はfixture 1k/10k/100kで実測する。

## 11. 保存・移行 [PROVISIONAL: CHAT_10]
提案: source_records(tenant,source,listing,extraction_rev unique)、resolution_decisions(tenant,unordered_pair,rule_rev,model_rev unique)、canonical_entities(tenant,id,revision)、canonical_membership(tenant,source_ref,canonical_id,valid_from,valid_to)、review_actions(actor,expected_rev)、invalidation_outboxをDB transactionで保存。物理DDL/FK/indexはCHAT_10が決定。featureVersion変更はbackfill shadow mappingに再処理しactive pointerをCAS切替。旧mappingは監査保持、削除は09 retentionに従う。

## 12. API/UI提案 [PROVISIONAL: CHAT_08]
候補説明は「確認済み同一」「要確認」「明確に別」「未判定」の4分類。左右の型番/容量/色/条件、URL、observedAt、sourceAtと差異、モデル・ruleVersion、取消とsplit UI、旧revisionの下流再計算表示。GET relationはviewer-authorizedのみ、POST confirm/splitはCSRF+fresh auth+If-Match+Idempotency-Key必須、応答202は確定mergeを意味しない。

## 13. Codex実装分割
ER-PR1 normalize/typed validators/GTIN checks + fixtures。ER-PR2 blocking/candidate budget + deterministic veto。ER-PR3 scoring/calibration offline + review flow。ER-PR4 PostgreSQL proposal owner合意後CAS/versioned merge/split/invalidation。ER-PR5 Web/REST契約導入+2-tenant fault/E2E。各PRで先行テストを追加し独立レビュー。

## 14. テスト設計（全てDESIGNED、実行0）
|TEST|setup → action → expected|
|---|---|
|ER-T01 Unit|invalid schema, GTIN check digits, Unicode/型番suffix → invalid reject / suffix維持|
|ER-T02 Unit|同名容量128/256・色差・新品/中古・bundle → すべてAUTO_MATCH 0|
|ER-T03 Unit|ホテルチェーン同名/別住所・同館別rate → property/rateは別段で保持|
|ER-T04 Unit|同名人物・公式ID欠損、法人住所差 → reviewのみ|
|ER-T05 Unit|候補0/201/大量入力 → 0はUNRESOLVED、過多はTOO_BROAD|
|ER-T06 Contract|校正なし・負例が多いscore → auto-match 0、reasons保存|
|ER-T07 Integration|同pair 2workers → unique receipt、revision一意|
|ER-T08 Fault|commit前Kill/commit後ACK loss → 0 or 1 mapping、再実行は同じreceipt|
|ER-T09 Integration|human splitとstale expectedVersion → CAS失敗、下流STALE|
|ER-T10 Security|tenant違いで近傍探索、source revoke → 結果0、秘密ログ0|
|ER-T11 E2E|SC-10 mixed catalog prices → 誤容量の利益0、正しい候補のみ検討可能|
|ER-T12 Benchmark|10k/100k fixture/label sample → blocking recall・誤統合/未統合別報告、CPU/RSS|
|ER-T13 Contract|旧mappingを使ったWatch再評価 → stale result非通知|
|ER-T14 Fault|cancel/expiry/Steer while processing → 未承認変更0|
|ER-T15 Security|注入HTML＋SNS人物名 → instruction実行0|
## 15. Traceability
|MUST|TEST|SC|pass oracle|
|---|---|---|---|
|ER-001,ER-002|T01,T02|01,10|invalid拒否、属性保持|
|ER-003,ER-004|T02,T05,T12|01,10|pair漏れ計測、誤統合0 fixture|
|ER-005,ER-006|T03,T04|04,10|property/rate独立・人物自動同定0|
|ER-007,ER-008|T06,T12|10,14|calibration不在でreview|
|ER-009,ER-010|T07,T09|07,10|履歴付きCAS|
|ER-011,ER-012|T07,T09,T13|02,10|stale下流抑止|
|ER-013|T08,T14|07|中途半端なmatch 0|
|ER-014,ER-015|T01,T12|09,10|独立誤差指標と根拠|
|ER-016|T10,T15|08|権限漏れ0|

## 16. Cross-team proposals/未決
P0-ER-01 [PROVISIONAL: CHAT_01] CanonicalEntity relation・mappingRevision・invalidationの論理共通型/ID UUIDv7、Evidence claim link。P0-ER-02 [PROVISIONAL: CHAT_09/10] owner→workspace scope、tenant unique FK、history/split CAS・retention。P0-ER-03 [PROVISIONAL: CHAT_05] listing item vs rate plan、provider stable identifiers and conditional ASIN/GTIN interpretation。P1-ER-04 [PROVISIONAL: CHAT_08/11] reviewer audit UIとprecision/recall別テスト。P1-ER-05 [PROVISIONAL: CHAT_07/12-04] split時Watch/Decision invalidation atomic delivery。各オーナー承認とschema version compatibility testまではDraft維持。未知の人物同定はPhase1除外してもよい。

## 17. Sources (2026-10-30 static inspection; no upstream test execution)
[S1] https://github.com/moj-analytical-services/splink/blob/5b719296d62bc48d55c1a63322fd60c0bfba4c2e/splink/internals/blocking.py ; https://github.com/moj-analytical-services/splink/blob/5b719296d62bc48d55c1a63322fd60c0bfba4c2e/splink/internals/accuracy.py ; https://github.com/moj-analytical-services/splink/blob/5b719296d62bc48d55c1a63322fd60c0bfba4c2e/tests/test_accuracy.py ; issue #793/#2059 (Issue report), MIT (Code verified).
[S2] https://github.com/J535D165/recordlinkage/blob/b93d97641952f8c85106be5794ca93b1f1298fbc/recordlinkage/base.py ; https://github.com/J535D165/recordlinkage/blob/b93d97641952f8c85106be5794ca93b1f1298fbc/tests/test_indexing.py BSD-3 (Code verified).
[S3] https://github.com/dedupeio/dedupe/blob/3f61e79102910bd355e920a2df7e44c14c9cb247/dedupe/api.py ; https://github.com/dedupeio/dedupe/blob/3f61e79102910bd355e920a2df7e44c14c9cb247/tests/test_api.py ; issue #1032/#989 (Issue report), MIT (Code verified).
[S4] https://www.gov.uk/algorithmic-transparency-records/moj-splink-master-record (Docs verified; clerical labels are imperfect). Library adoption/performance/license notices must be independently reviewed at implementation lock.
