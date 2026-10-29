# Plugins, Recipes and Hooks — Detailed Implementation Specification

> CHAT_06 | `docs/06-memory-skills/plugins-recipes-and-hooks.md` | **Draft / Provisional** | 2026-10-30 | main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Dependencies: 06-03 Skill Lifecycle; CHAT_01/02/03/05/09/10

## 0. 採用アーキテクチャ

**Pluginは供給元・manifest・bundled recipes/hooksを登録する導入単位、Recipeは宣言的なタスク手順、Hookは厳しく型付けされた事前・事後拡張ポイント**。これらをTool GatewayそのものやApproval Authorityと同一視しない。v1は**専用プロセス内で任意Plugin JS/Pythonコードを動的実行しない**。署名されたBundleを検査・レビュー後にregistryへ登録し、allowlisted declarative recipe actionsだけをHarness側が解釈する。外部API ConnectorはCHAT_05のProvider Adapter、MCP実行はCHAT_03のGateway、Script実行はCHAT_09のcapabilityポリシーに従う。

Hookは入力改竄・承認回避の温床になりやすいため、`before_tool_prepare`等で**拒否または狭い制約を追加できても、Grantを増やさない**。post hookは再実行や外部副作用を起こさない。Hook発火順序/timeout/失敗モードを明記し、同一pluginの重複ロードと最新版への無断アップデートを防ぐ。

## 1. Scope / 非目標・シナリオ

対象：Plugin登録/更新/検疫/無効化、Tool/Recipe/Hook metadata、互換性、ユーザー設定、capability要求、Hook呼出の順序/メモリ/費用、安全なアップグレード・設定移行・rollbackと監査。研究用Use casesはSC-01/02/03/04/05/06/07/08/09、SC-11（X event ingestion自体はCHAT_13）、SC-13（Browser replayはCHAT_14/04）、SC-16（feedback提供、学習自動化しない）。

非目標：任意Host Shell・SSH・Docker socket・privileged container、未承認Gmail/X投稿・購入/予約、secretそのものをSkill/Recipe/LLMへ渡すこと、HTTPプラグインを無制限でfetch、PluginがTask State/Event canonical schemaを書き換えること。PluginはRuntimeを置換しない。

## 2. OSS調査：実コード・テスト

| OSS / commit / license | 確認コード・テスト | 今回の設計に生かす部分/不採用 |
|---|---|---|
| **OpenClaw** `aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3` MIT | [`src/plugins/hooks.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/plugins/hooks.ts) `createHookRunner`, `runBeforeToolCall`, `intersectToolsAllow`, `runModifyingHook`, `runClaimingHook`、[`agent-tools.before-tool-call.policy.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/agents/agent-tools.before-tool-call.policy.ts) `runBeforeToolCallHook` は実行前のpolicy chain、[`manifest-registry.test.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/plugins/manifest-registry.test.ts) はID衝突・trust spoof・prototype pollutionのテスト | Hook chainのテストと権限制限のintersectionを参考。汎用JS plugin/host Tool実行やplugin manifestで権限を増やすモデルは不採用 |
| **OpenClaw** 同commit MIT | [`src/commands/doctor-plugin-manifests.test.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/commands/doctor-plugin-manifests.test.ts) `preserves undecodable manifests while migrating valid neighbors`、[`src/skills/lifecycle/install.test.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/skills/lifecycle/install.test.ts) `blocks install when before_install rejects` | schema移行のpartial failureとインストール拒否を具体的な受入試験へ採用 |
| **Deep Agents** `3efa098130d6670fe713f841072bc11ff40545da` MIT | [`plugins/adapters/skills.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/code/deepagents_code/plugins/adapters/skills.py) `namespaced_skill_name`/`plugin_skill_sources`、[`middleware/skills.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/skills.py) `_validate_skill_name`/`_parse_allowed_tools`、[`test_skill_tools.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_skill_tools.py) `test_call_before_any_read_is_rejected_without_running`/`test_registered_tool_wins_over_a_skill_tool_of_the_same_name` | plugin namespacing、Tool登録の優先順位と未読Skill Tool遮断を参考。Tool登録/権限はGatewayが所有 |
| **Hermes** `fcc26fbd9f1641755e49793d7ed608f3b7214c3d` MIT | [`tools/write_approval.py`](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/write_approval.py) `evaluate_gate`/`stage_write`/`discard_pending`、[`tools/skills_tool.py`](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/skills_tool.py) `_skills_scan_signature`/`skill_view` | Stage→Reviewとtool presentation metadataを参考。Plugin任意コード実行、記憶・Approval権限同一化は不採用 |

