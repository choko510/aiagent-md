#!/usr/bin/env bash
# Provision the two-VM topology on an Ubuntu Operator Host (KVM/QEMU + libvirt). Run as root on the HOST.
# NOT executed in CI (needs /dev/kvm). After both VMs boot, run verify-topology.sh INSIDE the control VM: it checks the
# boundary and only then writes /etc/aiagent/isolation.json, without which production services refuse to start.
set -euo pipefail
IMG=${IMG:-/var/lib/libvirt/images}
BASE=${BASE:-$IMG/ubuntu-24.04-server-cloudimg-amd64.img}   # verify against Ubuntu SHA256SUMS before use
HERE=$(cd "$(dirname "$0")" && pwd)

[ -e /dev/kvm ] || { echo "no /dev/kvm: hardware virtualisation unavailable" >&2; exit 2; }
for c in virsh qemu-img cloud-localds; do
  command -v "$c" >/dev/null || { echo "missing $c (apt install qemu-kvm libvirt-daemon-system cloud-image-utils)" >&2; exit 2; }
done

for vm in control exec; do
  qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$IMG/aiagent-$vm.qcow2" 40G
  cloud-localds "$IMG/aiagent-$vm-seed.iso" "$HERE/cloud-init/$vm-user-data.yaml"
done
virsh define "$HERE/trusted-control-vm.xml"
virsh define "$HERE/untrusted-execution-vm.xml"
virsh start aiagent-exec
virsh start aiagent-control
echo "Started. Next: install the cell supervisor + keys in aiagent-exec, copy deploy/ into aiagent-control,"
echo "run deploy/kvm/verify-topology.sh there, then: docker compose -f deploy/compose/compose.yaml up -d"
