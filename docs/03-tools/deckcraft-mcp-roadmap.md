# DeckCraft MCPスライド生成 — 調査・統合ロードマップ

> **Status: Draft / PROVISIONAL**（2026-11-09調査。設計のみ。実装・MCP接続・実機試験は未実施）  
> 対象: [aiagent-md](https://github.com/choko510/aiagent-md) × [DeckCraft](https://github.com/storytold/deckcraft)  
> DeckCraftの調査対象: GitHub `main` のソースと公開Issue、最新Release `v0.16.0`（2026-11-06公開）。**Releaseバイナリを実行した検証ではない**。  
> 位置付け: 既存のAgent、MCP、Gateway、Sandbox、Artifact、UIの**拡張計画**であり、共有契約の新しい正本ではない。

## 1. 目標・採用判断

**結論: DeckCraftを、MCP経由で新規プレゼンテーションを生成するための交換可能なバックエンドとして採用候補にする。**

利用者は「市場分析を日本語で10枚、16:9、グラフと出典付きで」と自然言語で依頼できる。Agentが調査→スライド構成→DeckCraftによる編集→構造検査/画像検査→必要なページだけ修正→`.deckcraft`編集元、`.pptx`、`.pdf`、プレビュー画像を保存する。続く「3枚目だけ修正して」では**前の版を残し**新しい編集版を生成する。

**初期段階で保証しないもの:** PowerPoint完全互換、あらゆる既存PPTXを無損失で編集、GUIのリモート操作、MCPを通じた無制限のホスト操作、巨大な資料を制約なく生成、AIの視覚判定の完全な正しさ。aiagent-mdは汎用Harnessのままとし、DeckCraft専用Agent基盤/DBは作らない。

## 2. 調査結果：何がすでに存在するか

| 対象 | 確認した事実 | 主要ソース |
|---|---|---|
| MCP | `deckcraft-cli mcp`は**stdio MCPサーバー**で、既定ではGUIを起動しないheadlessエンジンを使用。`--connect PORT`は実行中のGUI接続モードで別物 | [MCP docs](https://github.com/storytold/deckcraft/blob/main/docs/mcp.md)、[headless.rs](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/headless.rs) |
| Protocol | 実装に`2025-06-18`と互換候補`2025-03-26`、`2024-11-05`。aiagent-mdは`initialize`で**交渉したバージョン**だけを使用。最新版MCPプロトコルの全面対応を前提にしない | [server.rs](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/server.rs) |
| ツール | ソースに**25 MCPツール定義**。新規作成、スライド追加、文字/図形/画像/表/チャート、`inspect_document`/`inspect_slide`、`render_slide`、保存と各種出力、`run_command`、`batch` | [tools.rs](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/tools.rs) |
| 生成能力 | 200以上のコマンド、テーマ/レイアウト、PPTX、PDF、PNG/JPEG、outline、GIF。`render_slide`はPNG画像を返す | [README](https://github.com/storytold/deckcraft)、[MCP docs](https://github.com/storytold/deckcraft/blob/main/docs/mcp.md) |
| ファイル隔離 | `--automation-read-root`と`--automation-write-root`でread/writeを独立制約、複数指定可。未指定なら制限なし。`--connect`との併用不可。link差し替え競合（TOCTOU）は残存 | [MCP docs](https://github.com/storytold/deckcraft/blob/main/docs/mcp.md)、[root tests](https://github.com/storytold/deckcraft/blob/main/crates/mcp/tests/it/automation_roots.rs) |
| フレーム上限 | stdioのMCP要求フレームは最大16MiB。大きな画像をモデルにインライン全送信しない | [server.rs](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/server.rs) |
| ライセンス | 本体はMIT / Apache-2.0デュアルライセンス。フォント、アイコン、素材は別条件があり、商用/配布時に表示義務などを確認する | [MIT](https://github.com/storytold/deckcraft/blob/main/LICENSE-MIT)、[Apache](https://github.com/storytold/deckcraft/blob/main/LICENSE-APACHE)、[ATTRIBUTION](https://github.com/storytold/deckcraft/blob/main/ATTRIBUTION.md) |

ソースを読んだ事実と、実行・互換性の**未確認事項**を区別する。配布物にCLI/root引数が含まれているか、Linux headless環境の日本語レイアウト、PowerPoint/LibreOffice表示、CPU/RAM/フォント負荷は**Phase 0/1で実測**する。

### 2.1 既知の不具合・導入上の危険

| 重要度 | 根拠・問題 | aiagent-md側の対策 |
|---|---|---|
| **P0** | [#203](https://github.com/storytold/deckcraft/issues/203)：外部PPTXを再保存してSmartArt/OLEを失う報告。上流の[gaps](https://github.com/storytold/deckcraft/blob/main/docs/gaps.md)でもPPTX round-tripの不足が指摘されている | MVPは**新規生成のみ**。アップロード済み元PPTXを絶対上書きしない。既存資料編集は独立した検証ゲートへ |
| **P0** | [#263](https://github.com/storytold/deckcraft/issues/263)：GUIの`--control` TCPポートに認証不足との指摘 | 本番はheadless stdio限定。GUI/TCPに直接Agent接続しない |
| **P1** | [#786](https://github.com/storytold/deckcraft/issues/786)：CLI/MCPの`format.font`がEast Asian/complex-script属性を更新しない報告 | CJKフォント/異体字/太字/句読点/折返しのPNG・PPTX実測ゲート |
| **P1** | [#1923](https://github.com/storytold/deckcraft/issues/1923)：PDFグラフ文字がラスター画像化し検索不能という報告 | PDFをフルベクター/アクセシビリティ対応と表現しない。PPTX併用、解像度とサイズ検証 |
| **P1** | [#74](https://github.com/storytold/deckcraft/issues/74)：音声/動画のplayback optionsがPPTX往復で消える報告 | メディアの完全再現はMVP対象外。既存PPTX編集解禁時に回帰テスト |
| **P1** | root pathのcheck→open間にはlink差し替え競合が残ると上流が明記 | DeckCraft自体のroot検査を**唯一の隔離境界にしない**。VM、mount、UID、名前空間、ACLで実際に制限する |

Issueは公開報告であり、全件をv0.16.0で再現したと断定しない。導入前に指定版の状態を再確認。

## 3. aiagent-mdへの統合アーキテクチャ

```text
User / Web Chat
  -> Agent Planner / Run Manager      (Trusted Control VM)
  -> Tool Registry / MCP Adapter
  -> Tool Gateway: Schema, Tenant, Lease, Budget, Approval, Ledger
  -> Authenticated Broker RPC
  -> DeckCraft Worker / headless MCP   (Untrusted Execution VM内の隔離Cell)
      inputs: verified/read-only Artifacts
      outputs: task-scoped staging files
  -> validated Artifact ingestion      (Trusted側で所有者・hash・format検証)
  -> Web Dashboard: previews + PPTX/PDF/download
```

**信頼境界の原則**
1. AgentやLLMがホストで`deckcraft-cli`を任意起動する経路を作らない。管理者が承認してdigestを固定したCLI imageを**専用Worker**として隔離VM内で起動する。[Sandbox Execution](../02-runtime/sandbox-execution.md)、[Runtime Cell Isolation](../09-security/runtime-cell-isolation.md)の設計に準拠。
2. **すべてのMCPツール呼び出しを既存のGateway経由**とする。[MCP Adapter](mcp-adapter.md)、[Registry](tool-registry-and-discovery.md)、[Gateway](tool-execution-gateway.md)、[Policy](tool-approval-and-policy.md)、[Side-effect Ledger](side-effect-ledger.md)の認可・権限分類・Run lease・予算・失敗状態を使う。`annotations.readOnlyHint`を許可根拠にしない。
3. Workerには**直接インターネット接続を与えない**。画像/資料は既存のHTTP Fetch・Browser・Connectorで取得し、[Sentinel](../09-security/sentinel-authority.md)・強制Egress Transportを通した後、検証済みArtifactとして渡す。DeckCraftにCookie/トークンを渡さない。
4. 入出力はユーザー/タスク専用に限定。root/guest FSにHost、Vault、DB、他ユーザーのファイルをbindしない。非root、権限全drop、read-only rootfs、tmpfs、memory/cpu/pid/disk/timeの上限、サブプロセスreap、キャンセル時のlease失効を必須とする。
5. 出力は[Artifact契約](../01-contracts/evidence-artifact-contracts.md)の`owner/tenant`・`hash`・`MIME`・`parent provenance`・`version`に紐付ける。公開前に形式/サイズ/文書構造を検証し、[Privacy](../09-security/privacy-audit-and-retention.md)に従う。新しいDeckCraft専用DBスキーマは設けない。
6. GUIに依存しない。プレビュー/UIは[Web Chat](../08-interfaces/web-chat-interface.md)、[Dashboard](../08-interfaces/task-and-watch-dashboard.md)の正本へ必要最小限のProposalを出す。

**隔離Worker内の起動例**（要事前作成ディレクトリ、管理者固定設定、実行試験未済）:

```sh
deckcraft-cli mcp \
  --automation-read-root /work/inputs \
  --automation-read-root /work/outputs \
  --automation-write-root /work/outputs
```

`--automation-read-root`に`outputs`を含めるのは自タスクの確定済み編集元を再オープンするため。write先は各Taskの専用領域とし、Agent指定の任意絶対パスを信頼せずBrokerが検査/割当する。**`--connect`や`--sample`は本番で使わない**。

### 3.1 最小ツールカタログと公開方針

**初期許可候補**：`new_presentation`、`add_slide`、`go_to_slide`、`add_shape`、`add_text_box`、`set_text`、`insert_picture`、`insert_table`、`insert_chart`、`inspect_document`、`inspect_slide`、`render_slide`、`save_presentation`、`export`。

- `run_command`は原則非公開。必要な`design.theme`/`format.*`/`shape.*`を**固定コマンドIDと型付き引数のラッパー**として段階公開する。`run_command`のparamsを自由入力させてはいけない。
- `batch`を使う場合、**配列の各操作ごとに**Gatewayでallowlist/schema/risk/budgetを検証する。既定`keepGoing=false`、途中失敗した時に全変更が自動rollbackされると仮定しない。
- `open_presentation`はMVPでは**本人所有かつ過去に検証済みの`.deckcraft`編集元**に限定。外部PPTXの編集はPhase 5まで保留。
- `pointer`/`select_tool`/`key`/`type_text`/`screenshot`/`call_app`はMVPから非公開。GUI専用機能は初期用途に不要。
- MCPツール説明/リソース/出力文字列は非信頼データ。Descriptorは管理者割当`providerId`/`namespace`/`sourceName`/revisionを使って衝突検査し、Server自己申告で権限を上げない。
- `render_slide`のBase64画像は、上限検査後Artifact Storeへoffload。LLMへは検査対象の縮小画像と構造要約だけ渡す。16MiB制限を超えるケースはスケール調整・失敗表示。

### 3.2 正常系・異常系フロー

1. **依頼受付/Plan**：目的・想定読者・枚数・言語・アスペクト比・資料ソース・必須要素・出力形式と予算を確定。入力なしの数値や出典を捏造しない。
2. **資料取得**：既存のSearch/Fetch/Browser/Uploadから出典/取得日時/権利/機密区分付きArtifactを取得。画像は許可形式へ正規化・サイズ検査。
3. **スライド構成**：各スライドのタイトル、論点、見せるデータ、図版、根拠とスピーカーノート（必要時）を計画。構成は計算結果であり実行権限ではない。
4. **作成**：`new_presentation`→`add_slide`→コンテンツ挿入→スタイル。MCPツールをGateway認可して実行。
5. **構造検査**：`inspect_document`/`inspect_slide`でスライド数、要素ID/座標/幅高さ、テキスト空欄、表とグラフの整合、超過・見切れを機械検証。
6. **視覚検査**：`render_slide`を保存し、可読性・不意の重なり・フォント/図表崩れを画像で点検。画像AIの判定は推定として記録し、決定的な座標/内容検査と分ける。
7. **限定修復**：失敗したスライドだけ再編集・再検査。回数/Token/時間/CPUの有限上限。未修復なら部分成功（既存Run状態名が正本）と欠陥リストを提示。
8. **保存/再検証**：`.deckcraft`の編集元と`.pptx`、`.pdf`、プレビューを**タスク専用staging**に生成。ZIP/PPTX integrity、PDF読み込み、各形式のサイズ/hash/スライド数を確認し、別セッションでPPTXを再オープンする。Pass後のみArtifactへcommit。
9. **納品/継続編集**：所有者だけがプレビュー/リンクへアクセス。修正指示を受けたら元版を保持し、新しいVersionとして保存。外部にアップロードする操作が必要なら別の認可済みConnectorとユーザー承認を経由。

**Crash/Replay:** `batch`の部分適用、保存後ACK喪失、接続断、取り消し、lease失効などでは**効果の有無がUNKNOWN**になり得る。保存済みArtifactやステージングを照合し、同じ変更を無条件で再送しない。`.saving`等の一時ファイルを完成成果物としない。確定した編集元から再開し、過去版を自動上書きしない。

## 4. Phase別ロードマップ

時間・コスト見積りは実測前なので記載しない。Prerequisiteを満たさずPhaseを「完了」としない。全ての項目は現時点`TODO`。

| Phase | 成果物 | 完了条件（Go/No-Go） | 状態 |
|---|---|---|---|
| **0 — 契約・配布物確認** | 固定するDeckCraft tag/commit/binary digest/ライセンス/フォント整理、MCP互換交渉、role/Worker境界のADR提案 | Linux配布CLIで`initialize`/`tools/list`/root引数を実機確認。既存Gateway・VM ownerと矛盾解消 | TODO |
| **1 — Headless隔離PoC** | 限定FSのWorker、stdio broker、10枚程度の日本語新規スライドとPPTX/PDF/画像保存 | GUIなしで生成/再読込、root外・他tenant・ホスト機密へアクセス不可。**PoCは本番保証ではない** | TODO |
| **2 — Gateway/MCP統合** | Registry allowlist、厳密schema、tool-level認可、Run/lease/budget/Cancel/Artifact binding、障害復旧 | 任意コマンド・直TCP・ホストspawnを禁止。途中失敗/再接続で二重実行や他tenant越境を防止 | TODO |
| **3 — MVP作成・納品** | 自然言語→構成→DeckCraft→構造検査→保存、出典/日本語フォント、Webプレビュー/ダウンロード | 10枚程度のPPTX/PDF/`.deckcraft`が同じTaskと結びつき、形式/枚数/画像検査および独立PPTX viewer確認に合格 | TODO |
| **4 — 自動QA・会話編集** | slide別preview、構造/画像の欠陥検出、有限修正、版管理、部分差分UI | 「3枚目だけ修正」が実現。旧版保存・検出/修正の再現性、回数上限、部分失敗の可視化 | TODO |
| **5 — 既存PPTX編集（任意・保留）** | アップロードPPTXコピー編集、サポート外パーツ検出、損失検証、PowerPoint/LibreOffice比較 | SmartArt/OLE/メディア等の損失とユーザーデータ改変を**検出/拒否**可能な対象だけ解禁。無損失を示せなければ先送り | DEFERRED |

**PR分割提案（不要なPR量産を避ける）:** このPRは**調査＋ロードマップ＋README導線のみ**。実装時は、A) Phase 0–1の実機PoC、B) Phase 2–3のGateway縦切りMVP、C) Phase 4の品質監査、D) Phase 5の独立互換性審査（必要なら）の順で機能単位にPRを作る。既存Gateway/Artifact/DB/Runが未実装なら依存側を先行実装してから接続する。新DB/共通EnumはOwner合意なく作らない。

## 5. Acceptance / Test Plan（全て**未実行**）

| Test ID | 検証内容 | 必要な結果 | 対応する既存SC |
|---|---|---|---|
| DCM-T01 | 配布CLI/MCP接続、protocol negotiation、全ツールschema取得 | 対応版で成功、未対応はfail closed | SC-05, 07 |
| DCM-T02 | 10枚程度の日本語・画像・表・チャート新規作成 | 構造、画像、保存/再オープン一致 | SC-05, 15 |
| DCM-T03 | `.deckcraft`/`.pptx`/`.pdf`/PNG export | hash/MIME/スライド数/所有者/リンクの一致 | SC-07, 15 |
| DCM-T04 | `run_command`/`batch`/`call_app`からallowlist外操作 | Gatewayで拒否。Server自己申告で権限上昇不可 | SC-08, 17–22 |
| DCM-T05 | `..`/symlink/差し替え競合/他tenantパスの読み書き | Host/越境アクセスが物理的に不成立 | SC-08, 17–22 |
| DCM-T06 | 保存中kill、MCP応答消失、batch途中失敗、Run cancel/lease失効 | 失敗とUNKNOWNを区別、再実行で二重commitしない | SC-07, 17–22 |
| DCM-T07 | CJK・異体字・太字・折返し・図表・フォント変更 | 視覚Diff、PPTX/PDF比較で欠落/見切れを検出 | SC-05, 15 |
| DCM-T08 | 巨大画像・PPTX ZIP爆弾・16MiB超過・大量スライド | bounded fail。OOMやHost破壊・秘密漏洩なし | SC-08, 09, 15 |
| DCM-T09 | 多ユーザー同時生成・重いレンダリング・予算超過 | CPU/RAM/ディスク/時間/Tokenの上限・公平性 | SC-07, 09 |
| DCM-T10 | 他人のArtifact ID/URL、未確定出力ファイル、古いバージョン | 認可なし閲覧不可。未確定物を公開しない | SC-08 |
| DCM-T11 | SmartArt/OLEを含む既存PPTX再保存要求 | Phase 5前は拒否。解禁時は元本不変/損失検出 | SC-07, 15 |
| DCM-T12 | 外部Web/画像メタデータからツール権限拡張指示 | Prompt injectionがGateway/Sentinelを上書き不可 | SC-08, 15 |
| DCM-T13 | 出典欠落/古い資料/ライセンス不明の画像 | 根拠なし数値・許諾ありと誤認させない | SC-05, 15 |

`DCM-Txx`は**ロードマップ内の提案ID**。既存SCのLocked変更や実行済みテストを意味しない。実装PRでは[QA正本](../11-qa/end-to-end-acceptance.md)に正式なテスト契約と証拠（ログ、バイナリdigest、Golden image、Artifact hash）を記録する。

## 6. 依存する既存仕様と未決事項

| 既存の正本 | 再利用・必要なレビュー |
|---|---|
| [MCP Adapter](mcp-adapter.md) / [Tool Registry](tool-registry-and-discovery.md) | stdioの安全なAdmin起動、旧MCP互換、provider ID、Toolを限定公開する方式 |
| [Tool Gateway](tool-execution-gateway.md) / [Approval](tool-approval-and-policy.md) / [Ledger](side-effect-ledger.md) | tool呼出しと`batch`内の副作用分類、同じ操作の再実行防止、UNKNOWNの照合 |
| [Sandbox Execution](../02-runtime/sandbox-execution.md) / [Runtime Cell](../09-security/runtime-cell-isolation.md) / [Sentinel](../09-security/sentinel-authority.md) | 管理者固定workerの起動責任、VM/Cell隔離、guestのネットワーク禁止、実測ゲート |
| [Artifact](../01-contracts/evidence-artifact-contracts.md) / [DB](../10-infrastructure/database-schema-and-migrations.md) | プレビュー/編集元/派生形式のlineage、immutable保存、Owner/tenant/version/retention |
| [Chat](../08-interfaces/web-chat-interface.md) / [Dashboard](../08-interfaces/task-and-watch-dashboard.md) | Slide一覧/Preview/部分修正/ダウンロード、既存API・Eventの変更 |
| [E2E](../11-qa/end-to-end-acceptance.md) / [Benchmark](../11-qa/benchmark-and-release-gates.md) | CJK Golden/PowerPoint互換/巨大ファイル/隔離の負例、未実行とPassedを区別 |

**判断待ち:** 配布CLIの実際のMCP互換バージョン、固定するリリース/digestと添付フォントの利用条件、stdioを通すBroker設計、各種Toolのrisk分類、ユーザーへのpreview API、PPTXのcompatibility threshold。いずれも**実装PRで正本のOwnerレビューが必要**。

## 7. 調査資料

- DeckCraft [README](https://github.com/storytold/deckcraft) / [v0.16.0 Release](https://github.com/storytold/deckcraft/releases/tag/v0.16.0) / [MCP docs](https://github.com/storytold/deckcraft/blob/main/docs/mcp.md)
- DeckCraft [MCP server](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/server.rs) / [Tools](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/tools.rs) / [Headless](https://github.com/storytold/deckcraft/blob/main/crates/mcp/src/headless.rs) / [Gaps](https://github.com/storytold/deckcraft/blob/main/docs/gaps.md)
- DeckCraft issues [#203](https://github.com/storytold/deckcraft/issues/203) / [#263](https://github.com/storytold/deckcraft/issues/263) / [#786](https://github.com/storytold/deckcraft/issues/786) / [#1923](https://github.com/storytold/deckcraft/issues/1923) / [#74](https://github.com/storytold/deckcraft/issues/74)

**採用候補の最小単位:** headless MCPでの新規スライド生成＋日本語/PPTX/PDFの実測＋安全なArtifact配布。既存PPTX編集は互換性を独立に立証するまで拒否する。
