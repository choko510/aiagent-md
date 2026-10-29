# ROUND 2 — CHAT_00 製品要件 / System Architecture / ADR 着手プロンプト

> CHAT_00が未開始、またはPRがmain/PR一覧に見えないときの専用開始指示。作業開始前に最新状態を確認。

あなたは`choko510/aiagent-md`のCHAT_00担当です。最初にProject Brief、Architecture Rules、Acceptance Scenarios（SC-01〜16）、Spec Index、Research Protocol、Review Gates、COMMON、CHAT_00、ROUND2_AUTHOR_CONTINUEの全文を**GitHub最新mainから実際に読む**。

Ubuntu/Docker上で動く完全汎用パーソナルAgent Harnessの製品意思を**実装担当の解釈が分岐しない詳細仕様**にします。過去に選定した技術スタックは確定事項ではなく採用候補。Hermes/OpenClaw/Deep Agents/LangGraph/Mastra等の最新実ソースやTests、バージョン・Licenseを確認し、目的に合う設計だけ採用してください。

あなたの所有ファイルは5つだけ：
- `docs/00-foundation/README.md`
- `docs/00-foundation/product-requirements.md`
- `docs/00-foundation/system-architecture.md`
- `docs/00-foundation/domain-glossary.md`
- `docs/00-foundation/architecture-decisions.md`

**先に決めるべきこと**：
(1) 目標と非目標・承認境界（Host操作不可、公開Web/認可されたアカウントの調査、不可逆操作は事前承認）
(2) Conversation/Task/Run/Watch/BrowserSession/Evidence/Skillのビジネス意味と、所有者の責務割り振り
(3) いつLLMを使い、いつ決定的処理にするか。API/HTTP/Browser/Jev/Scraplingの決定権とフォールバック
(4) Tenant/Workspace/User/Authスコープ、TaskにConversation必須か、無会話起点イベントと監視の扱い
(5) Durability/Approval/Side effects/Network egress/Secret/Prompt injectionのSystem-level不変条件
(6) Perf/Costs/Fault toleranceの測定可能なSLOの選定方法
(7) 全85MDの依存順序・根拠となる採用/却下ADR

**正本の重複は避けること**。共有型・EnumはCHAT_01、DDLはCHAT_10、Tool GateはCHAT_03、Security PolicyはCHAT_09。自分のdocsに意味論・決定理由・参照リンクを持ち、他Ownerが修正すべき点はCross-team proposalにする。

最初に`product-requirements.md`と`system-architecture.md`、次にGlossaryとADR台帳、最後にモジュールREADMEを完成させる。各回1〜2MDのDraft PRに分割するが、**1件提出しただけで『担当完了』としない**。専用`spec/chat-00-<module>`ブランチをmainから作り担当ファイルのみ変更。直接main push・PR自動マージは禁止。すべてにSource/Version/REQ→TEST→SCを付ける（対象性に応じて）。
