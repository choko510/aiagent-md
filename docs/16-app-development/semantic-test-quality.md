# Semantic Test Quality (ntlint) — 後期ロードマップと受信Adapter

> 2026-11-17: Slice 0 / Read-only code only / **NOT RUNTIME-INTEGRATED** / Non-blocking。
> DEV-07 Review、DEV-14 Adaptive Testing、CHAT_03/09/11の補助。新しいDEV-ID/Tool権限/DB/API/必須CI Gateは追加しない。

## 調査（upstream 0.1.1）

- 公式情報: https://github.com/L4Ph/ntlint （README、docs/design.md、packages/cli/src/main.ts、packages/core/src/report.ts、gate.ts）。
- npmは noul-testlint、CLIは ntlint、Node >=24、MIT。JavaScript系、Python、Rust、Goに対応するテスト構文抽出。
- 17ルール中、自前でfitted cutoff = 0、borrowed = 5、placeholder = 12。検出結果を**確定バグやP0/P1と扱わない**。
- CLIは JSONL が既定。finding行の後にsummary行。--format jsonは異なる構造。complete:false、missing>0、failures>0、exit 2は不完全。--fitted-onlyは現時点でルールが0になりexit 2。
- --dry-runはAPI不要で費用概算のみ。**実スキャンの既定 --arm file は実装ファイルも外部モデルに送る**。--arm bareでもテスト本文を外部送信する。どちらも個別同意が必要。
- .test-lint/cache.jsonはワークスペースへ書き込まれる。依存・pluginはCell外で起動しない。Pythonのテスト発見についてupstream設計書にpytestとの一致未検証とある。
- このツールはpytest、Ruff、mypy、mutation testing、独立3AI reviewer、既存required gateの代替ではない。

## 今回追加するもの

backend/packages/development/src/aiagent_development/ntlint_report.py:
- ntlint 0.1.1 JSONLを**純粋なPython関数**で解析。認証済みCell artifactを渡せるようになるまで呼び出しはしない。
- trusted ownerが与えたsource_shaとexpected_shaの一致を要求。JSONL本文内のSHAやLLMの自己申告を権限にしない。現段階でowner認証/Artifact transportは未実装。
- サイズ・レコード数・Path・行番号・数値・集計整合・完全性をチェック。欠落はInvalidEvidenceまたはINCOMPLETE。0テストはNO_TESTS。NO_CANDIDATESは合格証明ではない。
- 返すのはrule / file / line / severity / scoreと実行集計のみ。非信頼のsubject・docs・任意テキストを構造化findingに転記しない。
- subprocess、Node依存、外部API送信、データベース、Gateway、Runtime Cell、release gate、review receipt、CI設定は変更しない。

### ntlint 0.1.1 JSONL形式の厳格な受入（2026-11-17レビュー対応）

- finding 14フィールドとsummary 12フィールドを現行0.1.1の既定JSONL出力に合わせて固定する。重複JSONキー・未知キー・不足キーを拒否する。上流の形式が変わった場合にはversion pinとfixtureを同時更新する。
- finding: type, ruleId, docs, severity, messageId, file, line, endLine, subject, subjectSource, value, at, margin, confidence。summary: type, verdicts, reported, candidates, unsure, missing, requests, inputTokens, usd, cached, failures, complete。
- 集計分類は互いに排他的: reported + missing + unsure <= verdicts。閾値未満の診断は未報告のため等号を要求しない。candidates数は実際のcandidate finding数に一致させる。
- requestsとcachedを含む全summary数値を型・範囲検査する。返さないfinding補助フィールドも形式検査するが、subject/docsは依然として戻り値へ含めない。検出結果はコード品質の確定判定ではない。
- float変換時のOverflowError/ValueError、UnicodeのUTF-8変換失敗、重複キーはInvalidEvidenceで拒否する。Git SHAは既存のaiagent_development.contracts.require_sha()を使用して40桁と64桁を受理する。
- SHA一致は認証済みArtifactである証明ではない。外部API実行・Cell経由の取り込みは未実装、既存DEV-07/14の合否Gateは変更しない。
## 将来の実行統合に必要な順序

1. PR #110のDEV-13と共有する**認証済みsource+artifact broker**、実際のVM/Cell隔離、lease/Goal/policy/revoke連動の完成。SCIP indexerのB2は現時点PARTIALで、独自経路で迂回しない。
2. Tenant/Project/immutable commitに対する明示opt-in、送信してよいソース範囲・機密検査・撤回・TTLとトークン/ドル/時間上限。bareでも外部送信することを説明。
3. npm version+lock+digestの固定、Node >=24と依存を固定Cell imageへ。実行中のnpx package取得/postinstall不可。外部pluginは非信頼扱い。
4. Typesafe AI API宛てのTLS egressは既存Sentinel/Gatewayの認可・Permit・実送信を経由。Tool HTTPS CONNECT拒否を迂回しない。未対応ならBLOCKED。
5. SHA、tool version、runtime digest、input scope、receipt/artifact digestを記録し、現在のHEAD/Goal/lease/policyと再照合。Cacheはtenant+source SHAで隔離し、private codeやAPI keyをログに出さない。
6. DEV-14 FAST_FEEDBACKの任意diagnosticとして起動し、DEV-07の独立reviewへ**候補**として渡す。候補の自動修正・自動merge・P0/P1昇格は禁止。必要なテストの実行と独立reviewは従来どおり。
7. Python/JavaScript/Rust/Goのtest discovery vs runner、canary、false positives/negatives、安定性、実費、失効・中断・partial・secret漏洩の負例を実測してからlimited beta。

## 受入追跡（今回のSlice）

- NTL-T01: 正常候補を読み、独立Review/RequiredGateへ昇格しない。
- NTL-T02: missing、failures、unsure、zero-test、異常exitをcleanとしない。
- NTL-T03: stale SHA、unsafe path、偽summary、NaN、過大出力を拒否。
- NTL-T04: 非信頼のsubjectやdocsを転送しない。
- NTL-T05: Developmentのisolated Python tests/CIを検査。実ntlint/API/VM実機はNOT RUN。

## 既存オープンPRとの非干渉（2026-11-17）

- #110 https://github.com/choko510/aiagent-md/pull/110 : DEV-13 SCIP実行系 (Draft)。factory/workflow/code_intelligence/sandbox/既存テストに一切触れない。Runtime Ownerの権限を先取りしない。
- #111 https://github.com/choko510/aiagent-md/pull/111 : UI/Auth/通知 (Ready for review、2026-11-17確認)。API/Web/DB/workflowsに触れず、現行UIの実装済み前提を置かない。
- 本PRはmain 69404bb2fd067d048b1735ca801fa0db9dc4006e から分岐し、**Draft / mergeしない**。両PRの将来の統合後はその新しいSHAで再検証する。

受信Adapterだけでは、本番でのntlint実行、外部API安全性、全ソース検査や検出精度を実証しない。実行統合は上記権限を満たした後続PRで行う。
