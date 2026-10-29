# Skill Lifecycle — Detailed Implementation Specification

> CHAT_06 | `docs/06-memory-skills/skill-lifecycle.md` | **Draft / Provisional** | 2026-10-30 | main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Dependencies: 06-01 memory, CHAT_01/03/09/10, 06-05 plugin boundary

## 0. 結論と採用案

Skillは**バージョン固定された、読み取り可能な作業手順パッケージ**であり、Tool/Plugin/Secret/Roleではない。検索→候補取得→検疫・内容検証→review→install→enable→version pin→disable/update/rollbackの状態をDB正本で扱い、Gatewayが承認したTool一覧だけを使用する。SKILL.md内容、manifest中の`allowedTools`、依存宣言は**実行能力の要望**であり権限付与ではない。導入はホストにパッケージ管理コマンドや任意Shellを実行させない。高度なSkillのScriptsは実行不可が初期値。将来の限定sandbox runnerはCHAT_03/09の別審査で許可。

Skillの小さな名称/説明は発見用index、本文は要求時のみlow-trust dataとしてModelへ。大規模な一括prompt注入を禁止。Memory`procedural_hint`のテキストがSkillそのものに昇格することはない。ライブラリ固有のHookをそのままHarnessへ取り込まない。

## 1. 目的・非目標 / シナリオ

用途はEC/ホテル/X/API/一般Web調査に共通する再利用手順（「税込総額を比較する」「複数ソースと日付を照合する」）を可搬に保ち、ユーザーが選択・無効化・ロールバックできるようにすること。SC-01/03/04/05/06/07/08/09、SC-13（学習したread-onlyブラウザ操作は別Ownerの検証を通る）、SC-16（フィードバックからSkill候補作成は直接有効化しない）。

非目標：Hermes/OpenClawをforkすること、依存コードをホストで実行すること、Skillを独立Tool Endpointとして自動公開すること、SkillがApprovalやOS権限を変えること、User自作Skillでoperator設定を変更すること。

## 2. OSS比較：実ソース/テスト（Code verified、実行は未実施）

| OSS / commit | 実際に読んだ関数/テスト | 取り入れる点／採用しない点 |
|---|---|---|
| Hermes Agent `fcc26fbd9f1641755e49793d7ed608f3b7214c3d` MIT | [`tools/skills_tool.py`](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/tools/skills_tool.py) `skills_list`（概要）、`skill_view`（本文+linked files）、`_skill_catalog`、`_skill_readiness`、30秒catalog cache | Progressive disclosureに学ぶ。現在のHermes RuntimeやSkill package形式を直接依存にしない |
| Deep Agents `3efa098130d6670fe713f841072bc11ff40545da` MIT | [`libs/deepagents/deepagents/middleware/skills.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/deepagents/middleware/skills.py) `SkillsMiddleware`, `_validate_skill_name`, `_parse_allowed_tools`, `_parse_skill_metadata`, `_list_skills`, `wrap_model_call`, `wrap_tool_call` | metadata validationとbackend-based sourceを参考、Skill宣言がtool grantに変わることを厳禁 |
| Deep Agents CLI（同commit） | [`libs/code/deepagents_code/skills/load.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/code/deepagents_code/skills/load.py) `list_skills`/`load_skill_content` | CLI filesystemを本製品の権限境界へ持ち込まず、DB/catalog serviceで実装 |
| OpenClaw `aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3` MIT | [`src/gateway/server-methods/skills.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/gateway/server-methods/skills.ts) `skillsHandlers`、[`src/skills/lifecycle/install.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/skills/lifecycle/install.ts) `installSkill`、[`src/skills/lifecycle/install.test.ts`](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/src/skills/lifecycle/install.test.ts) `installs...`, `blocks install when before_install rejects...` | Gateway管理・install安全審査テストを参考。現行コードはパッケージ管理コマンド実行機能を含むが、**本HarnessではHost installを全面却下** |

ライセンス確認：Hermes/Deep Agents/OpenClawのroot LICENSEはMIT。上記commit/tagsは**branch head SHA調査値**であり特定Release tagとの一致は未検証。公式機能を自作のpermissionとして想定しない。Issue/PR追加調査は関連security issueの精査を後続review項目とし、未確認の発生率等を主張しない。

