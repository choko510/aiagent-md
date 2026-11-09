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
- **Muse migration 2026-11-07:** Agentの任意Shell/生成コードは**Trusted Control VMとは別ゲストカーネルのUntrusted Execution VM**のRuntime Cellでのみ許可。Agent LoopはTrusted Control VMに置くが、生成コードをその中で実行しない。ホストShell、管理API、Vault、保護DB、Browser CDP、ポリシー変更は禁止。
- Runtime Cellの全子プロセス・CLI・生成Skillsには直接外向き通信を一切与えず、限定Broker RPCごとに新しいGateway Intentを記録する。ブラウザ・Connectorなどの外向き通信も独立Sentinelが具体的宛先・方法・権限を認可し、kernel/network強制で直接通信を阻止する。Tool GatewayはIntent/Approval/Ledgerの正本、Sentinelは全egress/Connectorの最終許可、Enforced Egress Transportが送信の唯一の経路。二重承認正本は作らない。
- 本番KVM/QEMUの**Trusted Control VMとUntrusted Execution VMの2ゲスト分離**（保護DB/Vaultは別ゲストカーネル）を第一候補とし、VMなしコンテナ開発モードを本番と同等に扱わない。実装時に実測と攻撃テストを要求。
- 新正本案: 02-runtime/sandbox-execution.md、09-security/runtime-cell-isolation.md、09-security/sentinel-authority.md。すべてDraft/Provisional。
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

## 2026-11-07 Adaptive Context設計原則

[Context Manager 18節](../02-runtime/context-manager-and-compaction.md) とADR-023〜028を追加参照。A0:Policy/Approval、A1:Task Goal、A2:Control/Effect Ledgerは原本参照を維持する非圧縮領域。Evidence/Raw Artifact、Working Journal、Long-term Memory、Skill/Workspaceは独立正本であり、LLMへのProjectionはどれも権限を生成しない。

各Model callの入力は (1)現在のGoal・ACL/Source Epoch/Lease検査、(2)Model固有Tokenizer/Output reserve/Limit検査、(3)権限済み候補だけを関連性/出典/時刻で選択、(4)大きな原本をArtifactに退避しGrounded Summary、(5)ManifestをDurable checkpoint、(6)dispatch前に最新版再検証という順序を原則とする。失敗時はPartial/Blockedで停止し、条件削除・架空のEvidence・権限の推定補完を禁止。Sandboxの生成Script、stdout、外部HTML、Memoryは非信頼DataとしてSentinel/Gateway境界を越えない。選択Scoreは権限でない。実装未着手・未検証。

## E. Python-first 言語・実行環境規則（2026-11-09）

[ADR-036〜040](architecture-decisions.md) と [System Architecture §13.2](system-architecture.md) に従う全担当への拘束規則。既存B節の信頼境界より**弱い解釈を許さない**。

