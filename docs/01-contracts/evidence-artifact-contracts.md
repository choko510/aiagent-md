# Evidence, Observation and Artifact Contracts — Detailed Implementation Specification

> Owner CHAT_01 | Canonical docs/01-contracts/evidence-artifact-contracts.md | Status Draft / PROVISIONAL | Rev 0.1 | Research 2026-10-30 JST | latest main b45d4e989998663a00ef09673e69ae1f4191f00d | Dependencies 01-01 core, 01-02 events, 01-04 Tool IO, CHAT_04/05/07/09/10/12/13/15.

## 0. 採用案

**Artifact = 取得した原本/抽出物のバイト列。Observation = いつ、誰が、どのSourceから、何を読み取ったかという構造化観測。Evidence = ObservationやArtifactから、ある主張を支持/反証する証拠の単位。** 3つは別ID/別寿命。Agent MemoryやUIのWorking Contextを一次証拠に昇格しない。RawはContent-addressed Blob Store、Metadataとowner/provenance/refはPostgreSQLが正本。観測時刻、収集時刻、サイト公開時刻、情報の期限、抽出モデルの推測、正確性の検証状態を区別する。利用者へは出所と確認日時を示す。

## 1. Scope、non-scope、SC

対象: artifact metadata/identity/hash、observation typed values+time、evidence claim/provenance、cross-source relations、citation locators、freshness、unit/currency/confidence semantics、privacy/retention・immutable source。対象外: 実際のHTTP/Browser取得（CHAT_04）、Provider API（CHAT_05）、商品照合score（CHAT_12）、通知条件（CHAT_07）、物理storage（CHAT_10）、multimodal parser（CHAT_15）、verifier算法（CHAT_02）。特にSC-01 商品利益と容量、SC-02 価格監視、SC-03 SNS、SC-04 ホテル、SC-05 横断Web、SC-06 browser-auth、SC-07 recovery、SC-08 security、SC-09費用、SC-10/12/14/15/16に適用。

## 2. OSS横断調査（実コードとテスト静的閲覧、未実行）

| OSS/ref/license | 確認した具体コード/テスト | 設計利用と不足 |
|---|---|---|
| LangGraph ffc3336367e8139668d11e5f0b0e6def57e7c762 MIT [S1] | libs/checkpoint/langgraph/store/base/__init__.py: Item, PutOp, GetOp, BaseStore.get/put/search; libs/checkpoint/tests/test_store.py: namespace guard/search/pagination/cancel tests | namespace/key,value storeは参考だが原本バイトcontent hashと観測/主張の参照構造は別に設計する |
| PydanticAI 3809c1b861036197de4a06ae3ee38e27e5455100 MIT [S2] | pydantic_ai_slim/pydantic_ai/messages.py: ImageUrl/DocumentUrl/BinaryContent/StructuredContent、tests/test_messages.py: binary image/document, data URI, content serialization/roundtrip | Media type/bytes/structured textを別扱いする。画像PDFのpage/cell provenanceはSDK media objectsに含まれないため自作 |
| MCP TS SDK 39ae8e6192cf43273aaaf2de4169dfa9f7216e28 [S3] | packages/core-internal/src/types/spec.types.2025-11-25.ts: Resource, Content, CallToolResult; test/e2e/scenarios/dynamic.test.ts | MCP resource URI/embedded contentはtool transport用であり長期Artifact retentionとauth scopeを保証しない。SDK/revisionとlicense詳細は要再検証 |

Alternative A: LLM回答を根拠として保存する→二次推測と原資料が混ざり却下。B: 全HTML/画像を毎回LLMへ送る→コスト、秘匿、入力長超過で却下。C: immutable Artifact + structured Observation + claim-specific Evidence DAG→採用。D: URLのみを保存し再訪時再取得→価格/空室が変わり再現性がないため単独採用禁止（storage TTLが短い場合のみ制約明示）。

## 3. 所有権、依存・データフロー