### 2.1 比較選択

A. Agentに`SKILL.md`を自動ダウンロードさせ、依存の`npm/pip/apt`をHostへ導入：**却下**。Supply chain/egress/権限を一意に保証不能。
B. SkillをToolと同一視しfrontmatterだけでcapability付与：**却下**。Prompt injectionで実行権限が増える。
C. **Content-addressed immutable Skill Bundle + Registry + explicit user/owner approval + runtime capability intersection：採用**。代価は運用/審査コスト増だが権限と再現性を維持。

## 3. 所有権/境界/依存図

```text
[Discover provider / import] --- untrusted files ---> [Quarantine: SkillService]
                                                       | parser / digest / scanner
                                                       v
                                                [Candidate immutable bundle]
                                                       | approval CHAT_09
                                                       v
                                         [Registry + version catalog CHAT_10]
                                                       | enable binding
                                                       v
                                    [ContextLoader CHAT_02] (text, not authority)
                                                       |
          [Runtime task] ---------> [Tool Gateway CHAT_03] ------> [Approved executor]
                                      tenant policy ∩ skill request ∩ run policy
```

Skill bundle/activationの意味論はCHAT_06、shared ID/EventはCHAT_01、Tool permissionの正本はCHAT_03/09、plugin provider/connector integrationはCHAT_05、DB/Artifact StoreはCHAT_10、UI/APIはCHAT_08、watch triggeringはCHAT_07。本MDのパッケージ/actorの型はmodule-privateで共通仕様を奪わない。

## 4. Requirements

| ID | Level | 条件 | SC |
|---|---|---|---|
| SKL-LIFE-001 | MUST | bundled manifestのstrict validation + immutable digest | 05,08 |
| SKL-LIFE-002 | MUST | discoveryとinstallとenableを別状態で管理 | 05,07 |
| SKL-LIFE-003 | MUST | Skill instructionsを非信頼dataとして扱いTool実行権限を与えない | 03,08 |
| SKL-LIFE-004 | MUST | tenant/owner/scope隔離、operator共有Skillは一般Userが変更不可 | 06,08 |
| SKL-LIFE-005 | MUST | install/enable/update/rollbackに明示User承認とimmutable receipt | 07,08 |
| SKL-LIFE-006 | MUST | Tool要求はGatewayの現行許可との積集合に限定 | 07,08 |
| SKL-LIFE-007 | MUST | 有効Task/RunはSkill version pin、途中更新は次安全境界 | 07 |
| SKL-LIFE-008 | MUST | 同時install/retryをidempotentにする、CASで反対更新を検出 | 07 |
| SKL-LIFE-009 | MUST | 内容/script/URLをOS上で自動実行しない、untrusted importを隔離 | 08 |
| SKL-LIFE-010 | MUST | disable/revoke後の新規Skill loadと新規Tool intentを禁止 | 07,08 |
| SKL-LIFE-011 | MUST | digest / publisher / source / version / signature verification resultを保存 | 05,08 |
| SKL-LIFE-012 | MUST | rollout/rollbackで旧権限やSecretを復活させない | 07,08 |
| SKL-LIFE-013 | MUST | size/dependency-depth/cost/latency/tool countsに有限限度 | 09 |
| SKL-LIFE-014 | MUST | Skill link/readでpath traversal, symlink, remote SSRFを防ぐ | 08 |
| SKL-LIFE-015 | SHOULD | skill search/catalogをmetadataのみで実装し、本文はon-demand | 05,09 |
| SKL-LIFE-016 | EXPERIMENT | offline sandbox/Skill quality evalで採否を比較 | 09,13 |

## 5. I/O型・Validation・契約

