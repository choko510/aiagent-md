# Decision & Risk Engine — Detailed Implementation Specification (12-04)

> Owner CHAT_12 | Path docs/12-intelligence/decision-risk-engine.md | Draft / PROVISIONAL 0.1 | 2026-10-30 | All tests designed, 0 executed
> Depends CHAT_01 Evidence/Observation, CHAT_05 Keepa/buyback/hotel, CHAT_07 Watch, CHAT_09 owner, CHAT_10 DB, entity-resolution 12-02.

## 0. 結論
ユーザーに見せる「利益」は必ず通貨・数量・税区分・条件・時刻・確実性が付いたシナリオ計算の結果とする。表面の買取価格−購入価格を利益と呼ばない。Phase1のJPYは整数円（BigInt内部、Wireは正規化十進文字列）、他通貨はISO通貨minor-unitと明示fx/roundingで扱う。見積/不明の手数料・買取減額を0円に補完しない。結果はVERIFIED_FACT/CONDITIONAL/ESTIMATE/UNKNOWNを別々に表示し、利益保証はしない。

## 1. 目的と非目標
購入候補/販売候補/買取候補を、税込総コスト・送料・ポイント/クーポン条件・売却費用・査定減額・在庫確認・入出金タイミング・有効期限・市場変動で比較。一般的な調査の損益・選択肢比較にも適用可能な純関数評価器。商品購入・予約・売却・送金・税務申告・投資助言の実行は非目標。利益目標3,000円は条件成立かつデータ品質を満たす場合のみ「候補」。SC-01/02/04/07/08/09/10/14/16。

## 2. 実ソース比較と採否
|実装|pin/確認した関数・テスト|採否|
|---|---|---|
|decimal.js|MikeMcl/decimal.js@b1d1e311: decimal.js Decimal.config, P.dividedBy, P.toFixed; test/modules/toFixed.js|任意精度・丸めの参考。内部JPYはBigIntを優先し、多通貨小数補助で導入検討。MIT|
|Dinero.js|dinerojs/dinero.js@76b969e5: packages/dinero.js/src/api/add.ts, src/api/__tests__/add.test.ts; bigint tests|amount+currencyという型境界を参考。ライブラリ導入はJS互換/BigInt serializer調査後。MIT|
|浮動小数Number計算|JS Numberでprice-feeを単純計算|丸め/精度/税処理で危険。却下|
|LLMに式と価格を渡して利益を決定|柔軟だがnon-deterministic/条件落ち|却下。LLMは説明文生成のみ|
|期待利益単一値|不確実性を隠す|却下。worst/base/bestの各条件の伝播とunknownを必須|

## 3. 境界と正本
CHAT_05はread-only価格/商品条件、CHAT_01はEvidenceの観測属性と共通ID、12-02はversioned identity、CHAT_07は閾値Watch、CHAT_03は副作用ゲート。CHAT_12内部の計算ScenarioとDecisionReceiptは提案であり共有Event定義ではない。[PROVISIONAL: CHAT_01/10] TransactionとEvent/DDLの正本を提案する。所有者が違うSourceRefを合算しない。