~~~text
Search/API/HTTP/Browser/MCP/File upload
   -> Source intake policy + egress/permission gate
   -> RawArtifact (SHA256, bytes, MIME, sanitized metadata; content private)
   -> Extraction (deterministic parser; optional model)
   -> Observation (typed field, occurredAt/observedAt/fetchedAt/source)
   -> Evidence (specific claim, support/refute, verification, locator)
   -> Verifier & Result / Watch diff / Trend fusion / UI citations
~~~

Artifact cannot itself authorize crawling or replay of a credential-bearing page. CHAT_09 checks tenant/owner on every dereference, even when SHA256 hash is the same across users. External URLs are untrusted evidence locations, not commands. Source reliability/profile scores from CHAT_12 never silently replace source timestamps.

## 4. Requirements

| REQ | Level | Expected | SC |
|---|---|---|---|
| EVD-001 | MUST | Artifact immutable bytes and content hash validated | 05,07,15 |
| EVD-002 | MUST | Observation/Evidence/Artifact IDs and references separated | 01,02,15 |
| EVD-003 | MUST | observedAt/fetchedAt/publishedAt validUntil all distinct, unknown allowed | 01,03,04 |
| EVD-004 | MUST | money as decimal string+ISO currency, unit and product variant preserved | 01,10 |
| EVD-005 | MUST | unsupported/expired/estimate not presented as confirmed fact | 01,04,14 |
| EVD-006 | MUST | quote/provenance location (URL/page/box/cell) independently verifiable | 03,05,15 |
| EVD-007 | MUST | tenant/owner privacy enforced for every Artifact/Evidence read | 06,08 |
| EVD-008 | MUST | extraction/version/source lineage immutable, re-extract creates new Observation | 07,12 |
| EVD-009 | MUST | source failure not converted to price=0 or unchanged | 02,12 |
| EVD-010 | MUST | Evidence may support or contradict claim; contradiction surfaced | 03,14 |
| EVD-011 | MUST | raw bytes never dumped wholesale into Event/LLM/trace | 05,09 |
| EVD-012 | MUST | orphan Artifact and retention/deletion safe with legal/private constraints | 08 |
| EVD-013 | MUST | user-visible citations map to actual fetched source with date and locator | 01,04,15 |
| EVD-014 | MUST | before persistence and served response, HTML/script/URL token redaction policy | 08 |
| EVD-015 | MUST | provenance of transformed/derived values includes algorithm/version/inputs | 01,10,14 |
| EVD-016 | SHOULD | reliability/sampling warning carried without pretending numerical certainty | 14 |
| EVD-017 | SHOULD | duplicate raw bytes dedup only within authorized storage scope | 09 |
| EVD-018 | LATER | human annotations linked as opinions, not rewriting observations | 16 |

## 5. Types / JSON schema proposal

~~~ts
type ArtifactId = Brand<string,"ArtifactId">; // UUIDv7
type ObservationId = Brand<string,"ObservationId">;
type EvidenceId = Brand<string,"EvidenceId">;
type AcquisitionStatus = "ok"|"incomplete"|"failed"|"restricted"|"stale_cache";
type ValueState = "observed"|"derived"|"estimated"|"unknown";
type VerificationState = "unverified"|"cross_checked"|"verified"|"contradicted"|"retracted";
type CitationLocator =
 | {type:"web";url:string;fragment:string|null;textRange:{start:number;end:number}|null}
 | {type:"pdf";artifactId:ArtifactId;page:number;rect:{x:number;y:number;width:number;height:number}|null}
 | {type:"image";artifactId:ArtifactId;rect:{x:number;y:number;width:number;height:number}}
 | {type:"sheet";artifactId:ArtifactId;sheetName:string;cellRange:string}
 | {type:"text";artifactId:ArtifactId;startLine:number;endLine:number};
