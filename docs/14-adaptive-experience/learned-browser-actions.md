# Learned Browser Actions — Detailed Implementation Specification (14-01)

> Owner: CHAT_14 | Canonical path: docs/14-adaptive-experience/learned-browser-actions.md | Status: **Draft / [PROVISIONAL]** | Revision: 0.1 | Research: 2026-10-30 JST | Source baseline: main@3044c046b1caeea70c5ef6edfea7c57387c6c6da | Runtime implemented: **NO** | Tests executed: **0**

## 0. 結論と採用設計

過去の成功済みBrowser Actionを、**目的・権限範囲・ページ構造・期待結果に拘束された不変Recipe候補**として記録する。再利用時は認証済み主体、現在のTask/Run fence、BrowserSession owner/Tab lock、最新URL・DOM/ARIA fingerprint、Gateway PolicyとBudgetをすべて新たに検証し、Playwright locatorで1 stepずつ実行する。結果を再観測してVerifierが成功と判定できなければ成功とは記録しない。**キャッシュに許可/認証情報/成功という事実を保存しない**。

初期Phase-1は **read-onlyと独立判定された操作のみ自動候補化・自動再生可能**。単にclick/GETであることはread-onlyの証明ではない。購入、予約、投稿、ログイン、設定更新、カート追加、フォーム送信、送信先変更、個人情報読み取りの権限拡大は自動学習・自動再生対象外。低リスク観測用Recipeも明示opt-in、scope一致、許可の再評価が必要。未知分類はブロック。

Stagehand v4のBrowserbase側server cacheを本Harnessのlocal Playwright cacheとして扱わない。Stagehand v3→v4の破壊的API差分を混同せず、Stagehand/Jevは動的候補提案まで、実行はCHAT_04 Browser Action Engineに一本化する。

## 1. 目的、Scope、非目標、受入

- 対象: SC-13の同条件再実行でmodel呼出しを減らし、URL/DOM/account変化で失効し、新規の安全な計画へ切り替える。SC-05一般調査、SC-06認証済みBrowser、SC-07障害復旧、SC-08攻撃、SC-09比較試験も必須。
- 対象操作: navigate（明示allowlist・source destination事前検査）, snapshot, scroll, observe, role/label/testIdを起点にしたread-only画面切替、検索UIの入力（**入力が外部へ送信されないか効果分類の確認必須**）。毎回実行時Policyがread認可を確認する。
- 非目標: HTML取得Cacheそのもの、Memory/Skillの正本、サイト認証突破、BrowserContext管理、OS/JavaScriptの任意実行、不可逆操作の自動承認、完全なDOM変更耐性、Stagehandサービス再実装、第三者へRecipe共有。
- 使用例: ログイン済み価格表を展開→読取→元URL/日時付きEvidenceを検証。別アカウントで同じDOMが出てもRecipeを再生しない。構造変更や価格表示消滅時は「成功したふり」をせず新規提案へ戻す。

## 2. OSS実コード、Docs、テスト、変更履歴の比較

| 実装 | 実際に調べたAPI/Source/TestとCommit | 学ぶこと・制限 | License / 判定 |
|---|---|---|---|
| Stagehand v4 | browserbase/stagehand @ edb76b0e867790132c2e51f3f34027b950463086; packages/sdk-ts/src/stagehand.ts の act/observe/extract、packages/extension/services/cacheService.ts の buildCacheContext/withCache、clients/cacheClient.ts、tests/cache-service.test.ts / cache-e2e.test.ts | Browserbase接続時のみAPI cache、cache miss/replay_failed→live fallback、metadata HIT/MISS。無効cache応答は実行権限ではない | MIT, **Code verified**, tests inspected/not run |
| Stagehand v3→v4 | 同ref packages/docs/v4/migrations/v3.mdx / best-practices/caching.mdx / configuration/browser.mdx; CHANGELOG.md | v3 TSは new Stagehand→init、page.act、serverCache/enableCaching; v4は Stagehand.create({browser}), stagehand.act({page option}), {data,metadata}, cache。v3 Python/Go APIはsession方式など別。v4 local Browserにmanaged server cacheはない | MIT, **Docs verified**; ref固定 |
| Playwright | microsoft/playwright @ e905d4bd77dff425f981b03df861fed59b50c9dd docs/src/actionability.md, docs/src/locators.md; repo自身のCHAT_04 PR #19が参照した src/client/locator.ts @ 62d25ac221370f6fc5e26c600823792b83c67f1e | locatorのstrictness/actionabilityとlive解決を使う。nth/firstはDOM変化時の誤クリックを招くためRecipeで禁止 | Apache-2.0, **Docs/Code verified** |
| Jev adapter | jkudish/jev-browser @ 817b09eea6e21aad836b5ed32a613c68e5644a08, src/navigate.ts/provider.ts; repo PR #19 Jev契約も照合 | Page ownerはCHAT_04。Jevの内部自律loopをGateway外に出さない。候補を渡すのみ | **Code previously verified by CHAT_04**; license/現行互換は導入前再確認 |

