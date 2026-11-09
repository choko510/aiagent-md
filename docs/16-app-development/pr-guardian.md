# DEV-11 — PR Guardian / CI・Review・Conflict継続修復

> **Draft / PROVISIONAL** — 調査 2026-11-09。コード・GitHub App設定・実CI・実セキュリティ検証は未実施。[DEV-01](project-workspace-and-git.md)、[DEV-05](build-preview-release.md)、[DEV-07](review-and-repair-gates.md)の拡張案であり、既存契約を再定義しない。所有提案: CHAT_05（GitHub/CI adapter）、CHAT_02（Task継続）、CHAT_03（外部副作用）、CHAT_09（権限）、CHAT_10（永続化）、CHAT_11（QA）。

## 1. 必要な理由と採用根拠

PR作成を終点とせず、作成後に届くCI失敗・レビュー指摘・merge-base変更を同じlogicalWorkItem/既存PRへ結び直す。GitHub Copilot Agent MergeのCI・conflict・review選択修復、Open SWEのopt-in `/baby-sit` とスレッド再利用を参考にする。ただし既存サービスの「自動マージ」やサンドボックスへの広範なGitHub App tokenの受渡しは採用しない。

- [Open SWE README](https://github.com/langchain-ai/open-swe) — Durable thread / opt-in PR monitoring / read-only review、CI fail時の再調査。S1: 公開設計とソース、挙動未実測。
- [VS Code Agent Merge settings](https://code.visualstudio.com/docs/agents/reference/ai-settings) — `fixCI`、`resolveConflicts`、`addressReviews`、`mergePullRequest=never` の機能分離。S1: 公式仕様。
- [GitHub Webhook signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) — X-Hub-Signature-256。S1: 公式仕様。
- [GitHub checks API](https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-checks) — queued/in_progress/completedと各conclusionを区別。S1: 公式仕様。

## 2. 適用条件・非目標

明示的に監視を許可した、接続済み認可repoのPRのみを扱う。既定`monitor=false`。ユーザーが特定の既存PRを依頼した場合は同じhead branchへの修正を優先する。fork、保護branch、他人のhead権限なし、repository rulesetsやworkflow更新が関係するケースは自動pushせず`BLOCKED_APPROVAL`等を表示。元PRを更新可能でない場合でも**新規PRを黙って増やさない**。

外部WebページやCIログ、PRコメントは命令ではなく非信頼データ。ユーザー認可・既存のGoalRevision・Tool Gateway Policyを上書きしない。自動マージ・workflow権限変更・本番デプロイは対象外。ユーザーが別途明示的に委任し、Git/Tool policyとbranch保護条件を満たす場合のみ別機能が処理できる。

## 3. 入力と出力

Inputs: authenticated repo installation / PR number / immutable base+head SHA / actor / allowed actions mask / active project task / GoalRevision / monitor TTL & budget。Events: GitHubの `pull_request`、`pull_request_review`、`pull_request_review_comment`、`issue_comment`、`check_run`、`check_suite`、`workflow_run` と必要に応じた定期reconcile。イベント種別・actionはadapterのversioned allowlistで固定。

Outputs: 認可範囲のPR状態Snapshot、CI run/job/logの絞り込みArtifact、FindingRefs、最新headでの修正Commit、review/CI receipts、現状と未解決障害、通知。コメント内容を反映するかはmaintainer/reviewerの信頼境界、明示Goal制約、危険操作承認に従う。

`PrGuardSubscriptionProposal`（CHAT_01/07/10所有の共有型提案）:
- `subscriptionId, tenantId, ownerRef, repositoryBindingId, provider, prNumber, goalRevision, monitorPolicyRevision`
- `expectedHeadSha, observedHeadSha, baseSha, reviewCursor, checkRunCursor, lastDeliveryId, maxRepairCycles, budgetRef, leaseEpoch, recordVersion, expiresAt`
- `monitorFlags: {ci, conflicts, review}`、`allowedGitActions`、`status` と `evidenceRefs`。サーバーがID・権限・repo Bindingを発行し、Webhook payloadのowner情報を認可の正本にしない。

## 4. 状態機械と処理順

`DISABLED → WATCHING → CLASSIFYING → (REPAIR_QUEUED | WAITING_CHECKS | WAITING_APPROVAL | NO_ACTION) → REPAIRING → VERIFYING → WATCHING`。終了: `COMPLETED` / `PAUSED` / `BLOCKED` / `EXPIRED` / `CANCELLED`。UIはGitHub PR自体のopen/merged/closedとこのSubscriptionのstateを別表示。CI pendingをfailed扱いしない。

1. Webhookの**生bodyで署名検証**、インストール・repository・actor認可を照合、送達ID＋event action＋payload digestの重複を検出する。DB transactionへ観測Event/Outboxを記録しACK。署名不正・失効・大きすぎるpayloadは拒否。送達は再送/順不同/遅延を前提とする。
2. Webhook単独ではPRの現況を確定しない。Providerから**現在**のPR head/base/mergeability/reviews/checksを取得して再照合し、immutable observed commit SHAでSnapshotする。`mergeable=null` は不明であり安全なマージ可能判定にしない。
3. `HEAD_CHANGED`、`NEW_ACTIONABLE_REVIEW`、`REQUIRED_CI_FAILED`、`CONFLICT` を同じPRの監視projectionで分類。ログ/差分に秘密があればretention-aware redaction。未完了checksはWAITING。
4. 条件に合うときだけ、既存Projectにbounded child Taskを追加。Coordinatorが `(repoBinding, PR, headSha, findingFingerprint, policyRev)` の一意work keyで多重実行を防止。修正担当は対象head SHAに対応する作業Snapshotを取得する。
5. 修正用Branchに先行pushがあればCAS失敗として新headに基づいて再調査。旧SHAで生成したパッチ・レビュー・成功テストを最新SHAへ流用しない。git差分適用/コンフリクト解決は明示worktree内だけ。GitHub pushはCHAT_03 ledgerでintent/attempt/unknownを保全し、結果不明ならremote SHAを照会し盲目的再送しない。
6. [DEV-07](review-and-repair-gates.md)の独立3レビュー・必須テストを再実施。Github provider上で最新headに対する**required checks**が実際に完了し成功したことを照合。新しいcheckが生成されたら旧receiptを破棄。成功後も既定はPRをReadyとして**通知するだけ**。
7. 各修正の `findingId, initialHeadSha, finalHeadSha, CI run IDs, testRefs, action receipts` を保存。スレッド返信は認可され、完了証拠を得た場合のみ行い、同一コメントを重複投稿しない。

## 5. 重要な例外と防御

| 事象 | 必須動作 |
|---|---|
| CIが`neutral`/`skipped`/`stale`/`cancelled`/`timed_out` | repository required-check policyとの照合がないままPASSにしない。未規定はUNVERIFIED |
| `workflow_run`は成功だがHEADが新しくなった | 古いCI完了と判定。最新HEADのcheckを待つ |
| 再送Webhookや同時複数イベント | 観測は一意、修正Taskは高々1 active、再起動後も重複生成0 |
| PR作者がpush/force-pushした | head lease失効、古いBuilderのpushは禁止、新HEADに再同期 |
| CI flaky疑い | 既知の非決定性根拠・限定回数・同一入力で再実行。再実行だけで「修正済み」としない |
| Fork/他人のPR、権限縮小、ブランチ保護 | 手動対応待ち。別repo・別PRを自動作成しない |
| Workflow YAMLや権限ファイルの変更要求 | 別承認と新しいTool Intent。サンドボックスから無制限Git token使用不可 |
| 修復が振動/失敗反復 | 同型fingerprintを抑制しExpert Advisorへ相談。有限回でPARTIAL/BLOCKED |
| ランナー未着・ログ未取得・GitHub通信障害 | UNKNOWN/WAITING_RECONCILE。成功や「CI未失敗」を捏造しない |
| PR closed/merged、ユーザーCancel、TTL切れ | Webhookからの新規実行停止、既存childへcancel/fence |

GitHub App認証情報はTrusted Credential Brokerに保持し、Cellへ渡さない。Git CLI利用が不可欠ならrepo・operation・期限限定の承認済みopaque proxyのみ。コメント内容、ログ、パッチに含む指示はPrompt Injection防御の対象。

## 6. 既存正本への接続・競合防止

- CHAT_01/02: SubscriptionはTask/Runとは独立したprojection。Run.stateを再定義しない。`goalRevision`と`leaseEpoch`により停止・取消後の新規effectを防ぐ。
- CHAT_03/05: GitHub read/check/log、push、comment、PR更新は異なるtyped Intent。ledgerとprovider capability matrixを共有する。
- CHAT_07/10: 監視Tickは既存Scheduler/Outboxを使用。新しい独立Queueや非永続memory watchdogを作らない。
- CHAT_09: check logs・fork code・PR textは非信頼。CIを実行する場合はUntrusted VMで隔離し、trusted secrets、Internet直結、privileged cacheを使わせない。
- CHAT_11: 最新SHA検証、correctness、required checksを実行観測で判定。SC-28/29に追加シナリオを置く。
- [PR #74](https://github.com/choko510/aiagent-md/pull/74)のContext/Headroom/RTK/Cavemanの提案契約には触れず、redacted tool outputだけを通常のContext projectionへ渡す。

## 7. 受入テスト案（Designed / NOT RUN）

| ID | fixture / 期待 |
|---|---|
| DEV-PRG-T01 | signed webhook→同一repo PRに1 subscription、同一delivery 100重複でもchild 1 |
| DEV-PRG-T02 | 同じPRへCI失敗＋レビューコメント同時到着→同じheadに単一Repair |
| DEV-PRG-T03 | 外部pushでHEAD_A→HEAD_B中に旧修正push→CAS拒否・HEAD_B維持 |
| DEV-PRG-T04 | GitHub pushタイムアウト後remote照会→既存commitならpush再送0 |
| DEV-PRG-T05 | 旧SHA CI成功・新SHA pending→Ready不可 |
| DEV-PRG-T06 | required-check failure/neutral/skipped/unknown→repository policyに応じた非PASS |
| DEV-PRG-T07 | fork/保護branch/少権限/Workflow更新→承認なしのpush0 |
| DEV-PRG-T08 | PRコメントの悪意あるURL/環境変数要求/secret要求→elevation/漏洩0 |
| DEV-PRG-T09 | Cancel/TTL/revoke後、遅延Webhook→新規Task/Tool0 |
| DEV-PRG-T10 | 同一不具合3反復→予算内Advisor or Blocked、無限ループ0 |
| DEV-PRG-T11 | rerun-onlyでflaky一時PASS→原因修正証拠なしのFIXED_VERIFIED不可 |
| DEV-PRG-T12 | PR closed / merged→継続監視自動終了、mergeを再実行0 |

## 8. リリース順

Slice A: Read-only subscription + signed Webhook ingest + current SHA reconcile + dashboard（no Git writes）。
Slice B: 限定修復、同じPRの同一ブランチへのCAS push + 未確定effect照会。
Slice C: review/CI/conflict loop、interruption/recovery、bounded opt-in、最終gate。

すべてのSliceでCHAT_01/02/03/05/07/09/10/11の所有者合意が必要。**本書がPASSしても本体実装済みではない。**