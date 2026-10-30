# Sandbox and Capabilities — Detailed Implementation Specification

> **CHAT_09 / 09-03** | Path `docs/09-security/sandbox-and-capabilities.md` | **Draft / PROVISIONAL** | 2026-10-30 JST | main `b45d4e989998663a00ef09673e69ae1f4191f00d` | Depends: CHAT_01/02/03/04/08/09/10/11 and PR #9 ownership reconciliation.
>
> **Goal**: Agentは汎用Web調査・抽出・計算を行うが、ホスト管理Agentではない。AgentがHost shell/SSH/Docker Socket/privileged host mountsを使用できないことを**promptではなくOS/process boundary**で保証する。上流OSSコードは読解したが本プロジェクトでの試験実施はなし。

## 0. 採用設計

**Capability-first deny-by-default**。Agentが呼べるのはCHAT_03のTool Gatewayで事前定義・登録されたデータ取得/ブラウザ操作/安全な計算/通知/承認申請のみ。Agent自身に任意`exec`、ホストファイル編集、SSH、Docker control API、任意Python/JSコード、任意CDP URL、設定変更・ネットワーク権限変更を渡さない。数値計算や表変換は**仕様に固定されたdeterministic function**、複雑な隔離計算だけを短命のrestrictive compute sidecarに許す。

**Process boundaries (proposal)**:
- API/Web UI: 認証終端、Tool GatewayへのCommand登録のみ。Docker Engine API credentialsなし。
- Agent/queue Worker: Toolのintent作成はできてもexecutor endpoint直叩き不可。Vault keyなし、host mountsなし、network egressはGateway経由。
- Browser Worker: browserを一元所有、`BrowserContext` tenant/owner隔離、Chromium sandbox = trueで強制。Browser driverが承認済みのUser Takeover leaseを検証。CDPは外部/Agentへ公開しない。
- Compute Worker: input artifactをread-only scopedで読み取り、wasm/strict interpreterまたは隔離コンテナで定義済み変換のみ。general shell、docker command、pip install、`import os` / `child_process` / filesystem/networkは禁止。
- Credential broker: 接続先を限定した認証injectだけ。AgentとCompute processから到達不能。

Docker Composeはoperator（人間）が構成・更新する。Agentのtoolsで起動/停止/privileged化することは**禁止**。rootless modeや`userns-remap`は推奨される追加対策でありホスト攻撃完全防御の保証ではない。

## 1. Scope / assumptions / acceptance

対象: container/OS process isolation、seccomp/AppArmor/user namespace/capabilities、bind mount、filesystem/CPU/RAM/PID/open files、Browser takeover、Tool/MCP privilege、untrusted code/file inputs、recovery/race。対象外: Kubernetes/host fleet運用、任意host adminやremote SSH、Linux kernel exploitを完全に防ぐこと、任意パッケージの実行保証、モデルが作ったコードの自動ビルド。SC-06/07/08必須、SC-09費用、SC-13 learned browser action replay、SC-15 multimodal document safetyも該当。

## 2. Existing OSS source audit

| OSS / ref & license | Code & tests read | Verified observation / adaptation |
|---|---|---|
| OpenClaw `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, MIT | `src/agents/sandbox/validate-sandbox-security.ts` `validateSandboxSecurity`, `validateBindMounts`, `validateNetworkMode`, `validateSeccompProfile`; `src/security/audit-sandbox-docker-config.test.ts`; `src/agents/sandbox/docker.test.ts` | 実装が`/etc,/proc,/sys,/dev,/run/docker.sock`等の危険mountをdenyし、network/security profileを検査。**denylistのパターンは参考**だが本件はより厳しい*allowlist only / no host mounts*を採用 [S1–S3] |
| Playwright `38fb53bf4c181abb91f1ca2a2ab741983c5d177c`, Apache-2.0 | `packages/playwright-core/src/server/chromium/chromium.ts`: `options.chromiumSandbox !== true`なら`--no-sandbox`を付ける; `tests/mcp/config-resolve.spec.ts` Chromium sandbox flagのresolved設定を試験 | **重要:** Playwrightを使うだけでChromium sandbox有効とは限らない。必ず明示`chromiumSandbox: true`で起動し、無効ならBrowser Worker起動を拒否する [S4–S5] |
| Docker Engine docs / OCI | Linux seccomp/capabilities/user namespace/resource flags、non-root実行を参照 | **Docs verified**。Docker daemonの実ファイルは未調査、configでの安全性はDeploy testに委譲 [S6] |

**Rejected designs**: A) `--privileged` + promptで危険行為を抑止: **拒否**。B) Agentにdocker.sockを渡し一時コンテナで任意コード実行: **拒否**。C) 単一の高権限browserプロセスを全ユーザー共有しSession IDだけで区別: **拒否**。D) wasm/定義済みdeterministic function + 隔離BrowserWorker、必要な計算のみsandbox service: **採用**。E) MicroVM per task: 隔離強度は高いが起動・RAM・運用を実測してから採用する`EXPERIMENT`。

## 3. Dependency and authority map

```text
User/web client -> AuthN/AuthZ (CHAT_09; tenant/workspace binding) -> Task/Run state (CHAT_01/02)
LLM proposals (UNTRUSTED) -> CHAT_03 Schema/Policy/Approval/Budget/Gateway
                          | -> read-only fetch (network egress policy)
                          | -> Browser Worker (CHAT_04, CDP private, Context owned)
                          | -> deterministic compute (isolated pure function)
                          | -> notification via Outbox (CHAT_07)