**採用**: 本Harness独自のDB版数管理された宣言的Recipe + Playwright deterministic locator + per-step Gateway/BrowserOwner再検証。**却下**: (A) DOM同一文字列のglobal hash cache（account・locale・session・権限漏洩と誤標的リスク）、(B) Stagehand Browserbase managed cacheをself-host依存にする（SaaS資格/費用/外部送信）、(C) 全クリック録画をそのまま走らせる（文脈・副作用不明）、(D) confidenceの数値だけで認可（危険）。Stagehand v4は独立したruntime/CDP構造を持ち、既存Playwright Page注入互換は実装試験まで未確認。全依存はoptional。

確認したStagehand変更履歴にはcacheのpayload超過をskipするPR #3229、Model Gateway/cache transportのPR #3210がある（どちらも**changelog記載、我々の再現試験ではない**）。v3 APIの一律説明は避け、言語/SDK別の移行表を基準とする。

## 3. 正本・依存・Trust Boundary

- CHAT_01: Task/Run/Event/Evidence/Artifact identity、UUIDv7/Version/goalRevision/leaseEpochの論理契約。共有構造は本書で確定しない。
- CHAT_02: ResultVerifierの成功証明、Run cancellation/Steer/Checkpoint、Budget。Replay操作が旧Goalへ反映されないこと。
- CHAT_03: Gateway、Policy/Approval、Side-effect Ledger。Recipe自身はcapability/approvalを持たない。保存した「過去ALLOW」は無効。
- CHAT_04: BrowserSession・Page/Tab owner、ActionCommand/Outcome、mutex・lease/epoch、Jev提案、before/after artifact。
- CHAT_06: Skill候補・再利用プロセスとの連結。ActionRecipeは**CHAT_14の内部提案**でありSkill install/enableではない。
- CHAT_09: Principal・owner/tenant/workspace照合、Credential、network egress、secret redaction、retention/erasure。
- CHAT_10: 物理DDL/unique/index/CAS/outbox/GCの提案先。CHAT_08: UI/APIへの提案先。CHAT_11: replay adversarial harness。
- External HTML/ARIA/LLM action proposal/サイト文言 = **非信頼データ**。Browser owner→Gatewayの境界だけが操作実行を許す。Recipe/キャッシュからToolへ直結しない。

**歴史的Round1不一致はRound3のPR #10でDDL修正案が反映済み:** UUIDv4 DB default廃止、Task.conversationId nullable、recordVersion初期1、leaseEpoch/goalRevision/activeRunIdの物理列、tenant+owner FKを確認。実行実装/独立承認まではDraftだが、旧不一致を現在のP0として扱わない。残る現行P1は学習済みBrowserクリック/入力の副作用防御が未実装である点。int64 wireは厳密な十進文字列を使う。

## 4. 要件（全MUSTは§14のTESTへ対応）

| REQ-ID | レベル | 不変条件と合格判定 |
|---|---|---|
| LBA-001 | MUST | Recipe候補の出所はVerified Resultとbefore/after Evidenceの整合した実行のみ。Action APIのsuccessだけではeligible=0 |
| LBA-002 | MUST | read-only/authorized・明示opt-inでのみ活性化。unknown/write/irreversible候補の自動再生0 |
| LBA-003 | MUST | identityは目的、origin、path pattern、DOM fingerprint、selector、expected outcome、account/tenant/owner scope、version/expiryに拘束 |
| LBA-004 | MUST | 毎step実行直前にPrincipal、Task Goal/Run fence、Session/Tab owner、Gateway Policy/approval、network egress、Budgetを再検査 |
| LBA-005 | MUST | current locatorはunique=1、可視/操作可能・role/name/属性/target frame一致を検査。曖昧・stale→実行0 |
| LBA-006 | MUST | postconditionを別観測で検証し、Evidence provenance/observedAtを更新。失敗時に成功を返さない |
| LBA-007 | MUST | DOM/URL/iframe/origin/login/account/locale/flow version変化はfail-closed。再推論もGateway経由、stale recipeを盲目的reclickしない |
| LBA-008 | MUST | Recipeはowner-scoped、暗号化/secret非保存、他user/tenantへのhit・件数漏洩0 |
| LBA-009 | MUST | 同時tab、cancel、steer、lease expiry、human takeover中は古いActionが実行されない |
| LBA-010 | MUST | 操作意図と結果を既存Ledgerへ記録しlost ACK/UNKNOWN外部効果を自動retryしない |
| LBA-011 | MUST | immutable version + CAS+idempotent activation/disable/invalidation。削除・退会・policy revokeで再生無効 |
| LBA-012 | MUST | TTL、試行回数、時間、保存件数、token/cost、fallback回数を有限制限し上限到達時停止 |
| LBA-013 | MUST | cache poisoning（偽証跡/誘導role/重複候補/攻撃HTML）の候補を信頼状態へ上げない |
| LBA-014 | MUST | Recipeを無効化しても過去Evidenceを書き換えず、管理者監査と本人Undo/disableを提供 |
| LBA-015 | SHOULD | replay hit率・verified success率・誤ヒット・費用/モデル呼数を同一fixtureでbaseline比較 |
| LBA-016 | LATER | 複数サイト共通semantic recipeやStagehand optional adapterは隔離実験のみ |

