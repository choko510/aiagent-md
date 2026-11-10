#!/usr/bin/env sh
# Generate all Compose secrets into deploy/compose/secrets (mode 600). Never commit this directory.
set -eu
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
echo "secrets written to $D (add your provider key to anthropic_api_key)"