Operator -> Compose/build/deploy / config registry
Agent/Browser/Compute  -X-> Docker socket / host shell / SSH / privileged mount / Key Store
```

Scope of permissions is immutable `principal.ownerUserId + tenantId + taskId + runId + goalRevision + leaseEpoch + permittedTool + argsHash + expiresAt`; abstract conceptual grant, canonical Tool contract by CHAT_03 and Run lease type by CHAT_01/02 (Round2 unresolved). Browser Takeover ticket is a distinct human lease; human privilege cannot be turned into OS shell access.

## 4. Requirements

| REQ | Priority | Testable behavior | SC |
|---|---|---|---|
| SBOX-001 | MUST | Agent/Tool/MCPにhost shell/SSH/docker.sock/host admin APIを公開しない | 06,08 |
| SBOX-002 | MUST | Workerはunprivileged user/UID, no-new-privileges, cap-drop ALL, host namespace共有なし | 08 |
| SBOX-003 | MUST | host filesystem/bind mounts/secret key mountsをAgent/Browser/Computeに与えない。必要なstoreはscope API経由 | 06,08 |
| SBOX-004 | MUST | Browser Worker起動時Chromium sandbox=true、失敗したらfail closed、`--no-sandbox`不可 | 06,08,13 |
| SBOX-005 | MUST | BrowserContext/Page/Takeoverはowner/task/lease generationごとに許可、他ownerのstate/CDPを拒否 | 06,08 |
| SBOX-006 | MUST | モデル生成Python/JS/SQL/Codeを権限のあるホストプロセスで直接実行しない | 08,15 |
| SBOX-007 | MUST | 計算実行は明示的登録済みfunctionとtypes/schema、CPU/RAM/time/files/output/network禁止/上限 | 07,09 |
| SBOX-008 | MUST | Tool Gatewayを通さないexecutor呼び出しと権限昇格/side effectを禁止、承認再検証必須 | 07,08 |
| SBOX-009 | MUST | Worker crash/restart/leaseEpoch変化で旧ownerのgrant/Browser leaseが新実行に転用不可 | 06,07 |
| SBOX-010 | MUST | MCP server/tool descriptor・外部contentをauthorityとは扱わず、capabilitiesは静的policyに閉じる | 08 |
| SBOX-011 | MUST | network egressとDNS/redirectは別のSSRF policyに従い、bridge経由LANへの直通禁止 | 08 |
| SBOX-012 | MUST | artifact/media/PDF/ZIP等の入力サイズ、展開比、処理時間/同時数をboundedにする | 08,09,15 |
| SBOX-013 | MUST | deployment configを起動前audit、設定変更で権限緩和した場合拒否し監査 | 08 |
| SBOX-014 | MUST | Browser sandbox不成立やresource limit時は`BLOCKED_UNSAFE_RUNTIME`、権限落とさず続行しない | 06,08 |
| SBOX-015 | SHOULD | rootless Docker/userns + seccomp/AppArmor read-only rootfs/tmprfs等を硬化基準としCI負テスト | 08 |
| SBOX-016 | EXPERIMENT | untrusted computeをWASM/Firecracker等に差替え検証し負荷を比較 | 09 |

## 5. Internal contract and deployment profile (CHAT_09 security policy; CHAT_10 owns actual Compose)

```ts
type ExecutorKind = "fetch"|"browser"|"pure_compute"|"outbox_notification";
type CapabilityBinding = Readonly<{
  tenantId:string; ownerUserId:string; taskId:string; runId:string;
  goalRevision:string; leaseEpochDecimal:string; registryToolId:string;
  allowedActionHash:string; deadlineAt:string; maxCpuMs:number;
  maxWallMs:number; maxMemoryBytes:number; maxOutputBytes:number; expiresAt:string
}>;
interface SandboxAdmission {
  admitExecutor(binding:CapabilityBinding, actualProcess:AttestedWorker):Promise<ScopedOperationLease>;
  auditDeployment(profile:NormalizedDeploymentProfile):DeploymentAuditResult;
}
type DeploymentAuditResult = {accepted:true, policyVersion:string}
  | {accepted:false, code:"UNSAFE_CAPS"|"HOST_MOUNT"|"NETWORK"|"UID"|"BROWSER_SANDBOX"|"UNBOUNDED_LIMIT", reasons:string[]};
