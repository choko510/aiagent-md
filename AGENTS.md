# All AI Authors — Mandatory Repository Instructions

このリポジトリは実装コードではなく、実装可能な仕様MDを並列調査・作成するためのワークスペース。

## 必読
`README.md`, `docs/00-foundation/architecture-baseline.md`, `docs/00-foundation/coordination.md`, `docs/00-foundation/spec-index.md`, `docs/00-foundation/research-protocol.md`, `templates/MODULE_SPEC.md`, `prompts/COMMON.md`, 自分の `prompts/CHAT_XX.md`。

## 所有権とGit運用
- CHAT_XXは`docs/XX-<category>/`にある **自分の担当MDだけ** を作成・更新する。
- `README.md`, `AGENTS.md`, `prompts/`, `templates/`, `docs/00-foundation/spec-index.md`とbootstrap文書は管理担当のみ。
- 他領域の契約が変更必要ならPR本文に`Cross-team proposal`を書く。他人のMDを直接変更しない。
- 最新`main`から`spec/chat-XX-<module>`ブランチを作り、**1〜2MD/1 Draft PR**。直接main push・auto-merge禁止。
- 契約未確定の参照は`[PROVISIONAL: CHAT_XX]`。共有型の正本は01、DB DDLは10、セキュリティは09、全体ADRは00。

## 調査・執筆
- 公式ドキュメントだけでなく **実際のGitHubソース・テスト・Issue・変更履歴** を確認し、URL、タグ/commit、調査日を示す。
- 重要な採用判断は複数案を比較。コードで確認した事実と設計仮説を分ける。
- 型、契約、アルゴリズム、状態遷移、失敗、競合、中断復帰、セキュリティ、運用、計測、具体的テスト、受入条件まで埋める。
- 非互換ライセンス、未承認購入/投稿/予約、無制限の費用、サーバーホスト操作権限、CAPTCHA突破保証を導入しない。
- 仕様が長いことは完成の根拠ではない。証拠・整合性・テスト可能性を重視。
- 未検証は正直に`Unverified`と明示。`Locked`/`Verified`を自称しない。
