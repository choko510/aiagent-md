# aiagent-md backend (STEP 3)

Python 3.13（下限互換 3.12）の `uv` workspace。共有型の正本は `contracts/domain/v1/*.schema.json`（ADR-037）で、
Pydantic v2 モデルと UI 用 TS 型は `tools/gen_contracts.py` が生成する（手書き二重定義なし、CI で差分 0 を検査）。

## パッケージとプロセス

| Package | プロセス / 役割 | DB ロール | 主な依存 |
|---|---|---|---|
| `aiagent-domain` | 純粋な契約: 生成 Pydantic、int64 wire codec、UUIDv7、Tool Registry、RPC wire 認証 | - | pydantic, jsonschema |
| `aiagent-core` | migration runner、Run 制御（claim/fence/HEAD CAS）、commands、Event+Outbox、QueuePort、Relay、Scheduler、RPC、lifecycle | relay / ops | psycopg 3, APScheduler(時刻計算のみ) |
| `aiagent-runtime` | Runtime Worker（DeepAgents 0.7.34 + LangGraph 1.2.20、AgentRuntimePort） | runtime + checkpointer | deepagents, langgraph |
| `aiagent-gateways` | Model Gateway / Tool Gateway（独立プロセス、認証付き RPC） | model_gateway / tool_gateway | httpx |
| `aiagent-sentinel` | Sentinel 認可 + Trusted Egress proxy | sentinel | - |
| `aiagent-api` | FastAPI 公開 API | api（RLS） | fastapi |
| `aiagent-sandbox` | Untrusted Execution VM 内の Execution Supervisor（DB・Secret なし） | - | aiagent-domain |

信頼境界: Runtime は Provider 鍵・受信者 Credential・CDP・shell を持たない。HEAD への assistant/tool 追記は Gateway、
Approval/Intent/Effect Ledger は Tool Gateway、egress は Sentinel のみ。各プロセスは別 DB ログイン（列単位 GRANT、
API は tenant RLS）と呼出元ごとの RPC 鍵を持つ。

## ローカル実行

```bash
cd backend
uv sync --locked
docker run -d --name aiagent-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
export AIAGENT_TEST_PG_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres
uv run pytest -q tests --ignore=tests/test_compose_isolation.py   # 各 session は使い捨て DB を作成・削除
uv run python tools/gen_contracts.py --check
uv run ruff check . && uv run ruff format --check . && uv run mypy packages/*/src
```

Linux カーネル機能（rlimit / namespace / uid drop / **Landlock による Task 単位のファイル隔離** /
**seccomp による Task 間メモリアクセスの禁止**）の Cell 試験と
Compose 隔離試験は `tests/linux/test_sandbox_cell.py`、`tests/test_compose_isolation.py` の docstring 参照
（CI では `cell-and-compose` job）。Landlock が利用できない環境では生成コードは **実行拒否**される。
Linux Cell の試験は実際の子プロセスによる別 Tenant / 別 Task / symlink / `/proc` アクセス拒否を検証するが、
Docker Cell は同一ホストカーネル上で動くため、**別 VM / KVM 分離の受入試験とは区別**する。

## Egress の制限（セキュリティ上の fail-closed）

Tool に発行した単回 Sentinel Permit は HTTP の実メソッドごとに検証する。HTTPS `CONNECT` は暗号化トンネル内部の
要求回数・URL・本文を監査できないため、**Tool 用 Permit からの CONNECT を拒否**する。モデル Provider 専用
`model_gateway` identity の HTTPS CONNECT だけを静的認証で許可し、Tool の認可と共有しない。

したがって、STEP 3 の `HttpMessageExecutor` から HTTPS の外部副作用を直接送信する構成はまだサポートしない
（拒否して安全側に倒す）。HTTPS Tool 通信を利用する前に、信頼された HTTP Connector が TLS を終端し、
実際の method / path / body / 回数を検査する仕組みを実装する必要がある。URL を HTTP に弱めて回避しないこと。

## マイグレーションと運用コマンド

```bash
AIAGENT_MIGRATE_DSN_FILE=... python -m aiagent_core.migrate up|status|check   # advisory lock、checksum 不変、1 TX/版
python -m aiagent_core.admin logins        # プロセス別 LOGIN ロール（AIAGENT_LOGIN_<ROLE>_PASSWORD_FILE）
python -m aiagent_core.admin tenant        # tenant/user/workspace と API token（表示 1 回、DB には SHA-256 のみ）
python -m aiagent_core.admin restore-bump  # PITR/failover 後: restore incarnation +1、dispatch 停止、permit 失効
python -m aiagent_core.admin adopt-runs    # 検証後: 非終端 Run を新世代へ、SENDING を UNKNOWN へ、dispatch 再開
python -m aiagent_core.admin gc --days 30  # 派生 checkpoint と provisional stream の retention
```

`down` は開発/試験専用（`AIAGENT_ALLOW_DOWN=1`）。本番は forward-only（expand→contract）。

## デプロイ（Ubuntu Server）

1. Operator Host: `deploy/kvm/provision.sh` で Trusted Control VM と Untrusted Execution VM（NIC なし、vsock のみ）を作成。
2. Execution VM: `deploy/docker/Dockerfile.cell` 相当の supervisor を `aiagent-cell.service` で起動（専用 uid 10002）。
3. Control VM: `sudo deploy/scripts/gen-secrets.sh`（secret を root:10001・0440 で作成） → `deploy/docker/build.sh` → `deploy/kvm/verify-topology.sh`
   （境界を実測して `/etc/aiagent/isolation.json` を作成）→ `docker compose -f deploy/compose/compose.yaml up -d`。
4. `AIAGENT_PROFILE=production` では attestation 欠如・inline secret・FakeModel のみ・`allow_private` を検知すると
   各プロセスが起動を拒否する（fail closed）。

`deploy/compose/compose.dev.yaml` は単一マシン開発用で、Cell は別 VM ではない。**本番と同等の安全性ではない。**
