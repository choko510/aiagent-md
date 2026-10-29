# Buyback Price Providers — Detailed Implementation Specification

> Owner CHAT_05 | Path `docs/05-connectors/buyback-price-providers.md` | Draft/provisional | Revision 0.1 | Research 2026-10-30 | Base main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Reference: PR #5 Connector Framework.

## 0. Decision
**買取価格は「条件付き買取オファー」であって確定換金額ではない。** SKU（JAN/EAN/型番/容量/色/版）、状態（未開封/開封）、付属品、査定減額、会員限定、台数/期間、店舗と観測日時を全て紐付ける。取得失敗は0円ではなくunknown。利用許可済み公式API・フィードを優先し、公開Webの取得はCHAT_04のHTTP/BrowserとCHAT_09の権限・ToS・robots審査を経由する。未承認スクレイピング、CAPTCHA回避、売却予約・発送・購入は行わない。

## 1. Responsibility, non-scope, boundaries
- Input: Gateway-approved read-only lookup, model/JAN/market/condition/store/quantity, auth context, RunFence, cost ceiling, deadline.
- Output: timestamped `BuybackOfferDraft`, structured unknown/partial, canonical source URL/raw evidence refs, provider usage.
- CHAT_05: Provider manifest, native API/extraction profiles, domain semantics; CHAT_01 Evidence schema、CHAT_03 Policy/Gateway/ledger、CHAT_04 HTTP/Browser session、CHAT_07 Watch/通知、CHAT_09 Secret/egrеss/tenant isolation、CHAT_10 DB、CHAT_12 Entity Resolver/Risk、CHAT_13 Change Intelligence、CHAT_15 OCR/CSV。
- Non-scope: 商品照合の真偽や利益期待値の確定、Watchの状態機械、物理DDL、同じショップ名だからと別店舗を同一視する処理。
- SC-01/02/05/06/07/08/09/10/12/14/15。

## 2. Primary source/code/test research (2026-10-30)
| Source | inspected classes/functions/tests | apply / reject |
|---|---|---|
| scrapinghub/extruct [B1,B2] | `extruct/_extruct.py extract` (line 22), `extruct/jsonld.py JsonLdExtractor.extract_items` (line 211); `tests/test_jsonld.py` & `tests/test_extruct.py` located, body **not fully inspected** | structured JSON-LD may provide *candidates*. Retail Offer schema is not proof of buyback semantics |
| scrapinghub/price-parser [B3,B4] | `price_parser/parser.py Price.fromstring` (line 28): Decimal, currency hints, separator; `tests/test_price_parsing.py test_parsing` (1453) | lexical amount parser only. Tax, upper-limit, member eligibility require custom validation |
| D4Vinci/Scrapling [B5,B6] | `spiders/robotstxt.py RobotsTxtManager.can_fetch`, `spiders/throttle.py AutoThrottle.record`, `tests/spiders/test_throttle.py` and `test_robotstxt.py` inspected | robots/frequency examples. Do not copy in-memory quota into multi-worker architecture |
| IETF RFC 9309 [B7] | robots exclusion specification; explicitly not access authorization | ToS/permission review remains separate |

Design comparison: (A) giant Core scraper with site selectors, bypass on 403 (REJECT: brittle, unlawful/uncontrolled) vs (B) **versioned read-only Provider profile + allowlisted HTTP + fixture-gated release** (ADOPT). (A) JSON-LD amount as confirmed offer (REJECT) vs (B) price plus conditions/page evidence and unknown fields (ADOPT). (A) failed fetch→0 yen (REJECT) vs (B) unknown/no trigger (ADOPT). Licenses in [B8] are for OSS code, not the websites' content. Site-specific ToS/API permissions are not proven; production connectors default-disabled.

