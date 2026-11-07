# DEV-02 — Multi-Agent Development Orchestration
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/03/08/09/10。実AI同時実行試験なし。

## 1. 固定ロールと動的委譲
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

## 2. DAG と join
`IDEATE(A1,A2,A3 fan-out) → SYNTHESIZE(A4 join) → [OPTIONAL DESIGN_DECISION] → DEMO_BUILD(A5) → [OPTIONAL DEMO_APPROVAL] → IMPLEMENT(A5) → TEST → REVIEW(A6/A7/A8 fan-out/join) → REPAIR(A5) → TEST → REVIEW ... → VERIFY → PR/DELIVER`。
- A1-3の入力は同一ユーザー要望＋共通制約snapshot、**互いの案を見せず**独立性を維持。3つのうち失敗/未提出がある場合、指定数で無条件joinしない。個別 bounded retry→Partialと欠落を表示→責任者A4が明示的な不確実性付きで統合。デフォルトは3案必須。
- A4は単純平均/多数決でなくrequirements coverage、trade-off、採否、代替、security、acceptanceを記録。ユーザーに必要な選択だけ抽出。正本Goal/PolicyではなくVersioned DesignProposal Artifact。
- ユーザー選択は設計上の同意であって、Tool Gatewayの購入・投稿・デプロイ等の**外部作用承認ではない**。承認後、DesignRevisionをfreezeし、demo/実装/レビューが同じversionを参照。Steer時は旧版の承認・進行中Toolをfenceする。
- A5はデモ後の本実装も担当。同じ一体のモデルコンテキストに依存せず、Checkpointと承認済みArtifactsで段階をつなぐ。
- Reviewersは同一SHA・同一検証環境・同一要件版に対して**独立して**判定。A5の推論ログや互いの見解を初回評価の根拠にしない。レビュー集約はdeterministic finding key + evidenceによる重複排除。A4または専用aggregatorが調停するが、レビュー3件を増減させたことにしない。
- 新しいRole/Expert相談は別Taskとして永続化し、再帰深度/parallel/provider呼出数/総予算の有限上限を守る。

## 3. 状態と契約案
WorkflowPhaseは **Task/Run Stateとは別の開発Cycle projection**。`IDEATING/SYNTHESIZING/WAITING_DESIGN/DEMO_BUILD/WAITING_DEMO/IMPLEMENTING/TESTING/REVIEWING/REPAIRING/FINAL_VERIFYING/DELIVERING/COMPLETED/PARTIAL/BLOCKED/CANCELLED`。サーバーのみがrevision/CASと成功遷移を更新。人間承認待ちのcycleはParkし、他の独立したCycle/Taskは継続可能。
```ts
type DevCycleV1 = { cycleId:string; projectId:string; campaignId?:string; goalRevision:string;
 phase:string; designRevision?:string; demoRevision?:string; headSha?:string;
 reviewRound:number; childTaskRefs:string[]; budgetRef:string; recordVersion:string };
type AgentHandoffV1 = { producerTaskId:string; artifactRefs:string[];
  inputGoalRevision:string; inputTreeSha?:string; capabilityDigest:string; evidenceManifestRef:string };
```
上記は owner-approved shared schema ではない。すべての永続join/lease更新はDB+outboxで行い、Queueの重複配送による二重起動を防ぐ。

## 4. Fault/Cancel/long run
Worker死、Tool timeout、429、誤認可、Design選択待ち、Git push UNKNOWNでは各子の結果を区別してCheckpoint継続。Parent cancelは子へ伝播し新しいTool/Childを停止。Endless Campaignは永続だが、Cycleは有限で区切り、成功/部分完了/阻害の理由を保存する。モデルが考え続けるだけの空回し禁止。Review/Repair詳説は DEV-07。

## 5. Tests
DEV-TEAM-T01: 3A並行→A4は3異なるproposal artifactを照合し設計。
DEV-TEAM-T02: 相互に独立したReviewerの結果3件同一SHAでjoin。
DEV-TEAM-T03: 子Task失敗/429/再起動/重複queueで提案二重計上0。
DEV-TEAM-T04: design変更で旧demo/commit/review acceptanceを無効化。
DEV-TEAM-T05: Owner revoke/cancel時に未実行子Tool0、費用予約を会計精算。
DEV-TEAM-T06: 未提出案を存在したと虚偽表示せずPartial。
