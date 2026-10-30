# Hotel Research Providers — Detailed Implementation Specification

> Owner CHAT_05 | `docs/05-connectors/hotel-research.md` | Draft / provisional | Research 2026-10-30 | Revision 0.1 | Base main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Connector Framework PR #5.

## 0. Adopted decision
宿泊候補は**同じ日付・宿泊人数と大人/子の年齢・部屋割当・料金通貨・税/現地追加費用・駐車場とキャンセル規定**で比較する。PropertyとRate/Roomを混同しない。「合計15,000円以下」は**支払総額の必須項目が確認済み**のときだけ該当と表示する。検索時の在庫/料金は確定した予約を意味しない。Order create、予約確定、支払、キャンセルはConnector v1から完全に除外。将来の副作用は別ToolとCHAT_03/09の明示承認が必要。

## 1. Scope / ownership / non-scope
- Input: authorized hotel search, destination, local dates+IANA TZ, guest allocation by room, currency/budget, parking and cancellation hard filters, owner+RunFence+deadline+cost.
- Output: Provider-specific propertyId/rateId, availability status at observedAt, itemized total/unknown fees, parking availability/charge/reservation needs, cancellation schedule, source URL/raw Artifact, partial/uncertain reasons.
- Owners: CHAT_05 Provider read adapters; CHAT_01 Evidence, CHAT_02 Run/Steer, CHAT_03 policy/ledger, CHAT_04 browser, CHAT_09 secrets/egress, CHAT_10 physical DB, CHAT_12 property identity, CHAT_08 UI, CHAT_07 monitoring.
- Non-goals: hotel reservation/payment, promise of final availability, cross-provider hotel-ID collision resolution, pricing by simple per-night multiplication, partner qualification bypass.
- SC-04, SC-05, SC-06, SC-07, SC-08, SC-09, SC-10, SC-14.

## 2. Official docs and code/test audit (2026-10-30)
| Source | Inspected functions / facts | Applicability |
|---|---|---|
| Booking.com Demand [H1,H2,H3] | Partner prerequisites; Search/Availability/Orders Preview differ, price ``base/book/total` and extra charges ``included/excluded/conditional/non_conditional`; latest preview reflects booking-time conditions | **Docs verified**, API rights and account credentials **not tested**. orders/create prohibited. Preview must be separately read-only authorized if supported |
| Expedia Rapid [H4] | Lodging launch requirements: total price/taxes/fees, due at property and currency, charge frequency (e.g. per-night) | **Docs verified**. No assumption that each rate is bookable via our account |
| Amadeus official [H5] | Hotel Search v3 `GET /v3/shopping/hotel-offers` supports hotelIds/adults/checkInDate/roomQuantity | **Docs verified**; rate semantics may differ from Booking/Expedia |
| amadeus-python [H6,H7] | `amadeus/shopping/_hotel_offers_search.py HotelOffersSearch.get` calls v3 path, `specs/namespaces/test_namespaces.py test_shopping_hotel_offers_search_get`, `specs/client/test_access_token.py` | **Code/test inspected**. SDK does not perform cross-provider total-cost normalization |
| amadeus-node [H8,H9] | `src/amadeus/namespaces/shopping/hotel_offers_search.js HotelOffersSearch.get`, `client/access_token.js AccessToken`; `spec/amadeus/namespaces.test.js` and `client.test.js` | **Code/test inspected**. Do not expose cached OAuth token to LLM/tenant-independent Adapter |

**Alternatives**: (A) headline price of any OTA scraped page and automatic room booking: rejected for ToS, hidden fees, wrong occupancy and side effects. (B) authorized read APIs, source-bound rate, conservative complete-total computation, independent approval for any future booking: adopted. (A) parking listed as hotel amenity means free reserved spot: rejected. (B) parking exists / vacancy verified / cost known / reservation needed are separate facts: adopted. OSS licenses documented in Sources, to be reviewed before dependency adoption.