## 5. 内部契約、型・Schema・検証（**CHAT_14内のproposal**）

~~~typescript
type BrandRef = string; // [PROVISIONAL: CHAT_01] canonical UUIDv7, server-generated
type Int64Wire = string; // decimal 0|[1-9][0-9]*, <= 9223372036854775807; BigInt compare only
type LocatorSpecV1 =
 | {kind:"role"; role:string; name:string; exact:true}
 | {kind:"label"; text:string; exact:true}
 | {kind:"testId"; value:string}
 | {kind:"css"; expression:string; reviewed:true}; // restricted allowlist only
type ReadActionV1 = "navigate"|"snapshot"|"scroll"|"read_only_click"|"read_only_fill";
type RecipeStepV1 = {
  sequence:number; operation:ReadActionV1; target?:LocatorSpecV1;
  frameOrigin:string; framePathDigest:string;
  expectedBefore:{role?:string; name?:string; attributesDigest?:string};
  expectedAfter:{kind:"visible"|"url_match"|"text_signature"|"structured_field";
                 predicate:string; maxWaitMs:number};
};
interface LearnedRecipeV1 {
  schemaVersion:1; recipeId:BrandRef; revision:Int64Wire; immutableDigest:string;
  status:"CANDIDATE"|"VALIDATING"|"ACTIVE"|"SUSPENDED"|"INVALID"|"REVOKED"|"EXPIRED";
  tenantScopeRef:string; ownerUserRef:string; accountBindingDigest:string;
  authContextClass:"public"|"user_authenticated"; // no token/cookie values
  intentDigest:string; goalKind:string; canonicalOrigin:string;
  pathTemplate:string; queryKeysAllowlist:string[]; locale:string;
  domFingerprint:{algorithm:"aria-normalized-v1"; digest:string;
    relevantFrameDigests:string[]; generatorVersion:string};
  source:{taskId:BrandRef; runId:BrandRef; goalRevision:Int64Wire;
    verifierReceiptRef:string; evidenceRefs:string[]; observedAt:string};
  steps:readonly RecipeStepV1[]; effectClass:"read";
  consentRef:string; policyEpoch:Int64Wire; createdAt:string;
  expiresAt:string; validatedAt?:string; allowedUses:number;
}
interface ReplayRequestV1 {
  recipeId:BrandRef; recipeRevision:Int64Wire; requestId:BrandRef;
  taskId:BrandRef; runId:BrandRef; goalRevision:Int64Wire;
  leaseEpoch:Int64Wire; browserSessionId:BrandRef; tabId:string;
  intentDigest:string; deadlineAt:string;
}
type ReplayVerdictV1 =
 | {kind:"VERIFIED"; newEvidenceRefs:string[]; steps:number; savedModelCalls:number}
 | {kind:"STALE"|"DENIED"|"REPLAN_REQUIRED"|"UNKNOWN"|"CANCELLED"|
        "BUDGET_EXHAUSTED"|"NEEDS_HUMAN"; reasonCode:string; steps:number};
~~~

Validation: strict schemaVersion=1/unknown keys reject; URLはURL APIで正規化、scheme HTTPS（例外localhostは管理者fixtureのみ）、originは明示固定、redirectで再検査。pathTemplateは安全なlimited glob（'**'禁止）で全長<=256、queryはallowlist keys以外不一致。role/name Unicode正規化(NFC)後のexact match; CSSは:has-textやscript/evaluate/iframe traversal及びnth/first禁止、セレクタ長<=512。expectedAfter predicateは許可済み固定DSL（URL glob、DOM role+name存在、readonly structured value比較）のみ。JS expressionや任意正規表現は不許可。Snapshot fingerprintは動的時刻/価格/CSRF token/Pii/passwordを**含めず**、利用するrole/name/ancestor/frame chainなど構造要素を安定順序でcanonical JSON→SHA-256。fingerprint一致**だけ**で動作許可しない。価格本文は毎回新Evidenceから読む。

