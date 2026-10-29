# PRIOR ART STUDY MAP — 参考実装の取り入れ方

> **参考候補の索引であって、各機能が最新の各リポジトリに実装されていることの検証結果ではありません。** AI担当者が公式ドキュメントと実コードで存在・実装方法・ライセンスを確認し、採否判断を提出する。

| Source | Repository | 調べたい特徴 / 期待する学び | 主担当 |
|---|---|---|---|
| Hermes Agent | https://github.com/NousResearch/hermes-agent | Skills自己生成、Memory、チャネル、Goal/Heartbeat、Tool discovery | 00,06,07 |
| OpenClaw | https://github.com/openclaw/openclaw | Messaging Gateway、Context/Tool policy、Automation、Plugin | 00,03,07,08 |
| LangGraph | https://github.com/langchain-ai/langgraph | Checkpoint、Interrupt/HITL、状態グラフ、分岐・復帰 | 01,02 |
| Deep Agents | https://github.com/langchain-ai/deepagents | Agent planning、Context圧縮、Skill/Filesystem、Subagents | 02,06 |
| Letta | https://github.com/letta-ai/letta | 長期記憶、Memory block、記憶更新 | 06 |
| Mastra | https://github.com/mastra-ai/mastra | Agent loop、Workflow、状態、approval、tool execution | 02,03,07 |
| PydanticAI | https://github.com/pydantic/pydantic-ai | 型検証、deps、streaming、tool recovery、durable | 01,02,03 |
| OpenAI Agents SDK | https://github.com/openai/openai-agents-python | Handoff、guardrails、tracing、sessions | 01,02,03,11 |
| Microsoft Agent Framework | https://github.com/microsoft/agent-framework | Middleware、MCP、workflow/graph | 00,02,03 |
| pi | https://github.com/badlogic/pi-mono | Agent loop、session tree、extension/skills | 02,06 |
| OpenHands | https://github.com/All-Hands-AI/OpenHands | Run/event design、isolation、observability | 02,08,11 |
| Agent Zero | https://github.com/frdel/agent-zero | Human-in-loop/browser、UI、agent memory | 04,06,08 |
| Browser Use | https://github.com/browser-use/browser-use | DOM/vision操作、tab context、browser state | 04 |
| Skyvern | https://github.com/Skyvern-AI/skyvern | Visual interaction、workflow and recovery、license compliance | 04 |
| Scrapling | https://github.com/D4Vinci/Scrapling | HTTP/browser抽出、sessions、MCP/API、anti-botの制約 | 04,05 |
| jev-browser | https://github.com/jkudish/jev-browser | Browser action selection、Page injection、performance/limitations | 04 |
| Lightpanda | https://github.com/lightpanda-io/browser | Headless CDP lightweight engine、compatibility tests | 04 |
| pg-boss | https://github.com/timgit/pg-boss | Durable job, at-least-once, cron/retry/lock | 07,10 |
| Langfuse | https://github.com/langfuse/langfuse | Trace、datasets、experiments、cost/evals | 10,11 |
| Playwright | https://github.com/microsoft/playwright | Chromium Session/Context、state、browser testing | 04,11 |

## 必須の調査項目
- 該当する**実ソースファイルと関数**、テスト、タグ/commit、対応Runtime、依存/運用条件、Issue、LICENSEを挙げる。
- READMEしか見られないなら`Docs only`と明示。ソースベースの確定とは書かない。
- 具体的な採用パターン（抽象化、所有権、アルゴリズム、テスト）に落とす。名前の紹介や精神論だけで終わらせない。
- ライセンス、互換性、API契約、コスト、長期メンテの理由で不採用も可能。採用は義務ではない。
- 技術的に直接使えないものは再実装/Adapter化するが、OSSの著作権を無視したコードコピーはしない。

## Additional upstream study targets for expansion
- Stagehand (v4 and older v3 must be distinguished): https://github.com/browserbase/stagehand , https://www.stagehand.dev/ , caching docs https://www.browserbase.com/changelog/caching-configurable ; compare local self-host support vs Browserbase-managed cache.
- Composio trigger/event adapter: https://docs.composio.dev/docs/triggers and https://docs.composio.dev/reference/api-reference/webhook-subscriptions ; compare signed webhooks, delayed polling, supported provider triggers and cost.
- X official stream and timeline APIs: https://docs.x.com/x-api/posts/filtered-stream/introduction and https://docs.x.com/x-api/posts/timelines/introduction ; verify trigger entitlement, rates, pricing and actual data delivery options.
- Firecrawl monitor & diff: https://www.firecrawl.dev/monitor ; compare SaaS vs self-managed diff, cost and snapshot retention.
- Entity resolution/matching: inspect recordlinkage/Splink libraries, product EAN databases and similarity algorithms with false merge evaluation; actual support and license to be verified.
References above are candidates for source-level audit, not conclusive interoperability or benchmark claims.
