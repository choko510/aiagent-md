# Research Protocol & Quality Gate

## Research procedure per module
1. 用途・責務・非責務・信頼境界・前提・期待結果を固定する。
2. 該当する既存OSSを複数探し、公式Docs、関連ソース/テスト、CHANGELOG、既知のIssue/PRを確認する。各機能を「実装場所と関数・具体的な動作」で説明する。
3. ライブラリのバージョン・Gitタグ/commit SHA・調査日・API仕様・ライセンスを記録する。宣伝文句だけで性能を断言しない。
4. 重要設計は少なくとも2案比較。採用と却下理由・修正可能性・代替戦略を書く。
5. Interface/Type、状態・順序、アルゴリズム、タイムアウト、キャンセル、race、crash、再配信、権限/機密、予算、ログの扱いを設計する。
6. DB変更・イベント/API・UIが他領域の所有ならCross-team proposalを作り、直接仕様を重複させない。
7. Unit/Integration/E2E/障害注入/安全性/性能の**検証可能なテストケース**と受け入れ基準を書く。
8. 未確認点は仮説と明記して、どう検証するか・誰が判断するかを残す。

## Evidence levels
`Code verified`: 実際のコード/テストを調査。`Docs verified`: 公式ドキュメントの記載。`Issue report`: コミュニティ報告。`Hypothesis`: 未検証設計。`Unverified`: 未確認。引用はURL、対象バージョン・コミット、調査日を付ける。

## Rejection conditions
推測だけのAPI、架空のベンチマーク、ライセンス無視、共通型の勝手な再定義、無制限ループ・出費、不可逆操作の盲目的再試行、秘密情報のAIモデルへの露出、未承認の購入/予約/投稿、ホスト権限付与、CAPTCHAの突破保証、テスト未実施なのに『動作確認済み』と書くこと。

## PR checklist
- [ ] owned paths only
- [ ] current main and upstream versions confirmed
- [ ] code/doc/issue/hypothesis distinguished
- [ ] contracts and ownership explicit
- [ ] algorithms and failure/race/restart behavior specified
- [ ] security and licensing covered
- [ ] measurable test cases included
- [ ] unresolved decisions and cross-team proposals recorded
- [ ] no unapproved external side effects