## 4. 要件
|ID|Priority|要求|
|---|---|---|
|RISK-001|MUST|金額は整数minor-unit十進文字列、ISO通貨、丸め規約を検証し浮動小数で課金計算しない|
|RISK-002|MUST|入力をEvidence ref+sourceAt/observedAt/validUntil+condition/stock/identity revisionで束ねる|
|RISK-003|MUST|source失敗/未知fee・税/送料/査定条件を0と見なさずUNKNOWNを伝播|
|RISK-004|MUST|必須費用は仕入総額、税込区分、送料、決済手数料、輸送、売却手数料、買取条件、クーポン適用条件の多重計上を防ぐ|
|RISK-005|MUST|クーポンを利用資格、最低購入額、上限/使途、数量、期限、同時利用不可、地域で判定。不明なら自動控除しない|
|RISK-006|MUST|利益の3区間は独立した明示シナリオで求め、資料のない確率分布/収益率を創作しない|
|RISK-007|MUST|査定減額（開封、付属品、状態、期限）が不確実なら確認事項とリスクとして表示|
|RISK-008|MUST|原価と売値の通貨が違う場合FX rate Evidence/quotedAt/fees/rounding不足なら比較拒否|
|RISK-009|MUST|カタログ同一性がUNRESOLVED/STALEなら同一商品利益算出を保留|
|RISK-010|MUST|取得時点と購入可能/売却可能を区別し、stale価格・欠品を確定と表示しない|
|RISK-011|MUST|同一input evidence set+scenario version+ruleVersionで計算決定性と冪等性を保証|
|RISK-012|MUST|キャンセル・Steer・取消時に旧scenarioを新推薦に混ぜない|
|RISK-013|MUST|演算overflow/通貨違い/ゼロ除算/negative invalid/条件矛盾を明示エラーとして返す|
|RISK-014|MUST|候補の順位にexpected marginだけでなく情報鮮度、capital at risk、uncertaintyを使い、unknownで勝手に上位化しない|
|RISK-015|MUST|入力の誤記修正やMapping splitで全依存DecisionをSTALE化|
|RISK-016|MUST|出典・差分・税/送料/クーポン適用内訳を利用者に説明可能に保存|
|RISK-017|SHOULD|意思決定の比較軸をユーザー定義で変更可能だが権限・真実値は変更させない|
|RISK-018|LATER|原価保有期間/需要確率の校正モデルを実測検証の後に追加|

## 5. Portと型（internal; shared変更提案は別）
~~~ts
type Money = {currency:'JPY'|'USD'|'EUR'|string; minor:string; exponent:0|2|number};
type Quality = 'OBSERVED'|'CONDITIONAL'|'ESTIMATED'|'UNKNOWN';
type PriceComponent = {kind:'purchase'|'tax'|'shipping'|'payment_fee'|'coupon'|'points'|'resale'|'buyback_discount'|'selling_fee'|'other'; amount:Money|null; quality:Quality; evidenceId:string|null; conditionIds:string[]; observedAt:string|null; validUntil:string|null};
type Preconditions = {identityRevision:string; entitlementVerified:boolean|null; stockAvailable:boolean|null; conditionChecked:boolean|null; saleWindowValid:boolean|null};
type ScenarioInputV1 = {schemaVersion:'1.0'; tenantId:string; ownerUserId:string; scenarioId:string; revision:string; currency:string; quantity:string; buyComponents:PriceComponent[]; sellComponents:PriceComponent[]; constraints:{minProfit:Money|null; maxSpend:Money|null}; preconditions:Preconditions; inputDigest:string};
type RangeValue={quality:Quality; amount:Money|null; reasons:string[]};
type DecisionV1={schemaVersion:'1.0'; scenarioId:string; revision:string; status:'ELIGIBLE'|'CONDITIONAL'|'INSUFFICIENT_DATA'|'STALE'|'INELIGIBLE'; worst:RangeValue; base:RangeValue; best:RangeValue; sourceRefs:string[]; conditionFailures:string[]; effectiveCost:Money|null; rulesVersion:string; computedAt:string};
~~~
Validate ISO4217 currency code and configured minor exponent (JPY=0), quantity integer 1..1000, money magnitude <= 10^15 minor units, minor canonical regex /^(0|-?[1-9][0-9]*)$/, no leading '+', NaN, exponent string. Allow negative only signed discount and adjustment with explicit component kind. For JPY no fractional input; percentages rational basis points 0..10000 applied via BigInt numerator/denominator with explicit rounding policy: fee/tax cost toward +∞ (ceil), discount/benefit toward zero (floor positive discount). Already-tax-included purchase must not add duplicate tax. Component collection max100 each; unknown mandatory monetary term must set decision UNKNOWN.

## 6. 状態/シーケンス
~~~mermaid
sequenceDiagram
 participant A as Agent/Task02
 participant C as Connector05
 participant E as Entity12-02
 participant R as Risk12-04
 participant V as Verifier02
 A->>C: read-only offers with scope/budget
 C-->>A: Evidence + observedAt/conditions
 A->>E: identity relation + revision
 E-->>R: MATCH or ambiguous
 A->>R: scenario revisions+constraints
 R->>R: Validate / classify / calculate 3 bounds
 R-->>V: immutable receipt + uncertainty
 V-->>A: require proof before eligible suggestion
