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

## ADR-030〜034: Autonomous Development & Endless Campaign（2026-11-07、Proposed, NOT LOCKED）

| ADR | 採用提案 | 代替/却下 | Owner合意/実測 |
|---|---|---|---|
| **ADR-030** | 新規DEV-01〜10は既存88仕様の上に載るProject/DevelopmentCycle/Feature projection。Task/Run/Approvalは01/02/03の正本を使う | Code agent特化への基盤置換、別Runnerによる認可権限複製 | CHAT_00/01/02/03/10 pending |
| **ADR-031** | 3 blind proposal→1 synthesis→任意Human design→builder demo→任意demo consent→builder implementation→3 independent same-SHA review。Expert相談はread-only予算限定Child Task | 全AIに同一相互コンテキスト、単なる多数決、自分で承認、Reviewerが直接本番branch変更 | CHAT_01/02/03/08/11 pending |
| **ADR-032** | Design visual side-by-sideと動くDemoでUserが判断、Demo manifest digestにbind。実Deployment Approvalは独立 | 名称だけのデザイン選択、モックを実APIと偽る、Demo consentをTool Approvalへ流用 | CHAT_03/04/08/09 pending |
| **ADR-033** | 分離Preview Broker→独立Browser WorkerでConsole/Network/Trace/Visual regressionと安全なE2E。PR #71はoptional observe layer | localhost/任意TCP raw proxy、Cell直接CDP、private IP allowlistを一律緩和 | CHAT_03/04/09/10/11 pending。Bridge SSRF/secret試験必須 |
| **ADR-034** | 永続Campaignはbounded Cycleを成果物で連鎖。10件等のprogressはVerified distinct receipt。停止/Quota/費用上限はハードゲート | 無制限1 Run、空tickごとにModel呼出し、Unknown課金をゼロ、承認待ちで全タスクを停止 | CHAT_01/02/07/08/09/10/11 pending |

`ADR-029` はマージ済みPR #71のBrowser Live Observation仕様。ADR-030〜034はそのObserver/Takeover権限分離を継承する。Shared schema・外部Provider API・実際のKVM/Preview/Browserテストが揃うまで各ADRは採用候補でありLOCKEDと呼ばない。

## ADR-035: Context Optimizationを共通Adapterで評価し、独立Proxy正本を導入しない（2026-11-09 / PROPOSED、NOT LOCKED）

**Problem**: RTK CLI、Caveman Engine/Browse、Headroom Library/Proxyを全て無条件に重ねると、(a) Lossyによる極値/否定/失敗の欠落、(b) Browser CDP/Tool Gateway二重経路、(c) CCR/共有ProxyのTenant不整合、(d) Memory delete/Consent/Artifact GCの復元漏れ、(e) Prompt Cache prefix破壊、(f) コスト測定の二重計上、(g) RTK BUSL部分の再配布条件、が既存ADR-017〜028/033/034と衝突する。

**採用提案**: Context Managerが認可済みRedacted Projectionごとに0または1つのoptional codecを選択し、Raw Artifact/SourceFact/Tool Receipt/Goal/Approvalを保護する。Headroom in-process libraryをJSON/logの第一候補、Caveman AX-only read projectionをBrowser Worker内の第二候補、RTK OSS CLIのpost-exec filterをSandboxの第三候補として比較する。初期modeはdisabled/shadow、削減がtask品質/Total costで改善したformatのみcanary化する。独立Caveman/Headroom/RTK Proxy・第二CDP agent・独立Memory/CCR authority・Cloud routing・多段lossy・automatic telemetryは既定不採用。すべて既存Gateway/Sentinel/Artifact/Recipient consentとSealed ModelRequestSnapshotに従う。