## 3. Internal TypeScript contract — NOT shared canonical schema [PROVISIONAL: CHAT_01]
~~~typescript
interface HotelSearchV1 {
  destination: { country: string; region?: string; lat?: number; lon?: number; radiusKm?: number };
  checkIn: string; checkOut: string; timeZone: string; // local calendar dates and IANA TZ
  rooms: { adults: number; childrenAges: number[] }[]; // exact guest allocation
  totalBudget: { minor: string; currency: string };
  parking: { required: boolean; freeOnly?: boolean };
  freeCancellationRequired?: boolean;
  bookerCountry: string; maxCandidates: number;
}
interface HotelOfferDraftV1 {
  providerId: string; propertyNativeId: string; rateNativeId?: string;
  property: { name?: string; address?: string; lat?: number; lon?: number };
  allocation: { adults: number; childrenAges: number[] }[];
  checkIn: string; checkOut: string; timeZone: string;
  price: { currency: string; baseMinor?: string; displayMinor?: string;
    includedFeeMinor?: string; excludedMandatoryMinor?: string;
    onsiteMandatoryMinor?: string; conditionalFeeMinor?: string;
    totalKnownMinor?: string; completeness: "complete"|"partial"|"unknown";
    payNowMinor?: string; payLaterMinor?: string };
  parking: { exists?: boolean; availableForDates?: boolean; reserved?: boolean;
    reservationNeeded?: boolean; included?: boolean; feeMinor?: string;
    chargeUnit?: "per_day"|"per_night"|"per_stay"|"unknown" };
  cancellation: { freeUntil?: string; refundable?: boolean;
    feeRules?: { from: string; amountMinor: string }[] };
  availability: "seen_on_search"|"seen_on_preview"|"unknown";
  fetchedAt: string; rateExpiresAt?: string; sourceUrl: string;
  rawArtifactRef?: string; missingFields: string[];
}
~~~
Validation: Check-out strictly after check-in as **hotel-local dates**, TZ explicit. Distinguish 3 adults in one room vs 2+1 in two rooms; children ages and occupancy never dropped. Financial values are integer minor-unit decimal strings, no floating-point accumulation. Source native Hotel/Rate IDs are not internal UUIDv7. Unknown mandatory onsite fee or unknown parking cost means `completeness=partial`; NOT eligible for assured under-budget. FX conversions are estimates unless source-native total in requested booker currency is provided. Date/guest/locale/currency changes invalidate quote comparability.
Tenant ID and DB workspace ID require CHAT_09/10 explicit mapping. LeaseEpoch and goalRevision are Gateway-owned non-lossy fence values. Provider must not redefine Task/Run/Event/DB record revisions.

## 4. Requirements
| REQ | Priority | Passable acceptance | SC |
|---|---|---|---|
| HTL-001 | MUST | Partner entitlement/ToS/credential validated before call | 04/08 |
| HTL-002 | MUST | exact dates/TZ/adult-child allocation, room count/booker country retained | 04 |
| HTL-003 | MUST | Property, Room, Rate IDs distinct; incompatible occupancies rejected | 04/10 |
| HTL-004 | MUST | amount/tax/mandatory/onsite/conditional fees completeness clear | 04 |
| HTL-005 | MUST | parking exists, cost, availability, reservation status separated | 04 |
| HTL-006 | MUST | cancellation fee/freeUntil and unknown policies not invented | 04 |
| HTL-007 | MUST | search, availability, preview timestamps and validity differentiated | 04 |
| HTL-008 | MUST | no autonomous booking/order-create/charges/room modify | 04/08 |
| HTL-009 | MUST | no results vs provider failures/partial coverage differentiated | 04/05 |
| HTL-010 | MUST | stale lease, Steer/cancel, changed budget never commit old match | 07 |
| HTL-011 | MUST | secrets/redirect/SSRF/untrusted descriptions obey Gateway policy | 06/08 |
| HTL-012 | MUST | changed or expired price/inventory not silently substituted | 04/07 |
| HTL-013 | MUST | finite rate/quota/cost, unknown paid attempt never blind retry | 09 |
| HTL-014 | SHOULD | display source/fee/parking/cancellation/unknown and time | 04 |
| HTL-015 | LATER | source reliability and cross-OTA entity mapping | 10/14 |

## 5. Sequence and lifecycle
~~~mermaid
sequenceDiagram
 participant A as Agent
 participant G as Tool Gateway
 participant H as Hotel Adapter
 participant P as Approved Partner API
 participant E as Evidence
 A->>G: exact stay constraints, budget
 G->>G: owner/RunFence/approval/intent
 G->>H: approved read + pinned capability
 H->>H: entitlement, room and date validation
 H->>P: authorized search/availability
 P-->>H: property, rate, price, policy
 H->>H: normalize charges and completeness
 H->>E: raw receipt and rate/conditions draft
 E-->>G: committed receipt
 G-->>A: comparable quotes / partial / unavailable