**比較案**：A. 実行可能JS/Pythonをin-processロード=却下（secret/worker/process隔離不能）。B. Plugin HookにTool callのargs/permissionをそのまま全権移譲=却下（Approval/Scope bypass）。C. **declarative recipes + policy-first Hook adapter + immutable/approved Plugin Bundle**=採用。将来のPlugin Worker隔離は実験・セキュリティレビュー後。OSS本番での性能をそのまま流用しない。Release tagと既知Issueの網羅調査は未完で未検証とする。

## 3. 正本・Owner・依存図

```text
[Plugin source] --signed? scanned?--> [Quarantine / registry CHAT_06]
                             |                 |
                       user approval       immutable bundle
                             |                 |
                    [Installation/enable binding CHAT_06]
                             |
           +-----------------+-----------------+
           |                 |                 |
      [Recipe plan]      [Skill refs]       [Hook definitions]
           |                 |                 |
           v                 v                 v
    [Runtime CHAT_02]  [SkillLoader CHAT_06]  [HookRunner constrained]
           |                                   |
           +------------------+----------------+
                              |
           [Tool Gateway CHAT_03 / Policy CHAT_09] -> [Approved executor]
                              |
               [Provider adapter CHAT_05 / Browser CHAT_04]
                              |
            [Evidence/Event CHAT_01, SQL/outbox CHAT_10]
```

Pluginはそれ自体ではSource、Evidence、Task、Run、Watchを変更する権限がない。RecipeのGoal/Step定義はCHAT_02のPlannerへ候補として提供するだけでTask状態の正本ではない。Hook定義はCHAT_06、Tool準備/承認・外部副作用の制御はCHAT_03/09。User設定はCHAT_08/09認証済のみによる操作。`tenantId/ownerUserId`と`workspace_id`の写像は未決。

## 4. Requirements

| ID | Level | 要件 | SC |
|---|---|---|---|
| PLG-001 | MUST | Plugin manifest/schema/digest/source/compatibility strict validate | 05,08 |
| PLG-002 | MUST | discovery→quarantine→approval→installed disabled→enabled別管理 | 07,08 |
| PLG-003 | MUST | Plugin/Recipe/HookはTool Capability/Approvalを増加できない | 07,08 |
| PLG-004 | MUST | User/tenant/owner scope隔離、operator pluginは一般User変更不可 | 06,08 |
| PLG-005 | MUST | Hookの前後順序・拒否・Timeout・errorを決定的に定義 | 07,08 |
| PLG-006 | MUST | Recipeの実行は承認済Tool Gateway経由、直接HTTP/shell禁止 | 03,08 |
| PLG-007 | MUST | pluginの署名・digest・publisher・versionを変更前後照合 | 05,08 |
| PLG-008 | MUST | install/update/disable/rollbackはimmutable revision, CAS, idempotencyで管理 | 07 |
| PLG-009 | MUST | Plugin sourceのSSRF/path traversal/binary executable/rebindingを遮断 | 08 |
| PLG-010 | MUST | Hookはuntrusted context/secretを越境転送しない | 03,06,08 |
| PLG-011 | MUST | Recipe実行前にTask goalRevision/Run leaseEpoch・cancelを再確認 | 07 |
| PLG-012 | MUST | Hook decisionがdenyなら外部Tool start 0、post hookで新たな副作用を作らない | 07,08 |
| PLG-013 | MUST | Schema不一致のmigrationを部分失敗から安全に戻せる | 07 |
| PLG-014 | MUST | Hook/Recipe/Pluginの実行回数/深さ/時間/費用に有限上限 | 09 |
| PLG-015 | MUST | Secretはopaque Connector handle、plaintextをmanifest/Model/Logへ出さない | 06,08 |
| PLG-016 | SHOULD | 依存グラフ互換と最小権限diff、失敗原因をUIへ表示 | 05,09 |
| PLG-017 | EXPERIMENT | 隔離Plugin Workerアダプタを独立評価、v1で有効化不可 | 09 |

