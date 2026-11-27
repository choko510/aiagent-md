# Safe Multimodal Ingestion — 実装ノート（2026-11-25）

対象仕様: [multimodal-ingestion.md](multimodal-ingestion.md)
実装ブランチ: `feat/multimodal-ingestion`

この文書は「何を実装し、何を実際に実行し、何がまだ検証できていないか」を分けて記録する。仕様の記述や単体テストの成功を、実環境の安全性検証（Verified）に読み替えない。

## 1. 実装した範囲

### 1.1 Pythonパッケージ

`backend/packages/multimodal/src/aiagent_multimodal/`

- 契約: `MmiSource`, `MmiSourceLocator`, `MmiCandidate`, `MmiRequestV1`, `MmiOutcomeV1`, `canonical_parse_key`
- 入口: チャンク分割アップロード、owner-scoped quarantine blob、長さ・SHA-256・magic 検証
- 形式検出: PDF/PNG/JPEG/WebP/ZIP-OOXML/CSV/TSV/plain text。拡張子ではなく magic と内部構造を使う
- ゲート: ZIP Slip・traversal・absolute/UNC/drive/ADS/NUL・symlink/hardlink/device・重複名（NFKC + casefold）・nested archive・ZIP64 anomaly・CRC・ratio/entry/total/depth budget
- XMLゲート: DTD/ENTITY/XXE/XInclude/external relationship を拒否。strict expat と node/depth/text budget
- Parser: PDF native text/table + optional OCR、PNG/JPEG/WebP metadata + optional local OCR、厳格 CSV/TSV、allowlisted XLSX OOXML、DOCX/PPTX logical locator
- 正規化: 金額・日付・数量。税条件不明は effective price を unknown にし、0円や推測値で埋めない
- 独立検証: 元バイトを再読し、CSV byte span、sheet/cell、office block、PDF page text、image metadata を照合。不一致・owner失効・digest不一致は anchored にしない
- 文脈: untrusted タグ付き chunk、根拠候補だけを使う決定的 extractive answer、citation
- 永続化: `agent.mmi_upload_sessions`, `agent.mmi_upload_chunks`, `agent.mmi_artifacts`, `agent.mmi_parse_jobs`（RLS + lease CAS）
- Service: upload → parse job → lease claim → re-detect → parse → verify → CAS commit → retrieve/ask/revoke

### 1.2 API

`backend/packages/api/src/aiagent_api/multimodal_api.py` を `create_app()` に接続した。

- `GET /v1/multimodal/capabilities`
- `POST /v1/multimodal/uploads`
- `PUT /v1/multimodal/uploads/{upload_id}/chunks/{index}`
- `POST /v1/multimodal/uploads/{upload_id}/complete`
- `GET/DELETE /v1/multimodal/artifacts/{artifact_id}`
- `POST /v1/multimodal/artifacts/{artifact_id}/jobs`
- `POST /v1/multimodal/jobs/{job_id}/run`
- `GET /v1/multimodal/jobs/{job_id}`
- `POST /v1/multimodal/jobs/{job_id}/cancel`
- `GET /v1/multimodal/artifacts/{artifact_id}/context`
- `POST /v1/multimodal/artifacts/{artifact_id}/answer`
- `GET /v1/multimodal/jobs/{job_id}/citations`

認証は既存のBearerまたはHttpOnly Cookieセッション。状態変更は既存CSRF検証を通る。他ownerのartifactは403。

### 1.3 Web UI

`web/src/MultimodalView.tsx` を追加し、Control Consoleと並ぶ「Multimodal」タブから実行できる。

- ブラウザでファイルを選択し、`crypto.subtle` でSHA-256を計算
- 512 KiBずつ順次アップロードし、チャンクごとのSHA-256を送る
- 完了後にparse jobを作成・実行
- detected format、quarantine state、job status、warning、missing field を表示
- 抽出候補と independent verification の結果、page/record/cell/slide の locator を表示
- 決定的grounded answerとcitationを表示
- `parserIsolation` が `in_process_dev` の場合は、検証済みの隔離境界ではない旨を常時表示

