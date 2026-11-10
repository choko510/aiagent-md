# STEP 4 実機受入：Codex CLI 引継ぎ（Windows＋WSL）

この文書は実行指示であってPASSの証拠ではない。記録がない場合は `NOT_RUN`。実行不能なホスト試験は `BLOCKED_HOST_UNAVAILABLE`。GitHub ActionsのWindowsコンパイル成功と、Windows実画面・USB実機の操作成功を混同しないこと。

## 前提と境界

- 手元のホストは Windows＋WSL。管理者権限、KVM 2VM、macOS/iOS、USB端末、Docker Desktopなどの存在を仮定しない。
- Agentが作ったShell/ScriptをTrusted Control Hostで実行しない。動作検証はレビュー済み試験コードか、専用の使い捨て隔離Cellのみ。
- 実アカウントのCookie、認証トークン、SSH鍵、GitHub PAT、Provider鍵を試験へ入れない。ログやPRへDSNやSecretを貼らない。
- Windows Defender、仮想化保護などのセキュリティ機能を無効化しない。WindowsとWSLで**同じworktreeを同時編集しない**。
- Chatの成果確認は `CODE_READY`、共通実結線をE2Eで実証したものだけ `INTEGRATED`。ホスト検証は別。現在の未結線機能を強制有効化しない。

## 1. Windows（PowerShell、通常ユーザー権限）

リポジトリのWindows上の独立clone/worktreeで実施（WSL側と同時編集しない）。

```powershell
$ErrorActionPreference = 'Stop'
git status --short
git rev-parse HEAD
wsl --list --verbose
wsl --version
git --version
rustc --version
cargo --version
Set-Location device-client
cargo fmt --all -- --check
cargo test --workspace --locked
cargo check --workspace --locked
cargo build -p aiagent-device-desktop --locked
cargo run -p aiagent-device-desktop --locked
```

`cargo fmt --all`は元ファイルを書き換えるのでチェック時には `-- --check` のみを使用。Rust toolchain / MSVC / Windows SDK / WebView2が未導入なら `BLOCKED_HOST_UNAVAILABLE`として必要物を記録し、勝手に管理者権限を取得しない。

**GUI受入（実際に表示して記録）:** `UNPAIRED_OFFLINE`、`LOCAL_MOCK_ONLY`、Cloud-authorized=false、実デバイスI/O=false、ペアリング・実操作無効。Local StopとMock Revokeが操作を停止し監査表示に反映されること。再起動するとUIインメモリ状態はリセットされる（本番セッション回復の成功とはしない）。Chromeの普段使いタブを変更せず、USB/ADB、Shell/クリック実操作を試みない。画面に実操作が可能な状態が出たら直ちにFAIL。

**失敗時:** Rust target/MSVCと初発の`cargo`エラー、Windows版・WebView2版・Rust版、差分SHAを記録。秘密や個人データを含むログをそのまま共有しない。

## 2. WSL2 Ubuntu（ユーザー権限、別clone）

```bash
set -euo pipefail
git status --short
git rev-parse HEAD
python3 --version
uv --version
git --version
test -c /dev/kvm && echo KVM_DEVICE_EXISTS || echo KVM_DEVICE_MISSING
cd backend
uv sync --locked
uv run python tools/gen_contracts.py --check
uv run pytest -q tests/host_verification
uv run pytest -q packages/context/tests packages/memory/tests
uv run pytest -q packages/development/tests packages/automation/tests packages/browser/tests
uv run ruff check .
uv run ruff format --check .
```

ブラウザの実Chromium起動はFixture環境のみに限定。Playwright binaryやLinux sandbox権限がなければ `NOT_RUN` として記録。実サイトへ外部送信させない。コード開発用Git fixtureは使い捨てリポジトリのみ、実利用Git worktreeへpushしない。

PostgreSQL・Dockerが導入済みで確認できた場合に限り、Backend既存の使い捨てDB E2Eを実行する。