~~~
Provider-only states `ELIGIBLE→QUOTA_RESERVED→SENT_OR_UNKNOWN→RECEIVED→NORMALIZED→COMMITTED` or `BLOCKED/RATE_WAIT/PARTIAL/EXPIRED/UNCERTAIN`. Canonical Task states remain CHAT_01/02.

## 6. Deterministic read-only algorithm / race
~~~typescript
async function findHotels(grant, input, abortSignal) {
  const q = validateDatesRoomsCurrencyBudget(input);
  const providers = await registry.eligibleReadCapabilities(q, grant);
  const all = [];
  if (!providers.length) return unavailable("PARTNER_ACCESS_REQUIRED");
  for (const provider of providers.slice(0, 3)) {
    if (abortSignal.aborted) return partial(all, "CANCELLED");
    await gateway.requireCurrentOwnerFenceAndBudget(grant);
    const lease = await quota.tryReserve(provider, grant.worstCaseCost);
    if (!lease.granted) return partial(all, "RATE_WAIT");
    const receipt = await gateway.markSendStarting(grant, lease);
    try {
      const raw = await authorizedTransport.search(provider, q, receipt, abortSignal);
      await quota.settleOrUnknown(lease, raw.usage);
      const candidates = normalizeRatesAndCharges(raw, provider);
      const matched = candidates.filter(c => matchesExactStayAndGuestAllocation(c, q));
      const quotes = matched.map(c => calculateKnownTotalOrUnknown(c, q));
      await gateway.requireCurrentOwnerFenceAndBudget(grant);
      await evidence.commitRawReceiptAndClaims(receipt, raw, quotes);
      all.push(...quotes);
    } catch (err) {
      await quota.markUnknownUnlessDefinitelyUnsent(lease);
      await gateway.persistUncertainReceipt(receipt, sanitize(err));
      // preserve prior valid quotes; unknown does not mean no hotels
    }
  }
  return separateCompleteBudgetMatchesAndUnknowns(all, q);
}
~~~
Quote result with `price.completeness=complete` and all hard conditions verified can enter `search-time budget match`; **never label available for actual reservation without contemporaneous booking-stage verification**. Provider price change/occupancy mismatch invalidates old quote. Source permissions checked per request. Cache key includes tenant, credential scope, dates, rooms/children ages, bookerCountry, rate, currency and schema version. Co-located same hotel different rate plans stay separate.

Race: goal Steer changes 3 adults→2, cancellation, stale leaseEpoch, approval expires: new requests and current Evidence commit both rejected. Crash after network/ACK lost: reconcile raw artifact and payment receipt, do not repeat unknown paid call. Remote 429/503: shared cooldown + Retry-After. Quota unknown remains reserved until reconciliation. Booking `orders/preview` is separate eligibility-scoped **read/quote stage** where supported, never a substitute for user approval and never `orders/create`.

## 7. Failure matrix
| Failure | Result |
|---|---|
| no partner contract/token | no external API call; entitlement required |
| wrong adult/child allocation or room qty | reject not comparable |
| parking listed but capacity/fee unknown | partial, not strict parking budget match |
| room 12,000 + city taxes 4,000 | outside 15,000 total, not hidden |
| no free-cancel date/policy | unknown, not free-cancel |
| expired rate/price changed | historical observation / refresh only under new budget |
| Provider A timeout, B good | partial coverage with valid B source |
| 429/repeated failures | defer/partial with last observation time |
| Cancel/Steer worker stale result | not committed as current |
| malicious hotel text/redirect to private address | deny, audit, no secret leak |
| click “book now” in HTML | no Tool action in read adapter |

Security: no host commands, no egress to private IP, direct provider SDK raw bearer leaked, or unauthorized booking. PII/guest credentials do not enter LLM or general logs. All source text is untrusted. Defaults: max3 providers, max50 rates per provider, max150 items, max60sec provider request / 180sec task, max2 in-flight per tenant; actual provider contract can impose stricter budget. Metrics: source/provider, rate hash, comparable count, fee-complete ratio, cancellation evidence, parking completeness, fetchedAt age, p50/p95, usage reserved/unknown; **no benchmark measured**.