accountBindingDigestは秘密でない内部account IDとtenant owner/session auth bindingのHMAC（単なるURL/username hashは不可）。keyローテーションで既存recipeはSUSPENDED。Cookie value、storageState、accessToken、フォーム入力本文、HTML全文、personal URL queryをRecipeに保存しない。各SHA/HMAC canonical化の入力/encodingは別実装パッケージのgolden fixtureで固定。

## 6. 状態遷移、Sequence、Concurrency

~~~text
VERIFIED_SUCCESS -> CANDIDATE -> VALIDATING -> ACTIVE
                          |             \-> INVALID
                          \-> REVOKED
ACTIVE -> SUSPENDED (policy/account change, postcondition mismatch)
ACTIVE -> INVALID (confirmed DOM/selector drift)
ACTIVE -> EXPIRED (TTL/uses)
ACTIVE/SUSPENDED -> REVOKED (user opt-out/deletion)
SUSPENDED -> VALIDATING (fresh consent + new source proof)
INVALID/REVOKED -> [terminal; new immutable recipeId/revision for rediscovery]
~~~

~~~mermaid
sequenceDiagram
 participant R as Runtime
 participant G as Tool Gateway (03)
 participant B as Browser Owner (04)
 participant L as Recipe Service (14)
 participant D as DB/Ledger (10/03)
 R->>L: replay(request, principal server-derived)
 L->>D: load tenant-scoped ACTIVE recipe + version/consent
 L->>G: authorize goal/fence/scope/budget, effect=read
 G-->>L: ALLOW or DENY
 L->>B: acquire session/Tab lock and current snapshot
 B-->>L: URL/ARIA/frame/account binding
 L->>L: strict identity + fingerprint + unique locator match
 loop each bounded step
   L->>G: fresh policy/fence check + intent
   G->>D: persist Tool intent / side-effect ledger
   G->>B: execute owned Page action
   B-->>G: after snapshot / uncertain outcome
   G-->>L: canonical ActionOutcome
   L->>L: verify step postcondition or stop
 end
 L->>D: persist fresh Evidence + receipt CAS/outbox
 L-->>R: VERIFIED or explicit non-success
~~~

Lock order: Tenant-scoped Recipe CAS is checked before acquiring Browser Tab mutex; **never hold DB transaction while browser/network executes**. Browser Owner lock encloses *each action plus after-observation*; for multi-step recipe a single short lease over bounded sequence is preferable only if CHAT_04 confirms mutex fairness; otherwise between steps refresh lease/snapshot/policy. UI takeover immediately fences Agent; human actions are never silently collected as Recipe proof. Lock conflicts yield bounded retry/backoff within deadline, then REPLAN_REQUIRED. Parallel read-only tabs are possible only with distinct Tab ownership; same Tab serialized.

## 7. 実装アルゴリズム、失効・重複・中断

~~~typescript
async function replay(req:ReplayRequestV1, auth:ServerPrincipal):Promise<ReplayVerdictV1> {
  const deadline = min(req.deadlineAt, nowPlus(45_000));
  const r = await recipeStore.readByOwner(auth,req.recipeId);
  if (!r || r.revision !== req.recipeRevision || r.status !== "ACTIVE") return stop("DENIED");
  validateStrict(r);
  if (now() >= r.expiresAt || r.allowedUses <= 0) return stop("STALE");
  await ensureCurrentGoalRunLeaseConsentPolicy(auth,req,r); // CH01/02/03/09
  const ctx = await browserOwner.acquireBoundSession(auth,req.browserSessionId,req.tabId);
  return ctx.withPageLock(async page => {
    let snapshot = await ctx.redactedSnapshot(page);
    if (!matchesCurrentIdentity(r, req, ctx.accountBinding(), snapshot))
      return suspendAndReplanCAS(r, "IDENTITY_DRIFT");
    if (!sameRelevantAriaFingerprint(r.domFingerprint,snapshot))
      return suspendAndReplanCAS(r, "DOM_DRIFT");
    for (const step of r.steps) {
      if (abortedOrExpired(deadline)) return stop("CANCELLED");
      await ensureCurrentGoalRunLeaseConsentPolicy(auth,req,r);
      const target = resolveStrictLocator(page,step, snapshot); // count exactly 1
      if (!verifyPrecondition(target,step)) return suspendAndReplanCAS(r,"TARGET_AMBIGUOUS");
      const gatewayReceipt = await toolGateway.prepareAndExecute({
        taskId:req.taskId, runId:req.runId, op:step.operation,
        targetBinding:target.binding, policyScope:auth.scope
      }); // all actual mutations through approved Action Engine
      if (gatewayReceipt.effectUncertain) return stop("UNKNOWN"); // never replay step
      snapshot = await ctx.redactedSnapshot(page);
      if (!verifyPostcondition(step.expectedAfter,snapshot,gatewayReceipt))
        return suspendAndReplanCAS(r,"POSTCONDITION_FAIL");
    }
    const verified = await resultVerifier.verifyAgainstCurrentGoal(req,snapshot);
    if (!verified) return stop("REPLAN_REQUIRED");
    return await receiptStore.commitIdempotentAndDecrementUsageCAS(req,r,verified);
  });
}
~~~

