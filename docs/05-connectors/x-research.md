# X Research Provider — Detailed Implementation Specification

> Owner CHAT_05 | path `docs/05-connectors/x-research.md` | Status **Draft / provisional** | Rev 0.1 | 2026-10-30 | main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Supports CHAT_13 ExternalEvent; does not own Event Router/Watch/Notification.

## 0. Conclusion / choice
Xを「ニュースの確定情報源」とみなさず、**時刻、author ID、原投稿、引用元、Repost、API取得状態、入手した範囲**を持つProvider Evidenceとして扱う。過去24h検索はX公式 `GET /2/tweets/search/recent`（ドキュメント上7日検索）に適合するが、認証・課金残高/地域/レート/利用目的を満たさない場合は unavailable。named-account投稿イベントは**CHAT_05が公式検索/ユーザータイムライン/streamのアクセス経路と問い合わせを所有し、CHAT_13がsubscription/event dedup→Task dispatchを所有**。投稿本文をTool命令として扱わない。未承認のX投稿/いいね/DMは行わない。

現行公式はpay-per-useモデルを記載し、Post読取を1資源あたり**$0.005（2026-10-30閲覧時）**と案内。重複課金回避は24h UTC日ウィンドウの**soft guarantee**であり0円保証ではない [X3]。課金単価はAPI導入前にConsole/契約と突合、変動する価格は永続設定の上限で再検証。Filtered streamの接続/ルール数も契約と資格による [X2]。

## 1. Scope / interfaces / ownership
- In: `XRecentSearch(query, UTC [from,to), maxPosts, filters)`, `XUserTimeline(userId,sinceId,...)`, `XFilteredStreamRule` if explicitly enabled and eligible, authenticated Gateway grant + tenant/owner + RunFence + provider cost ceiling.
- Out: `XPostObservationDraft` + API cost/usage + pagination/cursor + coverage/partial, source permalink, fetchedAt / authoredAt, referenced Post IDs + confidence flags. All X text and source URLs are untrusted.
- Non-scope: canonical EventSubscription/ExternalEvent/Task/Watch/Notification state (CHAT_01/07/13), cross-platform trend scoring (CHAT_12), external source verification (CHAT_02 ResultVerifier/CHAT_12 Source Reliability), Tool permissions (CHAT_03/09), UI (CHAT_08), DB DDL (CHAT_10).
- SC-03/05/07/08/09/11/14。

## 2. Code-first prior-art audit (2026-10-30)
| Source | Code & tests actually inspected | Adapt / reject |
|---|---|---|
| **Tweepy** [X4,X5] | `tweepy/client.py Client.search_recent_tweets` (1187)、`Client.get_users_tweets` (1497): `start_time/end_time/next_token/since_id`とfields, `tests/test_client.py test_search_recent_tweets/test_get_users_tweets` (77/90) cassette-based. `tweepy/streaming.py StreamingClient.filter`. | Endpoint and pagination parameters confirmed in code; old docs in SDK docstrings need current official X docs precedence. Direct Python runtime integration optional, not required |
| **node-twitter-api-v2** [X6,X7] | `src/v2/client.v2.read.ts search` (129-143), `userTimeline` (287), `src/paginators/v2.paginator.ts` uses `meta.next_token`, `src/stream/TweetStream.ts onConnectionError` (338) max reconnect attempts, async iterator. | TS design reference. Reconnect requires persistent cursor/overlap and disabled Subscription checks. SDK comments about historic access tiers may be stale |
| **X official Search** [X1] | recent endpoint 7-day, up to 100/page, filter operators, last24h query feasible when eligible | Official contract primary. Search coverage ≠ complete universe; do not assert exhaustive sampling |
| **X official Stream/Timeline/Pricing** [X2,X3] | stream `/2/tweets/search/stream` and `/rules`, `/2/users/:id/tweets` pagination; quota, pay-per-use, per-resource pricing | Use only after user/account/token entitlement verified, no guaranteed instant delivery; push vs poll tradeoff measurable |

**Alternatives**: (A) Search snippets from unlicensed sites to impersonate X feed (reject), (B) official API with bounded read and documented eligibility (adopt). (A) stream always-on for everyone (reject; cost/connectivity/entitlement), (B) capability-based stream if authorized else bounded timeline polling/recent search (adopt). (A) volume equals verified trend (reject), (B) provenance + cross-source verification and coverage intervals (adopt). Libraries are implementation references, not forks. Licenses to be rechecked on adoption [X8].