## 5. Type / API proposal

**Plugin BundleとSkill Bundleは別**。Skill manifestは06-03が正本。Plugin metadataに`bundledSkills`があれば06-03 stageへリンクし、Toolにしない。共有Event/CommandはCHAT_01が正本。[PROVISIONAL]

```ts
type PluginManifestV1 = {
  schemaVersion:1; pluginId:string; version:string;
  description:string; harnessApiRange:string; publisher:string; license:string;
  bundles: readonly {path:string; digestSha256:string; kind:"skill"|"recipe"|"hook"}[];
  requestedCapabilities: readonly string[]; // grantではない
  dependencies: readonly {pluginId:string; semverRange:string}[];
  signature?: {alg:"ed25519";keyId:string;value:string}; // allowed algorithm is proposed
};
type RecipeV1 = {
  schemaVersion:1; recipeId:string; version:string;
  inputSchemaRef:string; outputSchemaRef:string;
  steps: readonly {
    stepId:string;
    adapterToolRef:string; // only existing registered Gateway Tool ID
    inputBindings:Readonly<Record<string,string>>; // validated, no dynamic eval
    onFailure:"abort"|"partial"|"retry_if_safe";
    maxAttempts:number; timeoutMs:number;
  }[]; // acyclic graph as v1; no unbounded loops
  successEvidenceRequired:boolean;
};
type PluginHookV1 = {
  hookId:string;
  point:"before_task_plan"|"before_tool_prepare"|"after_tool_observed"|
        "before_result_publish"|"after_run_finalized";
  priority:number; maxDurationMs:number;
  effect:"advisory"|"deny"|"restrict"; // no grant/mutate/execute
  recipeRef?:string; // for deterministic plugin recipe precondition checks only
};
type PluginBinding = {
  pluginId:string;releaseId:string;scopeRef:string;
  state:"quarantined"|"awaiting_review"|"installed_disabled"|"enabled"|
        "suspended"|"revoked";
  recordVersion:number; // starts 1, CHAT_01 physical mapping provisional
  approvalDigest?:string;policyVersion:string;
};
type HookResult = {
  decision:"continue"|"deny"|"restrict";
  constraints?:readonly string[];     // restrict tool allowlist only
  reasonCode?:string;
  metadata?:Record<string,string>;    // safe bounded key/value
};
```

**Manifest validation**: canonical UTF-8; allowlisted keys only, `pluginId`はnamespace + lower kebab、SemVer/harnessApiRangeはtrusted parserで検証しcycle reject。総Bundle≤1MiB、recipe steps≤32、hooks≤8、dependency depth≤4、linked resources≤64、nested zip/symlink/absolute/`..`禁止（**暫定初期値**）。スクリプト/評価式/任意URL/`eval()`はSchemaでreject。Input bindingは`$task.field`等のallowlisted JSON Pointer/pathだけ、値は必ずSchema検証。Unknown critical extension reject。署名はtrusted key ringでcanonical digest検証、未署名はpolicyによりreview/deny、署名が権限を与える訳ではない。

