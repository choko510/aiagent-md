# Data Extraction & Normalization — Detailed Implementation Specification

> CHAT_04 | `docs/04-web-browser/data-extraction-and-normalization.md` | Status: **Draft / PROVISIONAL** | Rev 0.1 | 2026-10-30 JST | Main `b45d4e989998663a00ef09673e69ae1f4191f00d` | **Harness tests executed: 0**

## 0. 採用設計・非目標・理由
検索/HTML/Browserから出た文章をそのまま価格や日付として扱うと、税込条件・人数・容量・送料の取り違えが生じる。元Artifactを正本として、決定的CSS/XPath/JSON-LD抽出→schema/型→単位/通貨/日付の正規化→source spanと観測時刻を保存する。LLMは欠損/曖昧候補の補助抽出に限定し、根拠テキストの存在検証が通らない値を確定しない。

**採用:** deterministic extractor first、strict typed field + evidence locator + raw claim/normalized value distinction、LLM assisted only with proven source span、unknown ≠ zero。
**却下:** HTML全量をLLMへ渡して真偽の判断まで委任、各サイトで商品同一性を暗黙merge、値だけをDBに上書きし出典を破棄。
**非目標:** 外部正式APIの契約、商品/ホテル/店舗間のEntity resolution（CHAT_12）、Watch triggerの閾値判定、スクリーンショット/OCRの総合パイプライン（CHAT_15）。

## 1. Scope / Goal / Ownership / SC
入力はHTTP/Browser/Sidecarから得たartifactRefと抽出要求Schema、対象source URL/observedAt。出力は、見つかったClaim（raw text、DOM XPath/CSS、JSON pointer、source timestamp）とNormalizedScalar、欠損理由、複数候補・矛盾マーカー。確定情報として結果を使うにはCHAT_02 Verifier/CHAT_12 Entity Resolveがさらに検証。
関係するシナリオ: SC-05, SC-10, SC-01, SC-12, SC-15, SC-04, SC-03, SC-02, SC-08, SC-09, SC-14, SC-07, SC-13。SC-10/12/14のEntityと変化検出はCHAT_12/13の所有物で、このモジュールが勝手に「同じ商品・重大な変化」を確定しない。

## 2. Source-code audit / テスト・license
[S1] **Scrapling** at `26dc85a4fa307257fb5c669c80aa44b15acb84c4` (2026-10-30, Code verified: source/test read, not executed; BSD-3-Clause): [`scrapling/parser.py — Selector.css/xpath/TextHandler`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/parser.py) · [`tests/parser/test_general.py — css/xpath/test data`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/parser/test_general.py) · [`tests/parser/test_parser_advanced.py — adaptive selector/text/edge cases`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/parser/test_parser_advanced.py) · [`LICENSE`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE).
[S2] **Browser Use** at `2cc71890e65056b67fdb29d98bb407749c15bb08` (2026-10-30, Code verified: source/test read, not executed; LICENSE再確認): [`browser_use/tools/service.py — Tools.extract / DOM extraction`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py) · [`tests/ci/test_structured_extraction.py — schema validation/extra fields tests`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_structured_extraction.py) · [`browser_use/browser/session.py — BrowserSession/stop`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py).
Scrapling SelectorはCSS/XPathとHTML text extractionを提供、`tests/parser/test_general.py`が価格/レビューのfixture、`test_parser_advanced.py`が適応Selectorの失敗/境界をカバーする。Browser Use Tools.extractにはLLM抽出経路があり、`tests/ci/test_structured_extraction.py`はschemaをPydanticへ変換した場合のoptional/null/extra fieldsやstructured extractionを試験する。いずれも本Harnessの通貨・税・商品同一性・元価格条件を自動で正しく解決する保証ではない。
Code verifiedは**ソースの内容を確認した**の意であり、元の自動テストを実行したとの意味ではない。OSSベンチマーク/コミュニティIssue/PR網羅調査は未完。プロジェクト固有設計をCode verifiedと呼ばない。

