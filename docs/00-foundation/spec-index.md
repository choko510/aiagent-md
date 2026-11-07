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

`COMP-01` は [`audit/muse/spec-provenance.json`](../../audit/muse/spec-provenance.json) の `companionSpecs` でも内容Hashを管理し、`validate_spec_migration.py` が存在・要求ID・TEST-ID・SC-23追跡を静的検証する。88件の正本モジュール数には加算しないが、監査対象外にはしない。PR #71/#72統合時のManifest再生成は `python3 audit/muse/refresh_spec_provenance.py` を実行し、続けて `python3 audit/muse/validate_spec_migration.py` を実行する。
