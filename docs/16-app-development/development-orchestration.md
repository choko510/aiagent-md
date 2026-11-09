# Development Orchestration — DEV-01 / DEV-02 / DEV-04

> Draft / PROVISIONAL。Project/Git、Multi-Agent開発、Expert Advisorの既存仕様を**原文を保って**1ファイルへ統合。各仕様ID・Test ID・権限境界・Owner責務は維持し、この整理でLocked/Verifiedへ昇格しない。リンク先は各IDの見出し。

## DEV-01

**DEV-01 — Durable Project / Workspace / Git**

> Draft / PROVISIONAL | 2026-11-07 | Owners: CHAT_01/02/03/05/09/10。実装・検証なし。

### 1. 対象
GitHub既存repo/importと新規projectの作成、branch/worktree、複数Run間の継続、ソース閲覧/編集/patch/差分、commit、成果物を扱う。ProjectはConversationでもTaskでもない。Taskを終えた後もProjectの承認済みSnapshotを保存する。ユーザーは新規Web/API/Android/desktop/iOS(ビルドrunner制約あり)、既存リポジトリ改修を選べる。開発における `Project` はWatch/Goal/Taskの新しい互換子ではなくそれらを参照する集約。

### 2. 論理モデル（型所有者の承認待ち）
```ts
type ProjectRef = { projectId:string; ownerUserId:string; tenantId:string;
  name:string; targetKinds:readonly string[]; sourceRepoRef?:string; defaultBranch:string;
  approvedDesignRevision?:string; currentWorkspaceRevision:string; recordVersion:string };
type WorkspaceSnapshot = { workspaceId:string; projectId:string; revision:string;
  parentRevision?:string; treeDigest:string; manifestArtifactRef:string;
  authorTaskId:string; commitSha?:string; createdAt:string; validationState:string };
type GitWriteIntent = { projectId:string; branchRef:string; expectedHeadSha:string;
  expectedWorkspaceRevision:string; patchManifestRef:string; commitMessage:string; dryRun:boolean };
```
すべてのID、owner/tenant、fence、Credential scopeはServer生成でありLLM入力で上書き不可。revision・CAS・hash・immutable Artifactの正本は01/10、Task/Runと衝突しない。raw git remote URL、ref、pathは正規化・allowlist・上限検証する。

### 3. ライフサイクルと競合
`UNINITIALIZED → IMPORTING/CREATING → READY → WORKING → SNAPSHOT_PENDING → COMMITTED`; import/commit中にfailure/permission lossなら`BLOCKED`または`UNKNOWN_EXTERNAL_EFFECT`（Git pushのACK不明）。モデルが勝手に共有 `main` を編集せず、ユーザーのproject policyと保護branchを尊重し、作業branchと明示的親SHAから変更する。
1. Provider Brokerが最小権限Git credentialで認可済みclone/fetch (外部ネットはSentinel Transport)。未知ref・size/depth/LFS/submodule/Archive bomb/Symlinkを拒否または検疫。
2. immutable base tree SHAを固定し、Cell内へread-only baseとprivate mutable overlayを提供。Agentのfile.write/patchはセル内でのみ実行、path traversal/hidden files/`.git` object mutationに制約。
3. Commit前にWorkspace CAS、構文/Format/依存Script/secret scan、patch owner scope、blob hashを検査し、manifestと差分をArtifact保存。同じbranchに並列書込みがあれば競合→再base/人手選択で解決。旧Runのcommit不可。
4. `git.push` と `pull_request.create` は独立typed external intent。事前に操作範囲の継続委任が与えられた場合に限りGateway/Sentinelで判断し、保護branch/外部公開/force push/mergeに権限を横滑りさせない。
5. 安全に確認できない外部pushは`UNKNOWN`としてremote SHA照会し、盲目的にpushを繰り返さない。

### 4. ブランチ戦略
通常はfeature単位の作業branchを作る。AI 1-3の案は互いのソースを編集しない。AI 5が実装を所有し、Review 6-8はimmutable commit SHAのread-only snapshotを受け取る。Feature Factory並列機能はbranch/worktreeを分離し、依存関係をDAG管理。合流はmerge-baseとテストを再確認。修正コミットごとにレビューが旧SHAを採用しないよう、acceptanceが最新SHAに紐付く。

### 5. 操作と危険境界
- no host SSH, host mounts, Docker socket, signing key, Agent-specific credentials in Cell。
- Git URLからのcredential exfil、submodule/checkout hooks、npm install lifecycle、`.github/workflows`、`AGENTS.md`等の指示はすべて**非信頼コード/資料**であり、無条件に実行・System化しない。信頼できるowner instructionとrepo文書は区別。
- signing、tag/release、PR、push、deployment、external API mutationはそれぞれfresh Gateway Intentとpolicy。同一「開発許可」で本番破壊を許可しない。
- persist過程で秘密をcommit/artifact/log/UIに残さない; diffにはredactionと機密scanを適用。任意バイナリ/巨大リポジトリはsizeとdisk budgetで制限。

