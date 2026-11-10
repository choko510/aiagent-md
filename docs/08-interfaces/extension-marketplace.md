# Extension Marketplace — Skills・MCPマーケット仕様

> **Draft / PROVISIONAL / NOT IMPLEMENTED / NOT TESTED**  
> 調査: 2026-11-10 JST。**base: main**。参考実装: [PR #82](https://github.com/choko510/aiagent-md/pull/82)（調査時 HEAD `d2db4f460de293adf56c04e7444ac8b296248e63`、未マージ）。  
> このPRは補足仕様・実装計画だけを追加する。PR #82の変更や、既存88基礎仕様の正本をコピーしない。

## 0. 採用案

Webに `/marketplace` と `/settings/extensions` を設け、**Skills・Remote MCPを検索し、原則ワンクリックで追加・有効化・無効化・削除・更新**できるようにする。

「ワンクリック」は、低リスクの承認済み拡張を**1操作で安全なインストールジョブに投入できるUX**を指す。OAuthログイン、権限拡大、管理者の審査は省略しない。HTTP `202 Accepted`をインストール成功と表示しない。検索結果・掲載元・署名・スキャナ判定は信頼やTool権限そのものではない。

| 種別 | v1 | 後続 |
|---|---|---|
| Skill | `SKILL.md`互換・読み取り専用バンドル。本文は必要時だけロード | 安全なCell内の明示許可Script |
| MCP | 許可済みRemote Streamable HTTP。OAuth/API Keyで接続 | 隔離されたstdio MCP Server |
| Plugin/Recipe/Connector | 将来対応の分類は表示できるが、v1で勝手に任意コードを実行しない | 既存Plugin/Connectorの権限仕様に従う |
| Source | MCP公式Registry、ClawHub、承認済みGitHub commit URL | Private Registry、追加Source |
| 自動導入 | Agentは候補を提案できるがインストールを自己承認しない | 同じ原則を維持 |

**既存所有者**：CHAT_06 = Skill/Plugin意味論、CHAT_03 = Tool Registry/実行、CHAT_09 = Policy/Secret/Sentinel、CHAT_08 = UI/API、CHAT_10 = 永続化、CHAT_01 = 共通契約/イベント、CHAT_11 = QA。マーケットがTool Gateway・Approval・Credential Brokerを新規に持たない。関連正本：
- [03-01 Tool Registry](../03-tools/tool-registry-and-discovery.md)
- [03-05 MCP Adapter](../03-tools/mcp-adapter.md)
- [06-03 Skill Lifecycle](../06-memory-skills/skill-lifecycle.md)
- [06-05 Plugins](../06-memory-skills/plugins-recipes-and-hooks.md)
- [05-01 Connector Framework](../05-connectors/connector-framework.md)
- [08-01 REST](rest-api.md) / [09-02 Secrets](../09-security/secrets-and-credentials.md) / [09-08 Sentinel](../09-security/sentinel-authority.md)
- [10-01 DB](../10-infrastructure/database-schema-and-migrations.md) / [Architecture Rules `E](../00-foundation/architecture-rules.md)

## 1. 外部レジストリの調査（2026-11-10）

| 元サイト | 検証したAPI/仕様 | 判断・制約 |
|---|---|---|
| **Official MCP Registry** | `GET /v0.1/servers?search=...&version=latest`、`/versions/{version}`、`updated_since`、`metadata.nextCursor` | **第一MCPカタログ**。`search`は空以外3文字未満だと422。無指定では旧版混在、version文字列順はSemVer順ではない。`version=latest`を指定。deprecated/deleted状態も反映 |
| **ClawHub** | `GET /api/v1/search?q=`、`/skills`、`/skills/{slug}`、`/versions`、`/scan`、`/file`、`GET /api/v1/download?slug=&version=` | **第一Skillカタログ**。downloadの応答はZIP、または`public-github` JSON handoff。両者に対応。429/Retry-Afterと出典リンク、スキャンの限界を尊重 |
| **GitHub** | Repository/commit SHAと対象サブパスの検査 | **手動登録**。可変ブランチでなくcommit SHA・bundle hashで固定。リポジトリ所有者表示と安全性は別 |
| **skills.sh** | `/api/v1/*`、認証にVercel OIDCを用いるAPI、`npx skills add` CLI | **直接API採用は保留**。一般セルフホストで使える無認証APIを仮定せず、Hostから`npx`を実行しない |
| **Smithery** | 2026-10-31にAPI/Connect/Toolbox/Proxy/CLI/SDK終了、public directoryは継続 | **接続管理として不採用**。旧`server.smithery.ai`/`run.tools`を使わず、公式Remote URLのみ個別に検証 |

**一次資料（静的調査）**
1. https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md
2. https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/registry-aggregators.mdx
3. https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/remote-servers.mdx
4. https://docs.openclaw.ai/clawhub/api
5. https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx
6. https://skills.sh/docs/api
7. https://www.smithery.ai/future
8. https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization

MCP公式Registryはpreviewのため、互換性/一時停止/データ更新の可能性を許容する。これらの**API仕様を確認した**ことと、aiagent-mdで**実API接続・実測・インストールE2Eを実施した**ことは区別する。現段階で後者は未実施。

## 2. PR #82のコード監査：何が存在し、何を追加するか

[PR #82](https://github.com/choko510/aiagent-md/pull/82)（HEAD `d2db4f4`）はPython Agent Runtime、Gateway、Queue、PostgreSQL、認証/隔離の**実装PR**。本仕様は**未マージのPRを前提とした本番稼働主張ではない**。

| 実ファイル | 静的に確認した現状 | Marketplaceで必要な差分 |
|---|---|---|
| `backend/packages/domain/src/aiagent_domain/tools.py` | `TOOLS`、`PROFILE_TOOLS`は静的dict、`tools_for`/`surface_digest`で固定Tool群 | 管理者登録Provider/Versionとtenant+profileの動的Activation、Run snapshot。Builtinとの衝突を禁止 |
| `backend/packages/runtime/src/aiagent_runtime/agent.py` | `make_tools`は`STUBS`から作成。Tool本体はGatewayへRPC | 動的Toolも同じGateway-only stubとする。RuntimeからMCP SDK/資格情報/Socketへの直接接続は禁止 |
| `backend/packages/gateways/src/aiagent_gateways/tool_gateway.py` | HEAD proposalとの一致・`TOOLS.get`/PROFILE_TOOLS・Intent/Approval/Effectと最終Admissionを保護 | 動的`providerId, sourceName, descriptorRevision`と現行PolicyEpochを参照し、途中失効時に新規dispatchを止める |
| `backend/packages/gateways/src/aiagent_gateways/model_gateway.py` | モデルへ固定Tool Schemaを提供 | RuntimeとGWで同一の正規化`ToolCatalogSnapshot`/digestを利用 |
| `backend/packages/domain/src/aiagent_domain/tools.py`の`packaged_skill_texts` | 同梱Markdownを一括・read-only読み込み | DB保管Bundleの有効Skillを必要時だけ読み、run単位にdigestを固定。同梱Skillはbuiltinとして維持 |
| `backend/packages/api/src/aiagent_api/app.py` | STEP3のTask/Run/Approval中心 | カタログとインストールのRESTのみ追加。APIが拡張機能のコードを直接実行しない |
| `backend/migrations/0001_core.up.sql`、`0005_roles_grants_rls.up.sql` | Tenant/Owner/Run/Approval/Effect/RLSの正式スキーマ、Extension表なし | PR82マージ後に新migration、厳格なRLS/列GRANT/restore世代対応。既存migrationを変更しない |
| `web/src/api/generated/domain.ts` | Canonical JSON Schemaから生成したUI型 | Wire DTOをJSON Schema→Pydantic/フロントエンド型生成。新Shared型を手書き二重定義しない |

**P0統合上の問題**：PR82のTrusted EgressはTool用HTTP `CONNECT`を禁止する。Remote MCPはHTTPS先が普通なので、**MCP Adapterを独立Trusted ConnectorとしてTLS終端・方法/宛先をSentinelが検査できる経路**を設計・実証するまで「接続可能」と扱わない。通常`httpx`で直接外向き通信する近道は禁止。既存のSchema固定/最終Admission/Side-Effect Ledgerを迂回しない。

PR #82は本番KVM 2VMや実Provider等の残試験がある。Marketplaceが動いてもその本番未検証項目をPASSとはしない。PR #82の変更139ファイルとは本PRで編集パスを分ける。

## 3. 責務とデータ設計

`UI (React + 型生成)` → `FastAPI Marketplace API` → `Extension Manager (Python/Control Plane)`、以下の独立境界を必須とする。

- **SourceAdapter**: Official Registry / ClawHub / GitHub。検索・詳細・version解決・ダウンロード。外部入力は常に非信頼。HTTPはSentinel/Trusted Egress経由。
- **Artifact Quarantine**: サイズ/圧縮比/ファイル数/パス/Manifest/リンク/署名/digestを検証。検査中にコード実行ゼロ。
- **Installation Manager**: Tenant/User/Workspace・Install/Enable/Disable/Update/Remove・CAS・idempotency・監査Receipt。
- **Skill Service (06-03)**: Content-addressed Bundleと段階的ロード。SkillのMarkdownはdataであり権限を付与しない。
- **MCP Adapter (03-05)**: 事前許可済み接続のみinitialize→capability negotiation→全ページ`tools/list`→strict validate→Atomic Registry publish。
- **Tool Registry/Gateway + Sentinel + Credential Broker**: Skill/外部MCPの自己申告ではなく、既存Policy/承認/秘密管理で最終認可。

**内部論理型案（正本確定前、既存共通型を再定義しない）**

- `CatalogKey = (sourceId, kind, upstreamStableId)`。`kind = SKILL|MCP|PLUGIN|RECIPE|CONNECTOR`。検索Cardには`displayName, publisher, sourceURL, description, sourceVersion, sourceUpdatedAt, compatibility, provenance, moderation/scanInfo`。全て低信頼の表示情報。
- `Release = (catalogKey, exactVersion, sourceCommitSha?, artifactSha256, manifestSha256, signatureStatus, scannerVerdict, policyRevision)`。immutable。`latest`の可変ラベルは実行時参照に使わない。
- `Installation = (installationId, tenantId, ownerUserId, workspaceId?, catalogKey, releaseId, desiredState, effectiveState, recordVersion, policyEpoch)`。インストールはCredential/ToolのGrantとは別。
- `Connection = (connectionId, installationId, endpointRevision, credentialBindingId?, healthState, authState)`。Token本体はCredential Brokerのみ。MCP Server 1個に複数Connectionを認める。
- `Activation = (installationId, targetAgentProfile, targetWorkspace?, enabled, recordVersion)`。Runに提示する候補の絞り込み用。
- `ToolIdentity = (providerId, sourceName, descriptorRevision)`。表示用aliasと分離し、`serverInfo.name`や短縮名だけで認可しない。
- `RunCatalogSnapshot = (catalogSnapshotId, registryGeneration, policyEpoch, toolIdsAndRevisions[], skillReleaseDigests[])`。モデルとToolGWのSchema digest一致。実行前の現行deny/revokeが最終優先。

Source重複は`catalogKey`で別管理。ClawHub+GitHubの同一Skillはcommit/hash等の証拠が一致した場合だけUI上の候補を束ねる。Source名・Publisher名・スター数が同じだけではdedupeしない。

## 4. インストール/削除状態とシーケンス

`DISCOVERED(UI) → STAGED → VERIFIED → INSTALLED_DISABLED → ENABLED`。
検査NGは`QUARANTINED/BLOCKED`、認証未完は`AUTH_REQUIRED`、接続失敗は`UNAVAILABLE`、意図的停止は`DISABLED`、削除は`REMOVING→REMOVED`（Receipt墓石保持）。State命名はCHAT_01/06/09/10合意前はPROVISIONAL。UIでは`INSTALLED≠CONNECTED≠ENABLED≠USABLE`を厳密に区別。

**Install**:
1. 本人が選んだ`catalogKey + exact release + scope`と署名済みセッションを認可。`Idempotency-Key`で永続InstallJob作成（`202+jobId`）。AIの自然言語Tool提案をインストール権限としない。
2. Source APIのpagination、429/Retry-After、ETag/TTL、version/deprecationをbounded取得。表示時の「latest」と取得時の版を再照合。検索だけではコードをダウンロード/実行しない。
3. ZIP/Git treeを隔離領域へ取り込み、Hash固定。`../`、絶対パス、symlink/hardlink、Unicode/大文字小文字衝突、巨大ファイル/深い依存/ZIP bomb/remote `$ref`/不正YAMLを拒否。`public-github` handoffならGit commit SHAを独立に取得・検証。
4. 依存・要求権限・送信先・コスト/外部副作用・ライセンスを判定しユーザーに表示。Policyに沿って承認済み低リスクなら一連の操作でEnableまで進む。新権限・認証・管理者審査があればそこで停止する。
5. DB TxでRelease + Installation + audit receiptを固定。Enableは独立CAS（ウィザードで連続操作は可）。準備に失敗したら`INSTALLED_DISABLED`のまま残し、使えると表示しない。
6. MCP接続は管理権限付きAdapterで`initialize/tools/list`を完全検証後にRegistry atomic publish。すべてのTool実行はGateway Intent→Policy→Approval→Sentinel→Effect Ledger経由。

**Disable/Remove**: 最初にPolicyEpoch/Activation変更をcommitし、**新規Tool Call/Skill loadを即座に拒否**。その後MCP Session閉鎖・依存停止・GCをJobで実施。`SENDING`済みの外部副作用を強制取り消したと偽らず、`UNKNOWN`/reconcileを維持。通常RemoveでAuditや既存Runに必要な固定Artifactを消さない。`Disconnect/credential revoke`は別操作（影響する複数Installationを確認）。

**Update/Rollback**: 新releaseを旧版と独立に検疫、Schema/権限/宛先/publisher/license差分を表示。再承認後にCASで新Snapshotへ切替。検証・認証失敗なら旧版維持。Rollbackでも旧Secret/旧Grantは復元せず現在Policyで再検証。同時更新は`If-Match`/409・412で競合を通知。

## 5. MCPの特殊条件

- v1は`remotes.type = streamable-http`だけ。`packages`のみのstd​​io、legacy SSE、URL-templateが未対応なら`UNSUPPORTED_TRANSPORT/CONFIGURATION`。`npx/uvx/pip`等をHost実行しない。
- OAuthはRFC9728 Protected Resource Metadata、RFC8414/OIDC discovery、OAuth PKCE+state+resource indicator、必要に応じてCIMD/事前登録/DCR。利用できないClient登録方式は`AUTH_SETUP_REQUIRED`。Callbackを単回・認証済Ownerへ束縛。Access/Refresh TokenをLLM/URL/ログ/Registryに出さない。EgressではOAuth issuer/redirect先/metadata/DNS rebinding/SSRFまで検査。
- MCP descriptions、`annotations.readOnlyHint`、prompts/resources/instructions、`tools/list`はすべて非信頼。MCPが`readOnly`を宣言しても、Gatewayの実効Riskは上がらない・自動的に書込みを許さない。
- `tools/list`のcursor上限、循環検出、tool数/JSON Schema bytes上限、`list_changed`再取得、旧Schema revisionの呼出拒否、Server再接続時のEndpoint Identity検証を実装。
- 使用中Runの`ToolIdentity`とSchemaをimmutable snapshot固定。失効なら安全側にToolUnavailableを返す。Serverから到着したTool結果に含まれる指示をsystem/developer命令として実行しない。
- MCP HTTPS通信はPR82 Sentinel/Egressの制限を満たす**信頼済みAdapter**で実証。外部ConnectionのHealthとCatalog APIの到達性を混同しない。

## 6. Skillの特殊条件

- Agent Skills標準`SKILL.md`（YAML frontmatter `name`/`description`、任意`license`/`compatibility`、`references/`/`assets/`/`scripts/`）を受け入れ、06-03のManifestへ正規化する。出典仕様は [Agent Skills spec](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)。
- `SKILL.md`の`allowedTools`や命令は「希望」であってTool Grantではない。`curl | sh`、`pip`、`npm`をInstall時やTrusted Python内で実行しない。
- `scripts/`は検査・保存できるが、Script実行を許可しない初期値。Cell実行は別Capability/申請/隔離の実装後。
- Frontmatterのみ短く公開し、本文は適切なRunの明示的な需要時に、トークン予算とProvenanceを確認して段階的ロード。ファイル参照はbundle内限定。大きな参照ファイルのSSRFやパストラバーサルを拒否。
- Skills依存MCPを無断連鎖導入せず依存グラフで明示し、循環/depth上限。自己改善Skillも人間Reviewなしに昇格しない。

## 7. REST / DB / UI案（Shared所有者の合意待ち）

**API**（CHAT_08。Serverが認可する、UIの制御は権限根拠にしない）

- `GET /api/v1/marketplace/items?q=&kind=&source=&cursor=&limit=`：複数Source検索、限定ページ、現在のキャッシュ時刻。`GET /api/v1/marketplace/items/{id}`：詳細/権限/互換性/差分/導入可否reason。
- `GET /api/v1/extensions/installations` / `.../{id}`、`GET /api/v1/extensions/jobs/{id}`：Installed/Enabled/Auth/Healthと非同期Job進捗。Authorization/Owner/Workspaceを必須。
- `POST /api/v1/extensions/installations`：`{catalogKey,releaseSelector,scope,enableRequested}` + `Idempotency-Key` →202+Job location。`POST /api/v1/extensions/installations/{id}/enable|disable|update|rollback|remove`：`If-Match`+Idempotency-Keyで永続Command。
- `POST /api/v1/extensions/installations/{id}/connections`：接続開始。OAuth CallbackはUser/Connection/nonce/PKCE/expiryを照合し、Credential BrokerのOpaque handleだけ保存。`disconnect`はローカル失効と外部revocationの可否を区別。
- `AUTH_REQUIRED` / `POLICY_DENIED` / `UNSUPPORTED_TRANSPORT` / `REGISTRY_UNAVAILABLE` / `PACKAGE_UNSAFE` / `STALE_REVISION` / `CONNECTION_FAILED` / `TOOL_SCHEMA_CHANGED` / `UNKNOWN_SIDE_EFFECT`は共有Error正本の型へ後続実装で写像する。未承認の新Shared Enumを確定しない。

**DB**（CHAT_10、PR82マージ後に新migration）：`extension_sources`、`extension_catalog_cache`、`extension_releases`、`extension_installations`、`extension_activations`、`extension_connections`、`extension_jobs`、`extension_receipts`。Catalog/Releaseは共通、Installation以降はtenant/owner/workspace RLS・専用Role・列GRANT。`credentialBindingId`以外のTokenを保存しない。リトライ/重複Jobを`(tenant,actor,idempotencyKey,requestHash)`で収束させる。restore世代が違う旧Job/Permitを拒否。Artifact GCは参照数とRun pinを尊重、監査は削除不可。Generated JSON Schema→Pydantic/フロントエンド型の正本を守る。

**UI**：`/marketplace`で全て/Skills/MCP/追加済み、検索、source/kind/リスク/利用可否、出典、Version、導入・更新状態。`/marketplace/{id}`でPublisher/Source/Digest/License/通信先/要求権限/スキャンの限界/依存/OAuth/費用リスクを表示。`/settings/extensions`でInstalled/Enabled/Connected/Usable、Profile割当、PIN、更新、Rollback、Disable、Remove、Disconnect。削除と認証情報失効は別の確認。OAuthはHuman Browser Handoff。Windows11/Fluent風の落ち着いたUI、320px対応、キーボード、スクリーンリーダー、disabled理由、focus復帰。Registry停止中も既存インストール一覧はDBから表示。「未取得」を0件/安全/無料と誤表示しない。

## 8. 必須の受入試験（すべて未実行）

| ID | 対象 | PASS基準 |
|---|---|---|
| MKT-T01 | MCP Registry検索 | 3文字未満422、`version=latest`、opaque cursor、deleted/deprecatedと更新反映 |
| MKT-T02 | ClawHub | 429/Retry-After、疎ページ、ZIPと`public-github`のハンドオフ両経路 |
| MKT-T03 | Skill検疫 | frontmatter、symlink、ZIP bomb、`../`、重複パス/巨大ファイル/不正Schemaを拒否 |
| MKT-T04 | Code Execution | 未審査Skill/MCPでTrusted Hostの`subprocess/importlib/npx/pip`実行ゼロ |
| MKT-T05 | MCP API | `initialize`、cursor循環/上限、`list_changed`、同名Tool、Schema差替えを検出 |
| MKT-T06 | Policy | Tool metadata/Skill`allowedTools`/Provider名による権限増加0 |
| MKT-T07 | Run整合 | Modelが見たTool SchemaとGW評価Schemaのdigest一致、失効後のdispatch 0 |
| MKT-T08 | OAuth | state/PKCE/resource/redirect/audience/Replay/別UserやTenantのCallbackを拒否 |
| MKT-T09 | SSRF/Egress | URL/redirect/DNS rebinding/private/metadata/CONNECT迂回が不可能 |
| MKT-T10 | Idempotency | 同一キー/同時クリック/Queue重複でInstallは一件、202を完了扱いしない |
| MKT-T11 | Disable/Remove | 新規Tool Call 0、SENDING/UNKNOWN履歴維持、Credential影響を分離 |
| MKT-T12 | Update/Rollback | 新版不正時旧版維持、権限増加を再承認、旧Grant/Token復活ゼロ |
| MKT-T13 | Isolation | 他User/TenantのInstallation/Private Catalog/Connection/Token/Jobを404/deny |
| MKT-T14 | Crash/Restore | download/commit/Enable中killでDB収束、restore世代不一致を拒否 |
| MKT-T15 | UX | 追加→認証待ち→有効化→停止→削除、モバイル/キーボード/disabled理由 |
| MKT-T16 | PR #82統合 | 現行Gateway/Effect/Sentinelの回帰、実Remote MCPのHTTPS E2E、Tool直接送信0 |
| MKT-T17 | Supply chain | Publisher spoof、版差替え、Git SHAとArtifact digest不一致を検疫 |
| MKT-T18 | Privacy | Token/Private Skill本文/認証パラメータのLog/LLM/Stream漏えい0 |

これらは設計ID。既存`REQ-ID→TEST-ID→SC-ID`運用ではSC-05/07/08/09/16を参照し、共有受入シナリオへの正式登録はOwner合意後。性能/成功率は未測定でありSLOを作り話にしない。

## 9. 実装順序とマージ条件

1. **MKT-1**：Read-only Catalog Adapter (Official MCP/ClawHub/GitHub)、Cache/normalized search、React Marketplace UI、Mock+API試験。**PR82未マージでも実装可**。
2. **MKT-2**：SkillのQuarantine/Bundle/Installation/ACL、Enable/Disable/Remove/Update/Rollback、on-demand loader。**PR82のBackend/DB統合後**。
3. **MKT-3**：Remote MCP/OAuth/Connection、Gateway-only dynamic Tool、Run Snapshot、Sentinel強制TLS経路、実API E2E。**PR82・隔離/実送信の受入完了後**。
4. **MKT-4**：権限差分再承認、監視、性能、管理者向けPrivate Source、障害復旧。
5. **MKT-5（将来別PR）**：stdio/Plugin/Script。独立隔離Cell/Sidecar＋専用Security ADRと実機試験なしには出荷しない。

**この仕様PRのDoD**：main起点、新規文書と索引だけを編集、PR82のコードに触れない、既存Owners/信頼境界を維持、外部一次資料・未検証事項・テスト設計・後続依存を明記する。PR82の本番KVM/Provider等のBLOCKEDは本仕様では解除されない。