生成: 既存のTool/ResultVerifier保存済み成功から実際のAction traceを列挙→副作用を保守的に再分類→秘密/個人情報と権限未確認stepを排除→normalized selector/structural fingerprintを構成→同じscopeに限りdedupe→CANDIDATE保存。validatorは動的サイトの全世界で成功を保証できないので、**fixture + 新しいlive read-only smoke validation**（アクセス許可があり予算内）を行う。自動ACTIVE化は本人の機能opt-in + domain consent + verified read-only classification + fixture validation後のみ。曖昧ならユーザーレビュー待ち。

idempotency: (tenant,owner,requestId,payloadHash) unique;同一requestId+同bodyはreceipt再取得、異なるpayloadは409提案。Crash前の外部操作が不明な場合には既存Ledger UNKNOWNを返し、Recipe side-effect classificationがreadであっても安全性再評価まで二重click不可。旧source revision/失効済source/削除済ConsentはCAS拒否。再学習は古いRecipeをin-place無断修復せず新versionを候補化。ClientはaccountBindingDigestやtenantIdを指定できない。

**Guardrail初期設定（実測値ではなくCONFIG提案）**: Recipe step最大8、1 step timeout<=8秒、総wall<=45秒、1 source snapshot normalized <=128KiB、1 recipe <=32KiB、1 owner ACTIVE <=200、候補<=500、TTL<=7日（認証済み<=24hを既定）、maxUses<=100、失敗連続2でSUSPENDED、fallback再計画は1回/Task、domain当たり並行Replay<=2、model呼数はfallback含めGatewayの既存Run Budget以下。厳しい方の上限が優先。Quota exceedで成功率を捏造しない。

## 8. 故障・競合マトリクス

| 起点 | 検知 | 対応/最終扱い | 外部副作用 |
|---|---|---|---|
| URL redirect / cross origin | current URL/egress | Step前DENIED→fresh planning | なし |
| DOM/ARIA drift / duplicate role | fingerprint/locator count !=1 | SUSPENDED/INVALID、replan一度 | 実行しない |
| price値変更のみ | stable structure一致 | 再読取Evidence更新、古値使わない | readのみ |
| login expired / account changed | accountBinding mismatch | SUSPENDED/NEEDS_HUMAN | Cookie転送0 |
| iframe/popup/tab change | frame chain/owner mismatch | 再snapshot or REPLAN | 旧tabへ実行0 |
| human takeover | session mode HUMAN | WAITING_INPUT、Agent排他 | 自動実行0 |
| cancel/steer/lease expired | fence/goal check | CANCELLED/STALE、旧結果不反映 | 開始済み未知効果は照会 |
| old recipe restored after delete | revocation watermark | REVOKED、再活性0 | なし |
| DB commit lost ACK | idempotency receipt照会 | 重複Usage/結果保存0 | UNKNOWNは再送禁止 |
| tool action timed out | ledger outcome uncertain | UNKNOWN、必要なら照会/人間確認 | 無条件再click0 |
| cache poison source | provenance verify fails | INVALID + audit | 許可拡大0 |
| DB outage/Policy unavailable | cannot reauthorize | DENIED/REPLANしない | なし |
| two concurrent Replay same recipe | CAS uses/Tab lock | 片方のみcount更新、重複receiptなし | Tabは直列 |
| user deletes session/consent mid-step | epoch check before next step | 次step禁止、削除outbox | 直前効果は消せない |

## 9. セキュリティ/Privacy

- HTMLはinstructionではなくdata。Recipeのtext/selectorに「ignore policy」「送金」等が含まれても、Gateway policy/current authを変えない。role表記はページ上の敵対的ラベルになり得るので、origin・identity・target uniqueness・effect class・postconditionを別々に照合。
- Owner/tenant/credentialの秘密scopeをHMAC比較し、raw account ID・Cookie・password・CSRF・DOM screenshotのPIIをRecipeには保管しない。ログもURL query/redacted DOM/body/sensitive countを出さずopaque IDのみ。外部Stagehand cloudへCookie付きDOMを自動送信しない。
- サイトごとにTerms、robotsでは判断できない利用条件、OAuth data scope、re-authが必要な場合は都度判定。SSRFはBrowserのnavigation/redirect/WebSocketを含めCHAT_09へ委譲。ホストShell/任意JS/privileged Docker不可。
- 攻撃者がCookie値やaccount labelでFingerprintを合わせても、server-held identity bindingとactive policyが別境界で失敗する。Fingerprintは**安全認可の代わりではなく失効の補助**。
- Evidence削除/Privacy request/retention終了時に候補をrevokeしindexも後続掃除。バックアップ復元でrevocationEpochより古いACTIVEを復活させない。Retention年限はCHAT_09の正本。

