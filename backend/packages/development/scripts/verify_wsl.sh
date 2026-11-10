#!/usr/bin/env bash
# User-triggered Codex CLI handoff. No host settings changed or external Git writes.
set -euo pipefail
cd "$(dirname "$0")/.."
printf 'kernel: '; uname -sr
printf 'python: '; python --version
printf 'git: '; git --version
if command -v uv >/dev/null 2>&1; then uv --version; else echo 'uv: NOT_INSTALLED (optional)'; fi
if [ -e /dev/kvm ]; then echo 'KVM_DEVICE_EXISTS (NOT VM-verified)'; else echo 'KVM_DEVICE_MISSING'; fi
bash scripts/ci_contracts.sh
