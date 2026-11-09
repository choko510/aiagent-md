# DEV-12 — Reproduction Lab / 不具合の再現・原因特定・修正証明

> **Draft / PROVISIONAL**, 2026-11-09。実装・実runner・実ブラウザ試験なし。DEV-06 Browser Debugger、DEV-07 Review/Repair、DEV-11 PR Guardianへ**証拠に基づく再現・履歴調査**を追加する設計案。型/状態はCHAT_01、ExecutionはCHAT_02/09、ArtifactはCHAT_01、Toolと外部アクセスはCHAT_03/04/05、QAはCHAT_11が正本。

## 1. 提案の核と比較

既存DEV-06はルート探索、Console/Network、Visual差分、Browser Workerの相関調査を持つ。新たに、ブラウザ以外のCLI/Unit/Integration/非決定的CI失敗も対象として、**「修正前に再現 → 修正後は同じ条件で解消 → 旧動作が退行していない」**を独立した判定にする。

採用の参考:
- [Git `bisect`公式](https://git-scm.com/docs/git-bisect/)：良いcommitと悪いcommit間の探索、`bisect run` の終了コード`0=good、1..127（125を除く）=bad、125=skip`。不確かなskipでは単一犯人commitを断言しない。
- [pytest Flaky Tests](https://docs.pytest.org/en/latest/explanation/flaky.html)：状態漏洩、順序依存、並列性、時間依存などが揺れを起こす。単純rerunは根本解決でない。
- [Open SWE](https://github.com/langchain-ai/open-swe)のCI分析・根拠がある場合だけのflaky rerun。自動再試行は証拠化が先。
- DEV-06 Browser Trace + DEV-07 independent findingsの共通Evidence/Workspace/Runをそのまま使用。独自証拠DB/別Browser権限は新設しない。

## 2. 実行条件と成果物

Input: `projectBinding`、`failureObservationRef`（CI/job log、exception、Issue、Browser trace、user steps）、`expectedBehavior`、`badSha`、任意`knownGoodSha`、`targetScope`、`reproductionBudget`。非信頼ログやIssueの文章だけでshell命令を生成し無条件実行しない。

Outputs: versioned `ReproCase` proposalと実行manifest、最小failure reproduction、原因候補とconfidence分類、`before`/`after`比較、必要ならbisect候補範囲、修正パッチと最新SHAに紐付くtests receipts。Fixtureにsecret、実個人情報、認証済みCookieを含めない。

論理型提案（canonical Entity/Error/Event/Artifactの変更は別owner決定）:

`ReproCaseProposal { caseId, projectId, logicalWorkItemId, goalRevision, originalFailureRef, expectedBehaviorRef, baseSha, failingSha, testRecipeDigest, runtimeImageDigest, dependencyLockDigest, fixtureDigest, seed, timeMode, networkMode, failureFingerprint, status, evidenceRefs, recordVersion }`

`ReproOutcomeProposal { caseId, evaluatedSha, runId, verdict: "REPRODUCED"|"NOT_REPRODUCED"|"FLAKY"|"INCONCLUSIVE"|"BLOCKED", attemptCount, actual/expectedRefs, testReceiptRefs, environmentDigest }`

- 再現`REPRODUCED`は該当SHAに対する同じrecipe/fixtureの失敗を意味し、根本原因確定とは別。
- `NOT_REPRODUCED`は成功ではない。環境依存/条件不足の可能性を明示。
- `FLAKY`は同一入力・同一SHAでPASS/FAILが混在した実測。
- `INCONCLUSIVE`はログ欠落・テスト不可・外部依存未制御・bisect skip過多。
- `BLOCKED`は認可、budget、安全なfixture、runtime欠如などで着手不能。

## 3. 実行パイプライン

`COLLECT → NORMALIZE → SELECT_RECIPE → BASELINE_RUN → [MINIMIZE] → [BISECT] → ROOT_CAUSE_HYPOTHESES → FIX → SAME_RECIPE_AFTER → REGRESSION → REVIEW`。各段階は有限Task/RunまたはExecutionに対応し、進行状態をcheckpoint/ledgerと接続する。`testRecipeDigest`や`goalRevision`変更時、以前の検証を再利用しない。

1. 症状を`source path/line, error signature, request ID, trace window, test name, platform, environment` に正規化。空のログを「問題なし」にしない。
2. データ・時計・乱数・タイムゾーン・Locale・feature flag・依存Lock・ブラウザviewport・OS/archを固定できる範囲で制御。ネットワークは固定fixture/mockが既定。LinuxとWindows等の差が疑われる場合は別cohortとして報告。
3. 既存テストで再現できればそのまま利用。できない場合は最小テストを**ゲスト内**で生成し、実行前に削除・決済・送信・本番接続を起こさないことをTool Policyで確認する。生成テストは不正なファイルやプロセスへのアクセスを試み得るため、Untrusted Execution VM内のみ。
4. 元の失敗SHAで**症状が出ることを実行確認**。必要に応じてdelta debuggingで入力/手順/依存を削り、最小再現へ縮小する。縮小によって症状が変わった場合は元の再現と同一視しない。
5. `knownGoodSha`の根拠と時系列/ancestorを検証できる場合に限り`git bisect`を隔離worktreeで実行。テスト不能なrevisionは`skip`で範囲に残す。全候補を検証できなければ`suspectRange`に留める。bisect用recipeは再現テストと独立した外部副作用なし。
6. Builderが修正後のSHAで**同一recipeを再実行**し、関連回帰・[DEV-14](code-intelligence-and-adaptive-testing.md)のimpact tests・DEV-06のBrowser E2Eを実行。失敗が消えたことだけで他要件を保証しない。
7. 根拠を`PROVEN`/`SUPPORTED_HYPOTHESIS`/`UNVERIFIED`に分け、ReviewFindingと紐付ける。推論だけで原因を`PROVEN`にしない。修正後だけテスト追加して旧SHAを走らせないケースは`REPRO_NOT_PROVEN`扱い。

## 4. Flaky Test と再試行の扱い

- まず同じSHA、入力、runner class、fixture digest、seedで許可された少数回を実行。結果の`passCount/failCount/attempts`を保存する。
- 一度でも成功したから安全と判定するのではなく、不安定なままなら`FLAKY/QUARANTINED_PROPOSED`として関連機能の完了Gateを独断で解除しない。
- 試験が一時的に成功しただけのCI rerunはDEV-11で`CI_RECOVERED_BY_RERUN`と区別。`BUG_FIXED`とは別。
- 時間・順序・資源競合などの原因を絞り込み、修正後は交互実行・ランダム順序・競合負荷といった**設定済み**検査を実施する。無限に繰り返さない。
- flaky quarantineは期間、責任者、解除条件を伴う明示の例外承認のみ。必須セキュリティ検査や要求されたE2Eを自動除外しない。

## 5. 安全性・競合・復旧

- 無関係な環境変数/secretをUser Issue、PR、CIログ経由で渡さない。失敗ログと脅威サンプルは不信任データ。試験結果はredact後にArtifactへ保存。
- 外部ネットワーク許可、環境準備、パッケージ取得、Git pushは既存Broker/Gateway/Sentinelに個別依頼。CLIやテストコードの直接Internetを禁止。
- 同一caseの複数workerは`caseId + evaluatedSha + recipeDigest + environmentDigest + attemptNo`を一意としてclaim。結果を二重加算しない。
- Worker deathや再起動後は、終了したRunの`test outcome`を引き継ぎつつ実行中未確定Attemptは`UNKNOWN`にする。新試行には新Attempt ID/予算予約が必要。
- 修正中にhead更新、designRev変更、Goal変更があれば旧結果は保持して新headのGateだけ無効化する。
- 相手サーバーへの負荷試験、破壊的データ作成、資格情報テストは事前承認のモック/使い捨て環境に限定。

## 6. UI/CLIと受入テスト（Designed / NOT RUN）

UI表示: 「現象」「再現条件」「修正前の実測」「原因の根拠」「対象コミット」「修正後の実測」「残る不確定性」の区別。再現用コマンド/動画・Traceを個人情報redact済みで提示。ユーザーが一部stepsを操作する場合はDEV-03のsynthetic demoまたはDEV-06のHuman takeover権限を使用。

| ID | シナリオ / 必須結果 |
|---|---|
| DEV-RPL-T01 | 再現テストが元SHAではFAIL、修正SHAではPASS→REPRODUCED/FIX_VALIDATEDの別receipt |
| DEV-RPL-T02 | 失敗が再現しない→成功扱い禁止、INCONCLUSIVEと追加条件 |
| DEV-RPL-T03 | 同一SHAでPASS/FAIL混在→FLAKY、単一rerunでFIXED不可 |
| DEV-RPL-T04 | Good/Badの間にbuild不能SHA→bisect skip、単一犯人を断定しない |
| DEV-RPL-T05 | 既存ブラウザTraceで再現→DEV-06同一Evidence refsを利用、重複収集を避ける |
| DEV-RPL-T06 | 生成テストがhost filesystem/secret/networkへアクセス→隔離・拒否 |
| DEV-RPL-T07 | same seed/fixture/recipeで2Run→比較可能、異digestを同一cohortとして集計しない |
| DEV-RPL-T08 | head SHA・依存Lock・GoalRevision変更→以前のPASSで最新検証を省略しない |
| DEV-RPL-T09 | cancel/lease expiry/予算超→新しいtest attempt 0、Partial/Blocked |
| DEV-RPL-T10 | 追加した再現テストが修正前にFAILしなかった→「修正証明済み」不可 |
| DEV-RPL-T11 | 外部支払い・メール・破壊的fixtureに対する自動再現→外部副作用0 |
| DEV-RPL-T12 | Worker killとrequeue→同一Attempt二重集計0、利用量・Evidence保持 |

## 7. 段階的導入

Slice A: 既存テスト/CIログとReproCase Manifest、before-after oracle（Linux・Web/CLI）。
Slice B: bisect/最小化/Flaky分類、Windows/Android等はrunner提供能力に応じて段階対応。
Slice C: GitHub PR Guardianの自動選択とCode Intelligence連携、root-cause evidenceを3 Reviewerへ転送。

合格の正本はCHAT_11であり、本稿は`DEV-RPL`の契約提案。`PROVEN`は実行証拠が揃った場合のみ。