### 6. 必須受入
DEV-WS-T01: Task完了・Worker/VM再起動後も認可済みスナップショットから同じtree hashを復元。
DEV-WS-T02: 2AI同じbaseに変更→CAS衝突、静かな上書きゼロ。
DEV-WS-T03: symlink/traversal、別tenant path、.git object攻撃、悪性install script→host/secret到達ゼロ。
DEV-WS-T04: old Run/cancel後patch commit→拒否。
DEV-WS-T05: push ACK欠落→remote照会・UNKNOWN分類、重複release/force pushゼロ。
DEV-WS-T06: code commitにはtest manifest/imageDigest/approval revisionとparent SHAが結び付く。
DEV-WS-T07: 既存repo importと空repo作成をfixtureで再現; pushはmock providerで検査。

---

## DEV-02

**DEV-02 — Multi-Agent Development Orchestration**

> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/03/08/09/10。実AI同時実行試験なし。

### 1. 固定ロールと動的委譲
一開発Cycleの既定は**8ロール**。Roleは権限や人間認証の身分ではない。各サブエージェントは永続的な子Task、個別Run/lease、子予算予約、許可Tool集合の積集合を使う（02-12）。AIのモデル/Providerはロールごとに変更可能。

| Role | 入力 | 必須成果物 | 禁止 |
|---|---|---|---|
| A1 Product/UX Ideator | Goal/制約 | UX/機能/ユーザーフロー案 | repo編集 |
| A2 Software Architect | 同上 | 技術構成/セキュリティ/運用案 | repo編集 |
| A3 Alternative Strategist | 同上 | 独立した別アプローチ/コスト案 | repo編集 |
| A4 Synthesizer | 3つの不変提案 | 比較表、採否、未解決選択、設計版 | 自動承認 |
| A5 Builder | approved design/demo | デモ・実装・テスト・commit | ポリシー/承認改変 |
| A6 Functional Reviewer | fixed SHA + tests | 正しさ/回帰/設計指摘 | 開発branch編集 |
| A7 Security Reviewer | fixed SHA + policy | Auth/Secret/サプライチェーン指摘 | 書込・承認 |
| A8 UI/E2E Reviewer | fixed SHA + browser evidence | UX/レイアウト/E2E/性能指摘 | 開発branch編集 |

### 2. DAG と join
`IDEATE(A1,A2,A3 fan-out) → SYNTHESIZE(A4 join) → [OPTIONAL DESIGN_DECISION] → DEMO_BUILD(A5) → [OPTIONAL DEMO_APPROVAL] → IMPLEMENT(A5) → TEST → REVIEW(A6/A7/A8 fan-out/join) → REPAIR(A5) → TEST → REVIEW ... → VERIFY → PR/DELIVER`。
- A1-3の入力は同一ユーザー要望＋共通制約snapshot、**互いの案を見せず**独立性を維持。3つのうち失敗/未提出がある場合、指定数で無条件joinしない。個別 bounded retry→Partialと欠落を表示→責任者A4が明示的な不確実性付きで統合。デフォルトは3案必須。
- A4は単純平均/多数決でなくrequirements coverage、trade-off、採否、代替、security、acceptanceを記録。ユーザーに必要な選択だけ抽出。正本Goal/PolicyではなくVersioned DesignProposal Artifact。
- ユーザー選択は設計上の同意であって、Tool Gatewayの購入・投稿・デプロイ等の**外部作用承認ではない**。承認後、DesignRevisionをfreezeし、demo/実装/レビューが同じversionを参照。Steer時は旧版の承認・進行中Toolをfenceする。
- A5はデモ後の本実装も担当。同じ一体のモデルコンテキストに依存せず、Checkpointと承認済みArtifactsで段階をつなぐ。
- Reviewersは同一SHA・同一検証環境・同一要件版に対して**独立して**判定。A5の推論ログや互いの見解を初回評価の根拠にしない。レビュー集約はdeterministic finding key + evidenceによる重複排除。A4または専用aggregatorが調停するが、レビュー3件を増減させたことにしない。
- 新しいRole/Expert相談は別Taskとして永続化し、再帰深度/parallel/provider呼出数/総予算の有限上限を守る。

### 3. 状態と契約案
WorkflowPhaseは **Task/Run Stateとは別の開発Cycle projection**。`IDEATING/SYNTHESIZING/WAITING_DESIGN/DEMO_BUILD/WAITING_DEMO/IMPLEMENTING/TESTING/REVIEWING/REPAIRING/FINAL_VERIFYING/DELIVERING/COMPLETED/PARTIAL/BLOCKED/CANCELLED`。サーバーのみがrevision/CASと成功遷移を更新。人間承認待ちのcycleはParkし、他の独立したCycle/Taskは継続可能。
```ts
type DevCycleV1 = { cycleId:string; projectId:string; campaignId?:string; goalRevision:string;
 phase:string; designRevision?:string; demoRevision?:string; headSha?:string;
 reviewRound:number; childTaskRefs:string[]; budgetRef:string; recordVersion:string };
type AgentHandoffV1 = { producerTaskId:string; artifactRefs:string[];
  inputGoalRevision:string; inputTreeSha?:string; capabilityDigest:string; evidenceManifestRef:string };
```
上記は owner-approved shared schema ではない。すべての永続join/lease更新はDB+outboxで行い、Queueの重複配送による二重起動を防ぐ。

