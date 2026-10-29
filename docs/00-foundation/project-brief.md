# PROJECT BRIEF — 作りたいエージェントの完全な共有文脈

> 本書は**すべてのAIチャットが必読**する唯一の製品意図説明。2026-10-30時点のユーザーとの合意を整理。設計案の「確定」「暫定」「未決」を区別する。仕様本文の正本は各担当MDとADRであり、本書は要求の出発点。

## 1. 何を開発するのか
自分のUbuntuサーバー上でDockerによりセルフホストする**完全汎用パーソナルAIエージェント（Agent Harness）**。人間が「何を達成したいか」を自然言語で伝えると、エージェントが調査計画を立て、Web検索、公式API、HTTP取得、ブラウザ操作、スクレイピング、情報抽出、計算、裏取り、比較、レポート作成を実行する。ユーザーが望む場合、定期調査・条件監視・変化検出・通知まで一貫して引き受ける。途中から条件変更や作業停止・再開ができる。

**主役はAgent Runtimeであり、Scrapling/Jev/Playwright等は交換可能なツール。** スクレイピング専用でもEC物販専用でもコーディングAgentでもない。新しい用途やデータソースに対応できるよう、ツール発見・アダプター・MCP・Skills・記憶を独立させる。

## 2. 開発の発端・絶対に誤解してはいけないこと
従来のHermes Agentの良さに惹かれたが、Hermesのフォーク/改造ではなく、Hermes以外のOpenClaw、LangGraph、Deep Agents、Letta、Mastra、PydanticAI、OpenAI Agents SDK、Microsoft Agent Framework、OpenHands、Agent Zero、Browser Use、Skyvern、pi等から有用な**仕組み**を選び取る独立プロジェクトを作る。実装やライセンスが異なるコードを安易にコピーしない。常に一次資料・実コード・テストを確認する。ユーザーは「とにかく長い文書」より、後日Codexが矛盾なく実装できる徹底した確定仕様を求める。

## 3. 主要ユースケース（固定ワークフローに限定しない）
### UC-01：EC商品リサーチ / 期待利益算定
入力例：「Amazonで仕入れて買取店に売ると利益が出そうな商品を探して」「利益3,000円以上、現実的に買えて売れる候補を比較」。検索、商品候補発掘、正確な型番/JAN/ASIN/容量/色/新品中古の照合、在庫、実質購入額、限定クーポン、送料、税、買取上限、必要な付属品、価格の有効時点、査定減額リスクを検証する。**表面上の価格差＝利益とはみなさない**。確実な事実・条件付き価格・推計を分ける。情報源と日時を示す。商品の購入/出品は自律確定しない。

### UC-02：買取価格等の継続監視
「この商品の買取価格が45,000円を超えたらTelegramへ」「値下がりしたら知らせて」。条件・監視先・周期・TZ・通知先を保存し、サーバー再起動後も続く。最短周期や対象サイト規約を守る。取得失敗を0円と誤認せず、複数観測の整合を取る。同じ条件の重複通知を防ぐ。通知で実測価格・出典URL・確認日時を示す。定期チェックは可能な限りAI不要の決定的処理にする。

### UC-03：X・SNS・Webから情報を集める
「Xで過去24時間の転売/せどり関連の有益な情報をまとめて」「セール、買取キャンペーン、値上がりの兆候をまとめて」。実投稿の取得可否、時間範囲、重複・引用・転載、元URL、真偽/公式発表、キャンペーン期限、情報鮮度を扱う。SNS投稿だけで事実確定しない。API利用資格・費用・制限を検証する。

### UC-04：ホテルの条件付き横断調査
「○月○日から1泊、3人、1.5万円以下、駐車場あり。できるだけ良いホテルを探して」。対象地域、日付、人数/部屋、車、税込総額、現地費用、空室の確認時刻、無料キャンセル、予約ページ、宿泊料金の変動を明示。予約を**確定**する操作は明示的なユーザー承認が必要。比較のために複数検索サイト/ブラウザ/APIを選択。

### UC-05：未想定の一般Web調査
「この商品が安い店舗を比較」「あるトピックの最近の動きを説明」「大量のページを見て比較表を作成」「会員サイト内をログインして調査」。固定のドメイン専用Workflowがなくても、道具の選択、検索、抽出、分析、出典保存と検証ができる。

## 4. ユーザー体験 — チャットから作業完了まで
- Webチャット中心。自然言語の指示を受付、調査ステップ・使用ツール・現在ステータス・進捗をストリーミング表示。根拠付き結果、表、リンク、日時、費用を確認できる。
- 長い調査を途中で放置してもバックグラウンドで進む。Pause / Resume / Cancel、Steer（次の安全な境界で条件変更）、Queueを扱う。
- ログインや手動認証が必要な場面では、隔離ブラウザへのHuman Takeover / Handoffを用意。正規の認証を自動突破できるとは主張しない。
- Telegramは双方向指示・状態参照・通知先として優先的に追加するが、Webと別個の真実の状態を持たせない。
- 価格等を監視する場合は、「監視を登録しました」の文章だけではなくWatch ID、次回実行、保存された条件、通知権限を確認できる。