## 3. 依存図 / Trust Boundaries
```text
Search/Fetch/Browser(CH04)→Raw Artifact(CH01/10)→static CSS/XPath/JSON-LD parser(Scrapling sidecar)→Candidate claims→Schema/unit/date validate→Evidence refs→Verifier(CH02)/Entity Resolution(CHAT_12)/Diff(CHAT_13)
```
- BrowserSession/PageはCHAT_04 Browser Worker所有。Tool approval/side effect classificationはCHAT_03正本。Secret/Authenticator/egressはCHAT_09正本。REST+WebSocket/SSE UXはCHAT_08、state/Event/Evidence/Error/公開UUIDv7はCHAT_01、Task/Run checkpoint/goalRevisionはCHAT_02、物理DDLはCHAT_10。
- CHAT_01のBrowserSessionはtenant+task+optional activeRunIdに束縛。Task.conversationIdは**nullable**で良い設計だがCHAT_10とCHAT_08が異なるDraft。外部から`tenantId/ownerUserId`の任意指定を受けない。
- `recordVersion`初期1 vs DB `revision`初期0、Run `leaseEpoch` number vs bigint/DB列不足、UUIDv7 vs DB `gen_random_uuid()`、Tenant vs Workspaceは**未合意P0**。具体的CASで旧ownerが操作できない保証をOwnerに提案。本書の型はローカルで[PROVISIONAL]。

## 4. Requirements
| ID | Level | 要件 | 期待値（assert可能） | SC |
|---|---|---|---|---|
| EXT-001 | MUST | 元Raw Artifactとsource URL/取得時刻を出典として参照 | 元HTMLへ逆参照可能 | SC-05/SC-10 |
| EXT-002 | MUST | raw textとnormalized valueを別fieldで保存 | 抽出原文を失わない | SC-01/SC-12 |
| EXT-003 | MUST | 抽出値ごとにCSS/XPath/JSON pointer/spanを記録 | quoteとlocatorが実DOMで一致 | SC-05/SC-15 |
| EXT-004 | MUST | priceを通貨・税込・送料・条件・単位別に型付け | ¥、$など記号だけで通貨決定しない | SC-01/SC-04 |
| EXT-005 | MUST | decimalとint minor unitの精度を保つ | 浮動小数の金額誤差0 | SC-01 |
| EXT-006 | MUST | Published/Updated/Observed/ValidUntilを異なる時刻に分離 | 掲載日時を取得時刻と偽らない | SC-03/SC-05 |
| EXT-007 | MUST | TZが不明なlocal dateをUTCへ推測変換しない | ambiguousとunknown区別 | SC-04/SC-05 |
| EXT-008 | MUST | 異なるSKU/容量/色/人数/部屋条件を同一Claimにmergeしない | false join 0 | SC-01/SC-04/SC-10 |
| EXT-009 | MUST | 0/空欄/未取得/アクセス拒否/parse失敗を区別 | fetch失敗から価格0を生成しない | SC-02/SC-12 |
| EXT-010 | MUST | LLM候補はraw evidence内source spanを照合して検証 | 存在しないquoteを棄却 | SC-05/SC-08 |
| EXT-011 | MUST | 抽出結果をHTML命令と分離しTool Policyに昇格させない | 注入で権限変更0 | SC-08 |
| EXT-012 | MUST | entity-scale regexやXPathにCPU/bytes/time上限 | ReDoS/巨大HTMLを有限停止 | SC-08/SC-09 |
| EXT-013 | MUST | 重複値と互いに矛盾する値のcoverageを保持 | 不確実性を隠さない | SC-05/SC-14 |
| EXT-014 | MUST | Run fence/revisionとtenant scopeを保存前に確認 | 旧Workerの値が最新観測にならない | SC-07/SC-08 |
| EXT-015 | SHOULD | LLMを使う場合model/Schema/費用をEvidence traceへ | 決定的抽出との差分計測 | SC-09 |
| EXT-016 | LATER | サイト別verified extractor recipeをCHAT_14へ連携 | 変更時は自動invalidate | SC-13 |

