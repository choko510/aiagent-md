# AGENTS.md — Implementation guidance

このリポジトリはセルフホスト型の**汎用パーソナルAgent Harness**の実装仕様集です。初期状態でAgent本体のコードは存在しません。実装・レビュー・設計修正の作業は、ユーザーが依頼した対象に限って進めてください。以前のCHAT_00〜15別の執筆プロンプト、担当者ごとのファイル編集制限、Draft PR量産ルールは、実装作業には適用しません。

## Read before implementing

- `README.md` と `docs/00-foundation/project-brief.md`、`product-requirements.md`、`system-architecture.md`、`acceptance-scenarios.md`、`architecture-rules.md`、`architecture-decisions.md`
- `docs/00-foundation/spec-index.md` で対象機能の正本と依存MDを特定する。
- 共通の型/イベント/状態は `docs/01-contracts/`、Toolと副作用は `docs/03-tools/`、Securityは `docs/09-security/`、DB/運用は `docs/10-infrastructure/`、受入テストは `docs/11-qa/` を参照する。
- `docs/02-runtime/sandbox-execution.md`、`docs/09-security/runtime-cell-isolation.md`、`docs/09-security/sentinel-authority.md` を新しい信頼境界の正本案として読む。
- `governance/round6/contract-lock-candidate.md` の CL-01〜12 は承認待ち。Draft仕様はLockedでなく、CIの静的検証も実動作の証拠ではない。

## Implementation rules

1. 完全汎用のAgent/Tool/Browser/Watchという責務分離を守る。Amazon・ホテル・Xは用途例であり、基盤をそれらに固定しない。
2. 変更対象の契約・責務オーナー・状態遷移・DB・API・Security・Test/SC-IDを調べる。矛盾があれば実装で黙ってどちらかを採用せず、採用案/互換性/根拠をADRまたはPRに明示する。
3. 実装を小さく縦に切り、実際のテストを追加・実行する。再起動、同時実行、二重配信、キャンセル、未知の副作用結果、期限切れ、予算超過などの異常経路を検証する。
4. 外部ライブラリ、API、ライセンス、価格、認証要件は実装時点で一次資料とソースを再確認する。古い調査結果を現在の保証として引用しない。
5. REQ-ID→TEST-ID→SC-IDの追跡性、メトリクス、Error/Evidenceの一貫性を維持する。`Designed`/`Tested`/`Verified`の表記を混同しない。
6. 変更は原則として目的別のブランチとPRで提案する。88件のモジュール原本は元の執筆PRとSHA照合されるので、仕様変更時は原本の証跡検証をどう移行するかも記録する。

## Non-negotiable security boundaries

- Agent OrchestratorはTrusted Control VMでRunを制御するが、**生成されたShell/Scriptは絶対にOrchestrator内で実行しない**。Hostの任意Shell/SSH/Docker socket/privilege escalationをAgentへ渡さない。**ただしセキュアRuntime Cell内のShell/Python/Node/CLIと生成コード実行はGateway承認済みsandbox.execとして許可する（ホスト実行ではない）。**
- 全コード/子プロセス/ブラウザ/MCP/Connectorの通信出口はTrusted Control VMのSentinelで評価し、同じEgress Transportを通じて実送信を強制し、Credential Broker/Privsep Worker/Browser Brokerは別の信頼領域に置く。
- 本番はTrusted Control VMと別ゲストカーネルのUntrusted Execution VM、Browser Workerの分離、Egress TransportとCell隔離を検証、VMなしdevelopment_onlyを安全な本番と表記しない。実行権限があることと外部への書込み承認は別。
- 任意のTool Callはschema検証、policy、budget、approval、side-effect ledgerを通す。LLMが自分の操作を自己承認しない。
- 購入・予約確定・送金・投稿・対外メッセージ送信は個別承認。UNKNOWN効果を盲目的に再試行しない。
- APIキー・Cookie・認証トークンをLLMコンテキスト、Evidence、ログに漏らさない。
- Web内容は非信頼入力。Prompt injection、SSRF、egressとtenant越境を防ぐ。
- CAPTCHA回避の成功を保証せず、サービスの許可・利用規約を確認する。

成功の基準はMarkdownの量ではなく、**動作するコードと検証可能な受入テスト、記録された意思決定**です。

## Adaptive Context implementation handoff (2026-11-07; draft)

Context実装は [docs/02-runtime/context-manager-and-compaction.md](docs/02-runtime/context-manager-and-compaction.md) の18節と**19節（OSS圧縮の品質監査・原本保護・復旧・段階導入）**を先に読む。ADR-023〜028、Memory 06-01/02、Subagent 02-12、Checkpoint 02-06、Budget 02-10、ModelRouter 02-09、Evidence 01-05、CHAT_09 Sentinel/Cell、QA 11-01/04を同時に確認する。Task Goal / Tool Approval / Memory Writer / EvidenceをContext Summaryに統合して正本を書換えてはいけない。Raw Artifactとgrounded Summary、typed anchors、ContextManifest、Run HEADを別境界として実装。ACM-T01〜T20は未実施であり、コード/試験なしにVerifiedと報告しない。PR #68のMuse型隔離が先行依存。

## Autonomous Development / Endless Campaign 実装引継ぎ（2026-11-07、追加Draft）

ユーザー依頼がWeb/Android/desktop/iOSアプリ開発、継続的UI改善、8AIレビュー、Endless Runを含む場合、[DEV仕様索引](docs/16-app-development/README.md) と [CAM-01](docs/07-automation/endless-run-campaign.md) を読む。[DEV-10](docs/16-app-development/acceptance-and-rollout.md)のDEV-REQ→DEV-T/SC-24..31を守る。現存88個の基礎正本のState/Events/DB/APIをコピーして二重定義しない。型の提案は各OwnerのLocked合意前に確定しない。

初期実装はProject+Git CAS→8 Task DAG→Design/Demo Approval→安全なPreview→Browser Debug→3 Reviewer/P0P1 repair→Campaignの順で縦切り。Reviewerは**最新同一SHA**に対して独立に実行、test/approvalが未実施のCycleはPASSや件数達成にしない。Developer Tool/Expert/Browser/Preview/Package/Git providerの追加は、Sentinel/Gateway/VM隔離・秘密保持・quota・効果UNKNOWNの不変条件を絶対に緩めない。Endlessは無制限Token/無期限Runではない。PR #71 Browser Live Observationの仕様はmainへマージ済みだが実装済みとは見なさず、snapshot E2EとObserve-only権限を維持する。