## 2. 実行境界（重要）

### 2.1 実装済み

- `ParserExecutor` Protocol と `Pipeline`
- API/Service は executor 未設定時に fail closed（`POLICY_DENIED`）。信頼済みruntimeへの暗黙fallbackはしない
- `InProcessParserExecutor` は開発・テスト専用。`AIAGENT_MMI_ALLOW_INPROCESS=1` は開発用の明示opt-inであり、`profile=production` ではこの設定や明示executor注入があってもMMI API全体を503で閉じる
- stale lease（owner/epoch不一致）の結果はcommitしない
- 同一parse keyのcanonical化、idempotent upload replay
- parser build digest と入力digestの照合
- 完了commit後にadmissionだけ失敗した場合、blobを消さずに再実行でadmissionを修復する
- 同一chunk indexの再送は同一長・SHA-256ならidempotent、異なる内容は`CHUNK_CONFLICT`。失敗時にstaging全体をabortしない
- parse jobは期限・attempts上限・lease owner/epochをCASで確認し、期限切れleaseは再claimできる。revoke後は派生job/outcome/document/citationを返さない
- `/jobs/{id}/run` は同期parseを`asyncio.to_thread`へ逃がし、event loopを塞がない
- workerが返したDocumentは信頼せず、パイプラインが元バイトを独立に再parseして候補を導出する。OCR候補は自己再parseでも`unverified`のまま

### 2.2 未実装（Verifiedではない）

- 非root・read-only rootfs・no-network・seccomp/AppArmor/Landlock・cgroup CPU/RSS/PID/disk・FD上限を備えた**実parser jail**
- 別guest kernelのUntrusted Execution VMでの実行
- parser workerのOS-level kill/cleanupの実測
- 悪性PDF/OOXML/画像の実corpusによるresource exhaustion試験
- 外部vision gateway（privacy gate・予算予約・provider retention確認を含む）
- Agent Task の Tool Gateway から `ingest_file_readonly` を呼ぶ経路

現時点のgrounded answerは、Task AgentのLLMではなく、検証済み候補だけを使う決定的処理である。これは「Agent Runtime経由の回答」ではない。Task Tool Gatewayへの登録は仕様15-01 §12の提案段階のままで、実装していない。

## 3. 設計判断と理由

### 3.1 migration 0029 の衝突

- 最新 `origin/main` のmigrationは `0028` まで。
- PR #122（Autonomous Development / Phase 7）が `0029` を主張している。
- 本ブランチは仕様の連番規約に従い `0029_multimodal_ingestion` を追加した。
- **merge順序により片方の番号を振り直す必要がある。** 本PRでは勝手にrenumberせず、衝突をここに記録する。
- `aiagent_core.migrate.APP_SCHEMA_VERSION` を `29` へ更新し、既存のmigrationテスト12件も通した。
- 本ブランチのDBテストは、このリポジトリの作業ツリーで `0029` まで適用した専用DBで実行した。

### 3.2 nested archive を拒否

ZIP内ZIPを許可すると、外側のentry/total/depth budgetを満たしても内側で再帰的に増幅できる。仕様のdepth budgetだけに依存せず、nested archiveは `BAD_ARCHIVE` で拒否する。必要な業務要件が出た場合は、展開を別job・別budget・別quotaに分離してから許可する。

### 3.3 optional dependency

- coreの必須動作はstdlibのみ。
- PDF native text/table は `pdfplumber` optional extra。未導入時は `PDF_TEXT_EXTRACTION_UNAVAILABLE` を付けたpartial resultにし、本文を捏造しない。
- 画像decodeはPillow optional。OCRは固定Tesseract binaryがある場合だけ動く。
- optional extraは実行環境の供給網・image digest・language pack digestの対象であり、自動downloadはしない。