**能力計算**: `Actor grants ∩ Tenant policy ∩ Task/Run grants ∩ Tool registry capability ∩ Plugin approved requests ∩ Hook restrictions`。後段でTool Gatewayが承認フラグ、idempotency、side-effect ledger、egress、rate/cost、authを毎回最終判定。PluginにpolicyToken/approvalTokenを文字列で渡さない。

### 5.1 Hook deterministic order

1. 認証済Principal＋scope/Run fenceとbudget確認（**Hookより先**。CHAT_09/02）
2. ToolのSchema validation＋typed arguments正規化（CHAT_03）
3. Trusted Tool Gateway policy+approval precheck（CHAT_03/09）
4. `before_tool_prepare`: enabled Hookのpriority昇順、同点は`pluginId`辞書順・`hookId`辞書順、各Hookの読取専用snapshotを独立に評価。`deny`が一つでもあればdeny、`restrict`はintersection、`continue`は無影響。
5. **Gateway最終policy/approval/epoch再チェック**（Hookを通過したことだけでTool実行不可）
6. Tool intent+ledger precommit、executor execute、Evidence/Observationを永続保存（CHAT_03/01/10）
7. `after_tool_observed`は**保存済Observationのredacted metadataのみ**受け、アドバイス/検証警告を返す。Tool output改変・再実行・秘密送信不可。
8. `before_result_publish`で証拠不足ならpublishをreject/partial提案できるが、最終判定はCHAT_02 Result Verifier。`after_run_finalized`はログ用read-only通知、失敗がRun終端状態を変更してはいけない。

Void/Advisory Hook失敗は`HOOK_DEGRADED`で継続可能、Security/Policy-sensitive Hook失敗は**fail closed**。Hookのpluginがdisabled/revokedなら新しいmodel call/tool prepareには即不適用。in-flight post hookは古いversionに縛られた可読metadataのみ処理、更新不可。

## 6. シーケンス / state

```text
Source -> Quarantine -> Manifest validated -> Review pending
              |                |                 |
           invalid          denied            approve exact digest
              v                v                 v
           REJECTED         REJECTED       INSTALLED_DISABLED
                                                  |
                                             User enable (CAS)
                                                  v
                                               ENABLED
                                               /     \
                                    disable/revoke   staged update
                                          |               |
                                          v               v
                                      SUSPENDED      REVIEW new digest
                                                          |
                                                       cutover (CAS)
                                                          v
                                                       ENABLED vN+1
```

**Hook順序は登録更新で変わらない**：Run開始時にregistered Hook set digestをsnapshot pinし、Tool直前にenabled/policy/`leaseEpoch`/`goalRevision`をrevalidate。動的Plugin更新中でも別のhookSetが混ざらない。disableは「次のToolの直前」から強制。hookSet version changeは安全境界で再planまたはfail closed。

Recipe実行：Goalから候補検索→compatibility & user scope検証→approvalが必要な新外部副作用はCHAT_03へ→実行順序のDAG topo sort→各stepでTool Gatewayを呼ぶ→結果をEvidence refとして保存→全step結果/Verifier決定でsuccess/partial/failure確定。Unknown tool/permissionがあれば必ずabort。New TaskはCHAT_02/01正本。Hooksが勝手にTaskをcreate/Watchをinstallしない。

## 7. Pseudocode

