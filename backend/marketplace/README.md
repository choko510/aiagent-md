# Marketplace MKT-1 — 検索・閲覧（開発用）

> **本番未対応 / Read-only**。Skills・MCPのインストール、認証、MCP Tool実行、削除、更新は実装していません。

これは [Extension Marketplace仕様](../../docs/08-interfaces/extension-marketplace.md) のMKT-1だけを実装する独立パッケージです。
PR #82は`main`にマージ済みですが、MKT-1は**まだ既存Backendの認証API・Sentinelとは未統合**です。`backend/packages/*` を変更せず `backend/marketplace/` に隔離しています。

## ローカル起動

Python 3.12 / 3.13、Node.js 22以上。依存をインストールして次を実行:

```bash
uv sync --project backend/marketplace --locked --extra test
export AIAGENT_MARKETPLACE_DEV_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
AIAGENT_MARKETPLACE_DEV_MODE=1 uv run --project backend/marketplace --locked --extra test \
  uvicorn aiagent_marketplace.app:app --host 127.0.0.1 --port 8001
# 別のターミナルで
cd web
npm ci --no-audit --no-fund
npm run dev
```

Windows PowerShell:

```powershell
uv sync --project backend/marketplace --locked --extra test
$env:AIAGENT_MARKETPLACE_DEV_MODE="1"
$env:AIAGENT_MARKETPLACE_DEV_TOKEN=[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
uv run --project backend/marketplace --locked --extra test uvicorn aiagent_marketplace.app:app --host 127.0.0.1 --port 8001
```

ブラウザは Vite が表示するローカルURL（通常 `http://127.0.0.1:5173`）。**Viteを起動する別ターミナルにも、同じ `AIAGENT_MARKETPLACE_DEV_TOKEN` を設定**してください（トークンを共有しないとViteは起動を拒否）。ViteはTokenをブラウザへ送らず、ローカルAPIへのProxyでだけAuthorizationヘッダを付けます。

開発用APIは環境変数だけでなく、実際のTCP接続元アドレスがloopback、HTTP Hostがlocalhost/127.0.0.1/::1、Bearer Tokenが一致することを**サーバー側で検証**します。誤って `--host 0.0.0.0` で起動しても外部IPからのアクセスは403です。外部Origin/クロスサイトFetchも拒否します。**Vite/バックエンドを外部公開しないこと**。

## API
- `GET /api/v1/marketplace/items?q=&source=all|mcp|clawhub&kind=all|MCP|SKILL&limit=20&cursor=`
- `GET /api/v1/marketplace/items/{opaqueItemId}`
- `GET /health`（開発用）

検索は Official MCP Registry の最新版 `/v0.1/servers?version=latest` とClawHubの公開Skills APIを使用。
ClawHubキーワード検索は外部APIの仕様上1ページのみ。MCPは3文字未満の検索を拒否するため、全体検索ではSkillの結果だけを出し警告します。
Cursorはフィルターと期限を署名で拘束し、プロセス再起動後には無効になります。
上流429や障害は`warnings`でソースごとに区別し、成功0件と混同しません。429の警告には上限120秒の`retryAfterSeconds`を付与し、そのソースへの再接続を指定秒数の間抑制します。UIにも再検索の目安を表示します。
外部情報は未検証の文字列として扱い、ブラウザのHTMLへ直接挿入しません。

## セキュリティ境界・後続統合

**開発用の通信クライアント** `FixedSourceHTTP` は2つの固定HTTPSホストだけを参照し、リダイレクト、無制限応答、任意URLを拒否します。ただし、これはSentinel・強制Egressの代用品ではありません。環境変数 `AIAGENT_MARKETPLACE_DEV_MODE=1` がなければ起動不能で、開発サーバーを外部IPへバインドしてはなりません。

本番用は既に`main`へ統合されたPR #82の認証・通信契約を確認してから、`create_router(service, authenticate=...)` を既存認証済みAPIへマウントし、`JSONTransport` を **Sentinelの許可を実送信で強制する専用Transport** と入れ替えます。
認証Dependencyは必須で、モデルやTool Gatewayへ第三者のメタデータを特権情報として渡しません。
PR #82のTrusted EgressはTool用CONNECT禁止なので、単にこのHTTPクライアントを本番で有効化してはなりません。

```bash
uv run --project backend/marketplace --locked --extra test python -m pytest backend/marketplace/tests -q
cd web && npm run build
```

CIはMock Transportでの構造・正常/異常試験とUIの型/ビルドだけを検証し、実Remote APIやVMでの成功を主張しません。