## 3. Internal I/O — proposal, not shared canonical schema [PROVISIONAL: CHAT_01/13]
~~~typescript
interface XResearchQueryV1 {
  mode: "recent_search"|"user_timeline"|"filtered_stream";
  query?: string; // strict allowlisted operator syntax / max 512 chars on recent by docs
  authorUserId?: string; // decimal X native ID as a string, never JS number
  handleHint?: string; // for display/lookup, never stable identity
  startTime: string; endTime: string; // UTC ISO; [start,end)
  excludeReplies?: boolean; excludeReposts?: boolean; includeQuotes?: boolean;
  maxPosts: number; // default 100; hard cap 500 approved task
  requestedFields: string[]; // fixed allowlist
}
interface XPostObservationDraftV1 {
  postId: string; authorUserId: string; handleObserved?: string;
  text: string; sourceUrl: string;
  createdAt: string; retrievedAt: string;
  referencedPosts: { type: "retweeted"|"replied_to"|"quoted"; id: string }[];
  language?: string; entities?: Record<string,unknown>;
  isOfficialClaim: boolean; verifiedByIndependentSource: boolean;
  mediaPresent: boolean; sourceCoverage: { queryHash: string; from: string; to: string;
    complete: boolean; returnedCount: number; pagesRead: number; truncatedReason?: string };
  sourceQueryKind: string; rawArtifactRef?: string; missingFields: string[];
}
interface XReadCursorV1 {
  authorUserId?: string; queryFingerprint: string; pinnedApiVersion: string;
  nextToken?: string; sinceId?: string; lastFetchedAt?: string;
  subscriptionGeneration?: string; // CHAT_13 owned if event flow
}
~~~
**Validation**: User ID and Post ID stay decimal **strings** even beyond JS safe integer; internal Task/Run IDs UUIDv7. Since/to times must be timezone-precise UTC and `end>start`, max 24h when user asks past24h, bounded to the recent search retention window. `query` syntax validated against current supported API operators, no arbitrary JSON conditions/malformed encodings. Fields include `created_at, author_id, referenced_tweets, entities` as eligible; absent fields are unknown. X returned result 0 is not proof there were no posts when pages incomplete, rate-limited or coverage partial. Text sanitization does not delete URLs/source evidence.

## 4. Requirements
| REQ | Priority | Observable requirement | SC |
|---|---|---|---|
| XRS-001 | MUST | X source/API/plan/usage entitlement verified, disabled if unavailable | 03/08 |
| XRS-002 | MUST | past24h is explicit UTC half-open interval and source time separate | 03 |
| XRS-003 | MUST | source Post ID, author ID, permalink, retrievedAt/createdAt retained | 03/11 |
| XRS-004 | MUST | reposts/quotes/replies linked to original Post IDs, dedup without erasing provenance | 03/14 |
| XRS-005 | MUST | pagination/cursor/coverage/result limit tracked, 0 hits not equated exhaustive | 03 |
| XRS-006 | MUST | per-resource billable ceiling reserves worst case, no free dedup assumption | 09 |
| XRS-007 | MUST | Actor matched by stable userId; handle rename cannot silently retarget subscription | 11 |
| XRS-008 | MUST | Stream/polling is optional entitlement-bound and routed through CHAT_13 | 11 |
| XRS-009 | MUST | webhook/stream/replay cannot trigger duplicate Tasks; disabled/revoked stops new dispatch | 07/11 |
| XRS-010 | MUST | text/quoted URLs do not change policy/capability, credentials never in LLM/log | 08 |
| XRS-011 | MUST | cancel/Steer/restart/stale leaseEpoch reject old results/side effects | 07 |
| XRS-012 | MUST | campaign status, expiry and official corroboration distinguish unverified claims | 03/14 |
| XRS-013 | MUST | failures/timeouts/429/auth changes surfaced as partial/unavailable, not factual empty set | 03 |
| XRS-014 | SHOULD | sample source reliability and lateness metrics for CHAT_12 | 14 |
| XRS-015 | LATER | qualified enterprise/archive search without new default rights | 03 |