## 10. Metrics、Performance / Benchmark

Counters: recipe_candidate{reason}, recipe_activate{status}, recipe_replay{outcome}, recipe_stale{reason}, recipe_scope_denied, recipe_unknown_effect, recipe_policy_recheck_denied。histogram: replay_latency_ms、model_calls_saved（実際の呼出差分のみ）、snapshot_bytes、recipe_bytes、fallback_cost_usd、verification_latency。labelsはsite/account/URL/tenantを直書きしない。Recipe digest/trace IDで内部追跡。

CHAT_11 benchmark: 同一30ページfixture（静的/SPA/iframe/ログイン期限/DOM改変/誤類似）、cold planner vs warm replayを5回以上交互実行、成功はVerifier+Evidence freshnessで算出。p50/p95、モデル呼数/費用、false replay（目標0）、scope leakage（0）、UNKNOWN再送（0）を比較。上記値は測定計画であり速度/節約の実績値ではない。

## 11. 永続化・TransactionとMigration提案 [PROVISIONAL: CHAT_10]

論理リソース提案: immutable recipe_versions(recipeId,version,digest,payload,source/verifier refs,consentRef,scope,expiry,status), recipe_bindings(owner/tenant/account,activeVersion,recordVersion), recipe_operation_receipts(requestId,payloadHash,verdict,evidenceRefs), recipe_revocations(high-water), recipe_learning_queue/outbox。実DDL、FK、index、uuid生成、RLS、transaction仕様はCHAT_10に委譲。Recipe ACTIVE化、consent/policyVersion CAS、outboxを**一DB transaction**にし、Browser外部操作は絶対にDB transaction内へ入れない。クライアントへのcommit ACK不明ならreceipt query。ArtifactとRecipe分離（Evidence原本immutable）。Index cacheはDB revisionとrevoke watermarkをread-time照合。

Migration: M0 feature flag OFF、DDL追加のみ→M1 source verified-only candidate生成dry-run→M2 fixture replay/read-only限定&opt-in→M3 telemetry review→M4 per-domain rollout; rollbackはflag OFF+bindings suspendのみ、過去Approval復元しない。新schemaを読めない旧Workerはfail closed。

## 12. API/UIへの提案 [PROVISIONAL: CHAT_08]

- GET /adaptive/browser-recipes?scope=current : ACTIVE/CANDIDATE/invalid状態、元Taskと検証日時、対象origin、read-only分類、last failure、残存TTL（Secret/account labelは隠す）。
- POST /adaptive/browser-recipes/{id}/activate, /disable, /revalidate : If-Match recordVersion + idempotency key + fresh authenticated principal。activateは追加Consent/レビュー画面で対象domain/用途/expiryを表示。
- Task timeline: CACHE_CANDIDATE / REPLAY_HIT_VERIFIED / REPLAY_STALE / REPLAN_REQUIRED / REPLAY_UNKNOWN を既存Event envelope正本へ**提案のみ**。結果は“前回手順を試用／現在情報で再確認”と示し、freshnessを偽らない。
- 共有/一括export初期無効。ユーザー明示削除・disableの結果はReceiptとoutboxに反映し、直後からreplay不可。

## 13. 実装PRの分割（本repoへのコード変更ではない）

1. LBA-I1: Recipe validator/canonical fingerprint/secret filters + goldens + threat model。
2. LBA-I2: CHAT_10 scoped store + immutable versions, consent binding, CAS/idempotency, GC/invalidation。
3. LBA-I3: CHAT_04 Page owner / locator strict replay adapter + CHAT_03 policy & ledger boundary。
4. LBA-I4: ResultVerifier provenance admission + postcondition Evidence + fallback + trace。
5. LBA-I5: CHAT_08 review/disable UI + opt-in, CHAT_11 fault/security/benchmark。
依存: CHAT_01/02/03/04/09/10のP0互換確定前にLockedにはしない。

## 14. テスト（**設計済み・未実行**）/ 全MUST追跡