1. **言語の割当**: Backend/API/Agent Runtime/MCP/Browser Worker/Memory/Automation/自律開発はPython。Web UI・ブラウザ側ClientはReact + TypeScript。DDL/Migration/TransactionはSQL。Rustは実測ボトルネック＋個別ADRがある箇所のみで、必須依存にしない。採用OSSの制約で他言語が不可避なものは独立Sidecar/Adapter（プロセス境界・版固定・ライセンス確認付き）として例外扱い。
2. **論理型記法**: 既存仕様の`~~~ts`/`typescript`ブロック、`interface`、`type X = "A"|"B"`は**言語非依存の論理契約記法**として読む。実装はcanonical JSON Schema（CHAT_01）→Pydantic v2 model（Python）/生成TS型（UI）。同じ共有型を手書きで二重定義しない（ADR-037）。
3. **整数・金額**: 「BigInt」「bigint」は“無損失の任意精度整数”を意味する。wire（JSON）では従来どおりsigned int64の**正規十進文字列**、Python内部は`int`、DBはBIGINT、金額はminor unit整数または`Decimal`でfloat禁止。Pydantic strictの`int`フィールドはJSON文字列を受理しないため、**strict `int`へwire値を直接流さず**、次の共通codecを入力/出力境界に置く。
   - **canonical JSON Schema**: int64項目は`{"type":"string","pattern":"^(0|-?[1-9][0-9]{0,18})$"}`（＋項目ごとの`x-int64-min/max`、非負なら`-`不可）。`"-0"`は拒否。JSON numberは型不一致として拒否。
   - **入力parser（共通の1実装）**: 型がstrでなければ拒否（JSON number・bool・null・float）→正規表現で符号・前置ゼロ・空白・`+`・指数・全角数字を拒否→`int()`→`-2^63 ≤ v ≤ 2^63-1`と項目固有範囲を検査→内部`int`。Pydanticでは`Annotated[int, BeforeValidator(parse_int64_wire), PlainSerializer(format_int64_wire, return_type=str)]`等の**専用型**として実装し、modelの他フィールドはstrictのまま。
   - **出力serializer**: 内部`int`→範囲検査→`str(v)`（正規十進）。JSON numberとして出力しない。DBとの変換はdriverのint64↔`int`で、範囲外は保存前に拒否。
   - **round-trip fixture（必須、実装時に追加・未実行）**: `"0"`、`"1"`、`"-1"`、`"9007199254740993"`（2^53+1）、`"9223372036854775807"`、`"-9223372036854775808"`を受理しwire→`int`→wireで同一文字列。`9007199254740993`（number）、`"9223372036854775808"`、`"-9223372036854775809"`、`"01"`、`"+1"`、`"-0"`、`" 1"`、`"1e3"`、`"１"`、`""`を拒否。API DTO、DomainEvent/SSE、Checkpoint、Tool I/O、Queue payloadの全経路で同じfixtureを実行する。UI（TS）は同じfixtureで`BigInt`/string codecを検証する。
4. **プロセス分離**: Trusted Control VMのPythonプロセスとUntrusted Execution VMの生成Pythonは、interpreter・venv・site-packages・FS・UID・network namespaceを共有しない。Agent Runtimeは生成コードを`exec`/`eval`/`subprocess`/`importlib`で実行しない。Trusted側の依存はlockfile固定で、Cellからpip installされたpackageをTrusted側でimportしない。
5. **権限を持つ部品の独立**: Model Gateway、Tool Gateway、Sentinel、Credential Broker、Trusted Egress、MCP Adapter、Browser BrokerはAgent Frameworkのin-process pluginにしない。Framework（PydanticAI/LangGraph/DeepAgents/DBOS等）のTool関数は**Tool Intentを作るだけのstub**であり、外部副作用を直接実行しない。
6. **正本の非委譲**: Task/Run/Checkpoint HEAD/Goal/Approval/Effect Ledger/Memory/EvidenceはPostgreSQL上のaiagent-md契約が正本。Frameworkのcheckpoint/state/store、Queue製品のjob状態、MCP SDK session、PydanticのmodelはいずれもRead-model/派生物で、権限・成功判定・再実行可否の根拠にしない。
7. **Retryの区別**: Provider/Model retry（Model Gateway）、Queue job retry（QueuePort）、Workflow step replay（候補A/Bの機構）、Tool retry（Tool Gateway＋Effect Ledger）を別概念として扱う。Framework/Queueの自動retryで外部副作用を再送しない。UNKNOWN/MAY_HAVE_SENTは保持。
8. **Async/Cancellation**: asyncioのtask cancelは協調的であり外部送信の取消しを意味しない。Cancel/Steer/Lease失効/Approval取消し後は、Gatewayで新規dispatch fenceを再検査し古いTool実行を禁止する。Blocking SDKはthread/process poolへ隔離し、event loopを長時間占有しない。
9. **品質基盤（実装段階で固定）**: `uv sync --locked`、`ruff check`/`ruff format --check`、`mypy --strict`（または採用時`pyright`）、`pytest`（async試験はanyio/pytest-asyncio）、Playwright Python E2E。UIは`web/`配下のみでTS typecheck/lint/testを実行。lockfile・image digest・Python版（ADR-040）は実装開始時に記録し、STEP 1では実行済みと記載しない。
10. **TS残置の扱い**: 上流OSSの調査引用（Mastra、OpenClaw、MCP TypeScript SDK、pg-boss等）は**調査履歴**として保持し、採用方針と混同しない。Pythonに同等機能が無い場合は無理に移植せず、必要なアルゴリズムだけを独立Portとして仕様化する。