```bash
cd "$(git rev-parse --show-toplevel)"
docker version
docker compose version
# 下記は新たな一時コンテナを作成する。既存の同名コンテナがある場合は実行しない。
docker run --rm -d --name aiagent-step4-test-pg \
  -e POSTGRES_PASSWORD=local-test-only -p 127.0.0.1:55432:5432 postgres:16
cd backend
export AIAGENT_TEST_PG_URL=postgresql://postgres:local-test-only@127.0.0.1:55432/postgres
uv run pytest -q tests --ignore=tests/test_compose_isolation.py
# 作成したテスト専用コンテナのみ終了する。
docker stop aiagent-step4-test-pg
unset AIAGENT_TEST_PG_URL
```

既存データベース名や実運用DBへの書込みは禁止。PostgreSQLを共有している環境で`docker run`を無理に実行しない。ポート使用中なら記録して別の一時ポートの利用を検討する。

## 3. Host/Queue/Restoreの安全診断

```bash
python3 deploy/kvm/acceptance.py capabilities
python3 deploy/kvm/acceptance.py topology
python3 deploy/kvm/queue_gate.py --rows 1000
python3 deploy/kvm/restore_probe.py
```

- `acceptance.py topology`はread-only。KVM独立2VMでないWSLでは`BLOCKED_HOST_UNAVAILABLE`が正常な境界。
- `queue_gate.py`の既定はdry-run。**`--execute`、`--dedicated-host`、本番DB接続を渡さない**。
- `restore_probe.py`の既定は`NOT_RUN`。リストアした隔離DBが別途存在し、安全確認ができた場合のみ`--inspect`の実施を検討。実データの復旧・再送は本手順の対象外。
- 証拠ハッシュは改ざん・混同検査であり署名された実行証明ではない。別の独立したOperator受入を経ずに`HOST_VERIFIED`・`PRODUCTION_READY`へ昇格させない。

## 4. Codex CLI に渡すプロンプト

> 上記の手順を**現在の実機環境で実施できる範囲だけ**実行してください。実行前にOS/WSL/Docker/Rust/uv/権限を検出し、未導入の物やセキュリティ設定の変更を要求しないでください。各操作に`実行場所`、`git SHA`、`コマンド`、`期待結果`、`実結果`、`PASS/FAIL/NOT_RUN/BLOCKED_HOST_UNAVAILABLE`、`次の対処`を付記してください。テスト用DB・Git fixture以外に書込み禁止。実際のWindows GUIを起動し、Mock表示・停止・取消を視認できなかった場合は未実施扱いにしてください。KVM 2VM、本番エグレス、Cloud pairing、実ADB、暗号化済みローカルJournal、実macOS/iOSの検証は行ったと主張しないでください。

## 5. 判定テーブル（実行時に埋める）

| テスト | 実行場所 | 状態初期値 |
|---|---|---|
| Backend `uv sync --locked` / 共有Schema生成 | WSL2 | NOT_RUN |
| STEP 3 + STEP 4 unit/mock / DB E2E | WSL2 / 使い捨てDB | NOT_RUN |
| ブラウザfixture / sandbox拒否 / 戻り値redaction | WSL2 | NOT_RUN |
| Queue小規模負荷 / Crash・旧lease・UNKNOWN | WSL2（権限と一時DBがある場合） | NOT_RUN |
| Rust core + Tauri compile | Windows GitHub Actionsとは別のPC | NOT_RUN |
| Desktop Mock GUIとStop/Revoke | Windows実画面 | NOT_RUN |
| Chrome既存プロファイル保護・USB/ADB実連携 | 実機/承認必須 | BLOCKED_HOST_UNAVAILABLE |
| 独立Control/Execution KVM 2VMと本番egress | 専用Linuxホスト | BLOCKED_HOST_UNAVAILABLE |
| 外部Provider実送信/本番準備/長時間負荷 | 本番相当承認環境 | BLOCKED_HOST_UNAVAILABLE |

**証拠がそろわない項目のPASSは認めない。**