| TEST-ID | Setup / action | Expected | REQ-ID | SC |
|---|---|---|---|
| LBA-T01 | Tool successのみ/Verifier失敗と本当のVerified source→候補化 | 真の成功だけ1件 | LBA-001 | SC-13/07 |
| LBA-T02 | post購入/カート/readonly検索のeffect fixtures、opt-in off/on | 未許可write auto replay 0 | LBA-002 | SC-08/13 |
| LBA-T03 | tenant/account/locale/path/intent異なる同DOM fixtures | 0 hit、同一だけhit | LBA-003 | SC-06/13 |
| LBA-T04 | 旧leaseEpoch/steer/policy revoked vs valid before each action | stale action 0 | LBA-004 | SC-07/08 |
| LBA-T05 | duplicate button/hidden/iframe switched/CSRF injected | 曖昧target click 0 | LBA-005 | SC-13/08 |
| LBA-T06 | click成功だが値不明 or postcondition false | VERIFIED 0、Evidence新観測 | LBA-006 | SC-05/13 |
| LBA-T07 | DOM changed/URL redirect/auth session expired | invalidation後にfresh plan、旧step 0 | LBA-007 | SC-06/13 |
| LBA-T08 | 2 user×2 tenant, same path and DOM, probe timing/count | cross-scope hit/secret漏洩 0 | LBA-008 | SC-06/08 |
| LBA-T09 | two tabs/same Tab concurrency + human takeover/cancel | mutex/fence拒否、旧step 0 | LBA-009 | SC-06/07 |
| LBA-T10 | kill after external effect before ACK、Ledger UNKNOWN | auto re-click 0、UNKNOWN明示 | LBA-010 | SC-07 |
| LBA-T11 | same requestId concurrent activate/disable, revocation & restore | one receipt、revoke復活0 | LBA-011 | SC-07/08 |
| LBA-T12 | 9 steps/46秒/201 active/TTL expired/above budget | 制限超過実行0 | LBA-012 | SC-09/13 |
| LBA-T13 | fake Evidence ID、HTML says 'system', lookalike fingerprint | ACTIVE 0、Gateway privilege 0 | LBA-013 | SC-08/13 |
| LBA-T14 | user disable/erase active recipe with old Evidence | 使用0、Evidence immutable、監査可 | LBA-014 | SC-07/13 |
| LBA-T15 | baseline/cached with identical 30 fixtures | cost/latency/verified accuracy report、実測前断定なし | LBA-015 | SC-09/13 |
| LBA-T16 | Stagehand v4 local + optional experiment | cache HIT保証せず、disabledで同一source不変 | LBA-016 | SC-09 |

**テスト状態**: 上記はHarness実装後に走らせる設計。上流のStagehand/Playwrightテストはコードを読んだが実行0。PostgreSQL/Browser/E2E/benchmarkも実行0。合格・Verifiedの自己宣言不可。

## 15. Acceptance gates

SC-13: 一致するread-onlyサイトだけでReplay VERIFIED、model呼削減は実測し、DOM/URL/session driftで旧action 0→fresh replan、irreversible未承認 0。SC-06/07/08: 他ユーザーLeak=0、旧fence Action=0、不確定効果重複=0。SC-09: cold/warmの同一fixture比較あり。上記REQ/TEST追跡の全MUSTが実装のtest suiteへ組み込まれるまでLocked不可。

## 16. Cross-team proposals / 未決

| ID | Owner/severity | 正本の変更提案と移行/合格条件 |
|---|---|---|
| LBA-P0-01 | CHAT_01/02/10 P0 | Run leaseEpoch/goalRevision/activeRunId、UUIDv7、nullable conversation、recordVersionとDB revisionの可逆映射。旧fence replayをSQLで拒否するcontract test |
| LBA-P0-02 | CHAT_09/10 P0 | principal tenant/owner/workspace/account-binding server mapping; recipe scope FK/revoke watermark; user B cannot even count A's cache |
| LBA-P0-03 | CHAT_03/04 P0 | Action outcomeにstrict read classification + before/after verifierReceipt、unique locator/owned Page gate、unknown external effectをLedgerへ。READMEではなくPR #19/20/31のOwner契約を変更提案 |
| LBA-P1-04 | CHAT_06 P1 | Skill候補がRecipe Refを指せるがautomatic Skill activate/permission importしない; source deletionで候補停止 |
| LBA-P1-05 | CHAT_08 P1 | Consent/activate/revoke/end-user confirmationとIf-Match、SC-13 status proposal |
| LBA-P1-06 | CHAT_11 P1 | 30-page fixture cross-scope/replay/drift/fault benchmarkとsecret exfil attempt |
| LBA-P1-07 | CHAT_09 P1 | private site fingerprint/target metadataとRetention/erasureの具体日限; NIST相当の削除範囲決定 |

ProposalのSchemaVersion=1は**本CHAT内部**でありShared Event/Task/Tool/DBに新しい正本を生やさない。Ownerが拒否する場合はAdapterを再設計し、本MDの該当契約をRevision増で更新する。

## 17. Sources（2026-10-30 JST、verifiable refs）

