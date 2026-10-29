# ARCHITECTURE RULES — 全担当を拘束する実装境界

> 本書は2026-10-30に並列設計のすれ違い防止のため導入。製品意図はproject-brief.mdを参照。細部に衝突があれば担当のADR/契約へ変更提案を提出する。

## A. Canonical ownership
| 正本/概念 | 正式な定義の担当 | 他担当で許されること |
|---|---|---|
| Product goal, use case, architectural ADR | CHAT_00 | 要件・採用理由への提案、参照 |
| Conversation/Task/Run/Event/State/Error/Artifact型 | CHAT_01 | 消費する契約の説明、暫定型への提案 |
| Agent step/loop, checkpoint, steering semantics | CHAT_02 | 契約参照・インターフェース提案 |
| Tool execution, side-effect ledger, approval gate | CHAT_03 | Tool adapterのみ提案、Policy判定は委譲 |
| BrowserSession, Page ownership, browser execution | CHAT_04 | Session ID契約の提案、独自権限昇格禁止 |
| External API/provider adapters | CHAT_05 | 正本のTask/Tool型を参照 |
| Memory/Skills and skill activation | CHAT_06 | Shared Event/Evidenceを再定義しない |
| Watch/Schedule/Observation/Notification semantics | CHAT_07 | Task管理・Tool利用を共有契約で参照 |
| User UI/REST/SSE/Telegram | CHAT_08 | Runtimeを直接置き換えず公開APIを参照 |
| Auth, egress, capability/secret and trust boundaries | CHAT_09 | 最小権限を守りセキュリティ要件を提案 |
| Tables, Indexes, Migrations, Jobs, deployments | CHAT_10 | 意味論の定義を別担当から輸入 |
| Tests, fixtures, benchmark and release gates | CHAT_11 | 正本を変更せずギャップを提案 |

## B. 前提となる技術構造
- 単一の中心AgentLoop＋必要時の並列化/専門Worker。Providerやモデルは変更可能。
- UI/APIはタスク永続化と同じ境界を共有するが、長時間タスクをHTTPリクエスト内に閉じない。
- LLM Tool Callは必ずTool Gateway→Schema→Policy→Budget→Approved Executorを通す。ツールアダプタが自己承認しない。
- PostgresへTool意図を記録→実行→結果のObservation/Evidence保存。再実行が外部副作用を起こす場合、状態確認や明示的承認がない限り盲目的再試行禁止。
- Browser WorkerがChromium/BrowserContext/Pageを所有。Session IDを渡すだけで、AI Workerがタブへ並列直接接続しない。Jevのような操作判断器はBrowser Adapterの機能でありSessionの所有者ではない。
- 多数の静的ページをHTTP/API/専用データProviderで処理できる場合はLLMを最小回数で使う。ツールバッチ実行は権限制限された形式のみ。
- 監視Workerは閾値比較・差分検知の決定的処理を優先し、不必要な長期LLMループを使わない。
- Evidence、Raw Artifacts、Working Context、Long-term Memory、Skillsは別リソース。古いデータをリアルタイムとして再利用しない。
- 暗黙の完了は不可。Goalの成功条件に対応するEvidence/状態を確認する。

## C. 仕様を変えた場合の必須作業
1. 更新が必要な正本オーナーと依存MDを列挙。
2. Requirements ID、契約/Schema version、変更前後の挙動、後方互換性を記載。
3. 失敗・キャンセル・再開・期限・重複配信・権限上の影響を確認。
4. Shared contractが未承認なら[PROVISIONAL: CHAT_XX]を残す。
5. 受入テスト/Benchmarksへの影響も通知する。

## D. 再現可能な評価
常に「SDK公式が提供する」→「指定版のコードで確認した」→「我々の統合で実測した」を別評価。複数ライブラリのベンチマーク条件が違う場合、数字を直接比較しない。