~~~
内部状態 CREATED→VALIDATED→{REJECTED,WAITING_DATA,CALCULATED}→{ELIGIBLE,CONDITIONAL,INELIGIBLE}。任意input revision変更→STALE→RECALCULATE。Cancel→CANCELLED（既存receiptは履歴に残す）。販売/購入の実行状態はここに持たない。

## 7. 正確な計算
~~~ts
function evaluate(x:ScenarioInputV1):DecisionV1 {
 validateStrict(x); authorizeIdentityAndEvidence(x);
 if (identityRevisionIsStale(x) || anyRequiredEvidenceExpired(x)) return stale();
 if (!requiredPreconditionsKnown(x) || mandatoryCostUnknown(x)) return insufficientData();
 const buy = sumBigInt(x.buyComponents, 'cost', pricePolicy);
 const sell = sumBigInt(x.sellComponents, 'revenue', pricePolicy);
 // Coupons: only if every applicability condition confirmed; other cases:
 // conditional in base/best; not cost-reduced in worst.
 // buy includes tax exactly once and all required shipping/fees.
 const net = sell - buy; // signed bigint, no float
 const variants = calculateExplicitWorstBaseBestFromVerifiedBounds(x);
 return classifyWithConditions(variants, x.constraints, currentAuthorizationAndGoalRevision());
}
~~~
算式: TotalCost = unitPurchase*quantity + nonincludedTax + inboundShipping + paymentFee + outboundShipping + otherVerifiedCosts − eligibleDiscount。GrossRevenue = acceptedBuybackOrSalePrice*quantity − buybackConditionDeductions − sellingFees − returnRiskCosts。Net = GrossRevenue − TotalCost。ポイントは現金同等か資格の証明がなければNetから控除しない。買取上限「最大」だけの場合、max scenarioは示せてもbaseを確定値にしない。利益閾値判定はworst/base/bestと証拠のqualityを併記。worstの金額下界すらunknownならminProfit満足をTRUEにできない。3 scenariosは観測されたboundsと明示仮定で生成し、恣意的確率重みなし。

例: unit purchase=40,000円、適用確認済のクーポン=1,000円、送料=500円、売却受領=45,000円、売却送料=700円、査定減額0円が公式条件で確証あれば TotalCost=39,500、GrossRevenue=44,300、Net=4,800円。ただし在庫不明/査定条件未知なら結果はCONDITIONALまたはINSUFFICIENT_DATA、確定利益4,800円とは表示しない。

## 8. 障害と競合
|事象|処置|表示|
|---|---|---|
|fee/tax/stock unknown|算入0を禁止、関連項目をWAITING_DATA|INSUFFICIENT_DATA|
|クーポン期限切れ、資格不一致|worst/baseから割引除外、失敗条件記録|INELIGIBLE/CONDITIONAL|
|異通貨FX refなし|拒否|UNCOMPARABLE|
|identity split/quote expire|stale markerをcommit|STALE|
|取得失敗、timeout|Evidence acquisition statusと分離|UNKNOWN|
|二重Task/同digest再送|immutable receipt idempotent lookup|同結果|
|並列新旧計算|source/goalRevision CAS、古い結果を公開しない|STALE|
|commit前Kill/ACK loss|transaction rollbackまたはreceipt再取得|重複出力なし|
|巨大数・divide-by-zero|typed validation error、異常な金額を不採用|REJECTED|
|cancel/Steer|safe boundaryで停止、旧version結果を不採用|CANCELLED|
未知のTool side-effectはCHAT_03 ledgerが判断。Risk側で非冪等な購入を再試行しない。

## 9. Security
Evidence URLも金額も認可を保証しない。Tenant/owner/credential scopeを計算前とpublish前に再検証。クーポン欄などのWeb文字列は指示扱いしない。secret/配送先個人住所/カード番号を計算イベントに出さない。買付や売却の提案から実行Toolを呼ばず、将来はApproval + Gatewayを必要とする。すべての数値のsourceとconversion rateを可監査にする。

