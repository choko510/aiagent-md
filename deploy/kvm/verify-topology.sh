#!/usr/bin/env bash
# Run INSIDE the Trusted Control VM (as root). Verifies the boundary for real and only then writes the attestation that
# lets production services start (aiagent_core.config.require_isolation). Any failed check -> no attestation.
set -uo pipefail
OUT=${OUT:-/etc/aiagent/isolation.json}
fail() { echo "FAIL: $*" >&2; rm -f "$OUT"; exit 1; }

[ "$(systemd-detect-virt)" = "kvm" ] || fail "not running inside a KVM guest"
nft list chain inet aiagent output 2>/dev/null | grep -q "policy drop" || fail "egress default-deny not loaded"
if docker ps -q | xargs -r docker inspect --format '{{range .Mounts}}{{.Source}} {{end}}' | grep -q docker.sock; then
  fail "a container mounts the docker socket"
fi
if su -s /bin/sh nobody -c "timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'" 2>/dev/null; then
  fail "an unprivileged process in this VM can open Internet sockets"
fi
python3 "$(dirname "$0")/vsock_probe.py" 3 7000 || fail "execution VM not reachable over vsock CID 3, or it answered unauthenticated"

mkdir -p "$(dirname "$OUT")"
printf '{"trusted_control_vm": true, "untrusted_execution_vm_separate_kernel": true, "egress_only_via_sentinel": true, "no_docker_socket": true, "verified_at": "%s", "verifier": "deploy/kvm/verify-topology.sh"}\n' \
  "$(date -u +%FT%TZ)" > "$OUT"
chmod 0444 "$OUT"
echo "attestation written to $OUT"