### 3.4 0幅bbox

PDFフォントに幅情報がない場合、pdfminerが同じx座標のglyph boxを返すことがある。bboxを正規化するとzero widthになり契約違反になるため、その場合はbboxをunknownにし、page/line locatorだけ残す。citationの座標を捏造しない。

### 3.5 OCR confidence と worker 出力の非信頼

OCR confidenceは「認識器の自己申告」であり真実ではない。confidenceが低い候補はestimated扱いにし、独立検証に失敗した値をanchoredにしない。未検出fieldはunknownのまま返す。

さらに、隔離workerが返したDocument全体を非信頼入力として扱う。候補抽出はworkerのDocumentではなく、immutable bytesをTrusted側で再parseしたDocumentから行う。ただしこれは同一Parser実装による再parseであり、Parser共通の抽出欠陥を排除する独立検証ではない。OCR由来の値は同じbytesを再parseしてもテキストを pixel から独立再認識しない限り`unverified`であり、自己申告confidenceでanchoredにしない。

## 4. 実行した検証

環境: Windows、Python 3.12、portable PostgreSQL 16（127.0.0.1:55433）、npm/Vite。

### 4.1 Pythonテスト

```powershell
$env:AIAGENT_TEST_PG_URL='postgresql://postgres@127.0.0.1:55433/postgres'
$env:PYTHONPATH='packages/multimodal/src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_multimodal_core.py tests/test_multimodal_service.py tests/test_multimodal_pg.py tests/test_multimodal_api.py
```

結果: `core 38 passed`、`service 15 passed`、`PostgreSQL 5 passed`、`HTTP 3 passed`、`migration 12 passed`（合計73、各グループを順に実行）。

内訳:

- core 38: detection、archive/XML gates、CSV/XLSX/DOCX/PPTX、PDF preflight + native text（pdfplumber導入時）、image container、正規化、verification、contract、worker Document非信頼、OCR非anchor、XLSX cached formulaのestimated維持、bbox限定PDF検証、normalized非replay/不一致、スラッシュ日付replay、同時chunk publish非上書き、金額の境界・通貨競合・桁上限、contextのformula状態投影、formula errorの明示unknown、送料ラベルの非amount化、hidden sheetのunknown化、OCR blockのestimated投影
- service 15: upload→parse→retrieve→ask、digest mismatch、revoke、stale/expired lease、chunk replay/conflict、完了再試行時のstaging保持、executor未設定のfail closed、commit後admission失敗からの修復、DB append失敗時の未確定chunk回収、質問entityと行の結合、期限切れrequest deadlineの非parse、chunk slot排他、commit ACK喪失後のblob保持
- PostgreSQL 5: RLS/owner isolation、lease CAS、chunk replay/conflict、期限切れlease再claimとrevoke fence、latest document、chunk transactionのadvisory lock排他
- HTTP 3: 実 `TestClient(create_app(...))` でchunk upload、parse、citation、answer、他owner 403、production全閉塞、256KiB bodyを読む前のproduction gate
- migration 12: `0029` までの連番・schema version・既存テスト

### 4.2 静的検査

```powershell
.\.venv\Scripts\python.exe -m ruff check packages/multimodal packages/api/src/aiagent_api/multimodal_api.py tests/test_multimodal_*.py tests/multimodal_fixtures.py
.\.venv\Scripts\python.exe -m ruff format --check ...
.\.venv\Scripts\python.exe -m mypy packages/multimodal/src packages/api/src
```

結果: ruff all checks passed、format 7 files unchanged、mypy `Success: no issues found in 35 source files`（multimodal + API source）。

### 4.3 Web build

```powershell
cd web
npm ci
npm run check
npm run build
```

結果: `tsc --noEmit` 成功、Vite production build 成功。

### 4.4 実ブラウザ

ローカルAPI（`http://127.0.0.1:8099`）と実PostgreSQLに対して、Chromiumの実ブラウザで以下を確認した。

