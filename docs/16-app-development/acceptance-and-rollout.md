# DEV-10 — REQ/TEST/SC Matrix, Security Gate & Delivery Phases
> Status Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_00/01/02/03/04/07/08/09/10/11。実装なし、試験は**NOT RUN**。

## 0. 責務と成果物の境界
Goalに対する独立8AI設計→デモ/ユーザー決定→実装→実Browser Debug→3Reviewer→修正→PR/公開を全体E2Eで検証。Endless Runは同一Taskを永続化せず有限Cycle群を復旧・継続する。テストの存在と実行済み検証を区別し、Owner契約がProvisionalな間はLockedと表示しない。

## 1. REQ → TEST → user scenario
| REQ | Priority | Acceptance | Primary tests | Scenario |
|---|---|---|---|---|
| DEV-REQ-01 | MUST | project/workspace/git CASがTask終了後も存続 | DEV-WS-T01..07 | SC-24 |
| DEV-REQ-02 | MUST | 3独立案→1比較統合→1Builder→3独立同SHA review | DEV-TEAM-T01..06 | SC-24 |
| DEV-REQ-03 | MUST | UI見た目で比較、同一content/viewport、選択をversion固定 | DEV-DES-T01..06 | SC-25 |
| DEV-REQ-04 | MUST | 動くDemoのApprove/Reject/Revision、未承認本実装拒否 | DEV-DES-T03..05, DEV-FF-T06 | SC-25 |
| DEV-REQ-05 | SHOULD | 高性能Advisorを有限費用/独立権限で相談 | DEV-EXP-T01..05 | SC-26 |
| DEV-REQ-06 | MUST | 実Browser探索/Console/Network/UI/Trace/E2E → 修正 | DEV-DBG-T01..10 | SC-27 |
| DEV-REQ-07 | MUST | 最新SHAで3レビュー、P0/P1未解決0、必須テストPASS | DEV-REV-T01..06 | SC-28 |
| DEV-REQ-08 | MUST | Build/toolchain/package/previewが隔離、Sentinel権限外0 | DEV-BLD-T01..08 | SC-29 |
| DEV-REQ-09 | MUST | approved limited scopeでGit commit/PR、未知effect再送なし | DEV-WS-T05, DEV-BLD-T06 | SC-29 |
| DEV-REQ-10 | MUST | Endlessly cycle work until stop/target/quota/safety limit | CAM-T01..11 | SC-30 |
| DEV-REQ-11 | MUST | Feature 10件はcurrent-INTEGRATED distinct countのみ | DEV-FF-T01..11 | SC-31 |
| DEV-REQ-12 | MUST | Approvals/ACL/data/egress/runtime isolationを緩めない | DEV-UI-T02..05, DEV-BLD-T04 | SC-29 |
| DEV-REQ-13 | MUST | restart/429/revoke/unknown effect/partialで正しい復帰 | CAM-T02/T05/T06, DEV-TEAM-T03 | SC-30 |

## 2. Cross-owner interfaces that MUST be reconciled before lock
- CHAT_01: Project/Design/Demo/DevCycle/ReviewFinding/Campaignのtyped IDs、events、GoalRevision/WorkspaceRevisionの関連、Artifact lineage、result statuses。
- CHAT_02: parent/child Task budget & DAG join、DesignApproval WAIT、review re-runのTask identity、Context縮小とAdvisor資料転送、Cycle Job fence。
- CHAT_03: Git/Package/Preview/CI/Deployment typed Tool Intent、外部副作用の分類、Design consentとTool Approvalの完全分離、UNKNOWN effect reconciliation。
- CHAT_04: Browser Context/CDP exclusive ownership、debug instrumentation、Preview Session virtual origin、PR#71 Live Observationとは条件付き連携。
- CHAT_05: Git Provider, package registry, CI provider, deploy/remote build runnerのversioned connectorの有無。
- CHAT_07: Campaign Goal/WorkItem/next-cycle scheduling、空tickを起こさない持続Run controller、quota waiting。
- CHAT_08: versioned API/REST/SSE DTO、Human decision, viewers/debug UI、キャンペーンのprogress。
- CHAT_09: 2 VM isolation, Browser domain, Preview Broker/SSRF/credential isolation, supply chain, secret redaction。
- CHAT_10: project/workspace/Campaign/receipt DB migrations, transactional outbox, dedupe, cross-tenant FK, retention/backup。
- CHAT_11: hostile concurrency, Browser SSRF/PII, review SHA correctness, infinite work bounded by hard envelopes。