## 5. Type/API/Event/Validation (CH04 local)
```typescript
type ExtractionRequest = {schemaVersion:1; artifactRef:string; sourceUrl:string; observedAt:string; expectedFields:Array<{key:string;kind:"money"|"date"|"text"|"number"|"boolean"; locator?:{kind:"css"|"xpath"|"json-pointer";selector:string}}>; localeHint?:string; timezoneHint?:string; maxClaims:number};
type Claim<T> = {fieldKey:string; rawText:string; normalizedValue?:T; unit?:string; currency?:string; priceTaxBasis?:"included"|"excluded"|"unknown"; conditions?:string[]; source:{artifactRef:string;url:string;locator:string;spanStart?:number;spanEnd?:number;observedAt:string}; dateBasis?:"published"|"updated"|"valid_until"|"observed"; confidence:"deterministic"|"llm_unverified"|"source_verified"|"unknown"; status:"known"|"conditional"|"ambiguous"|"missing"|"parse_failed"};
type ExtractionOutcome = {status:"complete"|"partial"|"failed";claims:Claim<unknown>[];unknownFields:string[];contradictions:string[];sourceVersionHash:string};
```
CurrencyはISO4217、amountはdecimal string/BigInt minor unit、timezoneはIANA / unknown、percent/yen etc分離。課金単位/泊/室/人などunitも必須。スクリーンショット出典はOCR結果だけで確定にしない。DOM offsetはArtifactのハッシュ一致時のみ有効。
- schemaVersion未知major拒否、オプションのmissing/null/unknownを区別。ブラウザ/Task/Runのpublic IDはCHAT_01 UUIDv7 validatorに委譲し、Browser internal id、Artifact hash、Ticket nonceと区別。
- Tool Gatewayから承認済みoperationRef、tenant-qualified ownership、Run/BrowserSession lease/goalRevision snapshotを受け取り、send/commit前に**最新版のfence**を照合。リプレイ時に静的IDだけで権限を証明しない。

## 6. 正常Sequence / 状態遷移
```text
RAW_STORED→PARSER_SELECTED→CLAIM_CANDIDATES→SOURCE_SPAN_VERIFIED→NORMALIZED→EVIDENCE_COMMITTED
INVALID_SOURCE→RETRY_FETCH; PARTIAL/AMBIGUOUS→VERIFIER; CANCELLED→STALE
```
1. Artifact ACL/Run fence確認→2. Source hash/MIME検証→3. CSS/XPath/JSON-LD deterministic extraction→4. schemasで見つかった候補すべてを保存→5. 金額のcurrency/condition/dateとunitを判定→6. 出典span再検証→7. 合致しない値はunknown→8. EvidenceRef保存とVerifierへ返却。

## 7. アルゴリズム（疑似コード）
```typescript
async function extract(request,ctx) {
 const raw=await artifacts.readScoped(request.artifactRef,ctx);
 ctx.assertFence(); const parsed=await parseSandboxed(raw,request.maxClaims);
 const candidates=runTypedExtractors(parsed,request.expectedFields);
 const claims=[];
 for (const c of candidates) {
   if(!pointsIntoRawArtifact(c,raw.hash)) { claims.push(unknown(c,"SOURCE_MISMATCH")); continue; }
   const value=normalizeNoFloat(c.rawText,c.kind,request.localeHint,request.timezoneHint);
   claims.push({rawText:c.rawText,normalizedValue:value.ok?value.value:undefined,
                status:value.ok?"known":"ambiguous",source:c.source});
 }
 await persistEvidenceWithFence(claims,ctx); return classifyCoverage(claims,request.expectedFields);
}
```
関数名は実装提案であって既存OSS API名を捏造したものではない。Gateway/DB/Secret Storeの呼出しが正式Contract確定前に[PROVISIONAL]であることを明示。

## 8. 失敗/競合/キャンセル/Steer/Restart
| 状況 | 検知 | 復旧 | 最終状態 |
|---|---|---|---|
| HTML changed after fetch | Artifact hash differs | new fetch/reparse not stale selector | STALE |
| price conditional | tax/coupon/stock ambiguous | store conditional claim | PARTIAL |
| invalid currency | ISO code absent | keep raw, unknown currency | AMBIGUOUS |
| source 403 | fetch status | do not produce 0 | UNAVAILABLE |
| LLM hallucinates quote | no raw span match | reject specific claim | UNVERIFIED |
| worker kill before persist | artifact available | idempotent parse after fence | RETRY_SAFE |
| source contains role injection | input classifier | no policy upgrade | DENIED |
- Controllerの複数worker claim時はepoch fenceにより一方のみ取得。旧workerは遅延完了を現行結果へcommitできない。
- 意図保存→外部Action→結果保存の間にKillしたら`UNKNOWN`を識別。ログイン操作やボタン押下はread-onlyと断定できない。不可逆な外部Actionの再送は禁止。Cancel/Steer後は新Action開始0件。

