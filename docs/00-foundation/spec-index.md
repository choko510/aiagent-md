# Canonical Specification Inventory: 88 Modules / 16 Independent Chats

> 2026-10-30。**88仕様MDは文書としてDraftで配置されている。契約のLocked、HarnessのImplemented/Verifiedを意味しない。** カテゴリオーナー以外は編集禁止。

| ID | Required specification path | Owner | Initial state |
|---|---|---|---|
| 00-01 | `docs/00-foundation/README.md` | CHAT_00 | Draft / documented (not Locked) |
| 00-02 | `docs/00-foundation/product-requirements.md` | CHAT_00 | Draft / documented (not Locked) |
| 00-03 | `docs/00-foundation/system-architecture.md` | CHAT_00 | Draft / documented (not Locked) |
| 00-04 | `docs/00-foundation/domain-glossary.md` | CHAT_00 | Draft / documented (not Locked) |
| 00-05 | `docs/00-foundation/architecture-decisions.md` | CHAT_00 | Draft / documented (not Locked) |
| 01-01 | `docs/01-contracts/core-domain-models.md` | CHAT_01 | Draft / documented (not Locked) |
| 01-02 | `docs/01-contracts/event-envelope-and-stream.md` | CHAT_01 | Draft / documented (not Locked) |
| 01-03 | `docs/01-contracts/task-run-state-machines.md` | CHAT_01 | Draft / documented (not Locked) |
| 01-04 | `docs/01-contracts/tool-io-schema.md` | CHAT_01 | Draft / documented (not Locked) |
| 01-05 | `docs/01-contracts/evidence-artifact-contracts.md` | CHAT_01 | Draft / documented (not Locked) |
| 01-06 | `docs/01-contracts/error-taxonomy.md` | CHAT_01 | Draft / documented (not Locked) |
| 02-01 | `docs/02-runtime/runtime-host-and-session.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-02 | `docs/02-runtime/task-run-manager.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-03 | `docs/02-runtime/agent-loop.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-04 | `docs/02-runtime/goal-and-planner.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-05 | `docs/02-runtime/result-verifier.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-06 | `docs/02-runtime/checkpoint-and-recovery.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-07 | `docs/02-runtime/steer-queue-pause-resume.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-08 | `docs/02-runtime/context-manager-and-compaction.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-09 | `docs/02-runtime/model-router.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-10 | `docs/02-runtime/budget-and-limits.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-11 | `docs/02-runtime/middleware-lifecycle.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-12 | `docs/02-runtime/subagent-delegation.md` | CHAT_02 | Draft / documented (not Locked) |
| 02-13 | `docs/02-runtime/sandbox-execution.md` | CHAT_02 | Draft / PROVISIONAL (Muse migration) |
| 03-01 | `docs/03-tools/tool-registry-and-discovery.md` | CHAT_03 | Draft / documented (not Locked) |
| 03-02 | `docs/03-tools/tool-execution-gateway.md` | CHAT_03 | Draft / documented (not Locked) |
| 03-03 | `docs/03-tools/tool-approval-and-policy.md` | CHAT_03 | Draft / documented (not Locked) |
| 03-04 | `docs/03-tools/side-effect-ledger.md` | CHAT_03 | Draft / documented (not Locked) |
| 03-05 | `docs/03-tools/mcp-adapter.md` | CHAT_03 | Draft / documented (not Locked) |
| 03-06 | `docs/03-tools/sandboxed-tool-pipelines.md` | CHAT_03 | Draft / documented (not Locked) |
| 04-01 | `docs/04-web-browser/search-router.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-02 | `docs/04-web-browser/http-fetch.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-03 | `docs/04-web-browser/browser-session-manager.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-04 | `docs/04-web-browser/browser-action-engine.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-05 | `docs/04-web-browser/jev-adapter.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-06 | `docs/04-web-browser/lightpanda-adapter.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-07 | `docs/04-web-browser/scrapling-sidecar.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-08 | `docs/04-web-browser/data-extraction-and-normalization.md` | CHAT_04 | Draft / documented (not Locked) |
| 04-09 | `docs/04-web-browser/login-challenges-human-handoff.md` | CHAT_04 | Draft / documented (not Locked) |
| 05-01 | `docs/05-connectors/connector-framework.md` | CHAT_05 | Draft / documented (not Locked) |
| 05-02 | `docs/05-connectors/amazon-keepa.md` | CHAT_05 | Draft / documented (not Locked) |
| 05-03 | `docs/05-connectors/buyback-price-providers.md` | CHAT_05 | Draft / documented (not Locked) |
| 05-04 | `docs/05-connectors/x-research.md` | CHAT_05 | Draft / documented (not Locked) |
| 05-05 | `docs/05-connectors/hotel-research.md` | CHAT_05 | Draft / documented (not Locked) |
| 06-01 | `docs/06-memory-skills/memory-data-model.md` | CHAT_06 | Draft / documented (not Locked) |
| 06-02 | `docs/06-memory-skills/memory-retrieval-and-update.md` | CHAT_06 | Draft / documented (not Locked) |
| 06-03 | `docs/06-memory-skills/skill-lifecycle.md` | CHAT_06 | Draft / documented (not Locked) |
| 06-04 | `docs/06-memory-skills/self-improving-skills.md` | CHAT_06 | Draft / documented (not Locked) |
| 06-05 | `docs/06-memory-skills/plugins-recipes-and-hooks.md` | CHAT_06 | Draft / documented (not Locked) |
| 07-01 | `docs/07-automation/scheduler-and-recurring-tasks.md` | CHAT_07 | Draft / documented (not Locked) |
| 07-02 | `docs/07-automation/watch-lifecycle.md` | CHAT_07 | Draft / documented (not Locked) |
| 07-03 | `docs/07-automation/observations-and-trigger-evaluation.md` | CHAT_07 | Draft / documented (not Locked) |
| 07-04 | `docs/07-automation/notification-outbox.md` | CHAT_07 | Draft / documented (not Locked) |
| 07-05 | `docs/07-automation/heartbeat-and-goal-loop.md` | CHAT_07 | Draft / documented (not Locked) |
| 08-01 | `docs/08-interfaces/rest-api.md` | CHAT_08 | Draft / documented (not Locked) |
| 08-02 | `docs/08-interfaces/realtime-events.md` | CHAT_08 | Draft / documented (not Locked) |
| 08-03 | `docs/08-interfaces/web-chat-interface.md` | CHAT_08 | Draft / documented (not Locked) |
| 08-04 | `docs/08-interfaces/task-and-watch-dashboard.md` | CHAT_08 | Draft / documented (not Locked) |
| 08-05 | `docs/08-interfaces/approval-and-browser-takeover-ui.md` | CHAT_08 | Draft / documented (not Locked) |
| 08-06 | `docs/08-interfaces/channel-adapters-telegram.md` | CHAT_08 | Draft / documented (not Locked) |
| 09-01 | `docs/09-security/authentication-and-user-isolation.md` | CHAT_09 | Draft / documented (not Locked) |
| 09-02 | `docs/09-security/secrets-and-credentials.md` | CHAT_09 | Draft / documented (not Locked) |
| 09-03 | `docs/09-security/sandbox-and-capabilities.md` | CHAT_09 | Draft / documented (not Locked) |
| 09-04 | `docs/09-security/network-egress-and-ssrf.md` | CHAT_09 | Draft / documented (not Locked) |
| 09-05 | `docs/09-security/prompt-injection-defense.md` | CHAT_09 | Draft / documented (not Locked) |
| 09-06 | `docs/09-security/privacy-audit-and-retention.md` | CHAT_09 | Draft / documented (not Locked) |
| 09-07 | `docs/09-security/runtime-cell-isolation.md` | CHAT_09 | Draft / PROVISIONAL (Muse migration) |
| 09-08 | `docs/09-security/sentinel-authority.md` | CHAT_09 | Draft / PROVISIONAL (Muse migration) |
| 10-01 | `docs/10-infrastructure/database-schema-and-migrations.md` | CHAT_10 | Draft / documented (not Locked) |
| 10-02 | `docs/10-infrastructure/job-queue-and-outbox.md` | CHAT_10 | Draft / documented (not Locked) |
| 10-03 | `docs/10-infrastructure/docker-deployment.md` | CHAT_10 | Draft / documented (not Locked) |
| 10-04 | `docs/10-infrastructure/observability-and-cost-tracing.md` | CHAT_10 | Draft / documented (not Locked) |
| 10-05 | `docs/10-infrastructure/backup-upgrade-and-recovery.md` | CHAT_10 | Draft / documented (not Locked) |
| 11-01 | `docs/11-qa/test-harness-and-fixtures.md` | CHAT_11 | Draft / documented (not Locked) |
| 11-02 | `docs/11-qa/end-to-end-acceptance.md` | CHAT_11 | Draft / documented (not Locked) |
| 11-03 | `docs/11-qa/fault-injection-and-concurrency.md` | CHAT_11 | Draft / documented (not Locked) |
| 11-04 | `docs/11-qa/benchmark-and-release-gates.md` | CHAT_11 | Draft / documented (not Locked) |

