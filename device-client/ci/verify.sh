#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v cargo >/dev/null || { echo 'cargo missing: NOT_RUN' >&2; exit 2; }
cargo fmt --all -- --check
# Linux/WSL validates the portable core and transport. Tauri's Linux build
# requires separate GTK/WebKit dependencies and is not a native GUI test.
cargo clippy -p aiagent-device-core -p aiagent-device-transport --all-targets --locked -- -D warnings
cargo test -p aiagent-device-core -p aiagent-device-transport --locked
cargo check -p aiagent-device-core -p aiagent-device-transport --locked