## 9. Security/Privacy/Limits/Operations
HTML/JSON-LDの<script>を実行せずparse-only、XML外部Entity/XPath外部I/Oを禁止。UUIDで示したArtifactをACL評価する。Model extractionを使う場合は最小のRedacted passage、schemaに制限し資格情報除外。ユーザーの明示した条件を外部HTMLが上書きしない。
- ページのHTML/Script/Metadata/視覚テキストはuntrusted data。DOM内「システム指示」は無権限。認証・秘密・PIIはLLM、Trace、Raw Artifactへ出さない。制限付きResourceとして必要最低限保持。
- Host Shell/SSH/Docker socket/privilegedコンテナ禁止。購入/予約/投稿/送金の承認なし実行禁止。
- **予算初期提案**: max raw HTML 4MiB、maxClaims100、1 Task50 fields、1 regex100ms・1 document parse5s、LLM fallback <=2回/Task（全て仮値）。Gatewayの厳格な総量上限・ユーザー拒否が優先。外部請求も含めfinite。
- Metrics: task/run/owner-safe trace ID、epoch、toolAttempt、source snapshot checksum、before/after status、observation time、parse success、unknown率、latencyMs、memory RSS、redaction failures、model token/total cost。原文・URL tokenはログではmask。
- 同一fixture（日本語/英語、HTML変更、タイムゾーン差、認証済み/非認証、滞留ページ）の成功/誤判定率と費用を測定する。実測値なし。

## 10. Persistence / CAS / Migration proposals
Immutable raw artifact hash、source URL/provider/capturedAt/observedAt、Claim field key、selector+source span、normalized Decimal、unit/conditions、schemaVersion、sourceVerification status、extractorVersionとuncertainty。価格の現在値を保持する場合も元観測行を上書きしない。
原子操作: intent/lease reservation commit → 外部Action → result/ref + audit/outbox commit。DB physical schemaとmigrationはCHAT_10、Event/Evidence canonical shapeはCHAT_01。新規型はowner合意までlocal representationに留める。

## 11. REST/UI/他担当接続
ユーザー表示に実測/推定/条件付き/不明を分離。金額は税込総額と税/送料/値引き条件を明記。欠損値を0表示しない。各Claimから元URL+取得日時+スニペットに辿れる。

## 12. 実装PR順序
1. **04-EXT-1** Typed schema and deterministic parser / provenance fixture
2. **04-EXT-2** Money/date/conditions normalization and contradiction detection
3. **04-EXT-3** LLM optional extraction with source-span verifier
4. **04-EXT-4** Entity/Watch/Change/Multimodal contracts+benchmarks

## 13. Test design・REQ→TEST→SC
**すべてテスト設計、実行なし**。Unit/Contract/Integration/Fault/Security/E2E/BenchmarkのFixtureと期待値。