1. Control ConsoleでBearer→HttpOnly Cookieのセッションを開始
2. Multimodalタブで実CSV（`price.csv`, 23 bytes）を選択
3. ブラウザがSHA-256を計算し、chunk PUT → complete → job作成 → run がすべて成功
4. artifact format `csv`、quarantine `admitted`、job `succeeded`
5. amount `100 JPY` が `anchored`、locator `record 2 · field 2`
6. questionへの回答が `status answered`、citationがamount
7. 実PDF（`invoice.pdf`, 588 bytes）でもformat `pdf`、amount `100` が `anchored`、locator `page 1`、回答が `page 1` を引用

この環境のAPIは `parserIsolation: in_process_dev` を返しており、UIにもその警告を表示した。**これは本番の隔離境界ではない。** この実ブラウザ確認は旧HEAD `d34cd95` 時点であり、現在のHEADでは再実行していない。

### 4.5 PR #126 レビュー対応（2026-11-26）

- P1-01: `profile=production` のMMI API全体を503にし、`/capabilities`も隔離・OCRを`unavailable`と返す。
- P1-02: workerのDocumentを候補抽出に使わず、元バイトをTrusted側で再parseする回帰テストを追加。同一Parser実装のため、独立検証としては部分的（R2-01参照）。
- P1-03: OCR候補は独立再parseでも`unverified`のままにする回帰テストを追加。
- P1-04: revoke時に派生jobを`cancelled`にし、outcome/document/citationを消去・拒否。
- P1-05: 期限切れleaseのfinish拒否と再claim、attempts上限、admission fenceを実PostgreSQLで試験。
- P1-06: chunk再送を同一index・長さ・digestでidempotentにし、内容差分は`CHUNK_CONFLICT`。
- P1-07: finalizeはstagingをcommitまで保持し、失敗時に全体abortしない。
- P1-10/11: 符号付き金額、`unknown`/`conditional`の伝播、無関係な質問の`unknown`を試験。
- P1-14: `/jobs/{id}/run` の同期処理をthreadへ退避。
- 未解決: P1-08（at-rest暗号化・保持/tombstone）、P1-09（Task/Run/workspace/RLS結合）、P1-12/13（実jail・資源上限）、P1-15のAgent Runtime経由の回答。

### 4.6 再レビュー R2/R3 対応（HEAD d34cd95 以降）

R2-01〜R2-12とR3-01〜R3-08を再監査し、次のコード変更と回帰試験を追加した。**未解消項目を解消済みとして扱わない。**

