# DEV-07 — Independent Triple Review & P0/P1 Repair Gate
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/03/08/10/11。試験未実施。

## Review roles / independent evidence
A6(機能・コード品質・回帰)、A7(セキュリティ・secret・依存・権限)、A8(UI/アクセシビリティ/Browser E2E)が**同一commit SHA/同一designRevision/同一fixture+build環境**をread-onlyで独立レビュー。初回レビュー時は他Reviewer/Builderの自己評価を混ぜない。必要なら再現コードやブラウザデバッグを実行し、Source line、再現条件、期待/観測、Trace/テスト結果を必須とする。1人でも未完なら「3AIが合格」と表示しない。

## Severity / status
- P0: user data loss、認証逸脱、secret漏洩、host/VM境界破壊、アプリ主要機能全滅など。
- P1: 主要な利用経路の破綻、重大な回帰、再現性ある深刻な不具合/セキュリティ問題。
- P2/P3: 限定条件の不具合/改善候補。P2もrelease policyで追加ブロック可。
Issue state: `OPEN`, `FIX_PROPOSED`, `FIXED_VERIFIED`, `DISMISSED_WITH_EVIDENCE`, `UNVERIFIED`。誤検知のdismissは反証を証拠として保存。モデルのseverityラベルや「解消済み」の文章だけで閉じない。

```ts
type ReviewFindingV1 = { findingId:string; reviewerRole:"functional"|"security"|"ui_e2e";
 cycleId:string; commitSha:string; designRevision:string; severity:"P0"|"P1"|"P2"|"P3";
 fingerprint:string; title:string; sourceRef?:string; evidenceRefs:string[];
 reproductionRef:string; state:"OPEN"|"FIX_PROPOSED"|"FIXED_VERIFIED"|"DISMISSED_WITH_EVIDENCE"|"UNVERIFIED";
 resolutionCommitSha?:string; verifyReceiptRef?:string };
type ReviewRoundV1 = { cycleId:string; round:number; codeSha:string;
 requiredReviewerReceipts:string[]; testManifestDigest:string;
 verdict:"REPAIR"|"APPROVED"|"BLOCKED"|"PARTIAL" };
```
shared IDs/events/stateはCHAT_01のcanonical契約レビューを要する。

## Repair Loop
`IMPLEMENT → TEST → COMMIT(SHA_n) → 3 REVIEWERS → MERGE FINDINGS → if P0/P1: REPAIR → TEST → COMMIT(SHA_n+1) → 3 NEW REVIEWS ...`。
- 3 reviewer receiptsとtest receiptsが最新SHAにbind。異SHA/old GoalRevision/old DemoVersionを使ったpassは無効。
- 重複findingはfingerprint+repro+根拠でdedupe。異なる根本原因を安易に同一扱いしない。
- 新SHAでaffected regression, whole required suite, Browser Debuggerを再実行し、見た目と機能の新規P0/P1も確認。
- 進捗なし/同じバグ再発/修正反転/レビュアー対立は [Expert Advisor](expert-advisor.md) を費用上限付きで呼ぶ。一定回数(Operator/ユーザー設定の有限閾値)で収束しなければPARTIAL/BLOCKEDと残件を公開し、無駄な反復しない。
- PASS条件: latest SHA + approved design/demo、必要テスト**実施して合格**、独立3AIのvalid receipts、未解決P0/P1なし、security isolation OK、外部Effect状態確定。未実行/unknown/missing evidenceはPASS不可。
- 各修正を一貫した粒度でcommitし、PRにはSHAごとの修正・再現・テスト・未実施を記述。ユーザーが許可したcommit/PR scope外へ自動push/mergeしない。
- Endless Campaignでは同一CycleがBlockedでも他の独立Cycleを継続可。ただし対象ReleaseがP0/P1残存のまま「完了数」を増やすことは禁止。

## Tests
DEV-REV-T01: independent3件同SHAでPASS、2件なら拒否。
DEV-REV-T02: P0を修正してP1新規→再Round必須。
DEV-REV-T03: oldSHAレビュー、missing E2E、partialログ、未検証dismissでPASS不可。
DEV-REV-T04: reviewer間矛盾→追加証拠/Expert、自己評価で決めない。
DEV-REV-T05: 反復失敗→bounded Escalation、無制限ループなし。
DEV-REV-T06: cancel/revoke後に新PR・external tool起動0。

## 2026-11-08 P1-10 — Feature Review is not Integration Review

Individual branch verification of SHA_F and integration verification of SHA_I are distinct signed/evidence-bound receipts. A protected Git integration must identify an immutable expected parent SHA and resulting integration SHA. The merge/fast-forward operation is CAS-protected by project. A verifier MUST NOT reuse the original A6/A7/A8 first-pass for SHA_F as if they reviewed SHA_I. Any changed UI/component/API/schema/auth/permission flow receives integration regression/E2E and independently attributable A6/A7/A8 receipts against SHA_I. A reviewer with no affected files can return `NO_IMPACT_WITH_EVIDENCE`, not silent PASS; three receipts still required for the integration gate. An external protected-branch provider may reject CAS merge; status remains BLOCKED and requires rebase then new integrated-head tests. The Feature receipt hierarchy is `IMPLEMENTED→VERIFIED→INTEGRATED→RELEASED`; failure at a later stage never retroactively changes a historical commit receipt, but **current completion eligibility** depends on integration head freshness. See DEV-08 and CAM-01.

**DEV-REV-T07**: SHA_F 3-reviewed but SHA_I contains another feature; no integration pass until tests and SHA_I reviews.
**DEV-REV-T08**: merge occurs after integration receipt; prior receipt becomes stale and feature count current eligibility is recomputed.