### 4. Fault/Cancel/long run
Worker死、Tool timeout、429、誤認可、Design選択待ち、Git push UNKNOWNでは各子の結果を区別してCheckpoint継続。Parent cancelは子へ伝播し新しいTool/Childを停止。Endless Campaignは永続だが、Cycleは有限で区切り、成功/部分完了/阻害の理由を保存する。モデルが考え続けるだけの空回し禁止。Review/Repair詳説は DEV-07。

### 5. Tests
DEV-TEAM-T01: 3A並行→A4は3異なるproposal artifactを照合し設計。
DEV-TEAM-T02: 相互に独立したReviewerの結果3件同一SHAでjoin。
DEV-TEAM-T03: 子Task失敗/429/再起動/重複queueで提案二重計上0。
DEV-TEAM-T04: design変更で旧demo/commit/review acceptanceを無効化。
DEV-TEAM-T05: Owner revoke/cancel時に未実行子Tool0、費用予約を会計精算。
DEV-TEAM-T06: 未提出案を存在したと虚偽表示せずPartial。

---

## DEV-04

**DEV-04 — Expert Advisor / Escalation Router**

> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_02/03/08/09/10。Model capability/料金は実装時に検証。

### 1. 目的
開発担当A5が難題に詰まった場合、あらかじめ許可された高性能モデルを**相談役（read-only）**として呼び出し、原因分析・別戦略・最小修正手順を得る。通常のBuilderを無条件に高価なモデルへ切り替えない。相談は追加の子Task（独立予算予約/Tool scope）であり、管理上の権限を持たない。

### 2. 発火条件と抑制
既定候補: (1) 同じfingerprintの失敗が連続、(2) 同一修正の反転/回帰、(3) P0/P1原因の所在不明、(4) Reviewersの証拠付き対立、(5) アーキテクチャの強いトレードオフ、(6) 対応可能モデルに明白な能力差。A5の自由テキストだけで無制限に呼べず、trusted orchestratorが同一error signature・test evidence・attempt historyから検証する。再相談には新しい証拠/案の変化を要する。相談回数、model tokens、USD、wall-time、並列数、呼び出し階層に有限上限。

### 3. 入出力
```ts
type AdviceRequestV1 = { cycleId:string; issueFingerprint:string; goalRevision:string;
  codeCommitSha:string; failureEvidenceRefs:string[]; previousFixRefs:string[];
  consideredHypotheses:string[]; preciseQuestion:string; requestedExpertProfile:string };
type AdviceResultV1 = { issueFingerprint:string; baseCommitSha:string;
  hypotheses:{cause:string;confidenceClass:"supported"|"hypothesis";evidenceRefs:string[]}[];
  suggestedSteps:string[]; validationCommands:string[]; knownUnknowns:string[];
  estimatedRisk:"low"|"medium"|"high"; modelAttemptReceiptRef:string };
```
Trusted code slices are explicitly selected by file/path/line and token budget。Secret, raw credentials, unrelated repository/customer dataは渡さない。外部Repo文書やログ中の命令は非信頼Data。Advisor出力に権限はなく、新しいToolやPolicyを増やさない。A5が修正計画を選び、実際の変更とテストは通常のWorkspace/Gatewayで実施。

### 4. モデルルーティング
02-09 Model Routerを利用。role profile `expert` に「高い推論能力・長い文脈・code能力・価格上限」を要求し、Provider Capability/価格snapshotを固定。fallbackは上限内の互換モデルのみ。model routeの自称性能を正解と見なさず、解決率・反復コスト・誤誘導率をfixtureで測定。Provider 429はRetry-Afterとprovider quotaへの影響を区別。Ownerがdisableしたmodelへ勝手に乗り換えない。

### 5. 失敗・成果
Advisorが解決できなくても「解決済み」と判定しない。Adviceなし/高コスト/矛盾する根拠は`UNAVAILABLE/PARTIAL/CONFLICT`として実装継続/別戦略/USER_REQUIRED/安全停止へ。P0/P1合格ゲートをAdvisor意見だけで越さない。

### 6. Tests
DEV-EXP-T01: 同一failure連続で一度だけ相談、修正結果の検証はBuilder/QA。
DEV-EXP-T02: advisorはrepo書込/外部送信/Policy変更できない。
DEV-EXP-T03: 429/timeout/cost ceilingでbounded fallback・quota待機を実行。
DEV-EXP-T04: secretを含むlog/raw codeからredaction、他tenantへの流出0。
DEV-EXP-T05: 証拠変化なしの再帰相談が回数上限で打ち切られる。