```ts
function orderedHooks(hooks, point, pinnedDigest) {
  const eligible = hooks.filter(x=>x.point===point && x.installedDigest===pinnedDigest);
  return eligible.sort((a,b)=>a.priority-b.priority || a.pluginId.localeCompare(b.pluginId)
    || a.hookId.localeCompare(b.hookId));
}
async function prepareWithHooks(principal, task, toolCall, hooks) {
  await gateway.verifyPrincipalSchemaPolicyAndBudget(principal,task,toolCall);
  let allowed = await gateway.currentApprovedToolSet(principal,task);
  for (const hook of orderedHooks(hooks,"before_tool_prepare",task.hookSetDigest)) {
    const current = await registry.verifyEnabledAndPolicy(principal,hook);
    if (!current) throw SuspendedHook(); // no stale enable
    let d;
    try { d = await withBoundedDeadline(hook.maxDurationMs,
      hookExecutor.evaluatePureRule(hook,redactedReadOnlyContext(task,toolCall))); }
    catch { throw SecuritySensitiveHookUnavailable(); } // fail closed
    if (d.decision==="deny") throw ToolDenied(d.reasonCode);
    if (d.decision==="restrict") allowed = intersection(allowed,d.constraints ?? []);
    // Never accept new permission or modified tool args
  }
  return gateway.prepareAndExecuteWithFinalPolicy(principal,task,toolCall,allowed);
}
async function runRecipe(principal,task,recipe) {
  assertAcyclicAndBounded(recipe);
  for (const step of deterministicTopologicalOrder(recipe.steps)) {
    await assertRunFenceGoalRevisionAndCancel(task);
    const tool = await toolRegistry.requireRegisteredTool(step.adapterToolRef);
    const argumentsTyped = strictResolveBindings(step.inputBindings,task);
    // Failure retry only if Gateway confirms retry-safe, same idempotency key.
    const result = await toolGateway.prepareAndExecute(principal,task,tool,argumentsTyped);
    await recordEvidenceReference(result);
    if (!result.ok && !gateway.isSafeRetry(result)) return choosePartialOrAbort(step);
  }
  return resultVerifier.verify(task); // never skip verifier
}
```

Hook APIはJSON data-only、プロセス/リモートRPCで任意コード実行しない。`HookResult.constraints`に実在しないToolが含まれても権限は増えない。`after_tool_observed`が「別のURLへPOSTしろ」と返しても実行しない。HookのErrorは共有Error Taxonomy正本へ変換。[PROVISIONAL: CHAT_01]

## 8. 障害マトリクス

| Failure / race | Recovery・影響 |
|---|---|
| Plugin install ACK lost | actor+idempotency receipt再照会、二重有効化なし |
| Plugin digest changes between approval and enable | CAS＋digest mismatch拒否、新しいReview |
| Two concurrent updates v1→v2/v3 | recordVersion CAS、一方Conflict、v1/v2混在なし |
| DB commit then worker killed | transaction outbox replay、Tool前最新policy照合 |
| Hook timeout before Tool | fail closed（security-sensitive）、executable 0 |
| Advisory hook timeout after Tool | metadata warningのみ、Run state/Toolを復元しない |
| Hook adds grant/changes arguments | Schema reject、audit + disable suspected plugin |
| Plugin revoked mid-run | 次stepのTool Gateway prepareを拒否/再plan |
| Recipe step lost ACK with side-effect uncertain | Gateway Ledger状態照会; 非冪等なら自動再送禁止・Needs approval |
| Recipe cancellation after step | 新step start 0; already confirmed result Evidence remains |
| Steer after plan created | goalRevision mismatch→再plan/partial、旧条件で完了しない |
| Migration invalid among valid plugins | 失敗pluginを検疫、他plugin transaction独立、旧bundle維持 |
| Secret provider unavailable | plugin cannot read fallback plaintext、Waiting input/degraded |
| SSRF/untrusted plugin import | Quarantine; no private network request、explicit rejection |
| Hook calls itself recursively | no re-entrancy, max depth1、bounded budget error |
| Restore resurrects revoked plugin | tombstone epoch check/Policy revalidate、旧bundleは無効 |

## 9. Security / Supply chain

Signed artifactは本人確認やtrust手段の一部、権限付与と無関係。Publisher ID/Plugin ID偽装、case-fold collision、path/symlink escape、prototype pollution（`__proto__`/`constructor` property）、plugin schema downgrade/unknown critical fields、dev package spoof、transitive dependency tamperingを負のテストに含める。MCP宣言もTool Registryを経由しHost Shellを提供しない。

