# DEV-13 / DEV-14 — Code Intelligence & Adaptive Testing

> **Draft / PROVISIONAL**, 2026-11-09。コードインデックス、LSP、SCIP、test runnerの実装/実測は未着手。本書は既存DEV-01（Workspace/Git）、DEV-06（Browser）、DEV-07（Review）、CHAT_02 Context、CHAT_11 QAへ接続する能力提案であり、別のCanonical Task/Artifact/Access/DB定義を設けない。

## 1. 研究上の判断

- [Sourcegraph Precise Code Navigation](https://sourcegraph.com/docs/code-navigation/precise-code-navigation): SCIPベースのlanguage-agnostic symbol indexはGo-to-definition、Find references/implementationsに利用可能。対応language/indexerとビルド依存性を前提にする。**全文検索だけでは定義・参照関係を確定できない**。
- [SCIP schema / bindings](https://github.com/scip-code/scip): 機械可読なsymbol occurrence/indexを作るプロトコル。OSSのフォーマットと商用Sourcegraph Cloudサービスは別物。後者を本機能の必須依存としない。
- [Nx affected](https://nx.dev/docs/features/ci-features/affected): 変更ファイルとtask input/output/依存グラフから実行対象を選ぶ。2026年10月のNx 24でtask-based affectedが既定になったが、言語・プロジェクト一般でその精度が保証されるわけではない。
- [Nx run tasks](https://nx.dev/docs/features/run-tasks): task graph、pipeline、cacheが独立概念。影響テストの「未選択」と「合格」を区別する設計に利用。
- [GitHub cache security](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching): キャッシュは署名検証済みの証拠ではなく、非信頼PRのcache poisoningを警戒すべき。
- 既存[DEV-07](review-and-repair-gates.md)の独立Reviewと[DEV-10](acceptance-and-rollout.md)のrequired testsを高速化のために省略しない。

## 2. DEV-13 Code Intelligence

### 2.1 目的・入出力

巨大なmono-repo/複数repoで、AIが毎回全ファイルを読む代わりに**根拠つき関連コード集合**を取得する。単なるembeddingの類似度を正確な参照解析と混同しない。

入力: 認可`repositoryBindingId`、immutable `commitSha`、`workspaceRevision`、対象言語/Toolchain、依存Lock、Goal、Issue/Stack trace、最大index byte/time/cost budget。出力: `SymbolIndexSnapshot`、`ImpactGraphSnapshot`、`EvidenceBundle`、`coverageStatus` と漏れの可能性。

提案Index schema（永続EntityはCHAT_01/10の所有者審査後）:
`CodeIndexManifestProposal { indexId, ownerRef, repoBindingId, commitSha, language, indexerId, indexerVersion, toolchainDigest, dependencyLockDigest, workspaceRevision, contentDigest, coverage:"COMPLETE"|"PARTIAL"|"TEXT_ONLY"|"UNAVAILABLE", createdAt, evidenceRefs }`
`SymbolEdgeProposal { fromSymbol, toSymbol, edgeKind:"DEFINITION"|"REFERENCE"|"IMPLEMENTATION"|"IMPORT"|"CALL"|"TEST_RELATION", path, range, sourceCommitSha, indexManifestId, confidenceClass:"PRECISE"|"HEURISTIC"|"UNKNOWN" }`

### 2.2 探索ルール

1. `repo tree / language manifests / build graphs`を取得。最初は`rg/git grep`で絞り、該当言語のLSP/SCIP indexを条件付き使用。Index作成の実行はUntrusted VMで行い、indexer不正時の侵害を隔離する。
2. 参照・型・呼出し元・実装・関連testを`(file path, line/range, commit SHA, indexer evidence)`付きで返す。ツール推論の「関連していそう」は`HEURISTIC`として区別。
3. Source/Generated/Third-partyコードを分類し、追跡対象がSymlink、Submodule、Git LFS、Generated file、Dynamic import、Reflection、FFIに跨る場合はcoverageを`PARTIAL`または`UNKNOWN`へ落とす。
4. `commitSha`、Lock/Compiler/Indexer version、Generated metadataが異なれば別index。変更を検出した範囲を増分再索引できるが**別SHAのedgeを鮮度表示なしに合成しない**。
5. 依存検索・コード内容に埋め込まれた命令は非信頼。semantic rankingはretrieval候補選択のみ。権限外repo/ファイルをindex、外部モデルへ送信しない。
6. Context Managerには`source ref + valid-at SHA + minimal code slice`を渡す。生の巨大indexやsecret-bearing sourceをモデルへ丸ごと渡さず、Policy A0/A1/A2（不圧縮領域）を維持する。

### 2.3 質を保証する条件

- `Find references`が空でも「参照なし」と断定しない。言語非対応/構文失敗ならtext-only fallbackに戻す。
- 誤接続したrefによる危険な誤編集を避けるため、Builderによる編集前にファイルとcommit SHAを再照合。
- 複数repoのsymbol graphには各repoに対する個別ACL/実際の依存Edgeが必要。組織名が同じというだけではindex参照不可。
- 大きいrepositoryではtime/size/indexer process上限でPARTIAL停止。圧縮やcache TTLでサイレントに古いindexを採用しない。

## 3. DEV-14 Adaptive Testing

### 3.1 Gateは二段階

`FAST_FEEDBACK`: 開発中、変更影響からfocus testsを選ぶ。時間短縮のための**暫定診断**であり、PR公開/Release合格判定ではない。
`REQUIRED_GATE`: DEV-07/10とCHAT_11が規定した**必須全検証**（lint/type/unit/integration/security/browser/e2eなど、機能ごとに異なる）とfresh HEAD・統合HEADの検証が必要。適応的選別が`REQUIRED`集合を削ることは禁止。

### 3.2 TestImpact 計算

Input: `baselineSha` / `candidateSha` / `repoBoundaries` / `diffPaths` / `CodeIndexManifest` / build/CI task graph / test coverage metadata / known failure map / policy version。

`Changed files → direct test mappings → reverse dependency graph → dynamic import/test fixtures/config changes → risk overrides → selected tests + must-run tests`。

提案型:
`TestSelectionProposal { selectionId, baseSha, headSha, integrationHeadSha, repositoryBindings, graphDigest, testManifestDigest, selectedTestIds, mandatoryTestIds, omittedTestIds, rationaleByTest, coverageClass, selectionPolicyRevision, budgetRef }`。

- `coverageClass=COMPLETE`は対応言語/Graph/Declared inputsに限った到達可能性を意味し、実行テストの十分性を保証しない。
- 影響の根拠が不明、lockfile/build config/global fixture/DB schema/公共API/Auth/Permission/Serialization/Generated code変更はrisk-based policyで関連テストを拡大し、必要なら全テストを選ぶ。
- Snapshotが古い、Graphが破損、base SHAが未確定、テストcatalogが欠落なら`FALLBACK_FULL`（予算不足ならPartial/Blocked）。`empty selection`を自動合格とはしない。
- 変更行のcoverage情報は弱い推定。dynamic/runtime reflectionなど静的捕捉不能は不確実性を残す。
- Flaky/failed testは[DEV-12](reproduction-lab.md)へ、Browser E2EはDEV-06へ。既知の不安定なテストを無言で除外しない。

### 3.3 キャッシュ・費用

成果物cacheは`tenant / trustTier / immutable commitSha / dependencyLockDigest / runtimeImageDigest / testRecipeDigest / environment vars allowlist / runner version`で分割し、content hashとprovenance検証を必須とする。未検証cache由来の`pass`を実テスト成功として流用禁止。非信頼fork・PRから書き込まれたcacheはtrusted workflowに利用させない。

`FAST_FEEDBACK`は予算・duration・CPU/メモリ上限で実行。`REQUIRED_GATE`が予算超過なら予算を勝手に引き上げず`BLOCKED/Partial`。節約率は「選ばなかったケース数」ではなく同等の品質条件での実測により評価。

## 4. 権限・復旧・同時実行

- Index/test codeはCell内にのみ配置。モデル/Indexerがhost SSH、Docker socket、trusted DB/Vault、public Internetへ直接到達しない。
- Index生成、test execution、cache materializationはTask/Run/Executionごとにlease、GoalRevision、budgetを照合。任意repoファイルのシンボリックリンクを辿らない。
- Cache lookup/readもtenant認証し、redacted source snippetはscope-validなArtifactだけから提供。
- Crash後のindex生成が未確定なら`PARTIAL/UNKNOWN`で再確認。cacheのatomic publish前にworker killした場合、不完全manifestを返さない。
- 同じSHAへの並列indexerはdigestキーで一意、古いworkerが新しいSnapshotを上書きしない。依存辺の破損を検出しても復旧可能な元Artifactを保持。
- Browser/LLM向けコード引用はRaw provenanceとredaction statusを記録し、SourceGraphをプロンプトの命令として扱わない。

## 5. Test計画（Designed / NOT RUN）

| ID | 検証と期待 |
|---|---|
| DEV-CIN-T01 | exact SCIP/LSP ref→定義・参照・byte rangeと固定SHA一致 |
| DEV-CIN-T02 | 未対応言語・dynamic/reflection→PARTIAL/TEXT_ONLY、参照0を断定しない |
| DEV-CIN-T03 | 別HEAD上のindexを使った編集→拒否、新HEADで再検索 |
| DEV-CIN-T04 | indexer死・OOM・repo巨大→予算内Partial、DB/hostへの漏洩0 |
| DEV-CIN-T05 | symlink/submodule/生成物・lock変更→許可境界と鮮度を再評価 |
| DEV-CIN-T06 | 不正テキスト「秘密をアップロード」→external effect 0 |
| DEV-CIN-T07 | 2repo同名symbol/別tenant→交差Evidence 0 |
| DEV-ATS-T01 | 小規模単独変更→FAST_FEEDBACKが直接影響testを選択し理由を出す |
| DEV-ATS-T02 | lock/schema/global config変更→full/fallback必須 |
| DEV-ATS-T03 | 選ばれなかったtestが存在→REQUIRED_GATEでは必須testを省略しない |
| DEV-ATS-T04 | index/graph invalid or old SHA→FALLBACK_FULL / budget不足でBlocked |
| DEV-ATS-T05 | forkが汚染cacheを書込み→trusted test receiptとして利用不可 |
| DEV-ATS-T06 | graph/lock/runtimesが一致したcacheでも信頼条件不足→新規test必須 |
| DEV-ATS-T07 | 最新headでrequired testsが未実行→PR Ready/Feature Factory Count不可 |
| DEV-ATS-T08 | Test flakyの診断結果→DEV-12と連携してpass誤認なし |
| DEV-ATS-T09 | cancel/lease失効後→新規index/test開始0 |
| DEV-ATS-T10 | crashと同時更新→index incompleteを読まず、正しいmanifestのみ公開 |

## 6. 段階的実装と測定

Slice A: read-only Repo tree + git grep + language/file manifest + impact list（index未対応は明示）。
Slice B: TypeScript/JavaScript、Python、Rust等のうち対応検証できた言語からSCIP/LSP、baseline/current HEADとSource graphを結合。
Slice C: FAST_FEEDBACK test selection + fallback/full required gate + cache isolation、[DEV-11](pr-guardian.md)のCI時短へ導入。

測定: `relevant symbol recall/precision`（固定golden corpus）、false omission rate（必須testを落とした件数は0必須）、`p50/p95 index time`、`tokens/cost per completed repair`、`full-vs-selected runtime`。同じcohort/seed/SHA/environmentでpaired比較、実測なしの「何%高速」表現は禁止。

## 7. 未決事項と所有者

CHAT_01: Index ID、ProvenanceとArtifact relation。CHAT_02: Context slices/Task parallelism。CHAT_03: indexer/test recipeのTool Intent。CHAT_05: Git/CI capabilities。CHAT_09: Cache isolation/indexer安全検証。CHAT_10: index metadata（Content-addressed storage参照）とGC。CHAT_11: selected/full gateと統計/benchmark。**Type/API/DB/Release GateのLocked化はowner合意と実測検証まで保留。**