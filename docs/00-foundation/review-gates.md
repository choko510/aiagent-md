# SPECIFICATION REVIEW GATES — PRをReadyにする条件

> 初期状態はすべてDraft。チャット担当AIが単独で`Verified`や`Locked`と判定することを禁止する。

## Research completeness (source evidence)
- [ ] 主要決定に一次資料または実ソース/テストのURL・ref/tag・閲覧日がある。
- [ ] Code verified / Docs verified / Issue report / Hypothesis / Unverified を書き分けた。
- [ ] 高インパクト決定で2案以上の代替を比較。費用、性能、事故時、運用、ライセンスを評価した。
- [ ] 実装不能/未対応の制限や開発対象外を明記した。

## Engineering completeness (implementability)
- [ ] Scope/non-scope、前提条件、入力・出力/副作用と所有者が明確。
- [ ] API/型/Validation/Event/DBのどれが正本か、重複なしに説明した。
- [ ] シーケンス/状態遷移/擬似コードを用いて処理を完全に定義した。
- [ ] timeout/retry/cancel/restart/duplicate/lost result/partial failureを網羅した。
- [ ] browser/session/worker/DB等の所有権と境界を明記した。
- [ ] 頻度、容量、トークン、費用、並列度、アクセス制限に有限の上限を設けた。
- [ ] セキュリティ/Privacy/権限/情報出典、Prompt injectionなどに対処した。
- [ ] migration/rollout/backward compatibility/observabilityを扱った。

## Test traceability
- [ ] 全MUST要件にREQ-ID、対応するTEST-ID、確認可能なexpectedを付けた。
- [ ] 正常・異常・競合・クラッシュ・侵入/権限・期限/費用・同一性の試験を含む。
- [ ] それぞれの『テストを設計した』/『実際に実行した』を区別した。
- [ ] 全体`acceptance-scenarios.md`のSC-XXと対応づけた。
- [ ] PRに未解決のP0 blockerがあればDraft維持。

## Parallel merge and lock
- [ ] 変更が担当パスに限定されている。
- [ ] 他チーム所有の契約変更は提案にとどめ、暗黙に追加していない。
- [ ] レビュアーが契約整合、引用、設計反証を確認。
- [ ] Cross-team proposalを所有者が合意または拒否して理由を残した。
- [ ] 最新mainに対する競合・参照リンクを確認。
- [ ] PRがReviewedでも実装・自動化の動作確認済みとは扱わない。

## Completion signal
- `Draft`: 著者が提案した仕様
- `Reviewed`: 実装責任者/独立レビュアーが欠陥と根拠をチェック
- `Locked`: 共通契約と矛盾せず、Codexに実装を指示できる
- `Implemented`: 実際にコードが実装された（別repo）
- `Verified`: 対象の自動試験・運用試験が合格