## 3. Input/output local TypeScript proposal — not canonical shared type
~~~typescript
interface BuybackLookupV1 {
  providerId: string; nativeSku?: string; jan?: string; ean?: string; mpn?: string;
  market: "JP"|"US"|"OTHER"; targetStoreIds?: string[];
  condition: "sealed_new"|"open_box"|"used"|"unknown";
  quantity: number; freshnessRequestedAfter?: string;
}
interface BuybackOfferDraftV1 {
  providerId: string; merchantId?: string; locationId?: string; offerNativeId?: string;
  product: { jan?: string; ean?: string; model?: string; capacity?: string;
             color?: string; region?: string; edition?: string; accessories?: string[] };
  condition: "sealed_new"|"open_box"|"used"|"unknown";
  amountMinor?: string; currency?: string;
  priceKind: "firm_listed"|"up_to"|"conditional"|"estimated"|"unknown";
  deductions: { reason: string; amountMinor?: string; known: boolean }[];
  requirements: { domesticModel?: boolean; membership?: string;
    originalBox?: boolean; receipt?: boolean; qtyLimit?: number;
    locationOnly?: boolean; notes: string[] };
  applicableFrom?: string; applicableUntil?: string;
  fetchedAt: string; sourcePublishedAt?: string; sourceEffectiveAt?: string;
  sourceUrl: string; rawArtifactRef?: string; extractorVersion: string;
  missingFields: string[]; status: "observed"|"partial"|"unverified";
}
~~~
- GTIN check digit/capacity+unit/region/edition normalization must keep original source strings. Product variants are not merged by connector; negative evidence travels to CHAT_12. Store ID and location ID remain distinct.
- `amountMinor` is an integer decimal string. Use price-parser Decimal semantics, not floating-point arithmetic. No price in untrusted HTML may become side-effect instructions. `priceKind=up_to` never equals a firm quote.
- Provider IDs are not internal UUIDs. Authenticated Task/Run/Watch/Observation internal IDs follow CHAT_01 UUIDv7; `tenantId`→`workspace_id` mapping is owned by CHAT_09/10 and NOT implicit. `leaseEpoch` lossless bigint/decimal, `goalRevision`/approval/descriptor revisions checked by Gateway before HTTP and Evidence.
- Runtime validators: max query 64KiB, results 100, raw body 2MiB, max attempts 3, deadline 60s, max provider concurrency 2. Strictest signed contract/robots/provider policy wins; these limits are design defaults, not tested throughput.

## 4. Requirements
| ID | Priority | Observable condition | SC |
|---|---|---|---|
| BBP-001 | MUST | approved ToS/credential/robots/egress only; unknown blocks execution | 08 |
| BBP-002 | MUST | distinguish advertised maximum vs firm offer | 01 |
| BBP-003 | MUST | preserve JAN, capacity, color, region, seal/accessory/condition | 01/10 |
| BBP-004 | MUST | parse currency, units, deductions, expiry, quantity; absent = unknown | 01 |
| BBP-005 | MUST | 403/429/timeout and parser failure do not become price zero | 02 |
| BBP-006 | MUST | source URL, fetched/effective time, parser version and raw receipt | 02/12 |
| BBP-007 | MUST | gateway/Broker/browser owner authority, no direct HTTP bypass | 06/08 |
| BBP-008 | MUST | cancel/Steer/crash/old leaseEpoch never commits current result or blind retries | 07 |
| BBP-009 | MUST | Watch threshold considers only declared semantics, not up_to as confirmed | 02 |
| BBP-010 | MUST | selector drift quarantined and not silently reinterpreted | 12 |
| BBP-011 | MUST | store/qty/period/native IDs kept for diff; notification separate | 02/12 |
| BBP-012 | MUST | secret leakage, HTML prompt injection and unapproved sell side-effect blocked | 08 |
| BBP-013 | SHOULD | emit parser drift/source reliability metrics | 14 |
| BBP-014 | LATER | ingest authorized PDF/CSV buyback lists through CHAT_15 | 15 |

## 5. Sequence and state
~~~mermaid
sequenceDiagram
 participant W as Agent or Watch
 participant G as Tool Gateway
 participant C as Buyback adapter
 participant H as Approved HTTP or Browser
 participant E as Evidence
 W->>G: read with owner and RunFence
 G->>G: validate policy budget approval and intent
 G->>C: approved read invocation
 C->>C: provider eligibility and pinned profile
 C->>H: quota-bound authorized retrieval
 H-->>C: response status raw snapshot
 C->>C: parse condition and amount, strict validation
 C->>E: raw artifact and claim draft commit
 E-->>G: receipt
 G-->>W: observed or partial or unavailable
~~~
Provider-only states: `DISABLED→ELIGIBLE→QUOTA_RESERVED→SENT_OR_UNKNOWN→RECEIVED→PARSED→COMMITTED`; branches `BLOCKED/RATE_WAIT/DRIFT/UNCERTAIN`. Canonical Task states remain CHAT_01/02. Unknown price does not mutate Watch threshold truth.

