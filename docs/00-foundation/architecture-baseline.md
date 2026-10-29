# Architecture Baseline (initial invariants)

> 2026-10-30。ここは基礎方針であり、SDK/APIのバージョンを検証済みとするものではない。

## Product
自然言語で依頼するとAIが検索・Web操作・API・情報抽出・比較・検証を行い、証拠付きで回答。監視依頼なら期限・条件・周期を保存し、再起動を越えて通知する。スクレイピング専用・コーディング専用ではない。

## Representative Tasks
- Amazon等で実質購入額、送料、商品一致、買取価格・条件を比較し期待利益を調べる。
- 買取価格の閾値監視、取得失敗の区別、重複通知防止。
- Xの新しいせどり/セール情報を情報源・日付・重複・公式根拠つきで整理。
- 日付・人数・総予算・駐車場・キャンセル条件でホテル比較。空室は確認済みの日時を表記。
- 上記に限らない一般的なWeb調査、複数資料比較。

## Safety & Operations
Ubuntu/Dockerで常駐。Agent自身にホストOSの任意Shell/SSH/Dockerソケット権限を渡さない。購入・予約確定・投稿・送金・許可外メッセージ送信は個別承認。APIキーやCookieはLLMコンテキストに流さない。外部サイト内容をシステム命令へ昇格させない。SSRFと認証/アクセス権を考慮。CAPTCHAやCloudflareを突破できる前提にしない。

## Preliminary technology directions, not irreversible decisions
TypeScript-first、Python Scrapling sidecar、モデルProvider交換可能、1メインエージェント＋必要時のみサブエージェント、durable events/checkpoints、Tool Gateway、PostgreSQL、ブラウザはPlaywright/Chromium基準、JevとLightpandaは試験採用、Data sourceは公式API優先。フェーズごとにバージョン・ライセンス・可否を再調査しADRを確定する。

## Verification priority
正確性・出典・安全性 > 完遂率・中断復帰 > 速度 > コスト > 管理の簡単さ。AIが「完了」と述べただけではタスク成功とみなさない。
