# Architecture Decision Register — Accepted Direction vs Proposed Contracts
> CHAT_00 **Draft** decision ledger. Status labels `PRODUCT_FIXED` mean the user-approved product boundary, not source-code verification. `PROPOSED` means individual owner PRs still require integration review. ADR entries here never change other owner's existing branch.
## Decision procedure and status
`PROPOSED→OWNER_REVIEW→CROSS_OWNER_ACCEPTED→LOCKED`; `EXPERIMENT` becomes accepted only after pinned tests. Each ADR has an owner, evidence, alternatives, rollback/trigger and test. Changing a `PRODUCT_FIXED` boundary requires explicit user agreement, not another Agent output.
## Decisions
| ADR | State | Decision and rationale | Alternative rejected/held | Owner / verification |
|---|---|---|---|---|
| ADR-001 | PRODUCT_FIXED | general research/monitoring Agent, no host server administration; extensible adapters | Hermes fork, code/OS admin Agent, scraping-only | CHAT_00/09; SC-05,08 |
| ADR-002 | PRODUCT_FIXED | Gateway enforces schema/budget/approval/effect ledger. **Host and Trusted Control VM shell forbidden; bounded offline sandbox.exec in isolated Execution VM permitted.** Each real external action requires its own Gateway Intent and Sentinel permit | LLM direct Host/Trusted Shell, Browser CDP, uncontrolled Internet | CHAT_03/09; SC-07,08,17,19 |
| ADR-003 | PROPOSED | durable Task/Run, immutable checkpoint, fence and outbox; atomic domain state/event append | stateless chat loops, blind side-effect retry | CHAT_01/02/10; PG/killpoint tests |
| ADR-004 | PROPOSED | one centrally owned Browser Worker; Playwright Chromium compatibility baseline, HTTP/API first | independent direct Page attachment from Agent, Lightpanda by default without compatibility | CHAT_04/09; SC-06,09,13 |
| ADR-005 | EXPERIMENT | Jev, Scrapling, Lightpanda as strictly scoped optional adapters based on feature/compat/license | mandatory untested third-party browser | CHAT_04/05; pinned source+benchmark |
| ADR-006 | PROPOSED | PostgreSQL as durable state/Event/Outbox; stream replay uses tenant delivery offset and scope signed cursor | task-local event counter for global stream, untrusted Redis offset | CHAT_01/08/10; SC-07 |
| ADR-007 | PROPOSED | same Run on crash + new lease; new Run only under nonterminal Task; terminal retry creates new Task | resurrect terminal Task or duplicate irreversible operation | CHAT_01/02/08; RETRY tests |
| ADR-008 | PROPOSED | wire full signed int64 via decimal string, in-process BigInt and DB BIGINT with validated overflow | JS Number for versions >2^53 | CHAT_01/10; boundary codec tests |
| ADR-009 | PRODUCT_FIXED | provider terms, credential authority, freshness and cost budget precede actual data assertions | CAPTCHA evasion or fabricated real-time availability | CHAT_05/09; SC-01,03,04 |
| ADR-010 | PROPOSED | source→entity/variant→risk/observation→Watch/Notification causal provenance; avoid false merges | merge by name alone or profit = sale − buy | CHAT_12/01/10; SC-01,10,14 |
| ADR-011 | PROPOSED | external named X actor triggers require stable provider ID, subscription generation, durable dedupe/cursor, disable barrier | cron-only with duplicate-run race or instant stream guarantee | CHAT_13/05/07/10; SC-11 |
| ADR-012 | PROPOSED | digest is decision layer over a single sender Outbox, unknown send remains unknown | dual senders, assume timeout means not sent | CHAT_07/13/10; SC-16 |
| ADR-013 | PROPOSED | cached Browser recipe is restricted to observed read/extraction and gateway effect guards | label read_only_click/fill as automatically safe | CHAT_03/04/09/14; SC-13 |
| ADR-014 | PROPOSED | user Feedback has scoped Memory Writer, undo/tombstone, cannot change authority | self-mutating runtime logic, overwriting facts | CHAT_06/14; SC-16 |
| ADR-015 | PROPOSED | untrusted file parser isolated no-network, raw→derived artifact + coordinates | model alone executing embedded PDF/Office content | CHAT_01/09/10/15; SC-15 |
| ADR-016 | PROPOSED | migration expand→backfill→switch→contract, pinned CI PostgreSQL+Browser+Docker+SSRF evidence | altering an already-applied migration or declaring smoke test production-safe | CHAT_10/11; SC-07,08 |
| ADR-017 | PRODUCT_FIXED | Allow bounded Shell/Python/Node/CLI and generated code inside isolated Runtime Cell, deny host/protected service code; maintain General Agent goal | permanent deny of any code; unprivileged host shell | CHAT_00/02/09; SC-17,18,21 |
| ADR-018 | PROPOSED | Production two KVM/QEMU guests with **different kernels** (Trusted Control VM vs Untrusted Execution VM) and userns-restricted Runtime Cell; no guest-kernel sharing with Vault/DB, no unsafe fallback, dev-only container distinct | Docker-only asserted equivalent to VM; one privileged process | CHAT_09/10/11; SC-18,21 |
| ADR-019 | PRODUCT_FIXED | Independent Sentinel is final authority for all connector operations and network egress, regardless of generated subprocess; Gateway retains intent/effect ledger | tool-only routing, environment-variable proxy without kernel route block | CHAT_03/09/10; SC-19,22 |
| ADR-020 | PROPOSED | Credential Broker/Privsep and Browser Broker external to Cell; agent never sees raw tokens or direct CDP | token env injection or unowned browser socket | CHAT_04/05/09; SC-18,22 |
| ADR-021 | PROPOSED | SandboxExecution and Workspace child contracts, lifecycle/kill tree/UNCERTAIN reconciliation and temp Skills | uncontrolled daemon, automatic self-upgrade of protected runtime | CHAT_01/02/03/06/10/11; SC-17,20,21 |
| ADR-022 | EXPERIMENT | Compare eBPF taint provenance and MicroVM-per-task after production mandatory path established | assume Meta's kernel data flow tracking implemented | CHAT_09/10/11; SC-19,22 |
| ADR-023 | PROPOSED | Adaptive ContextはPolicy/Goal/Controlをimmutable typed anchorsとして固定し、Evidence/Memory/Tool/Journalを別権限のData層として扱う。LLM summaryは正本/権限/成功判定を代替しない | last-N-only truncation、全History/MemoryをSystemへ常時注入 | CHAT_01/02/03/06/09/10; ACM-T01/T02/T04/T09 |
| ADR-024 | PROPOSED | Context priority P0〜P3とACL/freshness hard filterを先に適用し、Model版別Tokenizer+Output reserve+Margin内でprovenance/dedup付き選択を行う | Embeddingスコアだけで配置・権限を決定、Prompt cache hitをWindow拡大扱い | CHAT_02/06/09/11; ACM-T03/T06/T07/T10 |
| ADR-025 | PROPOSED | Raw Artifactとlocators、grounded summary claims、immutable ContextManifest/HEAD CAS、run journalを独立保存し、Worker Crash/Steer/Memory revoke/Model swapでrehydrate | summaryを原本化、旧Goal/旧Memory/旧leaseを無条件復元 | CHAT_01/02/06/10/11; ACM-T08/T09/T13/T15/T18 |
| ADR-026 | PROPOSED | 長時間Taskは複数有限Run/Watchを原本に接続し、鮮度とsource identityで必要情報のみ再取得。子Taskにはtyped Handoffと出典付きChildResultだけを共有 | 無期限Single Run、全History+Secretsを子Taskへコピー、子Agentの自称完了で成功 | CHAT_01/02/03/07/09/10; ACM-T11/T12/T17/T20 |
| ADR-027 | PROPOSED | Sandbox/Browser/MCP/Generated Script/Memory由来のコンテキストはTrustTierを引き上げず、Sentinel/Gateway/Protected Serviceが権限を再検証する | 要約/Embedding済み文字列にPolicy/Approval権限を与える | CHAT_02/03/06/09/11; ACM-T04/T10/T14/T15 |
| ADR-028 | PROPOSED | Context監査UIとsource確認/訂正/再構築のCommandsを版付きで定義し、Hard gatesをQuality gatesより優先してRelease判定する | 「精度99%」など無実測指標、Summary編集でGoal/Receiptを書換 | CHAT_08/11/02; ACM-T01〜T20 |
| ADR-029 | PROPOSED | サーバーBrowser映像は唯一OwnerのWorker内でCaptureし、認証済み一方向Media relayを通じてTask ownerへ見せる。ObserveはAgent leaseを奪わず、Takeoverは別の短命ticket/排他権限。生frame非保存、機密/不明ページは全面遮断 | raw CDP/VNCを公開、SSEにframe混入、Observeを操作権限と混同、未知感度をmaskのみで安全扱い | CHAT_04/08/09/10/11; BLO-T01〜12, SC-23 |
## Cross-team conflict register
| ID | Blocker | Required owner handshake | Closure evidence |
|---|---|---|---|
| DEC-P0-01 | X ExternalEvent receipt/Subscription/Cursor/Task dispatch | CHAT_01+05+07+09+10+13; API 08, QA11 | physical DDL, Event mapping, two-source duplicate/revoke/crash E2E |
| DEC-P0-02 | retailer/storefront identity absent across Entity/Feedback | CHAT_01+06+10+12+14 | canonical stable store identity, negative false-merge and Undo tests |
| DEC-P0-03 | digest membership/send UNKNOWN | CHAT_07+08+10+13 | single sender ledger + atomically claimed membership, ACK loss tests |
| DEC-P0-04 | multimodal locator/parser job & confinement | CHAT_01+03+09+10+15 | typed locator/shas/position, isolated parser cap/egress/cleanup tests |
| DEC-P1-01 | read-only labeled Browser actions can cause POST | CHAT_03+04+09+14 | evidence-backed side-effect guard or automatic replay limited to no-click read |
| DEC-P1-02 | Change baseline/provider source lineage not ratified | CHAT_01+07+10+12+13 | CAS baseline + source variant key + retention/error tests |
| DEC-P1-03 | direct Provider access entitlements/quotas | CHAT_05+09+12+13 | authorized provider contract smoke or explicit unsupported state |
| DEC-P1-04 | old Source conflict notes refer to superseded SQL | respective owner of each Draft PR | current HEAD reconciliation and superseded annotations |
| DEC-P0-05 | Execution/Workspace IDs, output/error/state and Run ownership crossing Gateway, Cell, DB and UI | CHAT_01/02/03/08/09/10/11 | canonical schema, migration, kill and crash E2E |
| DEC-P0-06 | Sentinel as sole final connector/network permission authority vs Gateway Approval ledger and egress backend | CHAT_03/04/05/09/10/11 | one authorize path, no bypass in curl/IPv6/UDP/opaque TLS/Browser |
| DEC-P0-07 | Secure VM + nspawn + Chrome sandbox production readiness and dev-only mode | CHAT_09/10/11 | actual kernel attestation, QA isolated profiles and fault test |
| DEC-P1-05 | Temporary generated Skill activation vs durable Skill publication/secret access | CHAT_02/03/06/09/11 | bounded temp use, reviewed promotion, revocation tests |