```

**proposed allowlist** (environment-specific and validated at runtime; not a claim of tested Compose deployment):
- `user: nonroot`, `privileged: false`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `read_only: true`, `tmpfs` for bounded scratch, `pids_limit` finite, `mem_limit` finite, CPU quota finite, `ulimits.nofile` finite.
- `network_mode: host`, `pid: host`, `ipc: host`, `cgroupns: host` prohibited; `volumes` of `/var/run/docker.sock`, `/run`, `/home`, `/etc`, `/proc`, `/sys`, `/dev`, host root or unreviewed mount prohibited.
- `devices`, additional linux capabilities, seccomp `unconfined`/AppArmor `unconfined`, user-provided docker args, Docker API TCP connection, `SYS_ADMIN`, `NET_ADMIN` prohibited.
- Browser requires Playwright `chromiumSandbox: true` and kernel supporting user namespaces/seccomp. If Docker+Chrome sandbox compatibility fails, no silent `--no-sandbox` fallback. Security team and operator must fix kernel/container policy or select separately reviewed compliant engine.
- Bounded example profile (experimental starting point, not measured): Browser Worker ≤2 contexts/owner, ≤1GB RSS/context, wall timeout 120s/action, 20 pages/task, script execution disabled unless Browser engine explicitly requires and policy allows it; compute ≤128 MiB, ≤2s CPU/5s wall, output ≤1 MiB, input ≤8 MiB, no outbound sockets.
- Browser's normal JavaScript execution is **web content execution inside Chromium sandbox**, not a new right to execute Node.js or Python on the host.

**Important**: Host runtime must still manage Compose and files, but only the human operator may do so. Remote agents do not receive those administrative privileges. Environment variables may leak on inspection; only credential broker may mount read-only key source, never the Browser/compute/Agent.

## 6. States / sequence

```text
PLANNED -> POLICY_VALIDATED -> PROVISIONING -> ATTESTED -> RUNNING -> COMPLETED
             |                   |             |          |
             v                   v             v          v
        DENIED_POLICY        FAILED_UNSAFE  INVALIDATED  CANCELLED/UNCERTAIN