## 5. Sequence + Provider-only lifecycle
~~~mermaid
sequenceDiagram
 participant A as Agent/CHAT_13
 participant G as Tool Gateway
 participant X as X Adapter
 participant Q as Quota/Credential Broker
 participant P as Official X API
 participant E as Evidence/CHAT_13
 A->>G: query / event source request
 G->>G: owner + RunFence + approval + ledger
 G->>X: approved capability and pinned version
 X->>Q: reserve worst-case reads, obtain scoped credential
 Q-->>X: lease
 X->>P: eligible recent search/timeline/stream
 P-->>X: posts, cursor, usage or status
 X->>Q: settle actual/unknown usage
 X->>X: validate UTC, IDs, reference graph
 X->>E: Evidence/ExternalEvent candidates + receipt
 E-->>G: commit/dedup ACK
 G-->>A: complete/partial/blocked + provenance
~~~
States `DISABLED→ELIGIBLE→RESERVED→SENT_OR_UNKNOWN→PAGINATING→NORMALIZED→PERSISTED` / exceptions `RATE_WAIT, AUTH_REQUIRED, PARTIAL, UNCERTAIN`. Continuous stream connection lifetime and Event Subscription state are CHAT_13's owner, not new Task state.

## 6. Algorithms and concurrency
~~~typescript
async function readRecentX(grant, query, signal) {
  validateTimeWindowAndQuery(query);
  await gateway.requireCurrentRunOwnerApprovalAndBudget(grant);
  const all = new Map<string, XPostObservationDraftV1>();
  let nextToken; let pages = 0; const cap = Math.min(query.maxPosts, 500);
  do {
    if (signal.aborted || !(await gateway.fenceCurrent(grant))) return partial(all, "CANCELLED");
    const remaining = cap - all.size;
    const lease = await quota.reserveWorstCaseRead(Math.min(remaining, 100), grant);
    if (!lease.granted) return partial(all, "QUOTA_WAIT");
    const attempt = await gateway.markExternalSendStarting(grant, lease);
    const response = await officialApi.searchRecent({
      query: query.query, start_time: query.startTime, end_time: query.endTime,
      next_token: nextToken, max_results: Math.min(remaining, 100)
    }, attempt);
    await quota.settleOrMarkUnknown(lease, response);
    if (!response.ok) return partialOrFailure(all, response);
    for (const post of response.posts) {
      const claim = normalizeSourcePostWithRefs(post, response.retrievedAt);
      if (createdInHalfOpenWindow(claim, query)) all.set(claim.postId, claim);
    }
    nextToken = response.meta?.next_token; pages++;
    await evidence.commitAttemptAndObservations(attempt, all);
  } while (nextToken && pages < 10 && all.size < cap);
  return resultsWithCoverage(all, !nextToken, pages);
}
~~~
- Pagination duplicates map by postId but preserve all reference edges/occurrences, query/time/window. `since_id` is an ingestion watermark, not guarantee of delivery; overlapping windows and durable idempotency key `(tenant,provider,postId,eventType,subscriptionId)` to CHAT_13.
- Stream reconnect: finite exponential backoff with jitter, token/plan/permission recheck after every disconnect, periodic recent/timeline gap recovery **only if budget/eligibility allows**. Stream rules changes require explicit subscription revision and staged publish; CHAT_13 owns connected consumer/gap checkpoint, CHAT_05 merely Provider operations.
- X 24h URL and result body time: Post `created_at` vs retrieval time; repost original text/source are different IDs. Verified author and official announcement **not** evidence that described sale price is true; corroboration link must be separate.
- Credential and policy revision/owner revoked after fetch but before dispatch → ignore for active subscriber, preserve immutable receipt as audit; no Task started.
- Worst-case read cap is returned resources × published per-resource price **plus** billed user expansions and request items if applicable; usage unknown retains reserved amount. X soft dedup across 24h UTC cannot be used to reduce budget reserve to zero.