| DEC-P0-08 | Adaptive ContextManifest/typed anchor/Journal/Artifact locatorの正本とHEAD CAS境界未Locked | CHAT_01/02/03/10/11 | schema-version、hash、owner/epoch、expand-contract migration、ACM-T01/13/18 |
| DEC-P0-09 | Memory revoke/read epochとModel-dispatch race、Source ACLとContext selectionの責務未Locked | CHAT_02/06/09/10/11 | deletion barrier、cross-tenant no-leak、ACM-T02/09/10 |
| DEC-P0-10 | Model Window/Tokenizer/Tool schema/Output reserve/Cost settlementの統一規定未Locked | CHAT_02/10/11 | model switch/overflow/partial/budget tests、ACM-T06/07/19 |
| DEC-P1-06 | Child context handoff/Artifact provenance/UX quality thresholds pending | CHAT_01/02/03/08/09/11 | Child source validation、Context dashboard、ACM-T11/12/14/17 |

## Locking and decision escalation
1. Core Owner publishes canonical proposed contract with schema version, precise examples, invariants, migration and negative tests.
2. Affected Owners review the HEAD and explicitly accept/reject or propose a compatible alternative; don't silently assume agreement.
3. DB Owner maps accepted model to FK/index/transaction and Migration; API/Tool/Watch Owners map interfaces; QA creates executable assertions.
4. Independent reviewer checks all latest HEADs; `LOCKED` only when P0/P1 in scope closed, with each unresolved external fact labeled; `VERIFIED` only after real code and tests.
5. No blanket merge of all **61** Draft PRs; no automatic main push. If Owner acceptance unavailable, mark `PROVISIONAL` rather than invent signer decisions.
## Sources and verification
Authoritative local: Project Brief, Architecture Rules, SC-01..16, full Round 5 PR #1..61 technical audit report (audit branch). Prior art: [LangGraph checkpoints](https://docs.langchain.com/oss/javascript/langgraph/persistence), [Playwright locators](https://playwright.dev/docs/locators), [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html), [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html). Specific versions/source signatures still need owner review; citations here do not assert external implementations tested.