**比較の根拠**: RTK [develop 08a261a](https://github.com/rtk-ai/rtk/tree/08a261a0d081ffe11a32986e2f4d76f3fdc33abb)、Caveman [main 3dc497a](https://github.com/JuliusBrussee/caveman/tree/3dc497a7f858c774d6e15b25e8c6120ef3a0eda8)、Headroom [main bb8d18a](https://github.com/headroomlabs-ai/headroom/tree/bb8d18a56abb07ca7e811235c86ea6b626eb1f8b)。公開benchmarkは異なるworkload/方式であり、それぞれの比率を単純比較/合算せず、同一fixture・Provider課金・タスク成功・cacheと復元で判断する。RTK OSSのApache-2.0とEngine/ProxyのBUSL-1.1を混同しない。Caveman v3以降のOSSはApache-2.0、Headroom OSSはApache-2.0、全てサードパーティ依存のNOTICE/有効ライセンスを実装時再確認する。

**Authority・移行**: 既存88の共有正本モジュール数、Core types/DDLに変更なし。Adapter/ReceiptはCHAT_02 internal draftで、Artifact/ACL/DB/Tool Registry/Browserの共同契約はCHAT_01/02/03/04/06/09/10/11にOwner reviewを依頼。OPT-T01〜T20は設計のみ、未実行。Source/Artifactを追加で保持することがプライバシー保持期限を延長しない。実装・実測・契約承認なしにLOCKED/VERIFIED・削減率保証と呼ばない。

**Rejection alternatives**: (1) RTK→Caveman→Headroom全段直列、(2) 外部OSS ProxyをTrusted Model Routerの代わりにする、(3) 共有CCR handlerを新しいAuthorizationとして公開、(4) Browserのmodel-selected UIDを直接click、(5) 書き換えたcompressed summaryを原本/effect receiptとする、(6) コスト低減のみでaccuracyとSecurity Gateを免除、はいずれも不採用。

## ADR-036〜040: Python-first アーキテクチャ移行（2026-11-09、STEP 1）

> **状態の意味**: ADR-036はユーザーが確定した開発言語方針（`PRODUCT_FIXED`）。ADR-037/039/040は`PROPOSED`、ADR-038は**STEP 2で選定する`OPEN`**。どれも実装・実行・Owner署名を伴わず、`LOCKED`/`VERIFIED`ではない。ADR-002/017〜021（VM分離・Sentinel・Credential Broker・Runtime Cell）とADR-023〜035の契約は**一切緩和しない**。物理トポロジーの正本は[System Architecture §13.2](system-architecture.md)、拘束規則は[Architecture Rules §E](architecture-rules.md)。

| ADR | State | Decision and rationale | Alternative rejected/held | Owner / verification |
|---|---|---|---|---|
| **ADR-036** | PRODUCT_FIXED（2026-11-09 ユーザー決定） | **Python-first**。Python＝Backend/API/Agent Runtime/MCP/Browser Worker/Memory/Automation/自律開発の主言語。TypeScript＝React Web UI・ブラウザ側Clientの主言語。SQL＝PostgreSQL DDL/Transaction/Migrationの正本。Rust＝実測で必要と判断された局所最適化に限る任意言語。理由: PydanticAI/DBOS/LangGraph/DeepAgents/Playwright/Scrapling/Crawl4AI/MCP/OpenHands等のPython OSSを**同一プロセス言語で直接利用**し、Python Sidecarという言語ブリッジと二重Schemaを無くす。 | TypeScriptモノレポ＋Python Sidecar（旧project-brief §9の方向。**履歴として保持し現行方針ではない**）、全面Rust/Go化、根拠のない性能理由による再実装 | CHAT_00。静的: `audit/cross-contract/check_python_first.py`。実装時: §E-9のCI |
| **ADR-037** | PROPOSED | **共有契約の正本は言語非依存のまま**。CHAT_01の論理契約→版付きcanonical JSON Schema（`contracts/`配下、CHAT_01所有）を唯一の正本とし、Python Pydantic v2 modelと、FastAPIが出すOpenAPI、UI用TypeScript型はそこから**生成または機械照合**する。仕様中の`~~~ts`/`typescript`ブロックは**論理型記法**であり実装言語の指定ではない。Pydanticを採用してもSchema所有権はCHAT_01からPythonコードへ移らない。 | Python/TSで別々に手書き、Pydantic modelを暗黙の正本化、OpenAPIだけを正本にして非HTTP契約（Event/Checkpoint/Tool I/O）を失う | CHAT_01/08/10/11。生成差分0のCI gate、wire互換fixture |
| **ADR-038** | **OPEN（STEP 2で選定）** | 主Agent Runtimeは**候補A: PydanticAI＋DBOS** と **候補B: DeepAgents Python＋LangGraph** を並記保持。どちらもaiagent-mdの`AgentRuntimePort`（[Agent Loop「2026-11-09 Python-first: Agent Runtime Port」節](../02-runtime/agent-loop.md)）の**内側**に置き、Task/Run/Checkpoint HEAD/Tool Intent/Effect Ledger/Approvalの正本はPostgreSQL上のaiagent-md契約に残す。Framework側のcheckpoint/stateは再構築可能な派生物。 | STEP 1での片方の採用確定、Framework runner/checkpointをTask/Run正本にする（agent-loop不採用Bと同旨） | CHAT_02/03/10/11。§ADR-038判定基準 |
| **ADR-039** | PROPOSED（製品選定はADR-038に従属） | Durable Job/Scheduleは`QueuePort`＋PostgreSQL Transactional Outboxを正本とし、**Queue/Workflow基盤は1系統のみ**。pg-bossはNode専用のため採用候補から外し、設計参考（attempt fence/scheduler挙動）として保持。候補: DBOS（ADR-038でA採用時に統合）、Dramatiq/Celery（Broker追加が必要）、Temporal Python SDK（別クラスタ）、APScheduler（時刻計算の補助のみ、Queueの正本にしない）。 | 複数Queue/Workflowの併用、Queue製品のexactly-onceを外部副作用exactly-onceと同一視 | CHAT_07/10/11。JQ/SCHのkill・ACK-loss試験 |
| **ADR-040** | PROPOSED | Python実行基盤: 推奨**CPython 3.13**（下限互換CIは3.12）、`uv`（依存/lock）、`ruff`（lint/format）、`pytest`、型検査は`mypy --strict`（pydantic plugin）を第一候補・`pyright`代替。Backendは単一`uv` workspace、特殊依存（Scrapling/Crawl4AIのBrowser依存、OCR等）は独立Worker imageを許可。lockfile・image digestは実装段階で固定。 | Python 3.12への無条件固定、Python 3.14/3.15を全依存の対応確認前に採用、Worker毎に異なる依存管理ツール | CHAT_10/11。§ADR-040根拠 |

### ADR-038 判定基準（STEP 2で実測・Owner reviewする事項）

| 評価軸 | 候補A: PydanticAI＋DBOS | 候補B: DeepAgents Python＋LangGraph | 合格条件（両候補共通） |
|---|---|---|---|
| 構造化I/O・Tool Schema | Pydantic型で入出力・Tool引数を直接検証 | LangChain Tool/State schema経由 | canonical JSON Schemaと生成差分0、未知field拒否 |
| Durable execution / 再開 | DBOS workflow/step（PostgreSQL上） | LangGraph checkpointer（`langgraph-checkpoint-postgres`）・interrupt/resume | Worker kill後、aiagent-md checkpoint HEADから再開し**Tool Intent再送0** |
| 承認・中断 | deferred/unapproved tool | `interrupt()`/HITL | Approval正本はCHAT_03、Framework内の承認状態を権限に使わない |
| Context/Subagent/Skills | 自前Port実装が多い | DeepAgentsのsummarization/offload/subagent/skills/filesystem backend | ContextManifest・SourceBackedFact・Sealed ModelRequestSnapshotを外部から強制できる |
| Queue統合 | DBOS queue/schedulingを流用可能（ADR-039と一体） | 別途Queue選定が必要 | Queue/Workflow基盤1系統 |
| 依存・ライセンス | pydantic-ai MIT、dbos MIT | deepagents MIT、langgraph MIT | 推移依存のライセンス/NOTICE監査 |
| 失敗モデル | DBOS step再実行と外部副作用の区別 | super-step再実行と副作用の区別 | 外部効果は全てGateway Ledger経由、UNKNOWN/MAY_HAVE_SENT保持 |
| 置換容易性 | — | — | `AgentRuntimePort`適合試験を両候補で同一fixtureで実行 |

**STEP 1時点の確認済み事実（2026-11-09 PyPI/GitHub API取得、コード実行なし）**: `pydantic-ai 2.66.0`(requires-python>=3.11, MIT)、`dbos 3.4.0`(>=3.10, MIT)、`deepagents 0.7.34`(>=3.11,<4.0, MIT)、`langgraph 1.2.20`(>=3.10, MIT)、`langgraph-checkpoint-postgres 3.2.0`(>=3.10, MIT)。DeepAgents Python main `0279f1d036a8a73ea0d99bf04583e9c9a9598ef2` に `middleware/summarization.py`、`middleware/_blob_offload.py`、`middleware/_message_eviction.py`、`backends/store.py` が**存在することのみ確認**（内容監査・テスト実行は未実施）。PydanticAI main `9860083ceabe4159d087673b6ee8372cd9750b99`、DBOS main `236142bd7a99ff77d04becf5f405a60d6f330591`、LangGraph main `e5e37b5d70dbfd93c7006bd90a658c6179a95946`。

### ADR-040 根拠（Python version）

- `requires_python`: openhands-sdk 1.60.2 `>=3.12`、pydantic-ai/deepagents/SQLAlchemy 2.1 `>=3.11`、その他候補の多く`>=3.10`。→**下限は3.12**。
- Trove classifierで宣言された上限: langgraph・scrapling 0.5.1・Crawl4AI 0.9.5が**3.13まで**、pydantic-ai/dbos/playwright 1.64.1/mcp 2.3.0/temporalio/dramatiqは3.14まで宣言。classifierは宣言であって動作保証ではない。
- CPythonサポート期限（endoflife.date取得）: 3.12 security 2028-10-31、3.13 2029-10-31、3.14 2030-10-31。3.12は既にbugfix終了。
- 結論: **3.13を実装基準の推奨**、3.12を下限互換CI、3.14は全依存の宣言/実測が揃った時点で再評価。Ubuntu/Docker base imageのPython版は実装時に公式image digestで固定し、OS同梱Pythonへ依存しない。

### Python-first 移行監査台帳（STEP 1、静的監査）

分類: **A**＝現行採用方針がTS/Node前提で本PRで修正、**B**＝妥当なため保持（Web UI、上流OSSのソース検証引用、履歴、Cell内でのNode/JS実行、論理型記法）、**C**＝Python代替・実装可否・ライセンスに不確実性があり再検討が必要。`audit/cross-contract/check_python_first.py`は、TS/Node採用を示す語を含むすべての`docs/**/*.md`がこの台帳に載っていることを検査する。

| File | 分類 | 処置 / 理由 |
|---|---|---|
| `docs/00-foundation/README.md` | A | CHAT_01型を「TypeScript定義」から言語非依存の論理型（JSON Schema正本）へ |
| `docs/00-foundation/architecture-decisions.md` | A | ADR-036〜040と本台帳を追加。旧Prior art（LangGraph JS docs等）は履歴 |
| `docs/00-foundation/architecture-rules.md` | A | §E Python-first拘束規則 |
| `docs/00-foundation/domain-glossary.md` | A | canonical TypeScript types→論理型（JSON Schema） |
| `docs/00-foundation/project-brief.md` | A | §9方向をPython-firstへ、旧TSモノレポ案を履歴化 |
| `docs/00-foundation/research-targets.md` | B | 上流OSS調査対象一覧（Mastra/pg-boss）。採用方針ではない |
| `docs/00-foundation/spec-index.md` | A | Python-first移行の索引注記（モジュール数不変） |
| `docs/00-foundation/system-architecture.md` | A | §9 Python package構成、§8 int64表現、§13.2実行トポロジー |
| `docs/01-contracts/core-domain-models.md` | A | 旧TSファイル配置を履歴化、Pydantic/生成型規則を追加 |
| `docs/01-contracts/error-taxonomy.md` | A | §5を論理型記法と明記。MCP TS SDK引用はB |
| `docs/01-contracts/event-envelope-and-stream.md` | A | Event Schema生成・配信規則を追加。MCP TS SDK引用はB |
| `docs/01-contracts/evidence-artifact-contracts.md` | B | `ts`論理型ブロックとMCP TS SDK引用（§E-2/E-10で解釈） |
| `docs/01-contracts/tool-io-schema.md` | A | Python sidecar→Extraction Worker、Pydantic/MCP Python SDK規則 |
| `docs/02-runtime/agent-loop.md` | A | 旧TS runtime前提を削除、AgentRuntimePortとSTEP 2候補A/Bを追加。Mastra引用はB |
| `docs/02-runtime/budget-and-limits.md` | B | Mastra/PydanticAI調査引用と論理型。BigIntは§E-3で解釈 |
| `docs/02-runtime/checkpoint-and-recovery.md` | A | 論理型見出し、Framework checkpointを派生物と規定 |
| `docs/02-runtime/context-manager-and-compaction.md` | A/C | §20: DeepAgents Pythonを第一調査、Mastra OM/OpenClawはPython Portとして独自実装（C） |
| `docs/02-runtime/goal-and-planner.md` | B | Mastra/PydanticAI調査引用と論理型 |
| `docs/02-runtime/middleware-lifecycle.md` | B | 調査引用と論理型 |
| `docs/02-runtime/model-router.md` | A | Model Gateway独立プロセス、Framework内Fallback無効化 |
| `docs/02-runtime/result-verifier.md` | B | 調査引用と論理型 |
| `docs/02-runtime/runtime-host-and-session.md` | A | 旧TS Host前提を削除、Python asyncio Workerプロセス構成 |
| `docs/02-runtime/sandbox-execution.md` | B | 論理型、Cell内npm/pip/Node実行は妥当 |
| `docs/02-runtime/steer-queue-pause-resume.md` | A | §5を論理型と明記 |
| `docs/02-runtime/subagent-delegation.md` | A | Python Sidecar表現をPython Worker/Sidecarへ |
| `docs/02-runtime/task-run-manager.md` | B | Mastra/pg-boss attempt fence調査は故障モデルの参考 |
| `docs/03-tools/mcp-adapter.md` | A | 公式MCP Python SDKを第一候補、TS SDK調査は参考記録 |
| `docs/03-tools/sandboxed-tool-pipelines.md` | B | 論理型と調査引用 |
| `docs/03-tools/side-effect-ledger.md` | B | pg-boss/Temporal TS SDK調査は故障モデル参考、Ledger契約は言語非依存 |
| `docs/03-tools/tool-approval-and-policy.md` | B | 論理型と調査引用 |
| `docs/03-tools/tool-execution-gateway.md` | A | Gateway独立Pythonプロセス、Framework tool stub規則 |
| `docs/03-tools/tool-registry-and-discovery.md` | A | Python実装・MCP Python SDK、OpenClaw/Mastra引用はB |
| `docs/04-web-browser/browser-action-engine.md` | B | 論理型 |
| `docs/04-web-browser/browser-session-manager.md` | A | Playwright Python Browser Worker |
| `docs/04-web-browser/data-extraction-and-normalization.md` | B | 論理型。Scrapling sidecarは§Extraction Workerとして読む |
| `docs/04-web-browser/http-fetch.md` | B | 論理型、Cell内Node fetchもSentinel対象という記述は妥当 |
| `docs/04-web-browser/jev-adapter.md` | C | PyPI jev-browserは別リポジトリ由来。由来・所有権を再監査 |
| `docs/04-web-browser/lightpanda-adapter.md` | A | Python Browser Worker内Adapterへ |
| `docs/04-web-browser/login-challenges-human-handoff.md` | B | Jev password.ts等の調査引用 |
| `docs/04-web-browser/scrapling-sidecar.md` | A/C | Sidecar→ネットワーク無しExtraction Worker、Crawl4AIの責務はC |
| `docs/04-web-browser/search-router.md` | B | 内部論理型（`.ts`パスは例示、Python module名へ読替え） |
| `docs/05-connectors/amazon-keepa.md` | B | 論理型、Python keepaライブラリ調査 |
| `docs/05-connectors/buyback-price-providers.md` | B | module-private論理型 |
| `docs/05-connectors/connector-framework.md` | A | 旧案: TypeScript host→Python host |
| `docs/05-connectors/hotel-research.md` | B | module-private論理型 |
| `docs/05-connectors/x-research.md` | C | node-twitter-api-v2→Tweepy等のPython client再検証 |
| `docs/06-memory-skills/memory-data-model.md` | A | §5.2を論理型提案へ |
| `docs/06-memory-skills/memory-retrieval-and-update.md` | A/C | pgvector第一候補、Graphiti/Mem0はC |
| `docs/07-automation/observations-and-trigger-evaluation.md` | B | pg-boss timekeeper調査は時刻故障の参考 |
| `docs/07-automation/scheduler-and-recurring-tasks.md` | A | 旧pg-boss第一候補を撤回、ADR-039 |
| `docs/07-automation/watch-lifecycle.md` | B | pg-boss/OpenClaw調査引用 |
| `docs/08-interfaces/approval-and-browser-takeover-ui.md` | A | Next.js/Fastify→React TS/FastAPI/Playwright Python |
| `docs/08-interfaces/channel-adapters-telegram.md` | C | grammY（TS）→Python Telegram client再調査 |
| `docs/08-interfaces/realtime-events.md` | A | Zod→JSON Schema/Pydantic、FastAPI SSE |
| `docs/08-interfaces/rest-api.md` | A | FastAPI実装境界、外部HTTP契約は不変 |
| `docs/08-interfaces/task-and-watch-dashboard.md` | B | Web UI（React/TS）の調査と構成 |
| `docs/08-interfaces/web-chat-interface.md` | A | Next.js Server境界→FastAPI認可、UIはReact TS |
| `docs/09-security/authentication-and-user-isolation.md` | A/C | Better Auth（TS）を既定不採用、Python AuthProviderライブラリはC |
| `docs/09-security/network-egress-and-ssrf.md` | B | Cell内Python/Node通信もSentinel対象（妥当） |
| `docs/09-security/runtime-cell-isolation.md` | B | Cell内Node.js実行は妥当。Python同一言語でも境界非統合の注記を追加 |
| `docs/09-security/sandbox-and-capabilities.md` | B | Chromium内JS実行・Cell実行に関する妥当な記述 |
| `docs/09-security/secrets-and-credentials.md` | B | Better Auth/Playwright調査引用 |
| `docs/10-infrastructure/backup-upgrade-and-recovery.md` | A | pgboss metadata→ADR-039選定基盤のmetadata |
| `docs/10-infrastructure/database-schema-and-migrations.md` | A | DB-X06、Python driver/Migration runner規則 |
| `docs/10-infrastructure/docker-deployment.md` | A | Fastify→FastAPI、Extraction Workerのnetwork無し化 |
| `docs/10-infrastructure/job-queue-and-outbox.md` | A/C | pg-boss撤回、Python Queue候補比較（DBOSはADR-038に従属） |
| `docs/11-qa/benchmark-and-release-gates.md` | A | Python-first静的Gateの位置付け |
| `docs/11-qa/fault-injection-and-concurrency.md` | B | pg-boss/Mastra fault testは言語非依存の反例として有効 |
| `docs/11-qa/test-harness-and-fixtures.md` | A | pytest/Playwright Python基盤、MSWはUI限定 |
| `docs/13-event-ingestion/event-driven-agent.md` | B | pg-boss調査引用（Queue採否はCHAT_10/ADR-039） |
| `docs/14-adaptive-experience/feedback-learning.md` | B | 論理型 |
| `docs/14-adaptive-experience/learned-browser-actions.md` | B | 論理型 |
| `docs/15-multimodal/multimodal-ingestion.md` | B | 論理型、parserは隔離Python Worker |
| `docs/16-app-development/README.md` | C | OpenHands SDK等の統合はSTEP 2で検証 |
| `docs/16-app-development/code-intelligence-and-adaptive-testing.md` | B | 対象プロジェクトの言語サポート一覧（TS/JS含む） |
