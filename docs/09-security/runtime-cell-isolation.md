# Runtime Cell Isolation — Muse型の隔離実行境界

> Owner: CHAT_09 | Spec: 09-07 (proposed) | Status: Draft / PROVISIONAL | Updated: 2026-11-07 | Depends: CHAT_01/02/03/04/06/08/09/10/11. 本文は実装仕様案であり、実機での隔離を実証したものではない。

## 0. 規範・目的・非目的

本HarnessはUbuntuセルフホストの汎用Personal Agent。エージェントには隔離されたLinux Runtime Cell内でShell、Python、Node.js、CLI、コード生成/実行、短命の依存導入、子プロセス、サブエージェントを許す。一方、**Ubuntuホスト、VM管理プレーン、保護サービス、秘密情報、許可されないネットワーク、他ユーザーのファイルに対する権限は絶対に付与しない**。Runtime Cellのrootはホストrootではない。

「Shell禁止」の旧仕様は **host shell / trusted services shellは禁止、runtime cell shellは条件付き許可** に改定する。実行権限と外部行為権限は別物であり、ローカルコードが起動できることはネットワーク送信・購入・予約・投稿・外部通知の承認を意味しない。

本番の基本構成は **KVM/QEMUの独立した2つのゲスト**（Trusted Control VMとUntrusted Execution VM）。**Vault/DB/Sentinel/Agent OrchestratorはTrusted Control VMに配置し、実行ゲストのカーネルとは絶対に共有しない**。生成Shell/Python/Node/CLIはUntrusted Execution VMのRuntime Cell（追加のUID/GID namespace・seccomp・LSM・cgroups）内だけで起動する。UbuntuホストはVM管理専用でAgentから操作できない。ブラウザWorkerはさらに別の制限された実行ドメイン。単一ユーザーでも信頼境界を省略しない。

VMなしのコンテナ単体は **development_only** として明示し、本番相当の保証を称しない。ハードウェア仮想化がない場合は**起動要件未達**と報告する。unsafeなno-sandbox / privileged fallbackは禁止。Firecrackerは将来の比較候補であり初期採用を断定しない。実行VMはユーザー/テナント境界で分離し、異なるユーザーへのVM再利用は禁止。単一ユーザー同一テナント内でのVM再利用は、プロセス/Workspace/Capability/IPC/Network状態の完全消去と隔離検証を条件に許可する（高リスクTaskは使い捨て）。詳細は§2/§4で定義。

Meta Muse公開資料は脅威モデル・設計パターンの参考であり、Meta非公開実装やeBPF taint追跡の再現を保証しない。
- Source: https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse
- Official overview: https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/

## 1. 信頼ドメインと所有者

| Boundary | Owns | Must never expose to Runtime Cell |
|---|---|---|
| Physical Ubuntu host (operator) | KVM/QEMU, backup, VM manager, physical firewall | SSH, Docker/libvirt/KVM socket, host mounts, root, cloud metadata |
| **Trusted Control VM (different guest kernel)** | Trusted Agent Orchestrator, Task/Run DB, Sentinel, Gateway, Policy, Vault, Credential Broker, trusted Connector, Browser controller | No direct API/secret/DB/admin socket mounted or reachable inside Execution VM |
| **Untrusted Execution VM + Cell (different guest kernel)** | Generated programs and shell, restricted workspace, ephemeral dependencies, process supervisor | Trusted VM mount/sockets, VM manager, kernel capabilities, public Internet route; only scoped broker RPC |
| Browser execution domain (separate sandboxed process/VM) | Chromium renderer and scoped Context through trusted Browser Broker | Direct protected Vault/DB access, unmediated CDP, other owner Context |
| Artifact Store | Provenance-backed input/output, owner/scope/retention metadata | Direct privileged volume mount, unreviewed symlinks |

All Execution VM→Trusted Control VM communication must use **one narrowly exposed authenticated broker RPC** over an operator-configured VM transport (such as vsock/virtio-serial), not direct DB/Vault/Sentinel/Browser admin sockets. AuthN must bind actual guest/Execution supervisor identity to server-owned tenant/owner/Task/Run/Execution/lease/policy epoch and per-operation scopes/nonce; **guest/process identity alone is never authorization**. VM network adapters have no unrestricted route, including DNS, IPv6, UDP and tunnels. The host/hypervisor firewall rejects every guest attempt except the broker endpoint, even if a guest obtains root. The broker cannot expose generic raw TCP CONNECT. Protected services are not colocated with the guest kernel; guest kernel exploitation must not give access to Vault/DB memory (subject to hypervisor/host compromise residual risk).

## 2. Execution profile & filesystem