## Assigned ownership and counts
- CHAT_00: `docs/00-foundation/` (5 MD)
- CHAT_01: `docs/01-contracts/` (6 MD)
- CHAT_02: `docs/02-runtime/` (13 MD)
- CHAT_03: `docs/03-tools/` (6 MD)
- CHAT_04: `docs/04-web-browser/` (9 MD)
- CHAT_05: `docs/05-connectors/` (5 MD)
- CHAT_06: `docs/06-memory-skills/` (5 MD)
- CHAT_07: `docs/07-automation/` (5 MD)
- CHAT_08: `docs/08-interfaces/` (6 MD)
- CHAT_09: `docs/09-security/` (8 MD)
- CHAT_10: `docs/10-infrastructure/` (5 MD)
- CHAT_11: `docs/11-qa/` (4 MD)
**Count: 88 module specs.** README、AGENTS、補助設計文書および調査履歴はこの件数に含まない。旧CHAT_XXは執筆当時の所有者ラベルで、実装担当を制約しない。

## Change control
実装で仕様の変更が必要になったら、関連する正本・契約・依存モジュールを洗い出し、ADR、互換性、要件/テスト対応を含めてPRで更新する。元のCHAT担当区分は設計上の責務境界を示す履歴であり、実装作業を16チャットに分ける必要はない。`Locked`化は契約整合と承認後のみ。

