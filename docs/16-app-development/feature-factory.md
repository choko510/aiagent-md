# DEV-08 — Feature Factory: Up to Ten Approved Features (or Continuous)
> Draft / PROVISIONAL | 2026-11-07 | Owners CHAT_01/02/07/08/09/10/11 | 実装なし。

## User Goal
「このアプリへ新機能を10件実装。3AIで考え、1AIで統合、**動く本実装に近いDEMO**を提示。OKしたものだけ本実装→3AIレビュー→P0/P1解決→次へ」。既定目標10件は設定値であり上限例。長期モードは停止指示または利用上限まで繰り返せるが、各Cycleは有限に区切る。

## Feature lifecycle
`PROPOSED → SYNTHESIZED → DEMO_BUILDING → WAITING_DEMO_DECISION → APPROVED/REJECTED/REVISION_REQUESTED → IMPLEMENTING → TESTING → REVIEWING → DONE`。Design decision/実Demo決定はGatewayの外部行為approvalと別。Revision requestは既存承認を失効し、更新したDemoの再確認を要する。Rejectは後から明示的にUndo可能だが、同じ提案を直ちに延々再提案しない。

```ts
type FeatureCandidateV1 = { featureId:string; projectId:string; campaignId?:string;
 proposalRefs:string[]; synthesisRef:string; dedupeFingerprint:string;
 dependencies:string[]; designRevision:string; demoRef?:string;
 userDecision:"PENDING"|"APPROVED"|"REJECTED"|"REVISION_REQUESTED";
 state:string; implementationSha?:string; verificationReceiptRef?:string;
 countReceiptRef?:string; recordVersion:string };
```

## Counting / competition / concurrency
「10件達成」は**承認済・実装・必要テスト実施PASS・3レビュー完了・P0/P1未解決0**のdistinct featureIdごとに原子的に一度だけcount receipt。提案10件、未承認Demo10件、commitだけ10件では未達。導入/merge/release済みかは別のstatusにする。
- `#5 IMPLEMENTING`, `#6 WAITING_APPROVAL`, `#7 SYNTHESIZING`, `#8 DEMO_BUILDING` のように待機中でも他の独立作業を継続する。各Featureのbranch、workspace CAS、subTask予算を分離し、同じschema/ファイルを競合更新する場合は依存DAG/直列化で調停。
- 候補は価値/重複/ユーザー制約/性能/セキュリティ/実装コスト/保守負荷で比較。3AIの新規提案に既存実装・却下履歴・ユーザーの禁則を与え、理由なき類似提案を重複させない。
- Backendのみの機能もAPI fixture/CLI observable demoを提示し、モックと実装範囲を区別。UIのScreenshotだけで「動くDEMO」と呼ばない。
- ユーザーがデモを確認するまで当該Featureの本実装は始めない。preview中の外部API/購入/削除を実行しない。許可されたデザイン変更や手動採用部品はDesignRevisionで固定。
- 承認待ちだけが残る場合、CampaignはWAITING_INPUTでparkし無駄なLLMを連打しない。他独立タスクがあれば進める。

## Tests
DEV-FF-T01: DONE4件+pending2+implementing2→progress=4。
T02: denied/only demo/duplicated/oldSHA/missing reviewで件数増分0。
T03: pending featureが他独立Featureの実装を妨害しない。
T04: 共同schema更新はCAS競合時にfail/serial。
T05: 最終10件目は原子countで一度だけ達成。
T06: Demo revision変更は旧承認無効、実装開始0。