## 8. Persistence, interfaces and cross-team proposals
- **P0 [PROVISIONAL: CHAT_01]** Evidence: room allocation and adult/child ages, rate native IDs, source and fetched time, charge breakdown, parking/cancellation condition status, raw receipt.
- **P0 [PROVISIONAL: CHAT_09/10]** owner/Tenant→Workspace mapping, UUIDv7/nullable Conversation/record revision and leaseEpoch fencing, priced rate snapshot keyed by conditions; DDL not ours.
- **P0 [PROVISIONAL: CHAT_03]** prohibit orderCreate/reservation/charge in read-only Tool, use approval/side-effect ledger to classify any future preview/booking.
- **P1 [PROVISIONAL: CHAT_04]** entitled login Browser session/human takeover; no unauthorized fallback.
- **P1 [PROVISIONAL: CHAT_12]** cross-source property matching with address/coordinates/negative evidence, do not combine different hotels' amenities and rates.
- **P1 [PROVISIONAL: CHAT_08]** show source link/check time, total components, onsite fee, parking availability/fee, cancellation conditions, strict matches vs partial and fallback conditions.
- **P1 [PROVISIONAL: CHAT_07]** for price monitoring, precise checkin/checkout/guests/rooms/locale/currency must be Watch key.
Unresolved: no partner credentials or actual live rate test, unknown exact API plan/usage/contract, hotel free-parking validation. All remain Draft.

## 9. Implementation PR plan
`HT-01` dates/room allocation/money validation+fixtures → `HT-02` entitlement/credential manifests + no-network admission → `HT-03` eligible Amadeus read-only adapter and optional Booking/Expedia gated adapters → `HT-04` evidence/fees/parking/cancellation normalization+receipt → `HT-05` UI, security, crash tests, benchmarking, canary rollout. Order create excluded.

## 10. Tests — **designed only / not run**
| Test | Setup/action | Expected |
|---|---|---|
| HTL-T01 | Booking key/partner contract missing | no call, unavailable |
| HTL-T02 | timezone DST, children ages/3 adults/2 rooms | exact validated allocation |
| HTL-T03 | 2-person rate vs request for 3 | exclude mismatched rate |
| HTL-T04 | 12k rate + 4k mandatory on-site tax, 15k budget | outside budget |
| HTL-T05 | parking amenity, missing paid capacity/fee | not strict parking match |
| HTL-T06 | cancellation unknown/freeUntil missing | no free-cancel claim |
| HTL-T07 | search 14k vs preview 18k | separate stage, latest quote |
| HTL-T08 | malicious page “book” link | no booking Tool/action |
| HTL-T09 | one API timeout, one empty OK, one valid | distinguish partial/empty and show coverage |
| HTL-T10 | stale leaseEpoch >2^53 / 3→2 Steer | stale writes 0, precision |
| HTL-T11 | cross-tenant cookie or private IP redirect | blocked, no secrets |
| HTL-T12 | rate expired/changed inventory | flag stale, no silent replacement |
| HTL-T13 | 2 worker quota race / ACK lost / cost ceiling | bounded, unknown not 0, no resend |
| HTL-T14 | fixed offline fixtures 1/4 Workers and taxes variation | measure latency/cost/completeness, no invented gain |

## 11. Traceability
| MUST | TEST | SC |
|---|---|---|
| HTL-001 | HTL-T01 | SC-04, SC-08 |
| HTL-002 | HTL-T02 | SC-04 |
| HTL-003 | HTL-T03 | SC-04, SC-10 |
| HTL-004 | HTL-T04 | SC-04 |
| HTL-005 | HTL-T05 | SC-04 |
| HTL-006 | HTL-T06 | SC-04 |
| HTL-007 | HTL-T07 | SC-04 |
| HTL-008 | HTL-T08 | SC-04, SC-08 |
| HTL-009 | HTL-T09 | SC-04, SC-05 |
| HTL-010 | HTL-T10 | SC-07 |
| HTL-011 | HTL-T11 | SC-06, SC-08 |
| HTL-012 | HTL-T12 | SC-04, SC-07 |
| HTL-013 | HTL-T13 | SC-09 |