## 7. Failures, security and boundaries
| Condition | Response / recovery |
|---|---|
| 401/403/revoked credits | disable only relevant credential/provider; no scraping bypass; admin diagnostics |
| 429 or exhausted credits | shared cooldown, next allowable window, no tighter sleep than Retry-After |
| 5xx/network/stream close | persist cursor and gap, finite reconnect; partial and data loss interval disclosed |
| query/date invalid or plan unknown | validation/eligibility error, external calls 0 |
| rate-limited mid-pagination | return partial with page/coverage/truncated reason |
| same Post in quote/retweet/reply | dedup by native ID but keep reference graph |
| handle changed/reassigned | new username lookup not sufficient; compare stable authorUserId |
| Worker kill after fetch | receipts+idempotent event dedup, no blind repeated billable read |
| unsolicited external text directing purchase | treat as data, Gateway rejects side-effect |
| expired campaign or unsupported verification | mark unverified/expired, do not present as active promotion |

API outgoing URL (no internal IP), TLS, OAuth bearer scoping, PII/user queries, data redistribution/retention must comply with X developer terms and CHAT_09. Cursor/token stored per tenant, credential, query hash, start/end, subscription revision with CAS/fencing; do not return raw bearer/Cookie. Proposed limits: max500 posts/task, max10 pages, page <=100, max60sec for one read Task call, max2 concurrent X reads/tenant, max1 stream per enabled project unless verified enterprise contract; **not measured defaults, server policy may further restrict**.

## 8. Storage / interfaces / proposal
- [PROVISIONAL CHAT_01] Post Evidence needs native post/user IDs, quote/repost ancestry, source/permalink, authoredAt/retrievedAt, unverified claim, coverage.
- [PROVISIONAL CHAT_13] stable account lookup, event source as typed X capability, replay dedup/cursors, subscriptions and dispatch, signing/webhook validation, event budget and disable generation. CHAT_05 does NOT redefine ExternalEvent.
- [PROVISIONAL CHAT_07] source events may trigger Watch/Task; notification via existing outbox only, not Provider.
- [PROVISIONAL CHAT_12] trend fusion treats correlated quote/reposts as non-independent, source reliability and sampled coverage.
- [PROVISIONAL CHAT_10] tenant-scoped cursor/snapshot/attempt receipt/store, atomic event checkpoint with leaseEpoch and goalRevision, mapping to workspace ID.
- [PROVISIONAL CHAT_03/09] Tool intent/secret scopes, signed incoming events by CHAT_13, no auto-post; billing per returned resources, not request count only.
- [PROVISIONAL CHAT_08] show query UTC window, “coverage incomplete”, rate status, createdAt/source URL, official corroboration and estimated API cost.
- No cross-owner file edited. Need historical archive and costs validated at runtime before unlock.

## 9. Implementation PR sequence
`XR-01` official endpoint & plan/credential read-only manifest + query validator → `XR-02` recent search pagination/normalization/price/budget → `XR-03` references+dedup+independent official-verification fields → `XR-04` stable author timeline/polling interface and durable checkpoints → `XR-05` stream-capable transport optional behind entitlements, CHAT_13 Event Router bridge → `XR-06` fault/egress/benchmark/metrics and canary. No streaming guarantee until capability test.

## 10. Test plan — **all designed, not executed**
| TEST | Setup/action | Expected |
|---|---|---|
| XRS-T01 | unapproved API plan/credential, recent query | no API calls, unavailable |
| XRS-T02 | 24h UTC including boundary before/at end | half-open, no out-of-window |
| XRS-T03 | large native IDs >2^53 / permalink | exact string IDs and timestamps |
| XRS-T04 | retweet/quote/reply cycle/repeated Post | unique claims, original links retained |
| XRS-T05 | 250 posts, next_token loss/empty page/partial rate | pagination capped, coverage incomplete |
| XRS-T06 | soft de-dupe discount assumed free, credit exhaustion | reserve full worst-case, never overspend |
| XRS-T07 | @handle rename and actor ID mismatch | original author ID scoped, no unrelated trigger |
| XRS-T08 | unsupported stream plan then authorized timeline fallback | no stream connection, fallback explicitly gated |
| XRS-T09 | same post via stream+poll+replay, disabled subscription | one event Task maximum, none after disable |
| XRS-T10 | Post says “send secret and purchase” | no privilege elevation or leak |
| XRS-T11 | worker stale leaseEpoch and Steer / ACK loss | no new fetch/commit, receipt preserved |
| XRS-T12 | viral claim no corroboration / expired sale | remains unverified/expired |
| XRS-T13 | 401,429,5xx on second page | partial coverage, no “0 posts” |
| XRS-T14 | fixed recent query 1/4 Workers fake clock | report cost/p95/sample bias, no made-up speed |