## 10. 上限/計測
1request最大100候補、候補当たり200 price components、3 scenarios、validation+compute p95 200msを仮SLO、1tenant同時計算4、recompute連鎖深さ5、1実行最大1,000候補、LLM call 0が基準。timeout2秒でpartial status。CPU/RSS/decimal conversion failures、missing-fee-rate、coupon-ineligible、stale/provisional ratio、error rates、false-positive profit alerts、JPY rounding driftを計測。ベンチ環境・fixtureを固定し実測後SLO採用。

## 11. 保存 [PROVISIONAL: CHAT_10]
Evaluation receipts(tenant,scenarioId,revision,inputDigest,rulesVersion,computedAt,status,components hashes,bounds,provenance,invalidatedAt) unique(tenant,inputDigest,rulesVersion)、immutable source refs、append-only invalidation links。原ソースへ書込まない。全額をDB numeric/int64へ落とす前に範囲検証、Wireはdecimal strings。Source expiry・entity split・auth revoke時、invalidationとoutboxを同一commitで記録する案。Schema migrationはdual-read old→new、計算version固定で過去の価格表示を再解釈しない。

## 12. UI/API提案 [PROVISIONAL: CHAT_08]
購入額/送料/税/手数料/クーポン適否/買取額/減額条件/在庫/価格時刻を項目別表にし、観測/条件付き/推定/不明を明記。利益フィルタは「確認済みの条件での利益」と「条件が満たされれば」の区別を必須にする。GET /decisions/:id は非秘密の派生Read APIであり、新しい承認の省略根拠にはならない。旧scenarioのstale表示は更新通知して再評価待ち。

## 13. Codex実装PR
RISK-PR1 bigint money validators, same-currency arithmetic, exact rounding fixture。PR2 coupon qualification & taxes/double-count guards。PR3 bounds/scenario classification + dependency invalidation。PR4 DB projection proposal owner合意後 + UI read model。PR5 fault/auth/benchmark/SC10 E2E。Runtime codeはこのrepoへ書かない。

## 14. Designed-only tests（Harnessで実行0）
|ID|setup/action/expected|
|---|---|
|RISK-T01|JPY 0.1/NaN/exponent/huge/negative-invalid → validator reject|
|RISK-T02|税込と税別、送料二重登録 → exact total and no double count|
|RISK-T03|期限切れ/併用不可/条件不明Coupon → 不正割引0|
|RISK-T04|査定減額/上限のみ/送料不明 → unknown propagated; no guaranteed profit|
|RISK-T05|USD-JPY FX証拠なし/古い → UNCOMPARABLE|
|RISK-T06|同商品128/256混在→ identity gate blocks profit|
|RISK-T07|同Evidence+ruleVersion 100回→ same receipt hash|
|RISK-T08|2 workers new/old revision race→ old publish 0|
|RISK-T09|commit前Kill/commit後ACK loss→ 0 or 1 receipt, replay dedup|
|RISK-T10|Steer/cancel/revoke→ no new eligible result|
|RISK-T11|3k円条件 threshold+unknown shipping→ no false positive|
|RISK-T12|price observed old + stock unknown→ stale/unknown visible|
|RISK-T13|two tenants same product price→ cross-owner evidence 0|
|RISK-T14|1000 candidates + memory budget→ bounded completion/failure|
|RISK-T15|source correction/identity split→ dependent decisions STALED|
|RISK-T16|injection in coupon source→ no policy/tool execution|
## 15. MUST requirement → TEST → SC 追跡（全ID・省略表記なし）

|Requirement|Designed TEST (未実行)|Acceptance Scenario|確認可能な合格条件|
|---|---|---|---|
|RISK-001|RISK-T01, RISK-T02|SC-01, SC-09|整数minor-unit・丸め|
|RISK-002|RISK-T12|SC-01, SC-10|Evidence・時間・Identity revision保持|
|RISK-003|RISK-T04, RISK-T11|SC-01, SC-10|未知項目が利益確定に変換されない|
|RISK-004|RISK-T02, RISK-T04|SC-01, SC-10|送料/税/手数料の重複0|
|RISK-005|RISK-T03|SC-01, SC-10|適用不可クーポン割引0|
|RISK-006|RISK-T04, RISK-T11|SC-01, SC-10|3区間と品質を独立表示|
|RISK-007|RISK-T04|SC-01, SC-10|査定減額条件を不明として表す|
|RISK-008|RISK-T05|SC-01, SC-09|FX根拠なし比較拒否|
|RISK-009|RISK-T06|SC-10|異SKU利益比較を停止|
|RISK-010|RISK-T12|SC-01, SC-10|時点/stock不明の確定表示0|
|RISK-011|RISK-T07, RISK-T09|SC-07|同入力/版で計算一意|
|RISK-012|RISK-T08, RISK-T10|SC-07|旧goal revision結果非公開|
|RISK-013|RISK-T01|SC-01, SC-09|不正数/負値/0除算拒否|
|RISK-014|RISK-T11, RISK-T14|SC-09, SC-10|unknownを上位確定候補にしない|
|RISK-015|RISK-T15|SC-10, SC-14|訂正時下流STALE|
|RISK-016|RISK-T12, RISK-T13, RISK-T16|SC-01, SC-08|内訳と認可済み証拠追跡|