- R2-01（一部）: PDFの検証をページ全体の文字列一致から、候補の`normalized_bbox`と交差する`TextBlock`だけに限定した。bbox外の同一文字列ではanchorしない回帰試験を追加。ただし独立再parseは同じParser実装・同一信頼プロセス上で動くため、Parser実装の共通欠陥を排除する独立検証ではない。
- R2-02（一部）: 正規化値の再計算結果が`None`（金額・日付以外の型、または通貨不明）の場合はanchorせず`unverified`にした。金額は生値の再parse、通貨、税条件を再計算し、金額/通貨の不一致は`contradicted`、税文脈の不一致は`unverified`とする。
- R2-03（未解消）: 数字の存在と価格の意味の混同は残る。今回、金額候補に価格列・価格ラベル・明示通貨のいずれかを要求し、`型番1234`等の裸の数字を除外した。`$100 CAD`/`$100円`は`CONFLICTING_CURRENCY`としてunknownにし、localeだけでは通貨を推定しない。ただし記号の複数通貨性と文脈依存の意味解決は未解消。
- R2-04（一部）: chunk publishを一意なtemp名＋`link()`の原子的公開に変更し、同一indexへの同時書込みが他方のバイトを上書きしないようにした。DB append失敗時は、DBに未確定かつ自分が公開したdigestと一致するchunkだけを削除し、後続の別バイトを恒久的にブロックしない。同時書込みとDB失敗の回帰試験を追加。DB/FSの予約・確定を単一トランザクションにする処理と、finalizeとDB commitの間のクラッシュ回復は未解消。
- R2-05: 未解消。finalizeのクロスプロセス排他、孤児blobの定期回収、commit不明時の照合は未実装。
- R2-06: XLSXの`TableCell.value_state`/formula/cached errorを`TextSource`へ伝播し、cached formula値は`estimated`、cached errorは`unknown`のまま候補化する。`_build`はsourceより強いstateへ昇格せず、検証も`estimated`/`unknown`セルをanchorしない。回帰試験を追加。
- R2-07（一部）: 既存のrevoke時job無効化・blob削除・GET/citation拒否を維持。HTTPリクエスト中のrevokeとの厳密な直列化は未解消。
- R2-08（一部）: `/jobs/{id}/run`に加え、complete/get/revoke/start/get/cancel/retrieve/answer/citationsの同期service呼出しを`asyncio.to_thread`へ退避した。スレッド実行の強制停止、実cgroup/CPU/RSS/FD上限は未解消。
- R2-09〜R2-12: 未解消。決定的extractive回答はAgent Runtime/Gateway経由ではなく、保持期限切れの物理削除、UIのcitation再照合は限定的。
- R2-10（一部）: requestの`deadline_at`がparse開始時に経過していれば、生バイトをparseせず`PARSE_TIMEOUT`でjobを失敗させる回帰試験を追加。idempotency/cache/latestの分離は未解消。
- R3-02（一部）: 金額の字句境界を厳格化し、桁数上限とquantize失敗を明示的に拒否した。`1,23`や`2026/11/270`のような不正な区切り・日付を候補にしない回帰試験を追加。
- R3-03（一部）: `ContextChunk`に`valueState`/`flags`/`observations`を追加し、XLSXのestimated/formula/cached errorをcontextへ投影する。formula errorの空セルは`FORMULA_CACHED_ERROR`の明示unknown候補にした。Context consumerの全てがこの状態を保持する保証は未検証。
- R3-04（一部）: 上記R2-04のDB append失敗時rollbackを追加。DB/FS間の予約順序・期限・孤児回収は未解消。
- R3-05（一部）: `create_app`がproductionのとき、`/v1/multimodal/*`（`capabilities`を除く）をbody読取前に503にするASGI middlewareを追加。256KiB bodyで503になる回帰試験を追加。`build_router`を単独で組む呼出しにはmiddlewareが付かないため、本番構成は`create_app`経由に限定する。
- R3-07（一部）: 回答時に質問のentity termと候補の行contextを照合し、`banana`の価格を聞いたとき`apple`の行を回答しないようにした。意味的同義語・複数行join・文脈の一般解決は未実装。
- R3-01/R3-06/R3-08: 未解消。独立Parser/Verifier、source epochとTask/Run/workspace結合、実CPU/RSS/FD上限と強制停止は未実装。

追加・実行した回帰試験: XLSX cached formulaの`estimated`/`unverified`、bbox外PDF文字列の非anchor、normalized値の非replay/不一致、スラッシュ日付のreplay、同一chunk同時publishの非上書き、金額境界・通貨競合・裸の価格、contextのformula状態、formula errorの明示unknown、DB append失敗時のchunk回収、entityと行の結合、production body前gate。

### 4.7 第4回レビュー R4 対応（HEAD b509fd2 以降）

R4-01〜R4-12を再監査し、次の限定的な変更と回帰試験を追加した。**部分解消を解消済みと扱わない。**