```ts
type SkillManifestV1 = {
  schemaVersion: 1;
  name: string;                      // ^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$
  version: string;                   // SemVer, canonical
  description: string;               // 1..1024 chars
  entry: "SKILL.md";                 // fixed
  source: {kind:"builtin"|"user"|"registry"|"generated"; originUri?:string};
  requestedCapabilities: readonly string[]; // *request*, no grant
  dependencies?: readonly {skillName:string;range:string}[];
  compatibility?: {harnessApi: string; platforms?: string[]};
  files: readonly {relativePath:string; sha256:string; byteSize:number; mediaType:string}[];
  license?:string; publisher?: string; signature?: {alg:string; signature:string; keyId:string};
};
type SkillRelease = {
  id: string; // [PROVISIONAL: CHAT_01] UUIDv7
  manifest: SkillManifestV1;
  contentDigest: string;            // sha256 canonical manifest+content, no timestamps
  status: "quarantined"|"validated"|"rejected"|"retired";
  originTrust: "built_in"|"user_reviewed"|"third_party_untrusted";
  verifiedPublisher?: string;
  createdAt:string; validatedAt?:string;
};
type SkillInstallation = {
  id:string; releaseId:string; ownerScopeRef:string;
  desired:"disabled"|"enabled";
  effective:"not_installed"|"installed_disabled"|"enabled"|"suspended"|"revoked";
  version:number; // local CAS >=1, [PROVISIONAL shared recordVersion mapping]
  approvedByUserId?:string; approvalReceiptId?:string;
  policyVersion:string;
};
type SkillChangeCommand = {
  requestId:string; name:string; releaseId?:string;
  action:"stage"|"install"|"enable"|"disable"|"update"|"rollback"|"revoke";
  expectedInstallationVersion?:number;
  approvedReleaseDigest?:string; // must compare exact approved digest
};
```

**Validation**: strict manifest JSON/YAML allowlist、未知schema major reject、正規化済SemVer、namespaced name collisionは明示UI review。`SKILL.md`は最大例64KiB、bundle最大1MiB、dependency depth≤4/count≤16、linked files≤64、user install/updateは同時≤1 per skill（**暫定上限**）。ファイルはdirectory-normalized POSIX相対pathのみ。`..`、absolute、NUL、Unicode変種によるconfusable、symlink/hardlink/device、nested archives、binary executable禁止。hashは取得後、検疫後、enable直前に照合。未署名は`unverified`表示のうえreview対象、署名がある場合はtrusted keyとexact digestを検証し、署名は**権限付与の証拠ではない**。

**ManifestのrequestedCapabilities**が`tool:web.fetch`であっても、実権限は`Effective = actorGrant ∩ tenantPolicy ∩ taskRunPolicy ∩ gatewayToolPolicy ∩ declaredSkillAllowlist`で毎Tool直前に計算。Skillに新しいSecret/Connector tokenを渡さない。`requestedCapabilities`不在なら新権限なしで単に手順表示。Wrapper toolの自動登録は禁止。

## 6. 状態遷移 / シーケンス

```text
DISCOVERED -> QUARANTINED -> VALIDATED -> STAGED_FOR_REVIEW
                    |            |                 |
                    +-> REJECTED  |                 +--deny--> REJECTED
                                 |                 +--approve exact digest-->
                                 |                             INSTALLED_DISABLED
                                 |                                    |
                                 |                              enable (CAS)
                                 |                                    v
                                 |                                 ENABLED
                                 |                            /          \
                                 |                        disable       UPDATE
                                 |                          v             v
                                 |                  INSTALLED_DISABLED  STAGE(new immutable release)
                                 |                                    | approve+CAS
                                 |                                    v
                                 |                                  ENABLED(new pin)
                                 +------------------------------------------->
                                                    REVOKED (terminal release binding)
```

1. User/Operator検索 → metadata card（title/description/publisher/capabilities/price/licensing）だけ返す。
2. User import/install依頼 → Source fetchはGateway approved network adapter経由、Quarantine Storeの限定パスへ。Untrusted scripts一切実行なし。
3. manifest/schema/path/digest/virus/security scanner検証 → approved trust policy → candidate releaseを永続化。
4. UIにdiff/permission request/hashed contentsを提示 → approvedBy＋exact hash＋expiryを保存。拒否されたらinstall不可。
5. TransactionにInstallationとVersion/CAS/idempotency receipt+outbox保存。enabledは**別の意思決定**。
6. Task開始時にeffective installed version、policy version、requestedCapability hashをpin。条件変更/Steer/skill disable時は次のTool準備境界で再検査。
7. Updateは既存release上書き禁止、**新releaseの検疫/承認**を通す。Rollbackも戻すreleaseの現行Policy・Security Verdict再検証、旧Grantを継承しない。

## 7. 実装可能な擬似コード