## 3. P0/P1 ship blockers
1. Reviewer SHAが一致しない or 3件未実施。
2. 上位承認済みDemoから大きく変化しているのに再決定なし。
3. P0/P1がOPEN/UNVERIFIED、あるいは必要fixture/E2Eが未実施。
4. 直 Internet/host/secret/CDPへの攻撃が1件でも成功。
5. 利用上限を越える予約/課金、停止後の新規dispatch、同じFeatureの二重計上。
6. 難所の`Expert Advisor`が認可なくtool変更。
7. git push/release/campaign stopのUNKNOWN効果を成功と断定。

## 4. Release / implementation slices
- Slice A: README/Project/CAS Git worktree/Artifact/ephemeral vs durable workspace、fixture test。
- Slice B: 8 role Task DAGと統合、横断report、P0/P1 review/repair loop。
- Slice C: Design Picker/可動DEMO/Preview BrokerとE2E（機密・SSRF検証を先に）。
- Slice D: Expert Advisor、browser debug Studio、Trace、Visual regression。
- Slice E: Package/toolchain/Android/desktop remote build、Git/CI、署名・デプロイbroker。
- Slice F: Campaign/Feature Factory continuous scheduling + 10-feature bounded demo approvals。
各Sliceは小さなPRで必要所有者が合意した契約から実装し、テストレシートを残す。実測のない費用/応答時間の保証を作らない。

## 5. Acceptance scenarios（独立ドメインの入力/期待/負例）
**SC-24 Multi-Agent Development:** 「Webアプリを設計→作成」→同一Goalから3独立案/A4比較統合、A5が実装、3 Reviewer同SHA、差分・最新テストと保存成果物。負例: 2 proposal、古いSHA review、テストなしの自称完了。
**SC-25 Visual Design/Demo:** 3つの画像/動くDemoからWindows11風等を視覚選択→Revisionと同一content保存→user Approve後だけ実装。負例: ダミーボタンを実APIと偽る、旧Approval受理、Previewがsecretを漏らす。
**SC-26 Expert:** 3回の同型失敗で高性能modelに限定質問→A5が実修正、成功/失敗を検査。負例: Advisor権限昇格、相談し続けて課金無限。
**SC-27 Browser Debug:** webUIがmobileでボタン見切れ、API500発生→Playwright/Browser Workerが再現・Trace・source相関・patch・E2Eを行う。負例: click API success=合格、HAR個人情報漏洩。
**SC-28 Repair Review:** P0/P1混在→新SHAで再試験/3AI reviewer、未解消0でverified。負例: reviewer1件未実行、P0の誤dismiss、旧SHA成功の流用。
**SC-29 Security+Build:** Cell build+Preview+Git PRの一連を検証。SSRF、raw direct TLS、CDP、署名鍵、host shell不可。外部push UNKNOWNは照会。負例: PreviewをLAN CONNECT proxy化。
**SC-30 Endless Campaign:** 「全体最適化をstop/上限まで」→有限Cycleを自動継続、停止、provider 429で安全待ち、復帰し計数/費用不変。負例: 空tick連続LLM、予算リセット、永続child Run。
**SC-31 Feature Factory:** 「デモ承認した10機能を実装」→複数候補、pending demoがあっても別feature進行、distinct verified 10で終了。負例: 10案作っただけ/重複/未レビューを数える。

## 6. Test execution status
本PRはMarkdown仕様だけ。上記SC-24..31、DEV-xx、CAM-Txxは**すべて Designed / NOT RUN**。本体未実装、Providerバージョン/Entitlement/実機隔離/Preview起動/Android/macOS Runnerも未検証。契約のLocked/Verified宣言は禁止。

## 2026-11-08 P0/P1 audit closure matrix / contract readiness

