#!/usr/bin/env python3
"""Read-only dynamic KVM 2-VM host verification probe.

Probes live `/dev/kvm`, nftables egress tables, libvirt 2-VM domain separation
(`aiagent-control` vs `aiagent-exec`), and optional AF_VSOCK control endpoint
reachability without modifying VMs, firewall rules, or forging attestation.
"""

from __future__ import annotations

import argparse
import importlib
import json
import socket
import sys
from pathlib import Path
from typing import Any

if "hv_acceptance_dep" in sys.modules:  # pragma: no cover - isolated pytest loader
    acceptance: Any = sys.modules["hv_acceptance_dep"]
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    acceptance = importlib.import_module("acceptance")


def check_nftables_ruleset(raw_json: str) -> dict[str, Any]:
    """Verify read-only nftables JSON ruleset contains an aiagent egress chain."""
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        return {"readable": False, "has_aiagent_egress_chain": False}
    entries = parsed.get("nftables") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return {"readable": False, "has_aiagent_egress_chain": False}
    tables: set[str] = set()
    chains: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("table"), dict):
            name = item["table"].get("name")
            if isinstance(name, str):
                tables.add(name)
        if isinstance(item.get("chain"), dict):
            table = item["chain"].get("table")
            chain = item["chain"].get("name")
            if isinstance(table, str) and isinstance(chain, str):
                chains.add(f"{table}:{chain}")
    has_aiagent = any("aiagent" in t.lower() or "sentinel" in t.lower() for t in tables) or any(
        "aiagent" in c.lower() or "egress" in c.lower() for c in chains
    )
    return {
        "readable": True,
        "table_count": len(tables),
        "chain_count": len(chains),
        "has_aiagent_egress_chain": has_aiagent,
    }


def probe_vsock_endpoint(cid: int, port: int, timeout: float = 2.0) -> dict[str, Any]:
    """Bounded AF_VSOCK handshake check when guests are explicitly probed."""
    af_vsock = getattr(socket, "AF_VSOCK", None)
    if af_vsock is None:
        return {"probed": False, "reachable": False, "reason": "AF_VSOCK_UNAVAILABLE"}
    if cid <= 2 or port <= 0 or port > 65535:
        return {"probed": False, "reachable": False, "reason": "INVALID_VSOCK_TARGET"}
    try:
        sock = socket.socket(af_vsock, socket.SOCK_STREAM)
    except OSError as exc:
        return {"probed": True, "reachable": False, "reason": type(exc).__name__}
    try:
        sock.settimeout(timeout)
        sock.connect((cid, port))
        return {"probed": True, "reachable": True, "cid": cid, "port": port}
    except OSError as exc:
        return {"probed": True, "reachable": False, "cid": cid, "port": port, "reason": type(exc).__name__}
    finally:
        sock.close()


def dynamic_probe(probe_vsock: bool = False, vsock_cid: int = 2000, vsock_port: int = 9001) -> dict[str, Any]:
    """Run read-only dynamic KVM 2-VM probe; fail closed unless all live prerequisites pass."""
    caps = acceptance.capability_report()
    topology = acceptance.topology_report()
    nft_ok, nft_raw = acceptance.run_readonly("nft", "-j", "list", "ruleset")
    nft_state = check_nftables_ruleset(nft_raw) if nft_ok else {"readable": False, "has_aiagent_egress_chain": False}

    cap_checks = caps.get("checks") if isinstance(caps.get("checks"), dict) else {}
    topo_checks = topology.get("checks") if isinstance(topology.get("checks"), dict) else {}

    checks: dict[str, bool] = {
        "linux_host": bool(cap_checks.get("linux_host")),
        "kvm_api": bool(cap_checks.get("kvm_api_open_and_version")),
        "qemu_binary": bool(cap_checks.get("qemu_binary")),
        "libvirt_access": bool(cap_checks.get("libvirt_readonly_access")),
        "topology_pass": topology.get("status") == "TOPOLOGY_READONLY_PASS",
        "execution_has_no_nic": bool(topo_checks.get("execution_has_no_nic")),
        "nftables_egress_chain": bool(nft_state.get("has_aiagent_egress_chain")),
        "vsock_supported": bool(cap_checks.get("vsock_socket_api")),
    }
    vsock_detail: dict[str, Any] = {"probed": False, "skipped": not probe_vsock}
    if probe_vsock:
        vsock_detail = probe_vsock_endpoint(vsock_cid, vsock_port)
        checks["vsock_control_reachable"] = bool(vsock_detail.get("reachable"))

    missing = [name for name, passed in checks.items() if not passed]
    return {
        "kind": "dynamic_kvm_probe",
        "hostId": caps.get("hostId"),
        "recordedAt": acceptance.now_iso(),
        "status": "DYNAMIC_PROBE_PASS" if not missing else "BLOCKED_HOST_UNAVAILABLE",
        "checks": checks,
        "missingPrerequisites": missing,
        "capabilities": cap_checks,
        "topologyStatus": topology.get("status"),
        "topologyDigest": topology.get("topologyDigest"),
        "nftables": nft_state,
        "vsock": vsock_detail,
        "note": "Read-only probe never boots VMs, mutates firewall rules, or substitutes for operator attestation.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-vsock", action="store_true", help="Attempt bounded AF_VSOCK connect test")
    parser.add_argument("--cid", type=int, default=2000)
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args(argv)
    report = dynamic_probe(probe_vsock=args.probe_vsock, vsock_cid=args.cid, vsock_port=args.port)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "DYNAMIC_PROBE_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