## 16. Cross-team proposal
P0-RISK-01 [PROVISIONAL: CHAT_01] typed Money/exact currency minor units+QualityをEvidence relationへ提案（既存Observationを再定義しない）。P0-RISK-02 [PROVISIONAL: CHAT_05] taxIncluded/fees/coupon/availability/quote validity/condition fieldsをConnectorのnormalized draftへ必須化。P0-RISK-03 [PROVISIONAL: CHAT_10] receipt/invalidation unique+FK/decimal string persistence。P0-RISK-04 [PROVISIONAL: CHAT_09] cross-owner quote use and proof of entitlement。P1-RISK-05 [PROVISIONAL: CHAT_07/08/11] Watch threshold, UI quality, adversarial exact-arithmetic tests。共同承認までDraft。通貨換算・税率の外部最新値は固定せず必ずsource付き。

## 17. Sources (source code/tests read 2026-10-30; not executed)
[S1] https://github.com/MikeMcl/decimal.js/blob/b1d1e311eb272b16f5b2ca0e0d1f4c4fa8558edf/decimal.js ; https://github.com/MikeMcl/decimal.js/blob/b1d1e311eb272b16f5b2ca0e0d1f4c4fa8558edf/test/modules/toFixed.js ; https://github.com/MikeMcl/decimal.js/blob/b1d1e311eb272b16f5b2ca0e0d1f4c4fa8558edf/LICENCE.md MIT (Code verified).
[S2] https://github.com/dinerojs/dinero.js/blob/76b969e519dc44675d4af898d25629995d0b16f2/packages/dinero.js/src/api/add.ts ; https://github.com/dinerojs/dinero.js/blob/76b969e519dc44675d4af898d25629995d0b16f2/packages/dinero.js/src/api/__tests__/add.test.ts ; LICENSE MIT (Code verified).
[S3] https://keepa.com/api-docs/plans-tokens.html (Docs verified 2026-10-30; tokens/limits, not a live query). Currency FX rates/tax policy/provider fees remain UNVERIFIED until configured/tested; no profit guarantee.

## 18. Round 4 — Risk decision receipt / exact money and freshness gate
CHAT_10 proposes immutable `risk_decision_receipts(tenant,candidate,engineVersion,inputDigest, expectedProfitMinorUnits NULLABLE, currency, uncertainty, scenarioJson, evidenceRefs)`. Decision computation uses BigInt minor units with currency exponent and source/tax/discount applicability; stock, buyback capacity, variant, coupon eligibility, purchase restrictions and validity time are independent conditions. Missing required cost or uncertain buyback cannot become verified expected profit: `expectedProfitMinorUnits` may be NULL, `uncertainty=conditional/estimated/unknown`; NO floating binary money.
Fenced candidate identity `canonicalEntityId + mappingRevision` must be current at both calculate and publish. A later source correction revokes a previous profit verification without rewriting its historical receipt. Discount campaign tweets do not imply source authority; only a matching, time-current provider price and eligibility Evidence can be used.
Tests RISK-R4-T01 unknown resale fee→expectedProfit null; T02 currency mismatch without FX Evidence→reject; T03 JPY exact coupon minor units→BigInt deterministic; T04 same inputHash/version replay→same receipt; T05 stale SKU mapping→no publish; T06 hidden tax/condition→conditional label. SC-01/10/14.

