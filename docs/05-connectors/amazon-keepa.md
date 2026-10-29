# Amazon / Keepa Provider — Detailed Implementation Specification

> Owner CHAT_05 | path `docs/05-connectors/amazon-keepa.md` | Status **Draft / provisional** | rev 0.1 | 2026-10-30 | Base main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Related `connector-framework.md` [PR #5](https://github.com/choko510/aiagent-md/pull/5).

## 0. 結論・採用案
Amazon商品情報の取得を **(A) Keepa公式有料APIの履歴と検索、(B) Amazon Creators APIの現時点の公式提供範囲、(C) 利用が認められた販売ページの観測** の別々のProvenanceとして扱う。**Keepa履歴は現在購入できる価格/在庫の保証ではない**。クーポンや招待条件、送料・税・ポイントは一般の価格から決して推測しない。Source APIに利用資格がない場合、Providerは`UNAVAILABLE / ENTITLEMENT_REQUIRED`を返し、代替の無断スクレイピングをしない。購入や出品のToolはこのAdapterに存在しない。API keyはCredential Broker内部で取り扱い、Query string中のKeepa keyも秘匿する。

**2026-10-30の重要な公式仕様差分**: Amazon Associatesは **PA-API 5の廃止とCreators APIへの移行**を案内している。新規PA-API 5固定実装は却下。Creators APIの`SearchItems/GetItems/GetVariations/GetBrowseNodes`は公式で案内されるが、アクセス資格（Associate審査、成果条件、登録、用途、地域）を満たしたことは未確認 [A1][A2]。

## 1. Scope / non-scope / owner
- Scope: marketplace domain選択、ASIN/GTIN等の入力validation、商品候補検索、Keepa価格系列/集計/買い物条件への紐付け、Creator APIカタログ検索、Provider cost token budgeting、provenance保持、監視向け価格観測の作成。
- Non-scope: 類似商品を同一SKUと確定（CHAT_12 Entity Resolver）、期待利益と査定リスクの最終判断（CHAT_12 Decision Risk）、HTTP/Browser実行権限（CHAT_04/09）、Watchと通知（CHAT_07）、DB DDL（CHAT_10）、購入（承認なしなら常に不可）。
- In: Gateway-approved read-only call with opaque tenant + RunFence + cost reservation。Out: Provider-specific observation draft → CHAT_01 Evidence。実データ/価格の正本は外部サイトであり、DBには証跡と時点を保存。
- SC-01, SC-02, SC-05, SC-07〜10, SC-12/14。

## 2. 実ソース調査・代替比較（2026-10-30）
| Source | 確認箇所・事実 | 採否 |
|---|---|---|
| Keepa公式API [A3][A4] | `/product`はASIN/コード、`/search?type=product`はkeyword検索、`/query`は複雑フィルタ、`/deal`は値下がり候補。レスポンスに`tokensLeft`、`tokensConsumed`、`refillIn`、`refillRate`。商品トークン費用と追加offer page費用は別。 | REST契約を参照、token予約額はoperationとoptionsの上界による。プラン/契約により利用可否が変化 |
| akaszynski/keepa [A5][A6] | `src/keepa/keepa_async.py` `AsyncKeepa.query`（156行以降）でitemを分割、`_product_query`（274行〜）でASIN/コード・domain・history/offerを送信し`parse_csv`。`wait_for_tokens`（144行）でrefill待機。`tests/test_async_interface.py`の`test_productquery_nohistory`、`test_productquery_offers`、`test_invalid_domain`。 | **コードはコピーせず参照**。このSDKのプロセス内`tokens_left`と`asyncio.sleep`は分散Worker課金管理には不十分。Python sidecarの採用は別判断 |
| scrapinghub/price-parser [A7] | `price_parser/parser.py` `Price.fromstring`（28行〜）は`Decimal`、通貨記号、separatorを分離。`tests/test_price_parsing.py`（`test_parsing` 1453行〜）。 | 文字列表記の参考。locale/税込・送料・適用条件は解析対象外、Parsed amountのみを総額と扱うことは却下 |
| Amazon Creators API [A1][A2] | 公式はPA-API 5廃止、Creatorsのカタログoperationと資格要件を案内。 | 公式対応を条件付きで検討。APIの利用権が確認されるまでdisabled |
| Scrapling [A8] | robots・fetch抽出に利用可能だがWeb所有はCHAT_04。 | APIの代用品としてアクセス制限回避をしない |

**比較**: Keepa-only は履歴分析に強いがクーポン現時点/確定在庫を保証しないため不採用。Creators-only は履歴・SKU差分の不足があるので不採用。**履歴＋利用可能な公式カタログ＋明示的に許可されたWeb実測**を別観測として統合する。料金/ドメイン別利用権はAPI契約に依存し、架空の円/月単価は定めない。Keepa SDK LICENSEは[A9]、price-parser LICENSEは[A10]で導入時に検証しSBOMへ記録する。

## 3. Dependencies / Trust boundary
`User→CHAT_02 AgentLoop→CHAT_03 Gateway(policy/approval/ledger)→CHAT_05 AmazonKeepaAdapter→CHAT_09 Authorized Transport/Credential Broker→Keepa or Amazon official API→CHAT_01 Evidence→CHAT_12 Resolver/Risk→CHAT_07 Watch`。Browser routeはCHAT_04に独立依頼、同一Credentialを転用しない。source textはuntrusted。Run/Task IDは内部UUIDv7、ASINやJANは**外部識別子**。`tenantId`≠`workspace_id`を仮定しMappingはCHAT_09/10に委譲。`leaseEpoch`はlossless string/bigint。Chat #05 internal typeは共有正本でない。

## 4. Requirements
| ID | Priority | observable rule | SC |
|---|---|---|---|
| AMK-001 | MUST | 資格/region/ToSが有効なSourceだけ公開・実行 | 01,08 |
| AMK-002 | MUST | KeepaとAmazon Creator APIを混同せずversion/endpoint/claimを分離 | 01,05 |
| AMK-003 | MUST | ASIN/domain/GTINのvalidation、色/容量/状態の不一致を自動統合禁止 | 01,10 |
| AMK-004 | MUST | Keepa token数を最大費用で事前予約し、結果`tokensConsumed`を突合、quota未知なら拒否 | 07,09 |
| AMK-005 | MUST | 価格履歴の時点/通貨/税込不明/series gaps/stock欠損を保持 | 01,12 |
| AMK-006 | MUST | クーポン、ポイント、送料、税、ユーザー資格の不明を利益確定値に加算/減算しない | 01,10 |
| AMK-007 | MUST | 現時点のAmazon実売・buybox・新品/中古・seller/stockを履歴や統計から捏造しない | 01 |
| AMK-008 | MUST | Secret/SSRF/egress/redirect/Rate policyはGatewayとBrokerを通す | 08 |
| AMK-009 | MUST | キャンセル・旧`leaseEpoch`・旧goalRevision時の再送/新規観測Commitを禁止 | 07 |
| AMK-010 | MUST | API取得失敗はunknown、0円・在庫なし・商品不存在と見なさない | 02,07 |
| AMK-011 | MUST | 再起動後もattempt/usage receiptと観測をreconcileし、API課金を盲目的再試行しない | 07,09 |
| AMK-012 | MUST | 出典URL・native IDs・obtainedAt・effectiveAt・history sourceを保存 | 01,14 |
| AMK-013 | SHOULD | 独立source間の突合とconfidenceをUserへ表示 | 01 |
| AMK-014 | LATER | Keepa deal / Product FinderをOpportunity Radarへsource capabilityとして追加 | 10 |

## 5. I/O / validation — 内部提案型 [PROVISIONAL: CHAT_01/03/12]
~~~typescript
interface AmazonQuery {
  kind: "product" | "keyword" | "finder" | "deal";
  marketplace: "JP" | "US" | "DE" | "UK" | "FR" | "IT" | "ES"; // v1 allowlist; eligibility per marketplace
  asin?: string; // ASCII uppercase [A-Z0-9]{10}; source-validated, not product identity proof
  gtin?: string; // digit-only, check digit verified when applicable
  keywords?: string; // NFC 1..200 chars
  history?: boolean; // explicitly requested; default false for low-cost current lookup
  offerPages?: number; // default 0; bounded by entitlement and budget
  maxResults: number; // default 10, max 50, pagination bounded
  freshnessNeededAt?: string; // UTC
}
interface AmazonPriceClaim {
  provider: "keepa" | "amazon_creators" | "approved_web";
  marketplace: string; asin?: string; gtin?: string; sellerId?: string;
  brand?: string; model?: string; capacity?: string; color?: string;
  condition: "new" | "used" | "refurbished" | "unknown";
  amountMinor?: string; currency?: string; taxIncluded?: boolean;
  shippingMinor?: string; couponMinor?: string; couponEligibility?: string;
  pointValueMinor?: string; stock: "available"|"unavailable"|"unknown";
  observedAt: string; sourceEffectiveAt?: string; sourcePublishedAt?: string;
  availableToUser: "confirmed" | "conditional" | "unknown";
  rawEvidenceRef?: string; sourceUrl: string; missingFields: string[];
}
~~~
**Invariant**: monetary input is integer minor units/decimal string, never JS float. Keepaの内部price単位/特殊値/CSV timeは**Keepa response objectの適切なfield定義とmarketplace**でdecodeし、推測して100で除算しない。ASIN valid形式だけで同一JAN・容量・色を確定しない。Providerのhistorical chart timestampsとFetchedAtは別。無効/負値/未知の特殊値は`missing`に変換するが実価格0として表示しない。Query/resultsは64KiB/2MiB, max50 results/3 requests per approved operation; Gatewayさらに厳格可。

## 6. Sequence / states
~~~mermaid
sequenceDiagram
 participant A as Agent/Watch
 participant G as Gateway
 participant C as AmazonKeepaAdapter
 participant Q as Quota/Broker
 participant P as Provider
 participant E as Evidence store
 A->>G: read query + RunFence
 G->>G: schema/policy/budget/intent persisted
 G->>C: approved opaque invocation
 C->>C: eligibility, marketplace, SKU validation
 C->>Q: reserve tokens worst case + scoped credential
 Q-->>C: lease
 C->>P: authorized read request
 P-->>C: response + consumption/status
 C->>Q: settle usage or unknown
 C->>C: normalize historical/current claims separately
 C->>E: raw receipt + Evidence draft
 E-->>G: committed receipt
 G-->>A: partial/complete + source/time/limits
~~~
`eligible→quota_reserved→sent_or_unknown→received→normalized→committed`, otherwise `blocked/auth_required/quota_wait/partial/uncertain`. Only CHAT_02 owns canonical Task statuses.

## 7. Deterministic algorithm / concurrency
~~~typescript
async function runAmazonRead(gatewayGrant, q, signal) {
  requireReadGrantAndFence(gatewayGrant);
  const src = chooseExplicitEligibleSources(q.marketplace, gatewayGrant);
  if (!src.length) return unavailable("ENTITLEMENT_REQUIRED");
  const valid = strictValidate(q);
  for (const source of src.slice(0, 2)) {
    if (signal.aborted || !(await gateway.fenceCurrent(gatewayGrant))) break;
    const worstCase = calculateCostUpperBound(source, valid);
    const lease = await quota.tryReserve(source, worstCase, gatewayGrant.deadline);
    if (!lease) return quotaWait();
    const attempt = await gateway.startExternalAttempt(gatewayGrant, lease);
    // at-least-once observation; no exactly-once remote call assertion
    try {
      const raw = await transport.fetchWithScopedSecret(attempt, valid, signal);
      await quota.settle(lease, parseUsageOrUnknown(raw));
      const claims = normalizeWithParserVersion(raw, valid);
      await evidence.persistAtomicReceipt(attempt, claims);
    } catch (err) {
      await quota.markUnknown(lease);
      await gateway.recordUncertainAttempt(attempt, redact(err));
      if (!transportCanProvePreSend(err)) return uncertainOrPartial();
    }
  }
  return verifiedClaimsOrPartial();
}
~~~
- Keepa`tokensLeft/refillIn`はProvider応答時刻の**参考値**。同一API keyを共有するmulti-WorkerではCHAT_10 atomic quotaが正本。高価なoffer/finder/dealは別上限+ユーザー予算を必須。再実行した同じreadが二重課金され得るため、output ACK lossではraw receipt照会を先に行う。
- Keepa API keyがURL parameter形式でもURLそのものをログに書かない。signed/per-session URLsをHTMLに返さない。
- CursorはProvider-nativeで期限/範囲付き。`queryFingerprint` + marketplace + source + credential scope + versionを固定し、cross-tenant reuse禁止。
- Known 4xx: unsupported plan/invalid code→fail; 401/403→Broker再認可・無限refresh禁止; 429→retry-after遵守、deadline越え再送なし; 5xx/timeout→課金不明/部分成功、Gatewayで再評価; cancellation→外部送信済ならunknown reconciled。

## 8. Failure matrix
| Trigger | Observed | Recovery | User visible |
|---|---|---|---|
| PA-API 5 legacy 403 | deprecation/eligibility | Creators APIへ **新たな契約承認後のみ** 移行 | unsupported API |
| Keepa exhausted token | 429/`tokensLeft`/quota | shared cooldown + schedule next within deadline | rate limited, nextCheckAt |
| No offers/seller unknown | empty offer list | history may remain evidence only | no confirmed buyable price |
| Coupon with user-eligibility unclear | page content | no auto-discount; request verification | conditional estimate |
| CSV negative sentinel/missing | parser | absent fields + raw provenance | unavailable, not free |
| Worker kill after request | ledger unknown | receipt or provider reconcile; no blind resend | uncertain |
| Other tenant cached item | cross-scope | reject cache; security audit | unavailable |
| Stale task lease/steer | fence fail | drop commit/current, preserve historical receipt | cancelled/stale |
| Time/locale mismatch | timezone/marketplace validation | require source-local definition | cannot compare |
| Data not current | old effectiveAt | stale flag; authorized refresh only | historical |

## 9. Security / privacy / rights
Official API利用資格とサービス条件を顧客ごとに確認。Amazon Associates/Creatorsのcontent use/キャッシュ/再配布範囲を管理者reviewし`approved`にするまでoff。Keepa key、Creators creds、セッションCookie、X-AuthをLLM/trace/artifactに含めない。QueryにPIIがあれば外部送信の明示ポリシー検証。Bot制限突破・未承認購入/出品・無許可スクレイピングは禁止。レビュー済みoriginだけをAuthorized Transportで利用。

## 10. Metrics and benchmarks
source capability, region, query kind, token reserved/consumed/unknown, item count, 401/429, bytes, parser error, fetchedAt age, missingFields, seller/condition gap, provider dollar cost (observed vs assumed), p50/p95 requests, duplicate probes, match dispute率（判定はCHAT_12）を測定。FixtureはJP/US・容量差・税込/税抜・欠品/クーポン、古いprice history。**速度/成功率/課金の実測なし**。

## 11. Persistence / UI / APIs / Owner proposals
- [PROVISIONAL: CHAT_01] Provider-native `asin/GTIN/sellerId`、Source URL、Observed/effective timestamps、price componentsをEvidenceの出典・条件として保存。`providerClaim`と`sourceVerified`を別にする。
- [PROVISIONAL: CHAT_10] source credential/provider quota receipt・history raw artifact hash・parserVersion・purchase condition/versionを同じ`tenantId→workspace` mapperに紐づけ、lease fencing付きCAS。物理DDLはCHAT_10が所有。
- [PROVISIONAL: CHAT_03/09] per-API operation cost/secret URL masking/TLS egress、read-only APIのPOST semantics、Gateway attempt idを合意。
- [PROVISIONAL: CHAT_12] entity-resolutionにAmazon variant+容量+seller/conditionを渡すが同一性を決めない。Risk Engineが未確認couponを利益に自動加算することを禁止。
- [PROVISIONAL: CHAT_07/08] Watchは`null`とAPI errorを価格0扱いせず、UIは観測時刻・履歴・実在の購入リンク・未確認項目を表示。API absentなら理由を示す。

## 12. Implementation PR phases
1. `AMK-P1`: credential/manifest/marketplace schemas, source gating, parser unit fixtures.
2. `AMK-P2`: Keepa `/product` read + token reservations + price history normalization + fault cases.
3. `AMK-P3`: authorized search/`/query`/`/deal` optional capabilities and cost policy.
4. `AMK-P4`: Creators API integration **資格・API migrationドキュメントを再確認後のみ**。未取得ならdisabled feature。
5. `AMK-P5`: reconciliation with Evidence/Entity Risk, Watch and UI, benchmarking/rollout.

## 13. Test plan（全て **設計のみ・未実行**）
| TEST | Type / setup → action | Expected |
|---|---|---|
| AMK-T01 | Unauthorized Keepa and Creators credentials → query | external calls 0, ENTITLEMENT_REQUIRED |
| AMK-T02 | PA-API 5 deprecation HTTP 403 fixture → fallback | no legacy retry, no automatic scrape |
| AMK-T03 | ASIN valid/invalid + capacity/color/condition mismatches | malformed input reject, wrong SKU never merge |
| AMK-T04 | two Workers race for 3-token pool, offers upper bound >3 | total admitted <=3 units; no negative quota |
| AMK-T05 | Keepa CSV sentinel/gap vs current offer/clock | missing separated, historical not current |
| AMK-T06 | Coupon only for targeted users, unknown shipping/tax | risk engine cannot claim certain positive profit |
| AMK-T07 | Keepa report no stock/unknown Buy Box | no claim of buyable price |
| AMK-T08 | SSRF redirect, credential in error URL, malicious seller text | blocked origin, log redaction, injection not instruction |
| AMK-T09 | canceled/steered run, leaseEpoch >2^53, stale receipt | no stale dispatch/commit; precision preserved |
| AMK-T10 | 503/429/timeout + 0 products | unavailable != zero price; 429 cooldown |
| AMK-T11 | crash after external send before evidence commit | no blind repeat; reparse cached raw receipt |
| AMK-T12 | two conflicting timestamps and native IDs | all retained, confidence remains source-level |
| AMK-T13 | optional search/deal cost max exceeded | stop before external send; no runaway loops |
| AMK-T14 | benchmark fixed JP fixture with fake token budget + 1/4 Workers | report latency, token, evidence completeness; no predefined speed claim |

## 14. Traceability
| MUST REQ | TEST | SC |
|---|---|---|
| AMK-001 | T01,T02 | SC-01,08 |
| AMK-002 | T02,T12 | SC-01,05 |
| AMK-003 | T03 | SC-01,10 |
| AMK-004 | T04,T13 | SC-07,09 |
| AMK-005 | T05,T12 | SC-01,12 |
| AMK-006 | T06 | SC-01,10 |
| AMK-007 | T07 | SC-01 |
| AMK-008 | T08 | SC-08 |
| AMK-009 | T09 | SC-07 |
| AMK-010 | T10 | SC-02 |
| AMK-011 | T11 | SC-07,09 |
| AMK-012 | T12 | SC-01,14 |

## 15. Open issues
- **P0 / CHAT_09/03** API credential and URL query secret masking, rights verification, entitlement plan check; no active provider until implemented.
- **P0 / CHAT_01/10** External evidence/native ID vs UUIDv7 internal, owner mapping, price history storage and atomic receipt; ``SC-01/07`。
- **P1 / CHAT_12** JAN/ASIN identity, costs and coupon condition verification; not this Provider's domain.
- **P1 / CHAT_07** watch stale vs failed vs zero and scheduling tokens.
- **P1 / CHAT_05** Keepa specific plan price/token max option behavior, Creators program acceptance, locale support must be revalidated with real credentials. Application not connected; no price benchmark.
- Review Gate: spec Draft and tests designed only, not runtime verified. A source code test read is not our test execution.

## 16. Sources (viewed 2026-10-30)
- [A1] **Docs verified** Amazon PA-API 5 deprecation: https://affiliate-program.amazon.com/creatorsapi/docs/en-us/paapiv5-deprecation
- [A2] **Docs verified** Creators API introduction, operations and Associates eligibility: https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction
- [A3] **Docs verified** Keepa API overview, endpoints/token costs: https://keepa.com/api-docs/
- [A4] **Docs verified** Keepa `tokensLeft/refillIn/tokensConsumed`: https://keepa.com/api-docs/request-basics.html
- [A5] **Code verified** akaszynski/keepa `AsyncKeepa.query/wait_for_tokens/_product_query`, pinned commit `32803c0e35e6adebf298074dc69c9c1e7f7e9e99`: https://github.com/akaszynski/keepa/blob/32803c0e35e6adebf298074dc69c9c1e7f7e9e99/src/keepa/keepa_async.py#L144-L331
- [A6] **Code verified, test content inspected not executed** Keepa `test_productquery_nohistory`/`test_productquery_offers`/`test_invalid_domain`: https://github.com/akaszynski/keepa/blob/32803c0e35e6adebf298074dc69c9c1e7f7e9e99/tests/test_async_interface.py#L185-L300
- [A7] **Code verified** scrapinghub/price-parser, pinned `6718bfe8447f2de17ecc152d3ad2a4c51520c755`, `Price.fromstring` and tests: https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/price_parser/parser.py#L13-L76 and https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/tests/test_price_parsing.py#L1453-L1490
- [A8] **Code verified (in connector framework)** Scrapling robots: https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/spiders/robotstxt.py
- [A9] **License path verified but license terms must be rechecked before use** Keepa: https://github.com/akaszynski/keepa/blob/32803c0e35e6adebf298074dc69c9c1e7f7e9e99/LICENSE
- [A10] **License path verified but terms must be rechecked before use** price-parser: https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/LICENSE
- Official API numeric token costs are examples of current official docs, **not an entitlement or current account quote**. SDK source read, upstream tests not run, current API account not tested.


### Round-2 upstream issue/PR, license audit (2026-10-30)
- **Keepa SDK license verified from actual LICENSE**: Apache-2.0 at pinned commit [source](https://github.com/akaszynski/keepa/blob/32803c0e35e6adebf298074dc69c9c1e7f7e9e99/LICENSE). Verify NOTICE obligations and transitive dependencies at import. The library license is not a Keepa paid API entitlement.
- **price-parser license verified**: BSD 3-Clause source and binary redistribution clauses with endorsement restriction at pinned [LICENSE](https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/LICENSE).
- **Upstream PR examined, evidence grade=Issue/PR report**: [akaszynski/keepa#203](https://github.com/akaszynski/keepa/pull/203) describes removing redundant proactive `/token` requests in main API call path; `AsyncKeepa.wait_for_tokens` still exists in pinned file. Thus *SDK method exists* ≠ *all queries proactively poll tokens*. Our design uses quota reservations and actual response accounting, never assumes a fixed extra `/token` request.
- **Bug report examined, evidence grade=Issue report**: [price-parser#62](https://github.com/scrapinghub/price-parser/issues/62) supplies string "2 pairs from $349" parsing an unrelated leading 2. Thus the amount parser MUST consume a site-profile-selected **price field**, not a whole product paragraph; requires a regression fixture added to AMK-T06/T07 as `AMK-T15`: ambiguous marketing text must not become purchase price, expected status partial/unknown when price element cannot be selected. All tests designed, not run.
- **Changelog/version**: upstream commit pinned; no local CI or live token/account capability verified. This section supersedes earlier statement that LICENSE contents were only located.