| TEST-ID | Setup → Action | Expected | REQ→SC |
|---|---|---|---|
| EXT-T01 | Raw artifact hash and URL retrieval | 元HTMLへ逆参照可能 | EXT-001→SC-05/SC-10 |
| EXT-T02 | raw 1290yen vs normalized amount | 抽出原文を失わない | EXT-002→SC-01/SC-12 |
| EXT-T03 | CSS/XPath value span | quoteとlocatorが実DOMで一致 | EXT-003→SC-05/SC-15 |
| EXT-T04 | USD vs JPY ambiguous sign | ¥、$など記号だけで通貨決定しない | EXT-004→SC-01/SC-04 |
| EXT-T05 | 19.99 decimal exactness | 浮動小数の金額誤差0 | EXT-005→SC-01 |
| EXT-T06 | publish 2024 vs observed 2026 | 掲載日時を取得時刻と偽らない | EXT-006→SC-03/SC-05 |
| EXT-T07 | missing timezone ambiguity | ambiguousとunknown区別 | EXT-007→SC-04/SC-05 |
| EXT-T08 | 256GB vs 512GB prices | false join 0 | EXT-008→SC-01/SC-04/SC-10 |
| EXT-T09 | 403 vs price0 | fetch失敗から価格0を生成しない | EXT-009→SC-02/SC-12 |
| EXT-T10 | LLM invented fact | 存在しないquoteを棄却 | EXT-010→SC-05/SC-08 |
| EXT-T11 | HTML system prompt injection | 注入で権限変更0 | EXT-011→SC-08 |
| EXT-T12 | catastrophic regex 50k | ReDoS/巨大HTMLを有限停止 | EXT-012→SC-08/SC-09 |
| EXT-T13 | two contradictory prices on page | 不確実性を隠さない | EXT-013→SC-05/SC-14 |
| EXT-T14 | stale lease DB commit | 旧Workerの値が最新観測にならない | EXT-014→SC-07/SC-08 |
| EXT-T15 | compare model extractor accuracy | 決定的抽出との差分計測 | EXT-015→SC-09 |
| EXT-T16 | recipe invalidated by DOM change | 変更時は自動invalidate | EXT-016→SC-13 |

## 14. Acceptance
上表で各MUSTに安定REQ-ID→TEST-ID→SC-IDを設定。Userのgoal条件に対する完了・原典検証、跨Tenant保護、cancel/steer/failure matrix、有限予算の試験に合格するまで`Locked`を主張しない。

## 15. 未決事項・Cross-team proposal
- **[PROVISIONAL: CHAT_01] P0 Claim/Evidence contract:** source span/observed vs published vs verified status。検証: EXT-T01/03/06。
- **[PROVISIONAL: CHAT_05/12] P1 identity/price semantics:** Entity resolutionとprice conditionsを抽出から切り分ける。検証: EXT-T04/08。
- **[PROVISIONAL: CHAT_13/14] P1 content diff and recipes:** versioned raw artifact hash+deterministic locatorを使用。検証: EXT-T13/16。
- **[PROVISIONAL: CHAT_10] P0 decimal/date schema:** NUMERIC/observed times/CAS/tenant scope。検証: EXT-T05/14。
- **CHAT_11**: Mock auth takeover、悪意あるHTML、複数Worker、照合不能フィールド、SC-05〜16の関係部分のE2E fixtureを正式テスト正本へ追加。
未解決P0はIdentity/Tenant/DB Epoch/Fencing/Authentication Ticket。全件Draft。

## 16. Sources / evidence grade（2026-10-30 JST）
[S1] **Scrapling** at `26dc85a4fa307257fb5c669c80aa44b15acb84c4` (2026-10-30, Code verified: source/test read, not executed; BSD-3-Clause): [`scrapling/parser.py — Selector.css/xpath/TextHandler`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/scrapling/parser.py) · [`tests/parser/test_general.py — css/xpath/test data`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/parser/test_general.py) · [`tests/parser/test_parser_advanced.py — adaptive selector/text/edge cases`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/tests/parser/test_parser_advanced.py) · [`LICENSE`](https://github.com/D4Vinci/Scrapling/blob/26dc85a4fa307257fb5c669c80aa44b15acb84c4/LICENSE).
[S2] **Browser Use** at `2cc71890e65056b67fdb29d98bb407749c15bb08` (2026-10-30, Code verified: source/test read, not executed; LICENSE再確認): [`browser_use/tools/service.py — Tools.extract / DOM extraction`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/tools/service.py) · [`tests/ci/test_structured_extraction.py — schema validation/extra fields tests`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/tests/ci/test_structured_extraction.py) · [`browser_use/browser/session.py — BrowserSession/stop`](https://github.com/browser-use/browser-use/blob/2cc71890e65056b67fdb29d98bb407749c15bb08/browser_use/browser/session.py).
**Design hypothesis:** 本Harness内の構造、limit、状態遷移、権限検証順序、パーサー・Human Handoff Bridgeは未実装。将来OSS仕様が変わればpinned SHAと再比較する。
