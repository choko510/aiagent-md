# AGENTS.md — Implementation guidance

このリポジトリはセルフホスト型の**汎用パーソナルAgent Harness**の実装仕様集です。初期状態でAgent本体のコードは存在しません。実装・レビュー・設計修正の作業は、ユーザーが依頼した対象に限って進めてください。以前のCHAT_00〜15別の執筆プロンプト、担当者ごとのファイル編集制限、Draft PR量産ルールは、実装作業には適用しません。

## Read before implementing

- `README.md` と `docs/00-foundation/project-brief.md`、`product-requirements.md`、`system-architecture.md`、`acceptance-scenarios.md`、`architecture-rules.md`、`architecture-decisions.md`
- `docs/00-foundation/spec-index.md` で対象機能の正本と依存MDを特定する。
- 共通の型/イベント/状態は `docs/01-contracts/`、Toolと副作用は `docs/03-tools/`、Securityは `docs/09-security/`、DB/運用は `docs/10-infrastructure/`、受入テストは `docs/11-qa/` を参照する。
- `governance/round6/contract-lock-candidate.md` の CL-01〜12 は承認待ち。Draft仕様はLockedでなく、CIの静的検証も実動作の証拠ではない。

## Implementation rules

1. 完全汎用のAgent/Tool/Browser/Watchという責務分離を守る。Amazon・ホテル・Xは用途例であり、基盤をそれらに固定しない。
2. 変更対象の契約・責務オーナー・状態遷移・DB・API・Security・Test/SC-IDを調べる。矛盾があれば実装で黙ってどちらかを採用せず、採用案/互換性/根拠をADRまたはPRに明示する。
3. 実装を小さく縦に切り、実際のテストを追加・実行する。再起動、同時実行、二重配信、キャンセル、未知の副作用結果、期限切れ、予算超過などの異常経路を検証する。
4. 外部ライブラリ、API、ライセンス、価格、認証要件は実装時点で一次資料とソースを再確認する。古い調査結果を現在の保証として引用しない。
5. REQ-ID→TEST-ID→SC-IDの追跡性、メトリクス、Error/Evidenceの一貫性を維持する。`Designed`/`Tested`/`Verified`の表記を混同しない。
6. 変更は原則として目的別のブランチとPRで提案する。85件のモジュール原本は元の執筆PRとSHA照合されるので、仕様変更時は原本の証跡検証をどう移行するかも記録する。

## Non-negotiable security boundaries

- Hostの任意Shell/SSH/Docker socket/privilege escalationをAgentへ渡さない。
- 任意のTool Callはschema検証、policy、budget、approval、side-effect ledgerを通す。LLMが自分の操作を自己承認しない。
- 購入・予約確定・送金・投稿・対外メッセージ送信は個別承認。UNKNOWN効果を盲目的に再試行しない。
- APIキー・Cookie・認証トークンをLLMコンテキスト、Evidence、ログに漏らさない。
- Web内容は非信頼入力。Prompt injection、SSRF、egressとtenant越境を防ぐ。
- CAPTCHA回避の成功を保証せず、サービスの許可・利用規約を確認する。

成功の基準はMarkdownの量ではなく、**動作するコードと検証可能な受入テスト、記録された意思決定**です。