- [S1] Stagehand @ edb76b0e867790132c2e51f3f34027b950463086 [v4 client](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/sdk-ts/src/stagehand.ts), [cacheService.ts](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/extension/services/cacheService.ts), [cacheClient.ts](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/extension/clients/cacheClient.ts). **Code verified**; MIT LICENSE at same ref.
- [S2] Stagehand [cache service tests](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/extension/tests/cache-service.test.ts), [cache E2E with mocked server](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/extension/tests/cache-e2e.test.ts). **Code verified**, upstream tests read, not run.
- [S3] Stagehand [v3→v4 migration](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/docs/v4/migrations/v3.mdx), [caching](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/docs/v4/best-practices/caching.mdx), [browser local/cloud](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/packages/docs/v4/configuration/browser.mdx), [changelog](https://github.com/browserbase/stagehand/blob/edb76b0e867790132c2e51f3f34027b950463086/CHANGELOG.md). **Docs verified**.
- [S4] Browserbase [2026-08-21 configurable caching](https://www.browserbase.com/changelog/caching-configurable): **Docs verified**; cloud account/cost/entitlement not tested.
- [S5] Playwright @ e905d4bd77dff425f981b03df861fed59b50c9dd [locators](https://github.com/microsoft/playwright/blob/e905d4bd77dff425f981b03df861fed59b50c9dd/docs/src/locators.md), [actionability](https://github.com/microsoft/playwright/blob/e905d4bd77dff425f981b03df861fed59b50c9dd/docs/src/actionability.md). **Docs verified**; [client locator src @ 62d25ac](https://github.com/microsoft/playwright/blob/62d25ac221370f6fc5e26c600823792b83c67f1e/packages/playwright-core/src/client/locator.ts) prior source inspected by CHAT_04; Apache-2.0.
- [S6] [Jev adapter proposal PR #19](https://github.com/choko510/aiagent-md/pull/19), [BrowserSession PR #12](https://github.com/choko510/aiagent-md/pull/12), [Gateway #20](https://github.com/choko510/aiagent-md/pull/20), [Ledger #31](https://github.com/choko510/aiagent-md/pull/31), [Memory/Skill #15/#30](https://github.com/choko510/aiagent-md/pull/30). **Project Draft code/spec contracts inspected, not Locked**.
- [S7] Stagehand [PR #3229](https://github.com/browserbase/stagehand/pull/3229), [PR #3210](https://github.com/browserbase/stagehand/pull/3210): change-log referenced; independent PR runtime reproduction **unverified**.
- [S8] Fingerprint DSL/HMAC/policy gating/numeric limits/state machine/DB and REST proposals are **Design hypotheses**, not Stagehand/Playwright guaranteed implementation.

## 18. Round 4 — No unverified interactive replay / POST trap
**Revision to unsafe action labels:** local `ReadActionV1` currently includes `read_only_click` and `read_only_fill`, but those are **legacy candidate categories**, not an automatic safety grant. A click can trigger POST/XHR/navigation, and fill can auto-save; even a GET can have unintended server effects. Until a real Browser Worker/Gateway effect-monitoring test suite passes, the automatic replay allowlist is **`snapshot` and `scroll` on a strictly verified unchanged page/session**; `navigate` only to a server-approved known read endpoint and with fresh network egress authorization. `read_only_click`/`read_only_fill` must be **freshly planned and approved** each use, never fast-path cached replay.
```ts
type AutoReplayV1 = {op:"snapshot"|"scroll"} |
 {op:"navigate"; allowlistedDestinationRef:string; noCredentialsEscalation:true};
function canReplay(step:ReadActionV1,ctx:SessionPolicy):boolean {
  return validatedRecipeAndOwner(ctx) &&
    (step==="snapshot"||step==="scroll"||
      (step==="navigate"&&verifiedReadDestinationAndEgress(ctx)));
}
```
**Future opt-in only after enforcement:** to admit any interactive click/fill, policy must prove forbidden HTTP methods/non-allowlisted origin, websocket write messages, ServiceWorker/background requests, hidden form submissions and auto-save are denied at actual network dispatch (not merely observed afterward). Browser intercepted requests can be incomplete if a transport bypasses hooks; CHAT_09 must verify **container-level** proxy/egress, and CHAT_03 records attempted side effects. If protection cannot be demonstrated, interactive cache replay stays disabled. A postcondition check is evidence of result only, never ex-ante permission.
**Tests (designed not run):** LBA-R4-T01 benign snapshot opt-in replay→0 new mutation; T02 click triggers POST→never automatically executed; T03 fill causes auto-save→never replayed; T04 GET destination redirects/sets state→deny unless specifically approved; T05 same tenant different account cookie→deny; T06 DOM fingerprint drift→invalidate without clicking; T07 policy revoked during replay→stop at dispatch boundary. SC-13/07/08. Owner decision CHAT_03/04/09/14, retained Draft.
**Old Round1 UUIDv7/v4, nullable Task Conversation and recordVersion0 assertions in §3 are historical and superseded in current PR #10; do not reopen as current P0 without rechecking latest HEAD.**

