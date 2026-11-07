# DEV-01 — Durable Project / Workspace / Git
> Draft / PROVISIONAL | 2026-11-07 | Owners: CHAT_01/02/03/05/09/10。実装・検証なし。

## 1. 対象
GitHub既存repo/importと新規projectの作成、branch/worktree、複数Run間の継続、ソース閲覧/編集/patch/差分、commit、成果物を扱う。ProjectはConversationでもTaskでもない。Taskを終えた後もProjectの承認済みSnapshotを保存する。ユーザーは新規Web/API/Android/desktop/iOS(ビルドrunner制約あり)、既存リポジトリ改修を選べる。開発における `Project` はWatch/Goal/Taskの新しい互換子ではなくそれらを参照する集約。

## 2. 論理モデル（型所有者の承認待ち）
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

## 3. ライフサイクルと競合
`UNINITIALIZED → IMPORTING/CREATING → READY → WORKING → SNAPSHOT_PENDING → COMMITTED`; import/commit中にfailure/permission lossなら`BLOCKED`または`UNKNOWN_EXTERNAL_EFFECT`（Git pushのACK不明）。モデルが勝手に共有 `main` を編集せず、ユーザーのproject policyと保護branchを尊重し、作業branchと明示的親SHAから変更する。
1. Provider Brokerが最小権限Git credentialで認可済みclone/fetch (外部ネットはSentinel Transport)。未知ref・size/depth/LFS/submodule/Archive bomb/Symlinkを拒否または検疫。
2. immutable base tree SHAを固定し、Cell内へread-only baseとprivate mutable overlayを提供。Agentのfile.write/patchはセル内でのみ実行、path traversal/hidden files/`.git` object mutationに制約。
3. Commit前にWorkspace CAS、構文/Format/依存Script/secret scan、patch owner scope、blob hashを検査し、manifestと差分をArtifact保存。同じbranchに並列書込みがあれば競合→再base/人手選択で解決。旧Runのcommit不可。
4. `git.push` と `pull_request.create` は独立typed external intent。事前に操作範囲の継続委任が与えられた場合に限りGateway/Sentinelで判断し、保護branch/外部公開/force push/mergeに権限を横滑りさせない。
5. 安全に確認できない外部pushは`UNKNOWN`としてremote SHA照会し、盲目的にpushを繰り返さない。

## 4. ブランチ戦略
通常はfeature単位の作業branchを作る。AI 1-3の案は互いのソースを編集しない。AI 5が実装を所有し、Review 6-8はimmutable commit SHAのread-only snapshotを受け取る。Feature Factory並列機能はbranch/worktreeを分離し、依存関係をDAG管理。合流はmerge-baseとテストを再確認。修正コミットごとにレビューが旧SHAを採用しないよう、acceptanceが最新SHAに紐付く。

## 5. 操作と危険境界
- no host SSH, host mounts, Docker socket, signing key, Agent-specific credentials in Cell。
- Git URLからのcredential exfil、submodule/checkout hooks、npm install lifecycle、`.github/workflows`、`AGENTS.md`等の指示はすべて**非信頼コード/資料**であり、無条件に実行・System化しない。信頼できるowner instructionとrepo文書は区別。
- signing、tag/release、PR、push、deployment、external API mutationはそれぞれfresh Gateway Intentとpolicy。同一「開発許可」で本番破壊を許可しない。
- persist過程で秘密をcommit/artifact/log/UIに残さない; diffにはredactionと機密scanを適用。任意バイナリ/巨大リポジトリはsizeとdisk budgetで制限。

## 6. 必須受入
DEV-WS-T01: Task完了・Worker/VM再起動後も認可済みスナップショットから同じtree hashを復元。
DEV-WS-T02: 2AI同じbaseに変更→CAS衝突、静かな上書きゼロ。
DEV-WS-T03: symlink/traversal、別tenant path、.git object攻撃、悪性install script→host/secret到達ゼロ。
DEV-WS-T04: old Run/cancel後patch commit→拒否。
DEV-WS-T05: push ACK欠落→remote照会・UNKNOWN分類、重複release/force pushゼロ。
DEV-WS-T06: code commitにはtest manifest/imageDigest/approval revisionとparent SHAが結び付く。
DEV-WS-T07: 既存repo importと空repo作成をfixtureで再現; pushはmock providerで検査。