Prompt Injectionはmanifest/recipe/source textにも存在。モデル文脈では`untrusted_plugin_instructions`のdata-onlyにし、Policy/Approval/Secrets/identityを変更不可。PluginのhookはEvent/Tool output書換や任意Network送信不可（v1 pure rule evaluator）。Secret handleの発行とProvider接続はCHAT_05/09で行い、Pluginにもユーザー認証CookieやOAuth token原文を渡さない。導入元に規約/ライセンスを確認、公開Marketplace自動インストール・勝手なUpdateなし。

## 10. Metrics / upper bounds

暫定限度：manifest64KiB、bundle1MiB、recipes32steps、hooks8/plugin、hook evaluation≤100ms/件（security-sensitive timeoutは停止）、total hooks≤16/Tool、combined hook budget≤500ms、dependency depth4、source URI redirect≤3（CHAT_04/09が検証）、retry2回（safe only）、1 Task recipe steps≤32、同一run plugin upgrade0。上限は設計案、性能測定値ではない。

`plugin_install_outcome`、`plugin_signature_verdict`、`hook_denied_total`、`hook_timeout_total`、`recipe_gateway_denied_total`、`plugin_migration_fallback_total`、`plugin_reload_epoch`。Traceはdigest/version/actor ref/hook ID/result categoryで相関し、生本文・Credentials・Cookieは出さない。Benchmarkは同一fixture+Plugin数、Hook有無、p50/p95、Token/Cost、不可逆side-effect重複数を測定。

## 11. Persistence / Migration [CHAT_10 proposal]

`plugin_releases` immutable digest/manifests/licenses/security status; `plugin_bindings` per principal/tenant/owner version/CAS (1開始); `plugin_approvals` exact digest + Actor + policyVersion + TTL; `plugin_recipe_versions` with step refs; `plugin_hook_declarations` with priority/phase/constraints hash; `plugin_operations_receipts` with request fingerprint; `plugin_outbox_events`; `plugin_revocation_epoch` for restoring read barrier。物理DDLはCHAT_10所有、別MDで定義しない。

Migration: version N reader+validator→quarantine N+1→validate known fields/migrate settings in sandbox pure function→new release approval→atomic binding cutover→old version retained for rollback but credentials/permissions invalidated。migration失敗は旧configを変えずblocked/degraded。Backup restoreはrevocation epochとissuer signature trustを再検証。UUIDv7/recordVersion=1はCHAT_01 draft、DBのv4/0とmapping不一致あり、別Owner修正が必須。

## 12. HTTP/UI [CHAT_08 proposal]

`GET /plugins` metadata、`POST /plugins/stage`、`POST /plugins/{id}/approve`、`PATCH /plugin-bindings/{id}` with If-Match、`POST /plugin-bindings/{id}/rollback`、`GET /recipes/{id}/dry-run`、`GET /plugin-hook-audit`（redacted）。これらは**正式Routeでない**。UIではPublisher、ライセンス、内容digest、version/compatibility、Capability request diff、Hook拡張ポイント、ユーザーscope、承認期限、error/retry可否を出す。

## 13. Implementation PR sequence

PH-1 manifest/recipe/hook JSON validators、normalizer、negative security fixtures。
PH-2 read-only quarantine importer/egress tests/digest verification。
PH-3 immutable registry/bindings/CAS/idempotency/outbox/migration。
PH-4 policy-first HookRunner with pure evaluator and stable order、Gateway integration。
PH-5 bounded Recipe Runner with Tool ledger / uncertain ACK / cancel / Steer tests。
PH-6 Review UI rollout, rollback, restore, benchmark and operator incident controls。

## 14. Tests（すべて設計のみ・未実行）

