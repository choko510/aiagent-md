# DEV-08 — Feature Factory: Up to Ten Approved Features (or Continuous)
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/07/08/09/10/11 | 実装なし。

## User Goal
「このアプリへ新機能を10件実装。3AIで考え、1AIで統合、**動く本実装に近いDEMO**を提示。OKしたものだけ本実装→3AIレビュー→P0/P1解決→次へ」。既定目標10件は設定値であり上限例。長期モードは停止指示または利用上限まで繰り返せるが、各Cycleは有限に区切る。

## Feature lifecycle
`PROPOSED → SYNTHESIZED → DEMO_BUILDING → WAITING_DEMO_DECISION → APPROVED/REJECTED/REVISION_REQUESTED → IMPLEMENTING → TESTING → REVIEWING → VERIFIED → INTEGRATING → INTEGRATED → RELEASED`。Design decision/実Demo決定はGatewayの外部行為approvalと別。Revision requestは既存承認を失効し、更新したDemoの再確認を要する。Rejectは後から明示的にUndo可能だが、同じ提案を直ちに延々再提案しない。

```ts
type FeatureCandidateV1 = { featureId:string; projectId:string; campaignId?:string;
 proposalRefs:string[]; synthesisRef:string; dedupeFingerprint:string;
 dependencies:string[]; designRevision:string; demoRef?:string;
 userDecision:"PENDING"|"APPROVED"|"REJECTED"|"REVISION_REQUESTED";
 state:string; implementationSha?:string; verificationReceiptRef?:string;
 countReceiptRef?:string; recordVersion:string };
```

## Counting / competition / concurrency
「10件達成」は既定で**承認済・実装・必須テスト・3レビュー・P0/P1未解決0・現在の統合HEADに反映・統合後検証PASS**のdistinct logicalWorkItemIdごとに原子的に一度だけcount receipt。提案10件、未承認Demo10件、commitだけ10件では未達。Merged/Integrated/Releasedはそれぞれ別のstatusとして必須。
- `#5 IMPLEMENTING`, `#6 WAITING_APPROVAL`, `#7 SYNTHESIZING`, `#8 DEMO_BUILDING` のように待機中でも他の独立作業を継続する。各Featureのbranch、workspace CAS、subTask予算を分離し、同じschema/ファイルを競合更新する場合は依存DAG/直列化で調停。
- 候補は価値/重複/ユーザー制約/性能/セキュリティ/実装コスト/保守負荷で比較。3AIの新規提案に既存実装・却下履歴・ユーザーの禁則を与え、理由なき類似提案を重複させない。
- Backendのみの機能もAPI fixture/CLI observable demoを提示し、モックと実装範囲を区別。UIのScreenshotだけで「動くDEMO」と呼ばない。
- ユーザーがデモを確認するまで当該Featureの本実装は始めない。preview中の外部API/購入/削除を実行しない。許可されたデザイン変更や手動採用部品はDesignRevisionで固定。
- 承認待ちだけが残る場合、CampaignはWAITING_INPUTでparkし無駄なLLMを連打しない。他独立タスクがあれば進める。

## Tests
DEV-FF-T01: DONE4件+pending2+implementing2→progress=4。
T02: denied/only demo/duplicated/oldSHA/missing reviewで件数増分0。
T03: pending featureが他独立Featureの実装を妨害しない。
T04: 共同schema更新はCAS競合時にfail/serial。
T05: 最終10件目は原子countで一度だけ達成。
T06: Demo revision変更は旧承認無効、実装開始0。

## 2026-11-08 P1-02/P1-03/P1-04/P1-10 — Integration-based completion

**Authoritative feature status has distinct evidence-bound milestones**:
| Stage | Required receipt | User-facing wording |
|---|---|---|
| `IMPLEMENTED` | feature branch commit+build artifact | 作業ブランチで実装済み |
| `VERIFIED` | current feature SHA with mandatory tests, browser evidence and three independent reviews; P0/P1 open=0 | 単体検証済み |
| `INTEGRATED` | commit in current project integration head, complete integration tests and integration review bound to **that head** | アプリ本体への統合完了 |
| `RELEASED` | provider-confirmed delivery/deployment to authorized target, with receipt and rollback plan | 公開・配布完了 |

The default for `"アプリに10機能追加"` is **10 distinct INTEGRATED**. A user asking for **PR creation only** may explicitly select `VERIFIED` + confirmed PR receipt or other selectable criterion, labeled separately. `RELEASED` is never inferred from merging. `FeatureCandidateV1.state=DONE` is **not** a terminal-counting oracle and is superseded by verified stage receipts. Feature Factory's DemoAcceptancePolicy default is `REQUIRE_USER_APPROVAL`; `AUTO_RECOMMEND` only chooses which proposal to show. Any preauthorized auto acceptance must be narrowly scoped and recorded (DEV-03); otherwise implementation remains blocked.

**Identity/dedupe:** use server-generated stable `logicalWorkItemId` per user-visible feature, not a hash of current commit. Distinct logical IDs are linked through semantic equivalence/rejected-history review; identical behavior cannot be counted twice by resubmitting with a new baseline SHA. `baselineSha`, `featureSha`, `integrationHeadSha`, `criterionVersion`, `integrationReceiptId` are separate. Count entries are unique on `(campaignId, logicalWorkItemId, criterionVersion)` (and detect semantic duplicate via feature key / equivalence record); after future criterion revision do not automatically count same feature again toward a new target. The counter is a DB view/sum of immutable eligible receipts, not a model-maintained integer. Reconciliation after crash derives count from rows, not a saved numeric summary.

**Integration race:** Feature A can be VERIFED on branch SHA_A but if Feature B changes the target integration HEAD, QA tests and at least risk-appropriate independent integration reviews must run against final HEAD_AB. For P0/P1-affecting shared changes (auth, schema, core behavior, API, UI flows), all three A6/A7/A8 re-review the combined HEAD; for low-risk changes, three reviewers may provide scoped no-impact receipts tied to the combined SHA, but absence of combined-SHA receipts never implies success. If tests fail, mark `INTEGRATION_BLOCKED`, remove from eligible completed count until recovery; don't delete historical verified feature evidence. Atomic update of integration head, receipt, count ledger and next-Cycle outbox via CAS; conflicting head requires rebase/retest before counting.

**DEV-FF-T07**: ten verified branches not integrated into current head → goal 0/10 by default.
**DEV-FF-T08**: ten distinct features integrated and independently tested on final head → target reached once.
**DEV-FF-T09**: two features conflict in API/schema/UX → no merged count until combined-head gate succeeds.
**DEV-FF-T10**: feature recreated at new SHA/new Cycle/new proposed ID but same logical identity → exactly one count.
**DEV-FF-T11**: old integration-head review is stale after concurrent merge; reset integration verification and stop target recognition until new gate passes.