## 2026-10-30 approved feature expansion — CHAT_12–15

11 new module specs **drafted; runtime not implemented**; these are additional to the original 74. See [feature expansion](feature-expansion-2026-10.md). The core contracts and side-effect ownership remain with the existing owners.

| ID | Required specification path | Owner | Initial state |
|---|---|---|---|
| 12-01 | `docs/12-intelligence/opportunity-radar.md` | CHAT_12 | Draft / documented (not Locked) |
| 12-02 | `docs/12-intelligence/entity-resolution.md` | CHAT_12 | Draft / documented (not Locked) |
| 12-03 | `docs/12-intelligence/trend-fusion.md` | CHAT_12 | Draft / documented (not Locked) |
| 12-04 | `docs/12-intelligence/decision-risk-engine.md` | CHAT_12 | Draft / documented (not Locked) |
| 12-05 | `docs/12-intelligence/source-reliability-profiles.md` | CHAT_12 | Draft / documented (not Locked) |
| 13-01 | `docs/13-event-ingestion/event-driven-agent.md` | CHAT_13 | Draft / documented (not Locked) |
| 13-02 | `docs/13-event-ingestion/change-intelligence.md` | CHAT_13 | Draft / documented (not Locked) |
| 13-03 | `docs/13-event-ingestion/notification-intelligence.md` | CHAT_13 | Draft / documented (not Locked) |
| 14-01 | `docs/14-adaptive-experience/learned-browser-actions.md` | CHAT_14 | Draft / documented (not Locked) |
| 14-02 | `docs/14-adaptive-experience/feedback-learning.md` | CHAT_14 | Draft / documented (not Locked) |
| 15-01 | `docs/15-multimodal/multimodal-ingestion.md` | CHAT_15 | Draft / documented (not Locked) |

- CHAT_12: `docs/12-intelligence/` (5 MD)
- CHAT_13: `docs/13-event-ingestion/` (3 MD)
- CHAT_14: `docs/14-adaptive-experience/` (2 MD)
- CHAT_15: `docs/15-multimodal/` (1 MD)

**Historical: 74 + 11 = 85 original drafts; 2026-11-07 Muse migration adds 3 = 88 module documents across 16 owner domains.**

## 2026-11-07 Adaptive Context integration addendum

