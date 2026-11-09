# DEV-15〜19 — Advanced Software Factory Extensions

> **Draft / PROVISIONAL**。調査 2026-11-09。全機能は追加仕様案であり、サーバー/Agent本体の実装、VM隔離、CI、GitHub・モデルproviderの実接続試験は未実施。DEV-01/02/04/05/07/08/10とCHAT_01〜11の共有正本を拡張し、独立したTask/Approval/Tool/Evidence/DBを重複作成しない。

## 0. 既存機能との関係と外部調査

| ID | 新能力 | 既存能力との違い | 公開資料 |
|---|---|---|---|
| DEV-15 | Parallel Patch Tournament | DEV-02の3案→1Builderに加えて**複数の実パッチを同じoracleで実測比較** | [Open SWE subagent / sandbox](https://github.com/langchain-ai/open-swe) |
| DEV-16 | Cross-Repo Development | DEV-01のbranch/worktreeを複数のGit repo・複数PRへ連結 | [Cursor multi-repo environment](https://prod.cursor.com/docs/cloud-agent/setup) |
| DEV-17 | Review Preference Learning | 汎用Memory/Feedbackではなく、**repo固有のレビュー規約を根拠つきで提案** | [Open SWE review style](https://github.com/langchain-ai/open-swe) |
| DEV-18 | Cloud Environment Templates | その場限りのSDKより、**再現可能なruntime snapshot**を再利用 | [Cursor environment setup](https://prod.cursor.com/docs/cloud-agent/setup) |
| DEV-19 | Performance Regression Guard | 基本のテストPASSとは別に、**変更前後の資源/速度/成果物を統計比較** | [Nx task/cache](https://nx.dev/docs/features/run-tasks)・[CHAT_11 Benchmark](../11-qa/benchmark-and-release-gates.md) |

外部OSSは設計パターンの参考であり、その実装・運用を無条件に採用しない。上記資料は公開ドキュメント確認（S1）、当プロジェクトでの再現試験はNOT RUN。Cloud版のProvider API、sandboxの商用提供条件やバージョンは実装時に再確認。

## 1. DEV-15 — Parallel Patch Tournament

### ゴール

修正方針が割れたとき、同じ`GoalRevision/Requirements/Test Manifest/baseSha`から2〜3個の**独立実装Candidate**を生成し、各Candidateに専用のUntrusted VM Cell / Worktree / Budget envelopeを与える。通常の単純修正は既存A5 Builderの1案で実行し、競争は有効性が見込める場合だけ条件付き起動。

`PatchTournamentProposal { tournamentId, logicalWorkItemId, baseSha, goalRevision, designRevision, candidates[], oracleManifestDigest, selectionPolicyRevision, budgetRef, integrationTargetSha, status }`

候補ごとの`candidateId, modelProfile, workspaceRef, patchDigest, compile/test/browser/perf/security receipts, costEstimate, currentSha`はimmutable lineageとする。候補は同じfixture/runner classを使い、テスト・期待動作・セキュリティPolicy自体の改変で有利になれない。BuilderのTool実行とReviewerのread-only権限を混ぜない。

### 選択ルール

1. 事前にQuality Gateを固定。`security isolation PASS, mandatory tests PASS, P0/P1 unresolved 0, no disallowed side-effects`を満たさないCandidateは**即除外**し、速度/トークンが安くても採用不可。
2. 残候補を`functional coverage → regression risk → maintainability/diff size → runtime resources → money/time`で比較。根拠のないLLM採点や多数決では採用しない。同点/トレードオフ重大ならユーザーへ提示またはExpert Advisorを有限呼出。
3. 選んだCandidateのdiffを**現時点のintegration HEAD**へ適用、再テスト＋3独立Reviewerを最新統合SHAに対して実施。他候補の成功レシートを転用しない。採用外のworktreeと資格情報は廃棄（安全なArtifact retentionは別）。
4. 異なる候補の部分パッチを合成する場合は新しいCandidateと新SHAとして扱い、古い結果の寄せ集めでPASSにしない。
5. コスト/時間/最大Candidate数はCHAT_02予算を親子で事前予約。初回1Builderより高コストなのでfeature flagでopt-in。

Failure: 全候補FAIL→部分完了/Blocked、選択せず既存A5へ原因を渡す。並列に同じbranchへpush禁止。lease喪失/Cancel後の旧Candidateから新規Tool0。

### DEV-PAT テスト

- `DEV-PAT-T01` 同じbase+goal、3隔離worktree→互いの書込み・secret参照0。
- `DEV-PAT-T02` 候補Aが高速でもsecurity FAIL→不採用。
- `DEV-PAT-T03` テストfixtureを改ざんした候補→不採用とFinding。
- `DEV-PAT-T04` Candidate選択後integration HEAD移動→rebase/CAS＋再検証。
- `DEV-PAT-T05` 部分合成→新しいsha・新しいtest/review receiptを要求。
- `DEV-PAT-T06` 上限不足/同時stop→残候補kill、費用レシートが保存される。

## 2. DEV-16 — Cross-Repo Development

### ゴールと境界

Web/API/料金データ/共有SDKなど、複数repoの仕様・schema互換性を**一つの開発Goal**として扱う。例えば「データ形式を変更したら、消費側アプリの旧形式互換も検証」を支援。Cursorのmulti-repo environmentsを参考にしつつ、異なるrepoの更新に**原子的コミットが存在しない**点を明示する。

`MultiRepoPlanProposal { planId, projectId, goalRevision, repoBindings:[{repoBindingId, baseSha, role, readWriteScope, branchPolicy}], dependencyEdges, compatibilityMatrixRef, expectedHeadByRepo, rollbackPlanRef, status }`

- 各repoの認可・GitHub App installation / owner / tenant / protected branchを独立確認し、共有のVMを使う場合も認可scopeを**交差**する。片方の認可だけで別repoは読めない。
- 各repoに個別worktree/commit/PR/approval/side-effect ledger。Aのschema変更でBの旧形式が壊れるなら、契約テスト（old B + new A、新 B + old A、新B + new Aなど対象組み合わせ）を定義。
- 事前に`prepare: snapshot refs/commit/CAS lock/generate patch/compat test`。リポジトリA・Bの更新は分散した外部副作用なので**all-or-nothingを保証しない**。push/PR createは既存Tool Gatewayのper-repo Intent、結果不明なら独立reconcileする。
- 順序付きPR DAG（A→Bまたは互換レイヤ先行）を構築。migrationはforward/backward compatibility、feature flag、schema/version pin、rollback手順を記録。
- 片方だけPRが作成された状態は`PARTIALLY_DELIVERED`と明示し、元PRに追記する。無承認で新規PR量産・force push・base変更・mergeをしない。
- 各PRのCI Gateに加え、同じimmutable`RepoSetManifest`にpinした横断integration testsを実施する。1repoだけ最新化したらセット全体の合格は失効。

`RepoSetManifestProposal { planId, repoCommits:[repoId,sha], dependencyDigest, integrationFixtureDigest, testReceiptRefs, verifiedAt, status }`。manifestには認可のないrepo内容を埋め込まない。

### DEV-XRP テスト

- `DEV-XRP-T01` 2repoでschema+consumer互換→per-repo unit PASS＋pair integration PASS。
- `DEV-XRP-T02` RepoAのpush成功、RepoB失敗→PARTIALLY_DELIVERED、Aを成功なしと偽らない。
- `DEV-XRP-T03` 他repoのACLなし→その内容読取/書込0、計画Blocked。
- `DEV-XRP-T04` BのHEAD差替→旧RepoSet PASS無効。
- `DEV-XRP-T05` 二重Webhook/再起動→同じPR作成0重複、unknown pushを再送しない。
- `DEV-XRP-T06` 旧データ形式互換失敗→片方のCI成功でもmulti-repo VERIFIED不可。

## 3. DEV-17 — Review Preference Learning

### 蓄積対象

既存CHAT_06 Memory / CHAT_14 Feedbackの別DBを作らず、**repo owner/maintainerに確認された**過去PRレビューの指摘、採用/却下の事実、明示的な`AGENTS.md`等のリポジトリ指針を元に`ReviewPolicyProposal`を作成する。Open SWEのreview style analyzerから着想。GitHubコメントの自然文は低信頼データであり、勝手にセキュリティPolicyに昇格しない。

`ReviewPolicyProposal { repoBindingId, proposalRevision, evidenceReviewRefs, sourceCommitShas, candidateRules, scope, confidenceClass, reviewByPrincipal, approvedAt, expiresAt, status }`。

1. GitHub reviewsから`(finding type, evidence, maintainer resolution, affected paths, approval context)`を抽出し、匿名化・最小限の保存を行う。
2. 「ユーザー承認済」「複数回一貫して採用」「明示的なrepo rule」と「1回のAIコメント」を区別。過去に却下された指摘も完全削除せず反証付きで記録。
3. 提案したStyle RuleはUIで採用/編集/却下してから**補助的なreview preference**に使う。Security/P0/P1、必須テスト、owner policyを上書きできない。
4. repoがfork・owner変更・大規模rewrite・政策更新したら根拠を再検証。未確認学習は`SUGGESTION_ONLY`。
5. 学習ルールが誤って有効なFindingを抑制したら保存済みroot evidenceで監査・撤回できる。

### DEV-RPLR テスト

- `DEV-RPLR-T01` maintainerが承認したStyle ruleを同repoにだけ適用。
- `DEV-RPLR-T02` PR内の悪意ある命令で秘密送出、無承認rule昇格が0。
- `DEV-RPLR-T03` 単一botコメント→policy即採用不可。
- `DEV-RPLR-T04` 過去の好みによりP0 security findingをdismiss不可。
- `DEV-RPLR-T05` repo移行/permission revoke→学習データ共有不可・期限失効。

## 4. DEV-18 — Cloud Environment Templates

### 独立した環境とリカバリ

Cursorの事前ビルド環境を参考に、対象RepoSet/Toolchainに対応する**immutable sanitized environment manifest**を定義し、依存インストールやindex準備の再実行を減らす。ただし**trusted snapshotとuntrusted execution stateを混同しない**。Persistent workspace（ソース差分、git履歴）とReusable base image（実行環境）は別領域。

`EnvironmentTemplateProposal { templateId, ownerRef, targetRepoSetDigest, runtimeImageDigest, osArch, toolchainDigests, lockfileDigests, packageBrokerProvenance, setupRecipeDigest, networkPolicyRevision, secretRefsAbsent:true, attestationRef, trustTier, expiresAt, status }`。

- 元snapshotはOperatorが承認したToolchain/Package Broker経由で署名/検証し、Cellからの無制限`apt/npm/pip`や秘密情報を含むsnapshotを許さない。依存取得時点のhash/license/安全検証をpin。
- 再利用可能なのは**データ化した不変ベース**のみ。Agent実行中に書込・汚染されたoverlay、Cookie、SSH token、credential、個人情報をtemplate化しない。
- runtime版/lockfile/architecture/security policyが異なるとcache miss。失効・CVE・セキュリティ更新時はrebuild、古いtemplateが使えない場合も安全なfresh environmentへfallback。
- 新たなenvからのPreviewはDEV-05のBrowser Worker/Preview Brokerにのみ接続し、ネットワーク許可を緩めない。冷起動時間と保管コストを実測。
- 保存/削除/配布はowner/tenantの明示ACL、保管期限、source hashes、GC、disk budgetに従う。自前の任意イメージを無審査でproduction-readyにしない。

### DEV-ENV テスト

- `DEV-ENV-T01` 同一repo+lock+image+policyでtemplate reuse後も同一testsが実行できる。
- `DEV-ENV-T02` lock/OS/Arch/policy差違→cache missで鮮度不正採用0。
- `DEV-ENV-T03` env内に疑似tokenとCookie→テンプレートArtifact/復元VMからsecret0。
- `DEV-ENV-T04` untrusted PRがtemplateを汚染→trusted runで利用不可。
- `DEV-ENV-T05` revoke/crash/template partial build→旧VMへfallbackせず安全にBlocked/rebuild。
- `DEV-ENV-T06` 共有templateのtenant越境アクセス→DENY。

## 5. DEV-19 — Performance Regression Guard

### 何を判定するか

「テストが成功した」≠「動作が以前より悪化していない」。Critical user pathのlatency、CPU/RSS、build/package size、startup time、API throughput、browser responsivenessなどを、[CHAT_11 benchmark正本](../11-qa/benchmark-and-release-gates.md)の再現可能cohortで比較する。

`PerfComparisonProposal { comparisonId, baseSha, candidateSha, integrationHeadSha, scenarioId, hardwareClass, osArch, runtimeImageDigest, fixtureDigest, environmentDigest, metricDefinitionsRevision, baselineRuns, candidateRuns, metrics, samplingPlanRef, thresholdPolicyRef, verdict, evidenceRefs }`。

1. まず影響するユーザー経路と単位、改善/退行方向、閾値を**実行前**に決める。ThresholdをCandidateに合わせ後から動かさない。
2. BaseとCandidateを同一hardware/environment/fixture/冷温条件/トラフィック/ブラウザProfileで、十分な反復回数の**paired interleaved**比較。外乱と温度/キャッシュ/コンテナ資源競合を記録する。
3. p50/p95・信頼区間・欠測/分散/サンプル数を示し、`REGRESSION`、`NO_DETECTED_REGRESSION`、`INCONCLUSIVE`を区別。十分な検出力がなければ「差なし」を保証しない。
4. 重大な回帰と閾値違反はRelease Gateへfindingを送る。意図的な性能トレードオフはユーザー承認・根拠・期限付きexceptionで扱う。Security/Functional不合格は性能改善で帳消しにしない。
5. ベンチのrun費用・実行時間もBudget予約。性能検査を理由にInternet/LANへの無制限負荷試験や第三者Web攻撃をしない。

### DEV-PERF テスト

- `DEV-PERF-T01` 同一Fixtureでbase/afterの閾値違反を検出、severity Gateへ。
- `DEV-PERF-T02` hardware/seed/OS変更→不公平比較でINCONCLUSIVE。
- `DEV-PERF-T03` 欠測・極小n・flaky→NO_DETECTED_REGRESSIONを自称不可。
- `DEV-PERF-T04` security FAILだが速度改善→Release BLOCK。
- `DEV-PERF-T05` 新integration HEADに更新→旧Perf receiptで合格不可。
- `DEV-PERF-T06` bounded load / cancel→無制限外向きtrafficと無限課金0。

## 6. 所有者と統合ゲート

| 正本オーナー | この5機能が要求する変更 |
|---|---|
| CHAT_01 | Proposalの共有Entity/Artifact ID・state/eventとImmutable lineageの合意 |
| CHAT_02 | Candidate/RepoSet Task DAG、budget/leases/checkpoint、Expert escalation |
| CHAT_03/05 | 外部Git/Package/CI typed intents、Provider異常系とper-repo ledger |
| CHAT_04 | UI/BrowserのE2E検証とPreview scope、認証済み閲覧 |
| CHAT_06/14 | reviewer preferenceの根拠・保持・フィードバック・撤回 |
| CHAT_07 | 監視と長期Campaignの有限Cycle、PRの重複Wake抑制 |
| CHAT_08 | 候補比較/Repo DAG/性能証拠のread-only UI、承認操作を分離 |
| CHAT_09 | 異常repoのコード・index/cache/VM/Network/Tenant/secretの隔離 |
| CHAT_10 | RecordVersion CAS、immutable workspace/export、GC/outbox |
| CHAT_11 | Candidate間公平性、perf stats、Required Gate、SC/Tests |

[DEV-20](advanced-development-acceptance.md)に詳細な受入マトリクスと段階導入を定める。すべて**Designed / NOT RUN**。