```

Sequence:
1. Gateway persists Tool intent (CHAT_03) and validates canonical schema/owner/goalRevision/budget/approval; BrowserWorker or compute requested by registered capability, no arbitrary command.
2. SandboxAdmission picks executor by immutable tool kind; verifies process identity, deployment config policy version, owner binding and current Run leaseEpoch.
3. Browser Worker locks owner+task BrowserSession with CAS and constructs fresh Context (or owner-matching encrypted state), enforces sandboxed Chromium and isolated temp dir. Compute Worker receives only bounded/redacted input and restricted function.
4. Executor rechecks authorization and allowed egress target before external IO. Outputs written as scoped Artifact; raw data never becomes grant.
5. Gateway commits outcome/UNKNOWN+event/outbox; releases Browser/human lease and compute scratch. Resume revalidates owner, leaseEpoch and tool policy; no arbitrary retries for external effects.

Human handoff: agent control lease -> `WAITING_INPUT` -> verify fresh authenticated same owner -> issue single-use human lease -> suspend Agent actions -> human browser operations through the same BrowserWorker constrained capability -> revoke lease on timeout/logout/cancel -> resume only after current Run epoch validation. Human takeover does **not** attach user local Chrome profile or host desktop by default.

## 7. Algorithm

```ts
async function launchEligibleExecutor(intent) {
  const record = await gateway.loadCommittedIntent(intent.id);
  const owner = await ownerResolver.resolveFromTaskRun(record.taskId, record.runId);
  if (!await authz.isActive(owner) || record.ownerUserId !== owner.userId) throw Denied();
  await gateway.requireCurrentApprovalAndGoalAndRunLease(record); // no stale epoch
  const descriptor = staticRegistry.get(record.toolId);
  if (!descriptor || !ALLOWED_EXECUTOR_KINDS.has(descriptor.executorKind)) throw Denied();
  const prof = await deploymentAttestor.readEffectiveProfile(descriptor.executorKind);
  if (!auditProfile(prof).accepted) throw UnsafeRuntime();
  const grant = await gateway.consumeAtomicCapability(record, owner, prof.policyVersion);
  const result = await isolatedExecutor.run({grant, immutableArgsRef:record.argumentsRef});
  if (!await gateway.stillCurrent(grant)) return gateway.recordUncertainIfExternal(result);
  return gateway.commitAuthorizedResultWithOutbox(grant,result);
}
function admitPureCompute(input, policy) {
  if (containsUntrustedCodeAST(input)) throw Denied(); // do not eval arbitrary expression
  validateRegisteredFunctionIdAndStrictTypedArgs(input);
  enforceFiniteInputOutputTimeMemory(policy);
  return deterministicFunctionRegistry.execute(input.functionId, input.args);
}
```

**Use of `containsUntrustedCodeAST`**: conceptual boundary, **not** a malware/code detector. Enforcement must be structural: only precompiled function ID+strict args are admitted; arbitrary source text is never compiled. Python sidecar may implement these deterministic transforms but cannot offer general `eval/exec` to Agent. Model-generated SQL queries cannot be run by DB admin; safe query DSL must be validated and bound.

## 8. Failure/recovery matrix

| Condition | Policy/recovery/final state |
|---|---|
| `privileged:true`, host PID/net, mounted docker.sock, `unconfined` | hard deployment validation fail; no executor launched |
| Chromium no usable sandbox | BLOCKED_UNSAFE_RUNTIME and operator remediation, never disable sandbox |
| unknown tool/MCP requests host commands | DENIED, audit security violation, 0 host API calls |
| compute CPU wall/OOM | kill child only; mark bounded error; release scratch, retry only pure idempotent transform with same args hash |
| browser worker killed mid-read | recover Context only from same owner encrypted state after fence check; if operation already side effect, UNKNOWN |
| handoff ticket replay | atomic single-use reject; old Agent lease remains fenced |
| stale Worker receives late result after successor claim | old lease cannot commit, result quarantine; external effect may require reconciliation |
| DNS or network policy bypass via proxy | block at egress layer; fail closed |
| PDF/ZIP decompression bomb | byte/ratio/page/time limits reject; no Python macro/embedded code execution |
| operator manually downgrades Compose security | startup/preflight refuses; audit reports exact unsafe setting |
| wrong tenant/profile cache key | reject before opening context; confidentiality failure in test is release blocker |

## 9. Security & privacy

Primary isolation is **kernel/process and Gateway policy**, not a system prompt or UI button. GPU/UI/CUPS/host peripherals default deny. Browser sandbox doesn't secure server egress by itself; it needs egress proxy DNS pinning. No direct shared /tmp across tenants, no access to host's `$HOME`, ssh-agent, Docker API, cloud metadata, local network, or credential broker master key. Browser screenshots/HAR/logs may include confidential content: encrypt and owner-scope; redaction before model. Human-provided website instructions are data. Never downgrade security to improve scraping success or promise bypass of bot protection.

## 10. Monitoring, capacity and evaluation

Metrics per executor kind and rejection reason (no secret or URL query): sandbox policy hash, startup failure, unexpected privilege, kernel sandbox enforcement status, RSS, PIDs, CPU, wall time, temp bytes, DNS policy denies, Browser lease conflict, OOM. Initial load test: 2 owners ×2 browsers ×10 compute actions, repeated kill/restart, p95 latency/RSS and host access failures measured. **No measured result or throughput claim**. Concurrency and resource limits must be strict, configured before rollout, not infinite/zero-as-unlimited.

## 11. Persistence / migration [PROVISIONAL: CHAT_10]

Persist normalized policyVersion & hash, immutable tool intent binding, BrowserSession owner/tenant/workspace mapping, Browser lease generation, artifact temporary key and deletion deadline, audit decision. Actual Docker Compose/OS security deployment owned by CHAT_10. Expand deploy with separate service identities and network zones; add no host mounts; protect browser persistent state behind encrypted store (CHAT_09 secrets). If migration finds old Context without owner or untrusted profile path, quarantine rather than assign default user.

## 12. API/UI [PROVISIONAL: CHAT_08]

UI shows `BLOCKED_UNSAFE_RUNTIME`, `WAITING_INPUT`, browser takeover status and limited correction guidance. Operator-only deployment error page may describe missing browser sandbox/kernel feature but must not expose host path/secret. Ordinary end user cannot override policy, toggle privileged settings, edit Docker options, or enter arbitrary shell commands. Approval UI cannot approve forbidden capabilities; it only approves allowed side effects.

## 13. Implementation PR sequence

1. **SBOX-PR1** static Tool registry capability manifest + security config validator + rejection fixtures.
2. **SBOX-PR2** least privilege Compose/policy attestor + kernel/Chromium sandbox enforcement.
3. **SBOX-PR3** Browser Worker owner/lease + Handoff credential isolation + egress mediation.
4. **SBOX-PR4** pure compute worker, bounded material extraction, crash/fence/OOM/recovery suites.
5. **SBOX-PR5** adversarial full-stack release gate with 2-user isolation and no host privilege.

## 14. Tests (designed only; no execution)

| TEST | Setup/action | Expected |
|---|---|---|
| SBOX-T01 | attempt `ssh`, `bash`, docker.sock, `/proc` from Tool catalog/MCP | tool unavailable / denied; host effects 0 |
| SBOX-T02 | run container with privileged, cap add, host network/PID, device, bind mount | config reject before start; no privileged process |
| SBOX-T03 | fake policy allowing seccomp unconfined or AppArmor unconfined | fail preflight, audit reason present |
| SBOX-T04 | Chromium fails userns/seccomp sandbox startup | no fallback to `--no-sandbox`; blocked status |
| SBOX-T05 | A/B BrowserContext and human takeover token swap | Cookie/Context/cross-owner access 0 |
| SBOX-T06 | malicious document includes Python `eval`/Node `child_process` code | never executed; marked untrusted data |
| SBOX-T07 | unknown compute function, runtime source code, oversized typed args | strict schema deny with 0 execution |
| SBOX-T08 | pure compute CPU/OOM/wall/network attempt | killed within budget; no socket; no retained temp |
| SBOX-T09 | worker crash at grant/lease/commit boundaries | old epoch cannot commit new result, no blind side effects |
| SBOX-T10 | MCP descriptor asks elevated capability or updates registry | no privilege growth, policy decision logged |
| SBOX-T11 | Browser follow redirect to 127.0.0.1/169.254.169.254 | egress denied, no internal bytes |
| SBOX-T12 | ZIP/PDF nested/decompression or image bomb > cap | bounded refusal, no JavaScript/macros |
| SBOX-T13 | operator downgrades checked config after worker active | hash change detected, worker stops/restart denied |
| SBOX-T14 | 2-user concurrent Browser & compute, profile cache race | no cross-owner context/Artifact leakage |
| SBOX-T15 | worker loses approval during human takeover | agent action cannot race human lease |
| SBOX-T16 | activity trace and audit after sandbox failure | no token/host secret, limited status only |
| SBOX-T17 | valid read/compute vs unapproved purchase/post via browser | read allowed, purchase/post blocked pending CHAT_03 |
| SBOX-T18 | trusted operator container configuration without Chrome sandbox feature | explicit unavailable status, no unsafe bypass |

All tests local/mock with fake sites/tools; no live irreversible operation. **Not executed**.

## 15. REQ→TEST→SC matrix

| REQ | TEST | SC |
|---|---|---|
| SBOX-001 | T01,T10 | 06,08 |
| SBOX-002 | T02,T03 | 08 |
| SBOX-003 | T01,T02,T14 | 06,08 |
| SBOX-004 | T04,T18 | 06,08,13 |
| SBOX-005 | T05,T14,T15 | 06,08 |
| SBOX-006 | T06,T07 | 08,15 |
| SBOX-007 | T07,T08 | 07,09 |
| SBOX-008 | T09,T17 | 07,08 |
| SBOX-009 | T09,T15 | 06,07 |
| SBOX-010 | T01,T10 | 08 |
| SBOX-011 | T11 | 08 |
| SBOX-012 | T08,T12 | 08,09,15 |
| SBOX-013 | T02,T03,T13 | 08 |
| SBOX-014 | T04,T18 | 06,08 |

## 16. Unresolved cross-team dependencies

| ID / owner | Priority | Proposal / exit criterion |
|---|---|---|
| SBOX-P1 / CHAT_03 | P0 | Static Tool permission type, lease binding to approval/GoalRevision/RunEpoch; T01/T09/T17 |
| SBOX-P2 / CHAT_04 | P0 | Browser owner+lease/Chromium launch options/human handoff; T04/T05/T15 |
| SBOX-P3 / CHAT_10 | P0 | Actual container profiles/OS kernel hardening/Compose and artifact isolation; T02/T13/T18 |
| SBOX-P4 / CHAT_01/02 | P0 | Lossless leaseEpoch and owner→workspace mapping in worker identity; T09/T14 |
| SBOX-P5 / CHAT_05/09 SSRF | P0 | Proxy/pinned egress for HTTP/Browser/MCP (not browser flag alone); T11 |
| SBOX-P6 / CHAT_11 | P1 | Rootless/Chromium test matrix, kernel feature compatibility and OOM test environment; T04/T08/T18 |
| SBOX-P7 / CHAT_00 | P1 | Approve actual compute interface (pure fn only vs isolated deterministic script language) by ADR |
| SBOX-P8 / CHAT_15 | P1 | Multimodal parser direct code/executable conversion restrictions and artifact input limits |

## 17. Sources — 2026-10-30 inspected

- [S1] **Code verified**, OpenClaw MIT, `8b8c33ef334b3754f42afe2a2e212418abfa80f4`, `validateSandboxSecurity` and blocked host mounts: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/agents/sandbox/validate-sandbox-security.ts
- [S2] **Test source verified, not run**, audit config: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/security/audit-sandbox-docker-config.test.ts
- [S3] **Test source verified, not run** sandbox docker tests: https://github.com/openclaw/openclaw/blob/8b8c33ef334b3754f42afe2a2e212418abfa80f4/src/agents/sandbox/docker.test.ts
- [S4] **Code verified**, Playwright Apache-2.0, `38fb53bf4c181abb91f1ca2a2ab741983c5d177c`, `chromium.ts` lines 386-387 `if options.chromiumSandbox !== true then --no-sandbox`: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/packages/playwright-core/src/server/chromium/chromium.ts#L380-L388
- [S5] **Test source verified, not run** Playwright `tests/mcp/config-resolve.spec.ts` sandbox cases lines 137-201: https://github.com/microsoft/playwright/blob/38fb53bf4c181abb91f1ca2a2ab741983c5d177c/tests/mcp/config-resolve.spec.ts#L137-L201
- [S6] **Docs verified** Docker Engine security: https://docs.docker.com/engine/security/ ; resource constraints: https://docs.docker.com/engine/containers/resource_constraints/
- [S7] **Hypothesis** Docker Compose exact flags/runtime support, compute limits and browser compatibility untested for user's Ubuntu host.
- [S8] **Merged upstream PR inspected** Playwright #42490 changes MCP sandbox default handling; see appended PR investigation. Actual Chrome/Compose integration remains unverified.

## Review gate self-check

- [x] 2 OSS implementations & code/tests read, security alternatives, contracts, controls, states, recovery, migration/API, traceability.
- [ ] Browser+Docker container sandbox actual execution, operating-system compatibility, ADR, canonical owner/Tool/lease contracts and measured budgets. **Keep Draft**.

## Upstream PR investigation — Chromium default sandbox changes
- **[PR verified, merged upstream]** Playwright [#42490](https://github.com/microsoft/playwright/pull/42490) modified MCP Chromium sandbox defaults for the bundled Linux browser and added config tests. Upstream defaults are version/channel dependent; the pinned actual launcher code still appends `--no-sandbox` if `chromiumSandbox !== true`. We therefore **explicitly require** sandboxed execution and block unsupported environments instead of inheriting upstream defaults (`SBOX-T04/T18`).
- Grade: merged PR report + pinned code/test inspection, **not** a successful container integration test.


### Round 4 shared Version/Epoch type reconciliation (2026-10-30)
Revision and lease fields use CHAT_01's canonical signed-int64 decimal **string** on wire, internal BigInt, PostgreSQL BIGINT. Never cast through JSON number or approximate IEEE754. Any bounded operational counts remain distinct. Contract fixtures (designed only): roundtrip 2^53 and INT64_MAX-1, stale revision and revoked owner reject. Harness runtime verification pending.