## 12. Sources (all reviewed 2026-10-30)
- [H1] **Docs verified** Booking Demand prerequisites: https://developers.booking.com/demand/docs/getting-started/prerequisites
- [H2] **Docs verified** Booking pricing and extra-charge breakdown: https://developers.booking.com/demand/docs/accommodations/pricing-faqs ; https://developers.booking.com/demand/docs/accommodations/prices-accommodations
- [H3] **Docs verified** Booking Orders Preview/occupancy mismatch: https://developers.booking.com/demand/docs/orders-api/order-preview-create ; https://developers.booking.com/demand/docs/orders-api/orders-faqs
- [H4] **Docs verified** Expedia Rapid lodging taxes and at-property fees: https://developers.expediagroup.com/rapid/setup/launch-requirements/lodging-launch-reqs
- [H5] **Docs verified** Amadeus Hotel Search API v3: https://admin.developers.amadeus.com/self-service/apis-docs/guides/developer-guides/resources/hotels/
- [H6] **Code verified** amadeus-python `HotelOffersSearch.get` pinned `0bc7089cb2e49b5b662ee890a5fe611f59ad4332`: https://github.com/amadeus4dev/amadeus-python/blob/0bc7089cb2e49b5b662ee890a5fe611f59ad4332/amadeus/shopping/_hotel_offers_search.py#L4-L23
- [H7] **Test inspected** amadeus-python `test_shopping_hotel_offers_search_get`/`test_cached_token_valid`: https://github.com/amadeus4dev/amadeus-python/blob/0bc7089cb2e49b5b662ee890a5fe611f59ad4332/specs/namespaces/test_namespaces.py#L206-L220 ; https://github.com/amadeus4dev/amadeus-python/blob/0bc7089cb2e49b5b662ee890a5fe611f59ad4332/specs/client/test_access_token.py#L24-L52
- [H8] **Code verified** amadeus-node `HotelOffersSearch.get/AccessToken` pinned `c4f00ea7929571dd6dabfb66bbf7fc140d821ce4`: https://github.com/amadeus4dev/amadeus-node/blob/c4f00ea7929571dd6dabfb66bbf7fc140d821ce4/src/amadeus/namespaces/shopping/hotel_offers_search.js ; https://github.com/amadeus4dev/amadeus-node/blob/c4f00ea7929571dd6dabfb66bbf7fc140d821ce4/src/amadeus/client/access_token.js
- [H9] **Test inspected** Node namespace/client tests: https://github.com/amadeus4dev/amadeus-node/blob/c4f00ea7929571dd6dabfb66bbf7fc140d821ce4/spec/amadeus/namespaces.test.js#L65-L135 ; https://github.com/amadeus4dev/amadeus-node/blob/c4f00ea7929571dd6dabfb66bbf7fc140d821ce4/spec/amadeus/client.test.js#L78-L125
- [H10] **License file paths identified, re-review required before integration**: https://github.com/amadeus4dev/amadeus-python/blob/0bc7089cb2e49b5b662ee890a5fe611f59ad4332/LICENSE ; https://github.com/amadeus4dev/amadeus-node/blob/c4f00ea7929571dd6dabfb66bbf7fc140d821ce4/LICENSE
- **Not verified**: real provider accounts/contracts, live hotel rates/parking, integration tests/CI, actual execution benchmarks. Draft until owner contracts and source entitlement confirmed.


### Round-2 upstream change, issue, license audit (2026-10-30)
- LICENSE **actual contents** read: [amadeus-python](https://github.com/amadeus4dev/amadeus-python/blob/0bc7089cb2e49b5b662ee890a5fe611f59ad4332/LICENSE) and [amadeus-node](https://github.com/amadeus4dev/amadeus-node/blob/c4f00ea7929571dd6dabfb66bbf7fc140d821ce4/LICENSE) are MIT. This is an OSS SDK license, NOT booking API/redistribution/affiliate entitlement.
- **Merged upstream PR inspected** [amadeus-node#188](https://github.com/amadeus4dev/amadeus-node/pull/188): Hotel Search V2 was decommissioned/migrated to V3. Thus preserve endpoint version `/v3/shopping/hotel-offers` in provider manifest and reject unverified legacy V2. **Issue report examined** [amadeus-node#133](https://github.com/amadeus4dev/amadeus-node/issues/133): legacy `/v2/shopping/hotel-offers` returned authorization error for one reporter although token worked on other endpoints; not evidence that V3 is available for our credentials.
- Add to HTL-T01/T13: fail-closed on obsolete API version or plan entitlement with no automatic V2 fallback; record approved API version, status and account credentials scope without exposing tokens. CI/integration and live availability tests remain **not executed**.