## Round 6 — Cross-Owner Lock Review (2026-10-30, unapproved proposal)
A green static Markdown gate or synthetic PostgreSQL test is **not** Owner acceptance. Technical readiness has two independent axes: **(A) contract convergence** (exact type/schema/API equivalence and no same-name divergent enums) and **(B) authority** (explicit responsible Owner review on the current HEAD). A third axis, Runtime Verified, is separate and cannot be inferred from specification work.

The coordinator may prepare a proposed reconciliation table but cannot impersonate 16 Owner signatures. Candidate states: `NEEDS_RECONCILIATION`, `OWNER_REVIEW_REQUIRED`, `SPEC_LOCK_CANDIDATE`, `LOCKED` (all gate approvals recorded), `IMPLEMENTED`, `VERIFIED`. For a Lock, require accepted canonical revision and corresponding dependent PR HEADs, owner approvals, a signed freeze manifest, conflict resolution disposition, and code/test gate boundaries identified. After any HEAD change, reopen affected acceptance. Never auto-merge merely for statuses. Source: [Round 5 integration review](https://github.com/choko510/aiagent-md/blob/audit/round4-runtime-validation/audit/round5/integration-gate-report_2026-10-30.md).

## 2026-11-07 PR #69 P0/P1レビュー反映（採用提案、NOT LOCKED）

ADR-023〜028の実装条件を次のとおり補強する。`P0-1` Source refだけでは意味照合にならず型付きSourceBackedFactと独立SemanticVerificationReceiptを要求。`P0-2` 最終Serialized Provider Requestの一意Token区分で二重計上を防止。`P0-3` ContextManifestとSealed ModelRequestSnapshotは別資産。`P0-4` MemoryWriter DeleteとModel Gateway DispatchAdmissionはscope row lock + epoch CASで線形化、物理送信は厳密に原子的でない。`P0-5` Goalの条件は演算子・値・単位・否定・強弱・未解決を含むtyped snapshot。

P1はSealed Original/Redacted Projection、Protected-only A0/A2、Historical/Current、Capacity-aware marginal utility、Provider別Recipient Consent、ChildResult再検証、Context GC/HEAD参照、失敗Journal、Context差分復旧、Tenant scoped Cache、Evidence Coverage Checkerを採用提案とする。

**新たなOwner review blockers**: CHAT_01/02/06/10のGoal/MemoryAdmission/ContextSnapshot Schema、CHAT_01/09/10/15のOriginal/Projection整合、CHAT_02/03/09のProvider Privacy gateway、CHAT_08/11の送信可能性UI/Fixtureを正式承認するまではLOCKED扱い不可。Test契約はACM-T21〜36（Designed only）。PR #68の隔離/通信境界はこの追加仕様によって緩和されない。