type ArtifactV1 = EntityMeta & {
  id:ArtifactId; ownerUserId:UserId; kind:"raw"|"normalized"|"derived"|"uploaded";
  mediaType:string; byteLength:string; sha256:string; storageKeyRef:string;
  sourceUrlSanitized:string|null; fetchedAt:UtcInstant|null;
  parserVersion:string|null; parentArtifactIds:ArtifactId[];
  piiClass:"public"|"personal"|"restricted"; retentionClass:string;
  integrityStatus:"pending"|"verified"|"quarantined";
};
type ObservationV1 = EntityMeta & {
  id:ObservationId; taskId:TaskId; runId:RunId|null; watchId:WatchId|null;
  ownerUserId:UserId; sourceArtifactId:ArtifactId|null;
  acquisition:AcquisitionStatus; observedAt:UtcInstant|null;
  fetchedAt:UtcInstant; publishedAt:UtcInstant|null; validUntil:UtcInstant|null;
  sourceUrlSanitized:string|null; originalSourceId:string|null;
  fieldKey:string; subjectRef:string|null; valueState:ValueState;
  value:unknown|null; unit:string|null; currency:string|null;
  extractorName:string; extractorVersion:string; confidenceCode:string|null;
  locator:CitationLocator|null;
};
type EvidenceV1 = EntityMeta & {
  id:EvidenceId; ownerUserId:UserId; taskId:TaskId; runId:RunId|null;
  claimId:string; relation:"supports"|"refutes"|"context";
  observationIds:ObservationId[]; artifactIds:ArtifactId[];
  locator:CitationLocator; verification:VerificationState;
  verificationNoteRef:string|null; reviewerKind:"rule"|"model"|"human";
  checkedAt:UtcInstant|null; provenanceVersion:"1.0";
};
~~~

All IDs UUIDv7 generated by trusted Server and Tenant/owner scoped. Content storageKeyRef is an opaque internal handle, never a public URL or secret. Metadata recordVersion/updatedAt use Core Domain (DB bigint, lossless string). For JSON/currency **never binary float**; decimal amount canonical string, currency ISO 4217 uppercase; dimensioned quantity requires unit and original measurement. A missing price is null+unknown state, not zero. Validation rejects NaN/Infinity and currency-less monetary fields. Timestamp written in UTC; source timezone and local date belong to source-specific provenance when necessary (hotel stay date is distinct from observation time). Overlapping conflicts cannot be resolved solely by newest fetchedAt.

**Confidence** is NOT an arbitrary percent without calibration. Initial use named confidenceCode (e.g. source_claim/unverified/corroborated) with source reliability separate; quantitative confidence scored only after empirical calibration by CHAT_12/11. verified is evidence review status, not guarantee of truth. Event schema for evidence.recorded references EvidenceId (no raw bytes). Observation can record acquisition failure without Value; such a record cannot satisfy monetary threshold.

**CitationLocator validation:** PDF page >=1, coordinates normalized to original dimensions with orientation/version stored; image rectangle within positive width/height; Sheet range case/format fixed by parser, no cells outside sheet; text spans within indexed artifact; WEB URL canonicalized, secrets removed and SSRF/allowlist enforced. Page/box/cell from OCR/LLM needs verification status unverified until reproducibly checked.

## 6. State/sequence and provenance

~~~text
1. authorize source and capture original response or upload in bounded staging
2. verify bytes and checksum, safe MIME detection, malware/active content quarantine
3. commit immutable Artifact metadata + bytes reference
4. parse under versioned extractor (no macro/script execution)
5. persist Observation for each field with time/locator and valueState
6. evidence verifier joins claim to concrete Observation+Artifact versions
7. persist Evidence and linked Event; finalize result only after verifier policy
8. later refetch -> NEW Artifact and Observation, never mutate original reading
~~~