| TEST | setup/action → expected |
|---|---|
| PH-T01 | manifest wrong digest/unknown major/bad SemVer→reject |
| PH-T02 | stage vs installed vs enabled → allow only reviewed exact hash |
| PH-T03 | Plugin Tool requested grant_admin→Gateway grant unchanged |
| PH-T04 | two tenant same plugin ID different config → isolation; operator plugin immutable |
| PH-T05 | hooks shuffled priorities→stable order; deny sticky; timeout fail closed |
| PH-T06 | recipe step calls HTTP directly/shell→schema rejects; Gateway only |
| PH-T07 | approval before payload tamper→digest mismatch rejects |
| PH-T08 | crash after install/duplicate request ID→one binding, no double side effect |
| PH-T09 | malicious symlink/prototype pollution/metadata IP redirect→quarantine, no SSRF |
| PH-T10 | post-observation gets cookie+prompt injection→redacted metadata only |
| PH-T11 | Steer/Cancel/expired leaseEpoch on pending recipe→new Tool start 0 |
| PH-T12 | before hook returns deny, after hook returns POST command→Tool start 0/side-effect0 |
| PH-T13 | migration valid+invalid neighbor → valid migrates, invalid quarantined, no data loss |
| PH-T14 | hook recursion/depth/16 hooks budget→finite bounded stop |
| PH-T15 | telemetry scan for API key/Cookie/PII→secret log 0 |
| PH-T16 | plugin dependency graph/capability request diff→UI supplies exact proposal |
| PH-T17 | plugin worker sandbox experiment→disabled in v1 |
| PH-T18 | Gateway Tool returns uncertain ACK after side effect→no blind retry, needs review |
| PH-T19 | restore older revoked plugin config→remains disabled/revoked |
| PH-T20 | named X event recipe attempts unauthorized Watch create→Gateway rejection (SC-11) |

## 15. Traceability

| REQ | Tests | SC | Pass |
|---|---|---|---|
| PLG-001 | T01 | 05/08 | strict manifest |
| PLG-002 | T02 | 07/08 | install/enable separate |
| PLG-003 | T03 | 07/08 | grant 0 |
| PLG-004 | T04 | 06/08 | tenant isolation |
| PLG-005 | T05 | 07/08 | ordered deterministic |
| PLG-006 | T06/T18 | 03/08 | Gateway-only |
| PLG-007 | T07 | 05/08 | digest bound |
| PLG-008 | T08/T19 | 07 | CAS/idempotent |
| PLG-009 | T09 | 08 | no SSRF |
| PLG-010 | T10 | 03/06/08 | secret-safe |
| PLG-011 | T11 | 07 | no stale Tool |
| PLG-012 | T12 | 07/08 | deny sticky |
| PLG-013 | T13 | 07 | reversible migration |
| PLG-014 | T14 | 09 | finite budget |
| PLG-015 | T15 | 06/08 | no Secret leak |
| SHOULD 016 | T16 | 05/09 | diff shown |
| EXPERIMENT 017 | T17 | 09 | not enabled |

## 16. Cross-team Proposals / open blockers

| Owner | Severity | 提案/受入条件 |
|---|---|---|
| CHAT_01 | P0 | PluginId/RecipeId/Event schema UUIDv7、Run leaseEpoch/goalRevision、recordVersion1開始 |
| CHAT_03 | P0 | Tool Gateway policy-first execution + side-effect ledger, capability intersection and uncertain ACK handling |
| CHAT_09 | P0 | plugin signer/key trust, execution deny, SSRF policy, Secret handle boundary |
| CHAT_10 | P0 | physical DDL/outbox/install receipts/epoch/rollback, workspaceId mapping to tenant/owner, v4/0 contradiction |
| CHAT_02 | P1 | Recipe plan and Hook Run fence admission/Cancel/Steer checkpoints |
| CHAT_05 | P1 | Plugin descriptor vs external API Connector Adapter/credential lifecycle |
| CHAT_04 | P1 | Browser replay code stays browser-owned, plugin is not browser tab owner |
| CHAT_07/13 | P1 | Event-driven X Watch policy, Hook cannot create Watch without approval |
| CHAT_08 | P1 | Review/capability diff/Hook audit UI schema |
| CHAT_11 | P1 | Duplicate/SSRF/prompt injection/timeout/unsafe retry tests |
| CHAT_06 | P1 | 06-03 Skill release pin, 06-04 learned recipe promotion are separate review flows |