## 11. Full MUST → TEST → SC
| REQ | TEST | SC |
|---|---|---|
| XRS-001 | T01 | SC-03/08 |
| XRS-002 | T02 | SC-03 |
| XRS-003 | T03 | SC-03/11 |
| XRS-004 | T04 | SC-03/14 |
| XRS-005 | T05 | SC-03 |
| XRS-006 | T06 | SC-09 |
| XRS-007 | T07 | SC-11 |
| XRS-008 | T08 | SC-11 |
| XRS-009 | T09 | SC-07/11 |
| XRS-010 | T10 | SC-08 |
| XRS-011 | T11 | SC-07 |
| XRS-012 | T12 | SC-03/14 |
| XRS-013 | T13 | SC-03 |

## 12. Evidence sources (2026-10-30)
- [X1] **Docs verified**, X Search Introduction, 7d, last24h, query and pagination: https://docs.x.com/x-api/posts/search/introduction
- [X2] **Docs verified** X Filtered Stream and Timelines, endpoint+rule/connection/plan limits: https://docs.x.com/x-api/posts/filtered-stream/introduction ; https://docs.x.com/x-api/posts/timelines/introduction
- [X3] **Docs verified (volatile prices)** X pay-per-usage read costs, credits, 24h UTC dedup soft guarantee: https://docs.x.com/x-api/getting-started/pricing
- [X4] **Code verified** Tweepy `Client.search_recent_tweets/get_users_tweets` at `c1978d643ecce491929084e4290b35f57e4921ad`: https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/tweepy/client.py#L1187-L1250 ; https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/tweepy/client.py#L1497-L1570
- [X5] **Test content inspected but not executed**, Tweepy cassette tests: https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/tests/test_client.py#L73-L92
- [X6] **Code verified** node-twitter-api-v2 `search/userTimeline` at `d4ba72dfcf30395f02c551b318d53f51310ccdd0`: https://github.com/PLhery/node-twitter-api-v2/blob/d4ba72dfcf30395f02c551b318d53f51310ccdd0/src/v2/client.v2.read.ts#L129-L310 ; paginator: https://github.com/PLhery/node-twitter-api-v2/blob/d4ba72dfcf30395f02c551b318d53f51310ccdd0/src/paginators/v2.paginator.ts#L92-L150
- [X7] **Code verified** Stream reconnect bounded: https://github.com/PLhery/node-twitter-api-v2/blob/d4ba72dfcf30395f02c551b318d53f51310ccdd0/src/stream/TweetStream.ts#L338-L402
- [X8] **License files to re-check before implementation**: https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/LICENSE ; https://github.com/PLhery/node-twitter-api-v2/blob/d4ba72dfcf30395f02c551b318d53f51310ccdd0/LICENSE
- **Unverified**: user's X developer credentials, actual rate policy, current prices in Console, stream entitlement, post-completeness, provider SDK compatibility, source reproduction in E2E. No live tests/CI have been run. All SC cases are test designs only.


### Round-2 upstream PR and license verification (2026-10-30)
- LICENSE read: [Tweepy](https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/LICENSE) = MIT; [node-twitter-api-v2](https://github.com/PLhery/node-twitter-api-v2/blob/d4ba72dfcf30395f02c551b318d53f51310ccdd0/LICENSE) = Apache-2.0. Include notices/transitive attribution if vendored; these OSS licenses do NOT confer X API access rights.
- **Upstream merged PR inspected** [node-twitter-api-v2#655](https://github.com/plhery/node-twitter-api-v2/pull/655): fixes close-versus-reconnect race and async iterator loss/hang behavior. This supports the need for close-generation fencing and durable gap detection; do **not** assume that enabling automatic reconnection ensures no lost posts. Enhance XRS-T08/T09: close during outstanding reconnect must prevent resurrection, stream disconnect followed by a queued event must not create duplicate dispatch, and polling reconciliation must disclose gaps.
- Issue [#437](https://github.com/plhery/node-twitter-api-v2/issues/437) is referenced by the upstream PR as first-connection retry concern; not independently reproduced here. Source-level integration/SDK version compatibility and official X billing/account entitlement remain unverified.