Artifact metadata integrity: STAGED→VERIFIED→QUARANTINED/EXPIRED via trusted store lifecycle. These are storage-specific phases, not Task/Run states. Original hash is immutable, and Artifact bytes cannot be modified after VERIFIED. Observation never rewritten to reflect newly changed price; record new Observation and compare with prior. Evidence can be superseded by a new Evidence record with relation refutes and reviewer note, not delete earlier observation. **Source claiming quoted price is not necessarily current sellable price**, contract explicitly distinguishes scrape time and eligibility/stock conditions.

## 7. Pseudocode

~~~ts
async function ingestSource(ctx, source, bytes) {
  requireCurrentTaskOwnerAndToolPolicy(ctx,source); // CHAT_03/09
  const staged = await blob.putStagingWithSha256AndByteCap(bytes);
  const detected = await safeDetectMedia(staged);
  if (!policyPermits(detected,source)) return quarantine(staged);
  const artifact = await db.tx(async tx =>
    tx.insertArtifactRefUniqueScoped(ctx, staged.sha256,detected));
  await blob.promoteAndVerify(artifact,staged);
  return artifact;
}
async function materializeClaim(ctx, input) {
  const original = await loadArtifactAndOwnerCheck(input.artifactId,ctx);
  const extracted = await sandboxedExtractor(original,input.extractorVersion);
  const observation = validateObservation(extracted,input);
  if(observation.acquisition !== "ok") observation.value=null;
  return db.tx(async tx => {
    await tx.requireSameTaskTenantAndRun(ctx, observation);
    await tx.insertImmutableObservation(observation);
    const ev = await verifier.buildVersionedEvidence(observation,input.claim);
    await tx.insertEvidenceAndEventOutbox(ev);
    return ev;
  });
}
~~~

**Sagas:** Object store and Postgres cannot generally share a transaction: staging may leave orphan bytes, DB insert may fail after successful upload, promote may fail after metadata commit. Use integrityStatus and deterministic reconciliation: unverified Artifact cannot support verified Evidence; unreferenced staging is deleted after bounded retention; never claim success just because DB commit happened.

## 8. Failure and safety matrix

| Fault | Handling | User representation |
|---|---|---|
| source timeout/403/429 | failed/restricted Observation, no false price | unavailable |
| site shows old cached HTML | source clock+fetchedAt distinct | stale/unverified |
| different capacity same product name | subjectRef includes variant, resolver validation | separate product candidates |
| price missing currency | reject monetary Observed value, mark unknown | cannot calculate reliable profit |
| upload contains PDF script/XLS macro | inert parsing, quarantine active content | extraction restricted |
| blob checksum mismatch | quarantine, reject Evidence join | cannot verify |
| DB commit after upload fails | orphan staging GC, retry only metadata safely | partial/unknown |
| parser crashes middle of table | incomplete Observation plus missing cell provenance | partial |
| two sources contradict | opposing Evidence links, no default pick by newest | disputed |
| authorizer revoked midread | deny deref; cannot continue user-private result | permission needed |
| old evidence supports Steered goal | verifier revalidate constraints/GoalRevision | not auto-success |
| OBS duplicate crawler replay | unique idempotency source tuple, raw versioning | no duplicate alert |
| retention purges raw | evidence becomes not independently retrievable; mark degraded | provenance limited |
| prompt injection in HTML | interpreted as source data only | no new tools |

## 9. Security/privacy/limits

No credentials/cookies/tokens or internal IP in exposed sourceUrl; raw protected URLs can only be stored as encrypted secret reference with access policy. Cross-tenant content hash dedup is not an authorization bypass; avoid exposing existence via SHA lookup/timing. Source content is hostile, cannot authorize Tool execution or rewrite policy. File inputs are size/CPU/page/memory bounded, macros/scripts never executed, no host Shell/SSH/Docker. Raw HTML and LLM-visible text clipped to relevant extracts and escaped before prompt. Proposed default caps: raw source 20MiB, parser 100 pages, 100k cells, 1000 Observations/Task; all finite [PROVISIONAL: CHAT_04/09/10/15] pending benchmark. Retention may vary by data source terms and privacy consent; no indefinite personal data by default.