## 6. Deterministic algorithm / race
~~~typescript
async function observeBuyback(approved, input, abort) {
  const profile = registry.getPinned(approved.providerId, approved.profileDigest);
  assert(profile?.readOnly && profile.complianceValidAt(clock.now()));
  validateLookup(input);
  await gateway.requireCurrentOwnerGoalLeaseApprovalAndBudget(approved);
  const lease = await quota.tryReserveAllScopes(profile, approved.costCeiling);
  if (!lease.granted) return rateWait(lease.nextAllowedAt);
  // Send-start intent is durably recorded BEFORE externally visible request.
  const receipt = await gateway.markExternalAttemptStarting(approved, lease);
  try {
    const raw = await authorizedSource.fetch(profile, input, abort);
    await quota.settle(lease, raw.reportedUsage ?? "unknown");
    const parsed = strictParseWithVersionedProfile(raw, profile);
    const claim = validateOfferConditions(parsed);
    await gateway.requireCurrentOwnerGoalLeaseApprovalAndBudget(approved);
    await evidence.commitReceiptAndClaims(receipt, raw, claim);
    return claim.status === "observed" ? complete(claim) : partial(claim);
  } catch (e) {
    await quota.markUnknownUnlessProvenUnsent(lease);
    await gateway.persistErrorReceipt(receipt, sanitize(e));
    return unavailableOrUncertain(e);
  }
}
~~~
- Profile changes require staged fixture test/admin approval/atomic digest promotion. Old attempt pinned to old parser. Emergency ToS revocation disables all generations, no rollback bypass.
- Two Worker attempts require CHAT_03 intent and CHAT_10 atomic quota. Lease fencing happens before external dispatch AND before current Evidence commit. Stale results can remain immutable historical receipt, not overwrite new Run.
- Crash after raw snapshot but before normalizing: reparse same hash/attemptId, no refetch without new approval and quota. Lost HTTP ACK ⇒ uncertain; arbitrary retry forbidden unless proven pre-send and read-safe.
- 429/503: honor Retry-After, shared cooldown; deadline expiry returns unavailable/partial rather than faking zero. Credential refresh only via scoped Broker and bounded 1x rule.

## 7. Failure and operational matrices
| Failure | Detection | Behavior |
|---|---|---|
| ToS/robots/credential unknown | compliance gate | block without HTTP |
| 404 or page moved | identity/validation | unlisted only after source-confirmed; otherwise unknown |
| 429/503 | status, Retry-After | shared cooldown, bounded deferred attempt |
| timeout/crash/lost ACK | ledger receipt | unknown charge, no blind resend |
| HTML selector drift | profile fixture / mismatched DOM | quarantine, partial |
| currency ambiguous | Price parser + currency validation | no amountMinor |
| “最大50,000円” | semantic qualifiers | up_to, no firm Watch alert |
| identical name different locations | native location ID | separate offers |
| Cancel/Steer, stale lease | Gateway fence | no new call/commit |
| prompt injection/signed URL | untrusted boundary/scrubber | no instruction/secret leak |

Security: no scraping permission circumvention, no CAPTCHA claims, no purchase or resale actions. Egress handles redirect/DNS rebinding/private IP; cookie/BrowserSession only CHAT_04. Redact all secret-bearing query parameters and PII. Logs: provider, profile digest, anonymized item fingerprint, status category, missing-field count, observedAt/sourceEffectiveAt, cost unknown, retries, drift; no HTML/content/token. Compare p50/p95 and extraction accuracy with fixed offline fixtures; no fabricated measurements.

## 8. Owner proposals / persistence / UI
- **P0 CHAT_01** Evidence requires `priceKind`/conditions/source effective time/native offer identity/uncertainty/raw artifact, not one price scalar.
- **P0 CHAT_09/10** tenant-workspace-owner mapping, UUIDv7 internal IDs, nullable Conversation, `leaseEpoch`/goalRevision fencing and quota receipt persistence. All physical DDL owned by CHAT_10.
- **P1 CHAT_12** model/variant identity and negative evidence, price adjustment risk; do not equate displayed upper limit to profit.
- **P1 CHAT_07** Watch failure vs observed 0, strict threshold semantics, notification idempotency owned by 07.
- **P1 CHAT_13** structural price diff ignores ad/time noise, emits only meaningful change; Event Dispatcher not duplicated.
- **P1 CHAT_08** show source URL/claim kind/validity/store/quantity/time, never “確定” for up_to.
- **P1 CHAT_04/15** browser owner & provenance for screenshot/PDF/table price lists.
- Unresolved blocker: actual merchant/API permissions and ToS must be researched per provider; this generic spec does NOT authorize production scraping.

## 9. Implementation PRs
`BB-01` Provider profile/ToS admission → `BB-02` Schema/JSON-LD/price+condition extraction fixtures → `BB-03` Gateway/Secret/Quota/Evidence integration → `BB-04` Watch/Entity/Diff contracts → `BB-05` UI/fault tests/observability/canary and rollback. Development only, no live sites without explicit authorization.