既存の仕様数は**88件のまま**で、新しい独立MDは増やさない。実装可能な詳細契約は **02-08 [Context Manager and Compaction](../02-runtime/context-manager-and-compaction.md) の18節** に追記。これを中心に02-03/06/09/10/12、06-01/02、08-04、09-05、11-01/04、01/10の各正本・横断提案を整合させる。ADR-023〜028とDEC-P0-08〜10、DEC-P1-06は [Architecture Decisions](architecture-decisions.md) を確認。**PR #68 Muse secure architectureに依存するDraft proposal** であり、承認・実装・QA実行済みではない。

## 2026-11-07 Browser Live Observation companion design

[Browser Live Observation](../08-interfaces/browser-live-observation.md) は04-03 BrowserSessionと08-05 Browser Takeoverの**共同補足仕様**。サーバー側Browserのライブ閲覧を`browser:observe`で追加し、見るだけではAgentを停止せず、操作は別のHuman Takeoverに限定する。BLO-001〜17 → BLO-T01〜16 / BLO-OFF-T01〜03 → SC-23。既存88件の正本モジュール数は維持し、ADR-029と関連文書を横断更新する（Draft / 実装無し）。

### Audited companion specifications (outside 88 canonical module count)

| Companion ID | Path | Owners | Status |
|---|---|---|---|
| COMP-01 | `docs/08-interfaces/browser-live-observation.md` | CHAT_04/08/09/10/11 | Draft / PROVISIONAL |
| COMP-02 | `docs/16-app-development/local-device-rust-client.md` | CHAT_00/01/02/03/04/05/08/09/10/11/16 | Draft / PROVISIONAL / NOT RUN |
| COMP-03 | `docs/08-interfaces/extension-marketplace.md` | CHAT_01/03/06/08/09/10/11 | Draft / PROVISIONAL / NOT IMPLEMENTED |

`COMP-01` は [`audit/muse/spec-provenance.json`](../../audit/muse/spec-provenance.json) の `companionSpecs` でも内容Hashを管理し、`validate_spec_migration.py` が存在・要求ID・TEST-ID・SC-23追跡を静的検証する。88件の正本モジュール数には加算しないが、監査対象外にはしない。

`COMP-02` はRust Device Client/Cua Driverとデスクトップ独自4機能の補足仕様（LDC-REQ-01〜32 ↔ LDC-T01〜32、§23の8件監査対処）。同じManifestで内容Hashを固定し、既存CIの`validate_spec_migration.py`にLDC IDの連続性、REQ/Tの1対1対応、安全境界の最小静的契約検査を追加する。**Static contract checkは実装・デバイス実機安全性の保証ではない**。88件の正本モジュール数は不変。PR #71/#72統合時のManifest再生成は `python3 audit/muse/refresh_spec_provenance.py` を実行し、続けて `python3 audit/muse/validate_spec_migration.py` を実行する。

## 2026-11-07 Development & Endless Campaign extension（Provisional）

従来の**88件は歴史的な基礎仕様の件数**であり、この節の11件は新たなDraft設計案として追加される。現行PRベースで **88 + 11 = 99 件の提案モジュール**（Browser Live ObservationはPR #71で別途統合済み。既存88件とは別の補足仕様として扱う）。既存の歴史的Countを削除・上書きするのではなく、採用時にindexを移行する。どれもLocked/Implemented/Verifiedではない。

| ID | New draft | Integration owner |
|---|---|---|
| DEV-01 | [Project/Workspace/Git](../16-app-development/project-workspace-and-git.md) | CHAT_01/02/03/05/09/10 |
| DEV-02 | [Multi Agent Development](../16-app-development/multi-agent-development.md) | CHAT_01/02/03/08/09/10 |
| DEV-03 | [Visual Design and Demo](../16-app-development/visual-design-and-demo.md) | CHAT_01/02/04/08/09/10 |
| DEV-04 | [Expert Advisor](../16-app-development/expert-advisor.md) | CHAT_02/03/08/09/10 |
| DEV-05 | [Build Preview Release](../16-app-development/build-preview-release.md) | CHAT_02/03/04/05/08/09/10 |
| DEV-06 | [Autonomous Browser Debugger](../16-app-development/autonomous-browser-debugger.md) | CHAT_02/03/04/08/09/10/11 |
| DEV-07 | [Review Repair Gates](../16-app-development/review-and-repair-gates.md) | CHAT_01/02/03/08/10/11 |
| DEV-08 | [Feature Factory](../16-app-development/feature-factory.md) | CHAT_01/02/07/08/09/10/11 |
| DEV-09 | [Development API/UI](../16-app-development/development-api-and-ui.md) | CHAT_01/03/04/07/08/09/10 |
| DEV-10 | [Acceptance & rollout](../16-app-development/acceptance-and-rollout.md) | CHAT_00/01/02/03/04/07/08/09/10/11 |
| CAM-01 | [Endless Run Campaign](../07-automation/endless-run-campaign.md) | CHAT_01/02/03/07/08/09/10/11 |

