# ROUND2_EXPANSION_AUTHOR — CHAT_12〜15 の追加機能仕様を全件完成させる共通指示

> 適用範囲：CHAT_12（5MD）、CHAT_13（3MD）、CHAT_14（2MD）、CHAT_15（1MD）。2026-10-30。
> 本ファイルは、個別の `prompts/CHAT_XX.md` と `prompts/ROUND2_AUTHOR_CONTINUE.md` を補強する。矛盾する場合、より安全な解釈を採り、契約Ownerへ提案して確定させる。

## 目的と停止条件
当初の最優先1ファイル提出で停止してはいけない。**担当全11新規MDを4チャットへ分担して作成し、担当内全ファイルをDraft PRへ提出することが執筆作業のゴール**。
単独MDの完成＝全体仕様のLockedではない。すべてのPRは人間のレビューを経て確定。実行中にツール/時間/コンテキストの限界に達する場合は、完成と未完成を区別した台帳・PR URL・次の開始指示を残して終了し、バックグラウンド処理を約束しない。

## 必読コンテキスト
`README.md`, `AGENTS.md`, `prompts/COMMON.md`, 自分の`prompts/CHAT_XX.md`, `prompts/ROUND2_AUTHOR_CONTINUE.md`, `prompts/ROUND2_CONTRACT_RECONCILIATION.md`, `docs/00-foundation/project-brief.md`, `architecture-rules.md`, `acceptance-scenarios.md`, `feature-expansion-2026-10.md`, `spec-index.md`, `coordination.md`, `research-protocol.md`, `review-gates.md`, `templates/MODULE_SPEC.md`を最新mainから全文取得。
さらに、参照する**既存のDraft PRのheadブランチの仕様MD全文**を読む。mainには未マージ仕様が存在しないことがあるため、mainだけで存在/未存在を判断しない。

## 各担当の所有ファイル数
- CHAT_12: `docs/12-intelligence/` 内の5MD。
- CHAT_13: `docs/13-event-ingestion/` 内の3MD。
- CHAT_14: `docs/14-adaptive-experience/` 内の2MD。
- CHAT_15: `docs/15-multimodal/` 内の1MD。

## Start-of-run inventory
`SpecID | path | main存在 | PR#/head | Draft/修正要/未着手 | 共通Owner依存 | source code調査の根拠 | 次の作業`の表を作る。初回のPRがまだないなら全件未着手。初回の1件がPR済でも残りを続ける。並行作業者の先行PR/Branchがあれば編集前に詳細確認し、二重PRを回避する。

## 設計の厳密さ（全11MD）
- Scope, user story, negative case, authority, provider資格, output provenance, data freshness, versioned schemaとバリデーション。
- 別案と実装ソース比較、license, API利用料金, quota/latency の現行一次資料を確認。
- TypeScript/JSON例、処理順、状態/シーケンス、疑似コード、failure matrix、同時実行、キャンセル、再起動、重複配信、証拠の更新と破棄。
- LLMへWeb/X/外部ファイルの命令を昇格させない。外部サイトへのアクセス権は現在のCredential/Policy経由のみ。AgentのホストShell、Docker socket、購入/予約/投稿の無断実行は禁止。
- AI出力や確率スコアは確定的な価格/在庫/本人同一性の証明ではない。欠測は0とみなさない。ソースの観測時刻とイベント作成時刻を混同しない。
- 規範的なREQ-ID（MUST/SHOULD/LATER）→TEST-ID（setup/action/expected）→SC-10〜16、必要ならSC-01〜09に対応。
- DB DDL (CHAT_10), Event ID/Schema (CHAT_01), Tool/Approval (CHAT_03), Browser owner (CHAT_04), Connectors (CHAT_05), Memory/Skills (CHAT_06), Watch/Outbox (CHAT_07), REST/UI (CHAT_08), Security (CHAT_09), Test infrastructure (CHAT_11)の**正本を再定義しない**。共通型や新規フィールドは`[PROVISIONAL: CHAT_XX]`と提案する。

## 具体的な見落とし対策
- Event起点の「Xの特定アカウントが投稿」：投稿者ID、handle変更、原ポスト/返信/引用/リポストの扱い、since_id/stream cursor、配送遅延、削除済み投稿、権限/費用、イベント重複・順不同、運用停止、失効。
- 自律発掘：空間探索 vs 定番監視、観測件数/レート/LLM予算、候補発見に偏りがある場合の説明、商品同一性と偽の利益評価。
- Learned Browser Actions：成功条件を検証してから保存/再利用。URL/DOM/ユーザー権限/セッション状態が異なると適用不可。副作用は毎回Policy確認。
- Multimodal：入力未信頼、ファイルサイズ・展開サイズ/ページ数/解凍率の制限、PDF/表/画像の出典座標、悪性ファイル・埋め込みスクリプト・式の不実行、機密保護。

## PR提出のループ
1. 次の未提出MDを選ぶ → 2. 最新OSS実ソース/テストと関連PR/headを読んで検証 → 3. 完整なMDを書く → 4. Review Gates → 5. `spec/chat-XX-<module>`の専用branchに担当MDのみ追加 → 6. **Draft PR (1〜2MD/PR)** → 7. 同じチャット内の次MDへ。
他Ownerのファイルに書き込まない。既存PRに修正が必要なら**自分の既存PR branch**のみ修正する。最新mainから新ブランチを起こすときには未マージPRの内容を勝手にmain扱いしない。main直Push・auto-mergeは禁止。

## Final report
担当総数、提出済、未完了、PR URL一覧、重大P0/P1、仮説/未検証、他CHATへの契約提案と修正待ち、次の担当MDを表で報告。**担当全ファイルが提出されたときだけ「執筆一巡完了」と言う**。Locked/Implemented/Verifiedは自己宣言不可。