## 10. Tests (all designed, NOT executed)
| Test | Setup/action | Pass |
|---|---|---|
| BBP-T01 | ToS absent/robots deny | external requests 0 |
| BBP-T02 | 50,000 yen upper limit and unknown deductions | up_to, not firm |
| BBP-T03 | model same but different color/GB/region/seal | separate claims |
| BBP-T04 | comma/currency ambiguity/conditional fee | Decimal amount or unknown |
| BBP-T05 | 429/503/timeout with prior valid sample | old stale, no new zero |
| BBP-T06 | fetchedAt vs effectiveAt, versioned raw artifact | both survive |
| BBP-T07 | foreign tenant Cookie + redirect to 127.0.0.1 | blocked, no secret |
| BBP-T08 | two workers and stale leaseEpoch, crash after send | stale writes 0, no resend |
| BBP-T09 | Watch 45,000 threshold, up_to vs firm | only configured triggers |
| BBP-T10 | ads/time DOM rotation and selector moved | quarantine, no wrong price |
| BBP-T11 | same named store two locations, expiring promo | distinct, validity kept |
| BBP-T12 | malicious page with instructions/sell link | no permission escalation/sale |
| BBP-T13 | 1/4 workers mock latency, rate, parser drift | report measured metrics only |
| BBP-T14 | unreadable authorized PDF/CSV list | partial/unknown, provenance |

## 11. Traceability
| MUST REQ | TEST | SC |
|---|---|---|
| BBP-001 | T01 | SC-08 |
| BBP-002 | T02 | SC-01 |
| BBP-003 | T03 | SC-01/10 |
| BBP-004 | T04 | SC-01 |
| BBP-005 | T05 | SC-02 |
| BBP-006 | T06 | SC-02/12 |
| BBP-007 | T07 | SC-06/08 |
| BBP-008 | T08 | SC-07 |
| BBP-009 | T09 | SC-02 |
| BBP-010 | T10 | SC-12 |
| BBP-011 | T11 | SC-02/12 |
| BBP-012 | T12 | SC-08 |

## 12. Sources and limitations — 2026-10-30
- [B1] **Code verified** extruct at `d24f5414fd32c874989492ebd04cb1deb6d7d04e`: https://github.com/scrapinghub/extruct/blob/d24f5414fd32c874989492ebd04cb1deb6d7d04e/extruct/_extruct.py#L22-L100 and https://github.com/scrapinghub/extruct/blob/d24f5414fd32c874989492ebd04cb1deb6d7d04e/extruct/jsonld.py#L202-L230
- [B2] **Test locations verified, bodies not fully read**: https://github.com/scrapinghub/extruct/tree/d24f5414fd32c874989492ebd04cb1deb6d7d04e/tests
- [B3] **Code verified** price-parser at `6718bfe8447f2de17ecc152d3ad2a4c51520c755`: https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/price_parser/parser.py#L13-L76
- [B4] **Test verified** https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/tests/test_price_parsing.py#L1453-L1490
- [B5] **Code verified** Scrapling `RobotsTxtManager/AutoThrottle` at `26dc85a4fa307257fb5c669c80aa44b15acb84c4`: https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/spiders/robotstxt.py and https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/spiders/throttle.py
- [B6] **Test code inspected Round 1**: https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/spiders/test_throttle.py#L121-L211 and https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/spiders/test_robotstxt.py#L110-L150
- [B7] **Docs verified** https://www.rfc-editor.org/rfc/rfc9309.html
- [B8] **License paths identified; terms not verified before dependency install**: https://github.com/scrapinghub/extruct/blob/d24f5414fd32c874989492ebd04cb1deb6d7d04e/LICENSE ; https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/LICENSE ; https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE
- **Unverified**: particular merchant permissions, API/ToS/rates, exact extraction accuracy; no E2E/live/unit tests executed. This MD remains Draft.


### Round-2 source/version and license verification (2026-10-30)
- LICENSE contents verified: [scrapinghub/extruct](https://github.com/scrapinghub/extruct/blob/d24f5414fd32c874989492ebd04cb1deb6d7d04e/LICENSE) and [price-parser](https://github.com/scrapinghub/price-parser/blob/6718bfe8447f2de17ecc152d3ad2a4c51520c755/LICENSE) are BSD 3-Clause-style; [Scrapling](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE) is BSD-3-Clause (reviewed in first round). Retain copyright/notices, confirm transitive dependencies and terms on actual adoption. These licenses do NOT grant permission to copy third-party price listings.
- **Code-change PR inspected** [extruct#266](https://github.com/scrapinghub/extruct/pull/266), merged: repairs nonstandard JavaScript escape sequences within JSON-LD after regular JSON parse fails. Treat repaired data as an *untrusted extraction*, not authoritative pricing. Extend BBP-T10 with malformed JSON-LD escape sequence and preserve raw snapshot. [price-parser issue #62](https://github.com/scrapinghub/price-parser/issues/62) demonstrates marketing text yielding wrong leading price; source element isolation is mandatory before `Price.fromstring`.
- **Evidence grade**: upstream PR+issue reports are verified as GitHub discussions, not reproduction in this project. No live store's ToS, robots, price, seller eligibility, or benchmark was tested.