## 10. Observability and benchmark

Trace object ID, tenant, task, run, source domain safely hashed, capture/extraction timings, checksum/integrity, number of observations, citation locator resolution, stale ratio, source error and uncertainty counts. Benchmark datasets: 100 HTML pages, 100 PDFs with tables, 10k XLSX cells, 429/timeout/UTF8-bad cases, two contradictory sources; compare deterministic extraction vs optional model and record p50/p95/p99 bytes, provenance precision, RAM, cost and false-merge count. No measured success or speed claim.

## 11. Persistence/migrations [PROVISIONAL: CHAT_10]

Tables/artifact metadata with content hash, blobRef and retention; observations typed claim values/provenance/time; evidence claims/relation/locators; provenance graph edges. Required unique tenant+artifactId and tenant+observationId; sameTask and tenant FKs, indexes (tenant,task,fieldKey,fetchedAt) and (tenant,watch,observedAt) etc. Immutability after VERIFIED; CAS recordVersion for metadata changes only; source Observation immutable. Do not add a global SHA uniqueness constraint that leaks cross-owner content existence. Migration for legacy one-row evidence: preserve raw old value in quarantined observation with provenance=unknown, not retroactively mark verified. Artifact backup and DB metadata must be restored consistently and rehash checked.

## 12. UI/REST/Telegram

Result cites safe source URL, when fetched, site-published date if known, evidence confidence/verifier status, source locator page or cell and elapsed age. Differentiate observed, derived, estimated, unknown. Chart/signal/notification consumers must not convert missing to zero, sampled series to full period, or stale to realtime. Owner-authenticated short-lived download/preview URL only. CSV export includes UTC observed/fetched, currency, unit, Source URL and exact locator.

## 13. Implementation PR slices

EVD-PR1 artifact metadata/typed validator/locator, storage checksum; EVD-PR2 observation extractor pipeline and schema versioning, error states; EVD-PR3 Evidence verifier links/citation API and safe projections; EVD-PR4 cleanup/migration, E2E/multimodal/benchmark. Physical blob adapter selectable; no mandatory Cloud provider.

## 14. Tests (planned only)

| TEST | Setup/action | Expected |
|---|---|---|
| EVD-T01 | write raw, mutate bytes, check sha | mutation rejected/checksum mismatch |
| EVD-T02 | observation/evidence/artifact id swap | validator rejects |
| EVD-T03 | missing observedAt, stale cache, publishedAt future | no invented realtime timestamp |
| EVD-T04 | same product two capacity variants, decimal money | no merged SKU, currency correct |
| EVD-T05 | expired hotel price vs current | cannot declare verified availability |
| EVD-T06 | PDF bbox/page, XLSX A12, image rect, line range | all locators map to source |
| EVD-T07 | other user sameTenant/otherTenant blobRef guessed | 0 bytes disclosed |
| EVD-T08 | re-extract same Artifact new parser version | new Observation, original immutable |
| EVD-T09 | 403/timeout/429 vs unchanged value | no zero/false alert |
| EVD-T10 | two independent contradictory sources | supports+refutes both visible |
| EVD-T11 | 10MiB source and 10 lines LLM context | raw not sent to model |
| EVD-T12 | expiration/quarantine with active reference | safe degrade, no dangling unauthorized ref |
| EVD-T13 | generated user-visible citation | exact source+observedAt+locator |
| EVD-T14 | malicious HTML/prompt and URL credential | no secret leak / action |
| EVD-T15 | derived profit from coupons/shipping, missing field | provenance algorithm/input and uncertainty |
| EVD-T16 | confidence uncalibrated source sparse | categorical not false calibrated percentage |
| EVD-T17 | duplicate hash across tenants | no cross-owner existence oracle |
| EVD-T18 | spreadsheet macro/pdf script embedded | no code execution |
| EVD-T19 | crash blob staging/DB metadata/promote | orphan reconciliation, no verified phantom |
| EVD-T20 | benchmark 100 PDF/HTML/XLS fixtures | p95/RSS/cost and locator accuracy measured |