Each execution is bound to user/tenant/task/run/goalRevision/leaseEpoch/executionId/policyEpoch/cellId. Profiles:
- secure_isolated (production): **separate Trusted Control VM and Untrusted Execution VM guest kernels**, verified VM/hypervisor networking and Cell confinement; immutable image, non-host UID mapping, dropped capabilities, seccomp+LSM/no-new-privileges, bounded cgroups; shell allowed inside untrusted guest only.
- restricted_compute: no direct network, read-only projections, bounded deterministic transformations.
- development_only: weaker local debugging explicitly marked; no persistent credentials, external side effects, privileged connector or unattended scheduling by default.

Filesystem: separate immutable base image from ephemeral writable overlay. Scoped task workspace is **not** direct host bind mount. Any durable workspace state must be intentionally committed through scope-validating Artifact/Workspace service; baseline snapshot, version, quota and provenance recorded. Distinct tenants never share writable layers or process namespaces. Symlinks, hardlinks, /proc magic paths, file descriptors, device files, Unix sockets and overlay whiteouts cannot bypass scope. /home, /etc, /run, /proc, /sys on physical host must not appear in Cell. Package caches and toolchains are untrusted and cannot mutate the VM image or protected service binaries.

Pseudoterminal allocation may be permitted for an interactive shell only via an owner-bound audited session. No host TTY, shared host clipboard, GPU/device passthrough or SSH agent. Runtime image upgrades and dependency installation are operator-owned immutable releases; ephemeral per-run install is allowed under Cell permissions with reproducible lockfiles where possible, license and supply-chain checks, egress constraints and bounded resources.

## 3. Kernel and network prerequisites

Production Cell admission requires **actual** rendered runtime checks (not only config intent): current UID/GID maps, effective capabilities, seccomp and AppArmor/SELinux, mount namespace, cgroup quotas, process tree, /proc exposure, socket reachability, network namespace routes, veth and physical host firewall. Probe that an unprivileged process cannot reach host bridge, RFC1918 LAN, VM metadata, Docker/libvirt sockets, Sentinel private admin paths or Browser CDP.

No outbound Internet route from the entire Untrusted Execution VM (not just the Cell): prohibit host networking, NET_ADMIN, SYS_ADMIN, SYS_PTRACE, permissive syscalls (including io_uring until separately reviewed), raw sockets, veth reconfiguration, IPv6 bypass, Unix-domain socket leaks, vsock guest-host escapes, alternate DNS, UDP/QUIC bypass, CONNECT tunnels to arbitrary destinations. Cell may call **broker_rpc_only** for typed HTTP Fetch/Connector/Package requests. These generate their own Gateway Intent before Sentinel approves and the Trusted Egress Transport sends bytes. Network control is owned by CHAT_09 Sentinel Authority and physical Operator Firewall; it must enforce egress beyond an application-level proxy environment variable. The Cell must not contain root CA material that lets untrusted code modify the trusted proxy's verification configuration.

Chromium sandbox is separate defense-in-depth: Browser Broker remains owner; Browser Worker must enable Chromium sandbox, not --no-sandbox. If kernel/browser combination cannot satisfy it, fail CLOSED.

## 4. Lifecycle, state and recovery

SandboxExecution is a child resource of a canonical Run, not a second Task/Run authority. Proposed transitions:
REQUESTED -> VALIDATING -> PROVISIONING -> READY -> RUNNING -> DRAINING -> {SUCCEEDED|FAILED|CANCELLED|TIMED_OUT|BLOCKED_UNSAFE|UNCERTAIN}.
A crash/timeout before proof of execution may be retried with the **same immutable intent** only after checking side-effect ledger. Failure to persist a result after external communication is **UNCERTAIN**, not success and not safely retryable merely because the process died.

An execution is admitted only if Gateway intent persisted, owner and lease are current, budgets reserved, requested profile attested and Sentinel ingress/egress ready. Start records image digest, Cell identity, tool versions, workspace generation, allowed capability set, limits, policy hash. No process may outlive the Run fence: Cancel/Steer/revoke stops admission, terminates entire process tree within a finite grace interval and forces kernel kill/reap; IPC/egress grants expire. Stale processes can never commit artifacts or emit new effects. Graceful shutdown must not overwrite externally UNKNOWN effects.

Workspace restart: only explicitly committed artifacts can survive Cell destruction. Restored workspace must preserve task owner, origin, digest and version; never resurrect raw credentials, old network grants or stale approval. External packages and generated scripts are **untrusted artifacts** on restore.

## 5. Requirements (MUST unless specified)