Start at [DEV README](../16-app-development/README.md) → [DEV-10](../16-app-development/acceptance-and-rollout.md) → referenced 01/02/03/04/07/08/09/10/11 canonical specs。将来の実装で定義重複があればこちらの型を勝手に正本化しない。差分で変更した基礎ファイルは本PRのOwner合意待ち。

## 2026-11-09 Cloud Coding capabilities — supplemental Draft proposals

既存**88基礎モジュールとDEV-01〜10/CAM-01の歴史的な件数を維持**したまま、[DEV-11〜20一覧](../16-app-development/README.md)を補足する。**9能力、追加5仕様MD、DEV-11..20の10設計ID**。いずれもOwner承認・実装・テスト未完了で、既存のShared契約の正本へ昇格していない。

| IDs | Companion specification | State |
|---|---|---|
| DEV-11 | [PR Guardian](../16-app-development/pr-guardian.md) | Draft / NOT RUN |
| DEV-12 | [Reproduction Lab](../16-app-development/reproduction-lab.md) | Draft / NOT RUN |
| DEV-13/14 | [Code Intelligence / Adaptive Testing](../16-app-development/code-intelligence-and-adaptive-testing.md) | Draft / NOT RUN |
| DEV-15..19 | [Advanced Software Factory](../16-app-development/advanced-software-factory.md) | Draft / NOT RUN |
| DEV-20 | [Acceptance / rollout](../16-app-development/advanced-development-acceptance.md) | Draft / NOT RUN |

対応REQはDEV-REQ-19..27、対応受入はSC-32..40（提案）。既存DEV-REQ-01..18/SC-24..31を置換しない。PR #74と変更パスを分離し、Context/Tool/Sentinel/BenchmarkのOwner調整を後続の契約審査事項とする。

## 2026-11-09 Python-first 移行（STEP 1）

開発言語方針はPython-first（[ADR-036〜040](architecture-decisions.md)、物理配置は[System Architecture §13.2](system-architecture.md)、拘束規則は[Architecture Rules §E](architecture-rules.md)）。**新しい独立仕様MDは追加せず**、88件の正本・COMP-01・DEV-01〜11のモジュール数とIDは変更しない。各正本MDへ「2026-11-09 Python-first」節または該当箇所の改訂を加え、TS/Node前提の記述は移行監査台帳でA（修正）/B（保持）/C（再検討）に分類した。静的検査は`audit/cross-contract/check_python_first.py`。Agent Runtime OSSはSTEP 2で選定（ADR-038）。


## 2026-11-10 Rust Local Device Client / Cua Driver companion（DEV-LDC）

[ローカルDevice Agent・Cua・Chrome・Build/ADB統合設計](../16-app-development/local-device-rust-client.md) はDEV-05/06およびTool/Sentinel/Sandbox/Browserの**横断補足仕様**。Rustの対象はユーザーPCにインストールするDevice AgentとデスクトップUIであり、Python-firstのサーバー実装や既存Browser Workerを置換しない。DEV-LDC-REQ/T（本文では`LDC-REQ/T`）を提案し、正式なDomain/REST/DB型は各正本Ownerの承認後に確定する。**Design only / NOT RUN**。88件・COMP-01/COMP-02・DEV-01..20の歴史的カウントは変更しない（Companionの登録数だけ1→2）。

## 2026-11-10 Extension Marketplace — audited Companion Spec (COMP-03)

Skills・MCPの検索、ワンクリック導入/無効化/削除/更新、Registry連携、認証、Tool Gateway/マージ済みPR #82との統合境界を [Extension Marketplace](../08-interfaces/extension-marketplace.md) にまとめる。**補足仕様・NOT IMPLEMENTED / NOT TESTED**であり、既存88基礎モジュール数、Owner、Shared契約、PR #82の実装状態を変更しない。