- R4-01（一部）: chunk slotを`Store.chunk_transaction`で排他し、FS publish→DB append→失敗時cleanupを同一ロック内に置いた。PostgreSQLは`pg_advisory_xact_lock`、in-memoryはper-index RLockを使う。DB append失敗後に別writerが受理済みのchunkをunlinkするTOCTOUを塞いだ。回帰試験はservice 2件、PostgreSQL 1件。
- R4-02（一部）: DB commitを試行した後に例外が起きてもfinal blobを削除しない。duplicate finalizerが既存artifactを受け取った場合は自分の未参照blobだけを削除する。**finalizeのクロスプロセス排他、orphan GC、commit不明時のDB照合は未実装。**
- R4-03（一部）: hidden sourceを`unknown`にし、normalized値を消す。`価格: 100円、送料: 500円`では送料をamountにしない。**金額のrole/conditionを型付きで表す完全な意味解決は未実装。**
- R4-04（一部）: OCR blockとhidden block/cellをContextで`estimated`/`unknown`、`ocr_unverified`/`hidden_source`、observation付きで投影する。**全consumerが状態を保持する保証は未検証。**
- R4-05（一部）: 質問のentityは全term一致かつASCII単語境界で照合する。`apple red`はapple行で回答せず、`pineapple`はappleに一致しない。**比較・複数行join・意味的同義語は未実装。**
- R4-06: 未解消。Context chunkの取得上限はあるが、consumerごとのcoverage/budgetのend-to-end保証はない。
- R4-07: 未解消。独立Parser/Verifier、VM/jail、network/UID/capability分離はない。
- R4-08（一部）: production gateはbody読取前に503、request deadlineはparse前に拒否する。**development/testの同期I/Oと実スレッド停止は未解消。**
- R4-09/R4-10/R4-11/R4-12: 未解消。revoke/source epochの厳密な直列化、FS ACL/symlink/所有権、Runtime/Gateway回答、Task/Run/workspace RLS・at-rest暗号化・restoreは未実装。

`INTEGRATED_VERIFIED=0`。上記はローカルunit/HTTP/PostgreSQLの範囲を出ない。

## 5. 状態の正直な区分

| 項目 | Designed | Tested | Verified |
|---|---|---|---|
| Upload/quarantine/hash/idempotency | Yes | Yes（unit/HTTP/PG） | ローカル実環境のみ |
| Magic detection / mismatch | Yes | Yes | ローカル |
| ZIP/XML security gates | Yes | Yes（fixtures） | 悪性実corpusは未検証 |
| CSV/XLSX/DOCX/PPTX | Yes | Yes | ローカル |
| PDF native text | Yes | Yes（pdfplumber導入時） | 実PDF corpusは限定的 |
| PDF table | Yes | 未（fixture未作成） | No |
| Image header validation | Yes | Yes | OCRは未実行 |
| Independent verification | Yes | Yes（CSV/PDF/office/cell） | 画像pixel再読は未実装 |
| RLS / lease CAS / owner isolation | Yes | Yes（実PostgreSQL） | ローカルPGのみ |
| Web UI E2E | Yes | Yes | ローカルChromiumで確認 |
| Isolated parser jail / VM | Yes | No | **No** |
| External vision | Yes（仕様） | No | **No** |
| Task Tool Gateway連携 | Yes（提案） | No | **No** |
| 悪性文書・資源枯渇の実測 | Yes（試験設計） | No | **No** |

## 6. 既知の制約・次の作業

- 実parser jail/VMを作り、非root・no-network・seccomp/Landlock・cgroup・kill後cleanupを実測するまでproduction有効化しない。
- 画像は現在、独立検証でpixelを再読していない。metadata/digest検証に留まる。
- OCRのEXIF座標変換は未適用。`OCR_COORDINATES_EXIF_UNCORRECTED` を警告する。
- `MmiSourceLocator` のhidden検証は、hidden sheetとhidden row/columnを混同しないよう整理が必要。
- PDF tableのbboxは回転0を仮定している。
- migration 0029はPR #122と衝突する。merge順を決めた担当者が片方をrenumberし、両方のDBテストを再実行する。
- Task Agentから文書を読むには、Tool Gatewayのread-only toolとしてmanifest/schema/citation変換を追加し、Tool Intent・approval・Run fencingに接続する必要がある。