```ts
async function stageSkill(auth, source) {
  const actor = requirePrincipal(auth); // not LLM-provided userId
  const bytes = await gateway.fetchSkillSourceAsData(source, boundedPolicy);
  const fileList = await isolatedArchiveInspector.scan(bytes, {
    maxFiles:64, maxBytes:1048576, rejectSymlinks:true, rejectDevices:true, rejectTraversal:true
  }); // never execute extracted content
  const manifest = strictValidate(fileList.get("SKILL.md"),fileList);
  const digest = sha256(canonicalBytes(manifest,fileList));
  const verdict = await security.scanBundle(manifest,fileList,actor);
  return registry.createQuarantinedRelease(actor,manifest,digest,verdict);
}
async function changeInstallation(auth, cmd) {
  const actor = requirePrincipal(auth);
  const approval = await approvals.validateExactDigestActorPolicy(
    actor,cmd.approvedReleaseDigest,cmd.action,cmd.releaseId);
  return registry.transaction(async tx => {
    const old = await tx.lockInstallation(actor,cmd.name);
    const prior = await tx.lookupIdempotency(actor,cmd.requestId);
    if(prior) return prior;
    if(old && old.version !== cmd.expectedInstallationVersion) throw Conflict();
    const release = await tx.getValidatedImmutableRelease(cmd.releaseId);
    assert(approval.matches(release.contentDigest));
    assert(currentPolicyAllows(actor,release,cmd.action)); // no auto permissions
    const next = applySkillTransition(old,release,cmd);
    await tx.writeInstallationAndReceipt(next,cmd);
    await tx.enqueueSkillCacheInvalidation(next);
    return next;
  });
}
async function planSkillToolExecution(actor,task,skillPin,toolCall) {
  assertCurrentRunFence(task.runId,task.leaseEpoch,task.goalRevision);
  assertActiveReleaseHashAndPolicy(skillPin);
  return toolGateway.prepare(toolCall, intersectCapabilityPolicies(actor,task,skillPin));
}
```

`source`のURIはSSRF/redirect/DNS rebinding checkを通るGatewayのみ。Activationは非冪等な外部行為と混同しない。Approval失効、Cancel、Steer、leaseEpoch変更時はstaged receiptを再検証。Network/Filesystemを直接Skillの手順で実行させない。

## 8. 故障マトリクス

| Failure | Handling / final state |
|---|---|
| Invalid source/unsafe archive | QUARANTINED→REJECTED、原本は短期隔離/保存期限後消去 |
| Digest changed between review & enable | reject + new approval、TOCTOU回避 |
| Retry commit ACK lost | same requestId receipt照会、二重installなし |
| Parallel install different versions | CAS競合、旧版維持、明示的な再選択 |
| Plugin removal/Tool grant revoked | Skill activeでもTool Gatewayでdeny、必要ならsuspend |
| DB crash after commit before cache refresh | outbox replay、read-time version/policy checkで旧release不使用 |
| Worker with old leaseEpoch | new tool intentを拒否、Taskは安全境界へ |
| Update fails after staging | 旧enabled release維持、partial stagingを掃除 |
| Rollback to vulnerable digest | current scan/policy拒否、旧承認復活させない |
| Skill disabled mid-Run | 次安全境界以降新規Skill-derived tool 0 |
| Source license/registry unreachable | cached immutable releaseの可否は現行Policy判定、最新保証をしない |
| Cost or recursion limit hit | install/resolve保留、unbounded lookupなし |

## 9. セキュリティ / privacy / trust

- Skill本文はSystem override不可。「プロンプトに書いてあるから購入」禁止。外部SkillにSecrets/credentialを渡さない。
- 署名/Publisher ratingはAuthenticityの補助、Authorizationを意味しない。悪意のあるregistry/README/linked fileのinstructionもuntrusted data。
- Source fetchはHTTP adapterのallowlist+egress検証、intranet/metadata IP block、redirect再検証。依存パッケージをホストshell/npm/pip/aptでinstallしない。
- Script実行要求はv1では`UNSUPPORTED_CAPABILITY`、将来はHardened Tool Gateway sandboxのみ。Skill install機能を任意コード実行代替にしない。
- Persistent tokens, webhook secrets, user browser CookieをSkill BundleやModel Contextに含めない。URL accessをUser毎に監査。
- User Skillとbuiltin共有Skillは異なるowner scope、Userがoperator shared Skillを書き換え不可。Tenant越境を拒否。
- 依存が未知/循环はreject、license terms/attributionを検証。Copyrighted codeの自動コピーなし。
- Outbox/監査に本文を記録せずdigest、actor、version、policy verdict、decision、timeのみ記録。

