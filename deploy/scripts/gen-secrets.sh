#!/usr/bin/env sh
# Generate all Compose secrets into deploy/compose/secrets. Never commit this directory.
#
# Compose bind-mounts file secrets with their HOST owner and mode, and the services run as uid/gid 10001 (the cell
# supervisor as root with all capabilities dropped, i.e. without CAP_DAC_OVERRIDE). On Linux the files are therefore
# handed to root:10001 with mode 0440: services read them via their group, root-owned readers as owner, nobody else.
# This needs root on Linux (`sudo sh deploy/scripts/gen-secrets.sh`). Docker Desktop (macOS/Windows) does not map
# host permissions into bind mounts, so there the files stay private to the invoking user.
set -eu
SERVICE_GID=${AIAGENT_SERVICE_GID:-10001}
if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" != "0" ]; then
  echo "gen-secrets: run as root on Linux (sudo) so secrets can be owned by root:$SERVICE_GID with mode 0440" >&2
  exit 1
fi
D="$(dirname "$0")/../compose/secrets"; mkdir -p "$D"; umask 077
if [ -f "$D/migrate_dsn" ]; then
  echo "gen-secrets: existing secret set detected; refusing to rotate DB, RPC, artifact, or tombstone keys" >&2
  echo "gen-secrets: use the operator change procedure and preserve prior artifact/HMAC keys" >&2
  exit 2
fi
valid_hex() {
  case "$1" in
    ''|*[!0-9a-f]*) return 1 ;;
  esac
  [ "${#1}" -eq 64 ]
}
r() {
  # POSIX deployments use /dev/urandom. Windows Git Bash can expose a dangling
  # /dev/urandom shim, so validate its output and fall back to OS-backed Python.
  value=""
  if [ -r /dev/urandom ]; then
    value=$(od -An -tx1 -N32 /dev/urandom 2>/dev/null | tr -d ' \n' || true)
  fi
  if ! valid_hex "$value"; then
    if command -v python >/dev/null 2>&1 && python -c 'import secrets' >/dev/null 2>&1; then
      value=$(python -c 'import secrets; print(secrets.token_hex(32))')
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import secrets' >/dev/null 2>&1; then
      value=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    else
      echo "gen-secrets: no cryptographically secure random source available" >&2
      exit 1
    fi
  fi
  if ! valid_hex "$value"; then
    echo "gen-secrets: entropy source returned invalid hex" >&2
    exit 1
  fi
  printf '%s' "$value"
}
w() { printf '%s' "$2" > "$D/$1"; }
PG=$(r); w pg_superuser_password "$PG"
w migrate_dsn "postgresql://postgres:$PG@postgres:5432/aiagent"
: > "$D/login_passwords.env"
for role in api runtime checkpointer model_gateway tool_gateway relay sentinel ops; do
  p=$(r); U=$(echo "$role" | tr a-z A-Z)
  echo "AIAGENT_LOGIN_${U}_PASSWORD=$p" >> "$D/login_passwords.env"
  w "${role}_dsn" "postgresql://aiagent_login_${role}:$p@postgres:5432/aiagent"
done
RM=$(r); RT=$(r); TS=$(r); OPS=$(r); API_MODEL=$(r)
w model_gateway_rpc_keys "runtime=$RM,api=$API_MODEL"; w runtime_model_rpc_key "$RM"; w api_model_rpc_key "$API_MODEL"
CONTEXT_KEY=$(r); w context_artifact_keys "{\"active\":\"context-1\",\"keys\":{\"context-1\":\"$CONTEXT_KEY\"}}"
w memory_tombstone_hmac_key "$(r)"
w tool_gateway_rpc_keys "runtime=$RT,ops=$OPS"; w runtime_tool_rpc_key "$RT"
w sentinel_rpc_keys "tool_gateway=$TS"; w tool_sentinel_client_key "$TS"
EM=$(r); w egress_model_token "$EM"; w egress_static_tokens "model_gateway=$EM"
w sentinel_permit_key "$(r)"; w sandbox_rpc_key "$(r)"; w sandbox_grant_key "$(r)"; w receiver_token "$(r)"
[ -f "$D/anthropic_api_key" ] || w anthropic_api_key ""
if [ "$(id -u)" = "0" ]; then
  chown 0:"$SERVICE_GID" "$D"/*
  chmod 0440 "$D"/*
  chown 0:"$SERVICE_GID" "$D"
  chmod 0755 "$D"  # traversable for the compose client; the files themselves are 0440
fi
echo "secrets written to $D (add your provider key to anthropic_api_key)"
