# Universal Personal Agent Harness — Parallel Specification Program

**セルフホストの汎用パーソナルAIエージェント**を、既存ハーネスを横断研究しながら実装可能な詳細仕様へ分解して設計するためのGitHubワークスペースです。スクレイピング専用・Amazon専用・コーディング専用ではありません。自然言語によるWeb調査、商品比較、SNS調査、ホテル調査、定期監視、通知を、同じAgent Runtimeとツール基盤で扱います。

> **重要：85個の詳細仕様MD（当初74＋追加11）はこれから作るものです。** 本repoにAgent本体のコードはありません。各担当AIはDraft PRに仕様を提出し、人間が横断レビューして確定します。

## Start here — AIチャットが必ず読む文書

1. **[Project Brief — ユーザーが本当に作りたいもの](docs/00-foundation/project-brief.md)**
2. **[Architecture Rules — 変更不可の境界・担当分担](docs/00-foundation/architecture-rules.md)**
3. **[Acceptance Scenarios — 実際の使用例と合格条件](docs/00-foundation/acceptance-scenarios.md)**
4. [Research Targets — 参考OSSマップ](docs/00-foundation/research-targets.md)
5. [AGENTS.md — 全チャットの作業ルール](AGENTS.md)
6. [COMMON — 詳細調査・納品プロンプト](prompts/COMMON.md)
7. [Specification Inventory — 全74MDの担当一覧](docs/00-foundation/spec-index.md)
8. [Research Protocol](docs/00-foundation/research-protocol.md) · [Review Gates](docs/00-foundation/review-gates.md) · [Module Template](templates/MODULE_SPEC.md)

## 16 parallel chats

| 担当 | 仕様カテゴリ | 開始プロンプト |
|---|---|---|
| CHAT_00 | `docs/00-foundation/` (5MD) | [CHAT_00](prompts/CHAT_00.md) |
| CHAT_01 | `docs/01-contracts/` (6MD) | [CHAT_01](prompts/CHAT_01.md) |
| CHAT_02 | `docs/02-runtime/` (12MD) | [CHAT_02](prompts/CHAT_02.md) |
| CHAT_03 | `docs/03-tools/` (6MD) | [CHAT_03](prompts/CHAT_03.md) |
| CHAT_04 | `docs/04-web-browser/` (9MD) | [CHAT_04](prompts/CHAT_04.md) |
| CHAT_05 | `docs/05-connectors/` (5MD) | [CHAT_05](prompts/CHAT_05.md) |
| CHAT_06 | `docs/06-memory-skills/` (5MD) | [CHAT_06](prompts/CHAT_06.md) |
| CHAT_07 | `docs/07-automation/` (5MD) | [CHAT_07](prompts/CHAT_07.md) |
| CHAT_08 | `docs/08-interfaces/` (6MD) | [CHAT_08](prompts/CHAT_08.md) |
| CHAT_09 | `docs/09-security/` (6MD) | [CHAT_09](prompts/CHAT_09.md) |
| CHAT_10 | `docs/10-infrastructure/` (5MD) | [CHAT_10](prompts/CHAT_10.md) |
| CHAT_11 | `docs/11-qa/` (4MD) | [CHAT_11](prompts/CHAT_11.md) |
| CHAT_12 | `docs/12-intelligence/` (5MD) | [CHAT_12](prompts/CHAT_12.md) |
| CHAT_13 | `docs/13-event-ingestion/` (3MD) | [CHAT_13](prompts/CHAT_13.md) |
| CHAT_14 | `docs/14-adaptive-experience/` (2MD) | [CHAT_14](prompts/CHAT_14.md) |
| CHAT_15 | `docs/15-multimodal/` (1MD) | [CHAT_15](prompts/CHAT_15.md) |

## 新しいチャットへ渡す最小限の開始メッセージ
```text
https://github.com/choko510/aiagent-md の CHAT_XX を担当してください。
必ず最新mainの prompts/CHAT_XX.md と prompts/COMMON.md を全文読み、
指定された project-brief・architecture-rules・acceptance-scenarios・
spec-index・review-gates・template・AGENTS.md も実際に確認してください。
担当の最優先MDを一次資料・OSSの実コード・テストまで調査し、
実装可能な詳細仕様を書いて専用ブランチのDraft PRに提出してください。
mainへ直接pushしたり、他担当のMDを変更したり、無断マージしないでください。
完了後にPR URL、調査根拠、未決事項を報告してください。
```

## 協調手順
- CHAT_00/CHAT_01を最初に起動し、残りの10チャットも並列で調査開始可能。共通型が未決なら[PROVISIONAL]を使う。
- 各チャットは自分の担当1〜2件の詳細仕様MDを専用ブランチでDraft PRとして提出する。重要な変更は他担当の契約Ownerに提案する。
- 仕様のDraftは「完成・確定」ではない。レビュー・契約照合・合格テスト設計を通してLockedにする。
- [Coordination](docs/00-foundation/coordination.md) と [Review Gates](docs/00-foundation/review-gates.md) が統合の規律を定義する。

## References
前バージョンの大きな統合マスターv0.2は会話で作成した歴史的な設計案。**このrepoへ未転記の内容を、AIが読んだことにしてはいけない。** 現時点では本repoのProject Brief・Architecture Rules・Individual Specsを正式な作業起点とし、必要な情報は一次資料で再検証する。

## Optional audit chats (separate from the 12 authors)
- [COORDINATOR — 12チャット横断の契約照合・PR統合監査](prompts/COORDINATOR.md)
- [INDEPENDENT_REVIEWER — 1 PRずつの詳細監査](prompts/INDEPENDENT_REVIEWER.md)

**推奨：16執筆チャット + 1統合コーディネータ**。74件のMDが全て揃っても、横断レビューで契約を整合させるまでは仕様確定としない。

## Approved expansion: Autonomous Discovery & Events (2026-10-30)
The user additionally selected 11 functions, including "start an agent when a specified X account posts". Read **[feature-expansion-2026-10.md](docs/00-foundation/feature-expansion-2026-10.md)** before new functionality planning. Four newly assigned chats are CHAT_12–15 (5+3+2+1 specs). All specs are investigation/draft until reviewed; no agent code exists here. The existing authors' open PRs are not overwritten.

## Round 2 — 全仕様完成に向けた継続プロンプト (2026-10-30)
- [各CHATの継続執筆指示 — 初回1件で終了しない](prompts/ROUND2_AUTHOR_CONTINUE.md)
- [重要な共通契約不整合の解消](prompts/ROUND2_CONTRACT_RECONCILIATION.md)
- [横断統合COORDINATOR指示](prompts/ROUND2_COORDINATOR.md)
- [CHAT_00 Foundation専用開始指示](prompts/ROUND2_CHAT_00_FOUNDATION.md)

**必ずmainだけでなく各担当ブランチ/Draft PRを確認**。11初回PRの提出は全85仕様完成ではない。各カテゴリの全MDを1〜2件ずつDraft PRで継続し、他Ownerの契約は提案で協調する。マージ・Locked化はreview gatesを通過してから。