### Upstream PR evidence and compatibility caveats (2026-10-30)

[OpenClaw PR #173178](https://github.com/openclaw/openclaw/pull/173178) discusses JSON5 manifest parsing compatibility, while [OpenClaw PR #158601](https://github.com/openclaw/openclaw/pull/158601) proposes purging rejected workshop candidates with ownership/revision guard. Both were **open/unmerged at inspection**; PR body is *Issue/PR report* and not source-code-verified accepted behavior. This Harness deliberately chooses strict canonical JSON for the v1 wire manifest even if upstream supports different local formats, with an explicit import parser/migration rather than silent acceptance. Any rejected candidate purge must pass owner/scope, CAS and dependent artifact retention checks.

**Shared contracts:** CHAT_01 PR #23 event envelope describes BIGINT counters as canonical decimal strings; DB/JS adapters must not convert Run leaseEpoch from arbitrary BIGINT to lossy JS numbers. CHAT_03 PR #20 requires policy-first approval with **fresh Run leaseEpoch fence on execution**; do not bind a pending approval irrecoverably to an obsolete lease if resumed Run reclaims a new epoch, but only the current epoch can dispatch. These remain [PROVISIONAL] until owner review.

### Official upstream documentation cross-check

[OpenClaw Hook reference](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/docs/plugins/hooks/reference.md) defines multiple hook classes and descending priority with registration order for ties; [Tool policy Hook documentation](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/docs/plugins/hooks/tool-policy.md) documents parameter rewrite/approval/exec-env capabilities. This Harness **deliberately differs**: only pure deny/restrict/advisory rules, ascending priority and deterministic PluginId/HookId tie-break, no request rewriting/exec-env from untrusted plugins. This difference is a conscious independent safety contract, not a claim about upstream behavior. [Deep Agents skill documentation](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/openwiki/concepts/subagents-skills.md) distinguishes tool visibility and tool authority (Docs verified). Existing Skill/Plugin integrations cannot bypass CHAT_03 policy even if a tool is presented to the model.

## 17. Sources / grades

- [S1] [OpenClaw hooks](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/plugins/hooks.ts), [pre-Tool policy](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/agents/agent-tools.before-tool-call.policy.ts), [plugin manifest tests](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/plugins/manifest-registry.test.ts), [migration tests](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/commands/doctor-plugin-manifests.test.ts) — **Code verified**, 2026-10-30。MIT。
- [S2] [Deep Agents plugin skill adapter](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/code/deepagents_code/plugins/adapters/skills.py), [Skill Tool tests](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_skill_tools.py) — **Code verified**, MIT。
- [S3] [Hermes approval](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/write_approval.py) — **Code verified**, MIT。
- [S4] [Project Brief](https://github.com/choko510/aiagent-md/blob/b45d4e989998663a00ef09673e69ae1f4191f00d/docs/00-foundation/project-brief.md) / [SC-01..16](https://github.com/choko510/aiagent-md/blob/b45d4e989998663a00ef09673e69ae1f4191f00d/docs/00-foundation/acceptance-scenarios.md) — **Docs verified**。
- [S5] Harness Hook Contract、schema、quota、DB/API、execution orderは **Hypothesis/Proposed design**, 実測・OSSテスト実行・独立安全審査は**未実施**。OpenClawのHookやHost Installerの存在を本Harnessでの採用・安全保証と混同しない。