## 5. Agentの能力と自律性
- 1つのメインエージェントを基本にする。複数の独立した調査が必要な場合のみサブエージェント/バッチ実行を使う。無意味なAI同士の往復は避ける。
- Search/API/HTTPで済むことはブラウザを開かない。静的抽出、JSブラウザ、DOM/視覚操作を状況に応じて自動選択。抽出と計算は決定的なコード処理を優先。Jevは「ブラウザ操作判断器」、Scraplingは「データ取得/抽出基盤」、Lightpandaは「互換性検証が必要な軽量ブラウザ」、Chromiumは基準の互換実装。
- 長いHTML・ログ・ページの全量を毎回LLMへ送らず、必要部分だけ選別し、原本はArtifact Storeへ。コンテキスト圧縮でユーザー制約や未解決事実を落とさない。
- 過去の作業から役立つMemoryやSkillを作れる構想。AIが自分の実行権限を増やしたりRuntimeコードを無審査で書き換えたりしない。Skillの学習・レビュー・有効化・ロールバックを分離。
- 目的達成条件・予算・期限・状態を明示し、成功・部分成功・失敗・承認待ち・取得不能を区別する。

## 6. 24時間動作と信頼性
- Task/Run/Conversation/BrowserSession/Watchを別概念として永続化。LLM一時実行だけに責任を負わせない。
- Toolを呼び出す前に実行意図を記録し、実行後に結果と出典を保存。Worker/Browserの再起動後に再開できることを目標とする。ただし非冪等な外部操作の「exactly-once実行」は一般には保証できないため、idempotency key、結果照会、疑義状態、承認による安全な回復が必要。
- 過去の閲覧データ・価格観測を再利用できるが、最新情報と古い情報を明確に区別する。価格や空室のリアルタイム性を捏造しない。
- 承認待ち、期限切れ、予算超過は明示的なタスク状態・ユーザーアクションを持つ。

## 7. セキュリティ・非目標【確定】
- ホストOSに対する任意Shell/SSH、任意ファイル編集、Dockerソケット、privilegedコンテナ、無制限の内部LANアクセスをAgentへ渡さない。ユーザーは**サーバー操作AgentではなくWeb調査エージェント**を求めている。
- 検索結果・SNS投稿・HTMLからの指示は信頼できないデータ。LLMやTool Gatewayの権限を変更できない。
- 購入・予約確定・投稿・送金・許可外連絡などの副作用は明示的承認。資格情報・Cookie・アクセストークンをAIコンテキストに流さず、管理者だけが限定権限で管理。
- タスク/Tool実行に上限。SSRF、DNS Rebinding、redirect、Browser/MCP egress、prompt injection、秘密情報漏えい、外部コード実行、複数ユーザー間漏洩を想定。
- 権限なきサイトアクセスやボット対策の突破を保証・推奨しない。サイト利用規約/API資格/レート制限に適合する取得方法を優先。

## 8. 非機能要件・品質の優先順位【設計目標】
1. 正確な事実、リンク/時刻付きEvidence、ユーザー制約の順守
2. セキュリティ、権限、ユーザー承認、個人情報保護
3. タスク完遂、キャンセル/再開、監視の信頼性
4. 速度・余計なLLM往復/ブラウザ起動を削減
5. API使用料・ハードウェア資源の最適化
6. セルフホスト運用・拡張・可観測性

ベンチマークの成功率・レイテンシー/コストの**具体目標数値は測定条件を設計してから**確定する。上記は優先順位であって現物の実測保証ではない。

## 9. 技術方針：合意された方向 / 未確定の候補
- 【方向】TypeScriptモノレポ＋Python Sidecar。Next.js UI / Fastify API / Worker / Browser Worker / PostgreSQL / Docker Composeは候補。
- 【候補】Vercel AI SDK（特定メジャー/関数名は最新版と導入時点を検証）、pg-boss等の永続ジョブ、Playwright/Chromium、Scrapling、MCP、Keepa、X検索API。
- 【実験】Jev連携（既存Playwright Pageの利用と隔離）、Lightpanda（CDPと対象サイトの互換性）。公式APIは資格・費用・規約確認後に採否。
- 【未確定】User/tenantモデル、ブラウザ保存方式、Key Store実装、監視周期上限、事前承認の厳密分類、導入SDKのバージョン、実際の価格/空室API提供可否。
- 【絶対条件】ライブラリの導入可否・関数・パフォーマンスは「今の実装」を調べ、決定と検証事実を分けて書く。

## 10. 各AIの成果物に求める具体性
担当MDが完成していれば、別のCodex/実装AIが実装可能なインターフェース、責務境界、状態遷移、疑似コード、DB/API/イベント変更、例外・race・障害復帰、セキュリティ、実装順序、具体的テストの合格条件を理解できる。**実装仕様であり単なる技術紹介ではない。**
実証していない事柄は未検証と記す。「完璧」「間違いなし」「成功率○%」を根拠なく断定しない。

## 11. 書類の優先順位
1. ユーザーの明示した要求（本書に忠実に反映）
2. セキュリティ/許可境界と、レビュー承認済みADR
3. LockedなShared Contracts
4. 個別のReviewed/Lockedな機能仕様
5. Draft/Provisionalな機能仕様と本書の技術候補
6. 既存OSSの仕様・README、過去のマスターv0.2（参考資料のみ）

**衝突時は勝手に片方を修正しない。Cross-team proposal → ADR/契約所有者 → レビュー → 仕様改訂を行う。**

## 12. User-approved expansion requests (2026-10-30)
The user wants proactive opportunity discovery beyond monitoring fixed SKUs; cross-site product/hotel/entity matching; **event-driven tasks triggered by a named X account posting**, Gmail sale mail or other approved external changes; validated replay of prior successful browser actions; meaningful Web page diff; X/price/buyback trend fusion; risk-adjusted deterministic profit analysis; PDF/image/Excel ingestion; importance-ranked, deduplicated notifications; user feedback learning; and source reliability profiles. See `feature-expansion-2026-10.md` for the 11 assigned spec modules and boundaries. These are **approved items for detailed design**, not declarations that the implementation has already been completed. General-purpose functionality remains the objective.