## 10. Limits / metrics / Benchmark

上限は暫定：bundle1MiB、SkillMD64KiB、linked files64、dependency depth4/count16、parallel installs per scope1、enable審査TTL<=24h、Skill一覧候補<=50、Model tool disclosure最大10件。実環境で性能・費用の合意が必要。

`skill_install_count{outcome}`, `skill_security_rejected_count`, `skill_version_conflict_total`, `skill_rollback_total`, `skill_active_runtime_fence_reject_total`。Benchmarkは同一ハードウェア・provider・prompt/Skill数でmodel token/lookup時間、Tool permission bypass数=0、race時の二重install数=0。実測未実施。

## 11. DB/Migration提案（CHAT_10）

Immutable `skill_releases(id UUIDv7, digest unique, manifest_json, source, scan_state, license)`、`skill_installations(scope,skill_name,release_id,recordVersion starting1, effective_state)`、`skill_approvals(exact_digest,actor,policyVersion,expiry)`、`skill_operation_receipts(scope,actor,requestId,payloadHash)`、`skill_invalidation_outbox`。**DDLではなく提案**。Backup restoreでもvulnerable/revoked digestを再enableしない。Deployment migration: schema validate→read-only registry→import metadata→review→enable opt-in、rollbackでは新Releaseレコードを消さずactive bindingだけ戻す。UUIDv7とCHAT_10のv4 default、recordVersion 1/DB revision0、workspace/tenant mapping不一致はP0。

## 12. REST/UIへの提案（CHAT_08）

`GET /skills/search`（metadataのみ）、`GET /skills/{id}/versions`、`POST /skills/stage`、`POST /skills/{id}/install`、`PATCH /skill-installations/{id}` with If-Match、`POST /skill-installations/{id}/rollback`、`POST /skill-installations/{id}/revoke`。**Routeの正本はCHAT_08、ここでは提案**。Review画面には出所・hash・差分・許可要求・license・署名検証/不明と対象範囲を表示し、Userによる同意を観測可能な記録にする。

## 13. 実装PR分割

SL-1 Immutable manifest parser/schemas（unknown field negative tests）。SL-2 Quarantine importer + digest/egress + limits。SL-3 Registry transactional release/install/version/receipt/outbox。SL-4 Approval/UI activation + bounded on-demand loader。SL-5 Tool Gateway permission intersection/run pin/disable race integration。SL-6 Rollback/migration/E2E fault security benchmarks。

## 14. テスト（すべて**設計のみ・未実行**）

| TEST-ID | Fixture/action → Expected |
|---|---|
| SL-T01 | Valid manifest 1、未知schema 2、不正SemVer/path→strict validation結果 |
| SL-T02 | Search/Stage/Install/Enable逐次→状態と承認なしenable0 |
| SL-T03 | `allowedTools: [shell, grant_admin]`を本文へ→Tool intent許可0 |
| SL-T04 | A/B tenant共有Skill参照とuser編集→Bのuser package不可、builtin編集拒否 |
| SL-T05 | Review digest Aの承認後content Bへswap→install拒否 |
| SL-T06 | Tool scope全部ALLOW/一部DENY/全部DENY→許可の積集合のみ |
| SL-T07 | Running task pinned v1、v2有効化→旧Taskは安全境界で再検証 |
| SL-T08 | Concurrent install100件、同requestId repeated→一つactive版+receipt |
| SL-T09 | tar symlink traversal/zip bomb/hidden executable→Quarantine reject、host exec 0 |
| SL-T10 | revoke/disableとin-flight Tool prepare交錯→新規Tool start 0 |
| SL-T11 | unsigned vs signed-with-wrong-key vs trusted exact digest→3種類の信頼表示/deny |
| SL-T12 | rollback before/after policy change→旧Grantを復活させない |
| SL-T13 | dependencies cycle/depth5/65files→error、無限loopなし |
| SL-T14 | URI redirect 169.254.169.254、SSRF→0 requests to forbidden IP |
| SL-T15 | metadata search 50、view selected1→他本文Promptへ漏れない |
| SL-T16 | sandbox experiment with mock runner→capability reject/rollback verdict記録 |
| SL-T17 | kill after commit before outbox delivery→after restart effective state consistent |
| SL-T18 | Skill instructions request purchase w/o approval→Tool Gateway rejects |
| SL-T19 | user feedback about a Skill→suggestion only、auto enable0 |