| ID | Requirement / measurable invariant |
|---|---|
| CELL-001 | Production runtime root cannot become VM/physical-host root; no privileged namespaces, sockets, host files or devices |
| CELL-002 | Exec permission is limited to Cell and distinct from external effect/credential authority |
| CELL-003 | Actual KVM/Cell/LSM/cgroup/kernel prerequisites attested before executing untrusted code |
| CELL-004 | Shared writable filesystem, home, PID/NET namespaces and unbounded /proc or device access prohibited |
| CELL-005 | Every child process remains constrained by Cell quotas, filesystem and egress; fork/exec cannot escape |
| CELL-006 | Production Cell has no unrestricted outbound socket path; all traffic enforced at boundary |
| CELL-007 | Every execution bound to user/tenant/task/run/goalRevision/leaseEpoch and policy version |
| CELL-008 | Cancel/revocation kills descendants, invalidates grants, prevents stale results and logs reconciliation |
| CELL-009 | Ephemeral packages never modify protected services or grant new tool/network/secret permissions |
| CELL-010 | Secret material, tokens, browser storageState, signing and DB keys never mounted in Cell |
| CELL-011 | Workspace commit/restore require scoped ACL, content digest, quota, malware and path traversal checks |
| CELL-012 | Production requests fail closed when VM, sandbox, egress, approval or isolation checks are absent |
| CELL-013 | Cell and Browser isolation are separate; agent has no direct CDP, only Broker actions |
| CELL-014 | Audit stores profile/image/policy and bounded/redacted stdout/stderr; never credentials |
| CELL-015 | Development profile conspicuously distinct; cannot silently become production readiness |
| CELL-016 | SHOULD: evaluate microVM-per-task vs nspawn-on-VM using startup, memory, crash and isolation measurements |

## 6. Required negative/positive tests

- CELL-T01: shell, Python, Node and package install run successfully in Cell, produce bounded file/exit output.
- CELL-T02: attempt access to host SSH, Docker socket, libvirt, /proc, /sys, local network and admin socket; 0 successful access.
- CELL-T03: direct curl, Python requests, DNS-over-HTTPS, raw socket, IPv6, UDP/QUIC, redirects, WebSocket, proxy tampering; no unauthorized egress.
- CELL-T04: fork bomb, memory bomb, infinite loop, disk fill, large stdout; quotas halt complete process group; host protected.
- CELL-T05: kill VM/Cell/Worker in every Tool intent/external I/O/result commit window; no duplicate externally irreversible action.
- CELL-T06: race Cancel + lease loss + child processes + artifact upload; stale execution cannot write or reconnect.
- CELL-T07: cross-user workspace files, symlink/overlay and restored snapshot; 0 cross-tenant leakage.
- CELL-T08: browser sandbox cannot launch; no insecure fallback, explicit BLOCKED_UNSAFE.
- CELL-T09: inspect real process capabilities/namespaces, ports, VM boundary and logs; no privilege or secret exposure.
- CELL-T10: dev-only mode cannot use production provider credentials or claim VERIFIED isolation.

Release gate: zero successful isolation bypasses in negative E2E; exact host/guest kernel, Docker/systemd-nspawn/KVM version, image digest, tested config and toolchain recorded. No absolute sandbox escape-proof claim.

## 6.1 Production VM reuse, cleanup and residual threats

VM pool keyed by owner+tenant (never shared across distinct users), immutable imageDigest and security policy epoch. On each Task/Execution: dedicated cgroup/PID namespace, Workspace overlay and per-Execution broker grant. On completion: close IPC handles, revoke all grants, cgroup.kill/reap descendants, unmount & erase writable overlay, flush DNS/network state and verify zero leftover sockets, PIDs and artifacts. Reuse only if all checks pass; if not, destroy/recreate VM. Per-tenant max idle duration and concurrency are finite deploy settings (values benchmarked; no unmeasured hard-coded promises). Cross-tenant reuse **requires VM destruction and image re-creation**, not only nspawn reset. Remaining risks: hypervisor exploit, malicious host operator, compromised trusted control plane/Browser worker, data released legitimately by authorized Tool, and unclassified content sent by trusted connector. These are not described as eliminated by VM isolation.

| CELL-017 | MUST | Protected Services never run under same guest kernel as Agent-generated code |
| CELL-018 | MUST | Execution VM has no Internet route: only per-Execution authenticated tool broker RPC, network off for shell |
| CELL-019 | MUST | Separate trusted Orchestrator process never shares PID/UID/Workspace or privileged IPC handles with generated code |
| CELL-020 | MUST | Guest VM reuse never crosses tenants; grant/Process/Overlay cleanup verified or VM destroyed |
| CELL-021 | MUST | Privsep/Browser/Notifier egress is also restricted to actual Sentinel-enforced transport |

## 7. Cross-owner contract checklist

CHAT_01 adds Execution/Workspace references, statuses/errors, artifact lineage; CHAT_02 implements admission/leases/cancel/subagent budget; CHAT_03 owns Gateway Tool intent/side-effect reconciliation; CHAT_04 Broker; CHAT_06 temp vs installed Skills; CHAT_08 logs/approval UI; CHAT_09 egress/secrets/policy; CHAT_10 physical VM/service deployment; CHAT_11 attack-driven gates. Do not duplicate an Owner's canonical enums or SQL in this file. Existing conflicting "arbitrary code prohibited" statements must be rewritten as "host/protected service code prohibited" and reference this spec. Status is PROVISIONAL until contracts accepted and implementation tested.