## 15. Traceability

| REQ | TEST | SC |
|---|---|---|
| EVD-001 | T01,T19 | 05/07 |
| EVD-002 | T02 | 01/15 |
| EVD-003 | T03,T05 | 01/03/04 |
| EVD-004 | T04,T15 | 01/10 |
| EVD-005 | T05,T16 | 01/04/14 |
| EVD-006 | T06,T13 | 03/15 |
| EVD-007 | T07,T17 | 06/08 |
| EVD-008 | T08 | 07/12 |
| EVD-009 | T09 | 02/12 |
| EVD-010 | T10 | 03/14 |
| EVD-011 | T11 | 05/09 |
| EVD-012 | T12,T19 | 08 |
| EVD-013 | T13 | 01/04/15 |
| EVD-014 | T14,T18 | 08 |
| EVD-015 | T15 | 01/10/14 |
| EVD-016 | T16 | 14 |
| EVD-017 | T17 | 09 |

## 16. Open issues and owner proposals

- EVD-P01 P0 CHAT_10: content-addressed storage key, integrityStatus, orphan GC, DB FKs/provenance graph, retention and backup; T01/T19.
- EVD-P02 P0 CHAT_09: owner-grant for raw byte reads, credentials, cross-user hash dedup oracle, privacy/delete/retention; T07/T17.
- EVD-P03 P1 CHAT_04/05: fetchedAt/site published/observedAt collection and source failure semantics; T03/T09.
- EVD-P04 P1 CHAT_07/13: Monitoring Observation and Event source provenance, dedup; T09/T10.
- EVD-P05 P1 CHAT_12: entity resolution, source reliability scoring and false merge costs; T04/T10/T16.
- EVD-P06 P1 CHAT_15: page/rect/cell extraction coordinate conventions, spreadsheet formula security, MIME sniffing; T06/T18.
- EVD-P07 P1 CHAT_08: owner-scoped citation and Artifact preview, explanation of unknown/partial; T13.
- EVD-P08 P0 CHAT_01 self: Artifact IDs and Evidence IDs referenced by 01-04 must import precisely this schema, no duplicate types; Error from 01-06.
- EVD-P09 P1 CHAT_00: privacy retention+reproducibility ADR, decisions about raw storage under source terms.

## 17. Sources (2026-10-30)

[S1] Code verified: https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/langgraph/store/base/__init__.py and https://github.com/langchain-ai/langgraph/blob/ffc3336367e8139668d11e5f0b0e6def57e7c762/libs/checkpoint/tests/test_store.py (Item, PutOp, BaseStore, namespace and paginated tests). MIT LICENSE.  
[S2] Code verified: https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/pydantic_ai_slim/pydantic_ai/messages.py and https://github.com/pydantic/pydantic-ai/blob/3809c1b861036197de4a06ae3ee38e27e5455100/tests/test_messages.py (DocumentUrl/BinaryContent/StructuredContent and tests). MIT LICENSE.  
[S3] Code verified: https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/packages/core-internal/src/types/spec.types.2025-11-25.ts and https://github.com/modelcontextprotocol/typescript-sdk/blob/39ae8e6192cf43273aaaf2de4169dfa9f7216e28/test/e2e/scenarios/dynamic.test.ts. Package licensing/revision compatibility not yet verified.  
[S4] Hypothesis: our Evidence DAG, numeric confidence prohibition, source locator/version/retention constraints. Independent code benchmark not done.  
[S5] Docs verified: main project-brief, architecture-rules, acceptance-scenarios and expansion requirements.

**Review gates:** Security/retention/physical storage/multimodal coordinate P0/P1 dependencies remain. None of the designed tests were executed. Keep Draft.
