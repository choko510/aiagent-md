#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v cargo >/dev/null || { echo 'cargo missing: NOT_RUN' >&2; exit 2; }
cargo fmt --all -- --check
cargo test -p aiagent-device-core
cargo test -p aiagent-device-desktop
cargo check -p aiagent-device-desktop