| Finding | Correction now specified | Trace | Runtime owner status |
|---|---|---|---|
| P0-01 | Preview scoped Capability + origin isolation + live stream revoke / process attestation | DEV-BLD-T09..14 / QA-E2E-T29N/F | CHAT_03/04/09/10 review pending |
| P1-01 | `preview:observe` vs mock `demo:interact` vs sensitive takeover | DEV-DES-T07/08/11 / QA-E2E-T25P/N | CHAT_04/08/09 pending |
| P1-02 | AUTO_RECOMMEND never accepts Demo; user decision or bounded mandate | DEV-DES-T09/10, DEV-UI-T07 | CHAT_03/08 pending |
| P1-03 | Integrated current HEAD default count for 10 features | DEV-FF-T07/08, QA-E2E-T31P/N | CHAT_07/10 pending |
| P1-04 | stable logicalWorkItemId, criterion distinct from identity, unique count | DEV-FF-T10, CAM-T17 | CHAT_01/07/10 pending |
| P1-05 | STOP controlVersion per dispatch, QuotaWaitReceipt, resumed generation | CAM-T12..16 / QA-E2E-T30F | CHAT_02/07/10 pending |
| P1-06 | additive candidate physical SQL/FK/CAS/outbox/forward migration | DEV-DB-T01..12 | CHAT_01/09/10 pending |
| P1-07 | explicit Auth registry actions + typed Event kinds | AUTH-DEV-T01..06, EVT-DEV-T01..05 | CHAT_01/08/09 pending |
| P1-08 | SC-24..31 P/N/F fixtures and dedicated CI spec checks | QA-E2E-T24P..T31F | CHAT_11 pending |
| P1-09 | shared security gates vs opt-in Development/Campaign feature gates | GATE-DEV-T01..04 | CHAT_09/11 pending |
| P1-10 | feature SHA vs integration HEAD triple review + revalidation | DEV-REV-T07/08, QA-E2E-T28F | CHAT_02/11 pending |

**New requirements**:
- **DEV-REQ-14** MUST: preview capability isolation, live revoke and malicious script safe origin. DEV-BLD-T09..14 → SC-29.
- **DEV-REQ-15** MUST: Campaign stop/quota epochs and stable logicalWorkItem identity and count. CAM-T12..19 → SC-30.
- **DEV-REQ-16** MUST: integrated HEAD, triple integration review, counting only current eligible receipts. DEV-FF-T07..12 + DEV-REV-T07..08 → SC-28/31.
- **DEV-REQ-17** MUST: conditional feature gates with common security always enforced. GATE-DEV-T01..04 → SC-29/30.
- **DEV-REQ-18** MUST: real authenticated Demo choice and per-mode Preview interactions. DEV-DES-T07..11 + DEV-UI-T07..10 → SC-25.

`DEV-REQ-11` from the earlier table is now interpreted as **distinct current-INTEGRATED feature count by default**, not branch-only VERFIED count; this explicitly supersedes earlier wording. `DEV-REQ-07` additionally binds triple review and required tests to combined integration SHA when target head moves. No independent merge/production deployment is authorized by accepting a Demo.

**Integration with merged PRs #71/#72 (2026-11-08):** PR #71とPR #72はmainへ統合済み。本PRは両者のBrowser/REST/Sentinel/Sandbox/Provenanceの最新契約を保持してmergeし、88正本とCOMP-01のGit blob SHAを実際の統合treeに合わせる。ADR-029/SC-23のLive Observationは維持し、Preview/Live Viewer権限を混同しない。#72のSecure Runtime再監査で追加されたSentinel/Tool承認・フェンシング要件を優先し、信頼境界は緩和しない。

**Verification label:** Static contract checks can be PASS while all VM, Browser, DB, concurrent execution and real feature tests remain **NOT RUN**. This addendum is an implementation plan and testable interface proposal, not a claim of Owner consensus or runtime correctness.

**Regression acceptance extension:** [DEV-DB-T11/T12](../10-infrastructure/database-schema-and-migrations.md), DEV-FF-T12, CAM-T19 require old/current integration HEAD comparison and immutable first-count identity. A previously reached Campaign must not advertise stale completion when current-head eligibility falls. Static schema checks cannot assert runtime transaction correctness.