## 15. Acceptance / Traceability

| REQ | Test | Scenario | 合格条件 |
|---|---|---|---|
| 001 | T01 | 05/08 | invalid manifest全拒否 |
| 002 | T02 | 05/07 | stageとenableを混同しない |
| 003 | T03/T18 | 03/08 | capability増加0 |
| 004 | T04 | 06/08 | cross-tenant隔離 |
| 005 | T05 | 07/08 | digest binding |
| 006 | T06 | 07/08 | Gateway intersection |
| 007 | T07 | 07 | pinned state一貫 |
| 008 | T08/T17 | 07 | 二重install0 |
| 009 | T09 | 08 | host exec0 |
| 010 | T10 | 07/08 | revoke後新規実行0 |
| 011 | T11 | 05/08 | provenanceあり |
| 012 | T12 | 07/08 | 旧Grant復活0 |
| 013 | T13 | 09 | finite bound |
| 014 | T14 | 08 | SSRF/traversal0 |
| SHOULD 015 | T15 | 05/09 | Progressive disclosure |
| EXPERIMENT 016 | T16 | 09/13 | Offlineのみ計測 |

## 16. Open questions / Cross-team

| Owner | Priority | Cross-team proposal |
|---|---|---|
| CHAT_01 | P0 | Skill ID UUIDv7、`recordVersion=1`、Event envelope/Run fencing shared representation |
| CHAT_03 | P0 | Skill-requested capabilitiesとGateway現行Tool policyのintersection、Skill本文はgrantでない |
| CHAT_09 | P0 | Publisher trust, signature policy, approval TTL, tenant isolation, banned execution |
| CHAT_10 | P0 | immutable releases, per-scope install CAS, idempotency outbox, v4/0→v7/1 mapping |
| CHAT_08 | P1 | versions/install/approval/revoke/rollback UI、If-Match API |
| CHAT_14 | P1 | Reusable browser action replayはSkillとは別、検証済みread-onlyだけSkill候補になりうる |
| CHAT_06 (06-05) | P1 | Plugin recipe/hooksには別のcapability/policy boundary |
| CHAT_11 | P1 | malicious archive, duplicate install, stale Run, disabled capability E2E |

### Additional code/test coverage and upstream issue/PR status

[Deep Agents `test_skills_middleware.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_skills_middleware.py) includes explicit `test_validate_skill_name_invalid`, `test_parse_skill_metadata_invalid_yaml` and tool declaration tests. [`test_skill_tools.py`](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/libs/deepagents/tests/unit_tests/middleware/test_skill_tools.py) includes `test_call_before_any_read_is_rejected_without_running` and `test_registered_tool_wins_over_a_skill_tool_of_the_same_name` (Code verified, not run). Relevant external PR [Hermes #79108](https://github.com/NousResearch/hermes-agent/pull/79108) describes a proposed approval-gate fail-open repair; **PR discussion only, merge status not assumed**. Our product requires gate failure **fail closed** regardless of upstream state. 

## 17. Sources / confidence

2026-10-30、``## 2`` のHermes/Deep Agents/OpenClaw commit-pinned sourceとテストは **Code verified (static)**。Licenseの原本: [Hermes MIT](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/LICENSE), [Deep Agents MIT](https://github.com/langchain-ai/deepagents/blob/3efa098130d6670fe713f841072bc11ff40545da/LICENSE), [OpenClaw MIT](https://github.com/openclaw/openclaw/blob/aa65a0ed16aa4a0ba70a26a3e9cb647eefe985f3/LICENSE)。Release tag/installer/API/benchmarkの採用版は **Unverified**。上記Registry/Policy/limits/DDL/APIはHarness独自の**Hypothesis/Proposal**。OSSテストおよびHarnessテストは**未実行**。
