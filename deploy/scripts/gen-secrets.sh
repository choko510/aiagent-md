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
r() { od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; }
w() { printf '%s' "$2" > "$D/$1"; }
PG=$(r); w pg_superuser_password "$PG"
w migrate_dsn "postgresql://postgres:$PG@postgres:5432/aiagent"
: > "$D/login_passwords.env"
for role in api runtime checkpointer model_gateway tool_gateway relay sentinel ops; do
  p=$(r); U=$(echo "$role" | tr a-z A-Z)
  echo "AIAGENT_LOGIN_${U}_PASSWORD=$p" >> "$D/login_passwords.env"
  w "${role}_dsn" "postgresql://aiagent_login_${role}:$p@postgres:5432/aiagent"
done
RM=$(r); RT=$(r); TS=$(r); OPS=$(r)
w model_gateway_rpc_keys "runtime=$RM"; w runtime_model_rpc_key "$RM"
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
