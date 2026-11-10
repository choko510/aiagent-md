#!/usr/bin/env python3
"""AI-05 read-only host/KVM acceptance probe and evidence gate.

No VM, firewall, queue, or database is modified. CI mocks never count as HOST_VERIFIED.
The operator must independently review evidence before using any production gate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

KVM_API_VERSION = 0xAE00
EXPECTED_API_VERSION = 12
BASE = ("HV-U01", "HV-U02", "JQ-SMOKE")
CI = ("HV-CI",)
HOST = (
    "KVM-CELL", "KVM-NETWORK", "KVM-SENTINEL", "KVM-RESTART",
    "KVM-FENCE", "KVM-FS", "KVM-VSOCK", "KVM-ATTESTATION",
)
PROD = ("JQ-T12-1M", "JQ-T14", "JQ-T16", "REC-PITR", "REC-EFFECT-QUARANTINE", "OPERATOR-SIGNOFF")
MAX_EVIDENCE_BYTES = 1_000_000


def host_id() -> str:
    # Pseudonymous; do not print hostname, machine-id, username, MAC, credentials, etc.
    try:
        identity = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except OSError:
        identity = platform.node()
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def run_readonly(*args: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return p.returncode == 0, p.stdout[:MAX_EVIDENCE_BYTES] if p.returncode == 0 else ""


def kvm_api_available() -> bool:
    path = Path("/dev/kvm")
    try:
        if not stat.S_ISCHR(path.stat().st_mode):
            return False
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            return fcntl.ioctl(fd, KVM_API_VERSION, 0) == EXPECTED_API_VERSION
        finally:
            os.close(fd)
    except OSError:
        return False


def capability_report() -> dict[str, object]:
    linux = sys.platform == "linux"
    libvirt, _ = run_readonly("virsh", "--readonly", "--connect", "qemu:///system", "list", "--all", "--name")
    nft, _ = run_readonly("nft", "-j", "list", "ruleset")
    checks = {
        "linux_host": linux,
        "kvm_api_open_and_version": linux and kvm_api_available(),
        "qemu_binary": bool(shutil.which("qemu-system-x86_64")),
        "libvirt_readonly_access": libvirt,
        "vsock_socket_api": hasattr(socket, "AF_VSOCK"),
        "nftables_ruleset_readable": nft,
    }
    return {
        "kind": "host_capabilities",
        "hostId": host_id(),
        "recordedAt": now_iso(),
        "checks": checks,
        "status": "CAPABILITY_DETECTED" if all(checks.values()) else "BLOCKED_HOST_UNAVAILABLE",
        "note": "Capability detection is NOT a booted guest, network isolation, or KVM acceptance test.",
    }


def topology_report() -> dict[str, object]:
    """Read live libvirt definitions and running state; never provision/undefine/start/stop."""
    xmls: dict[str, str] = {}
    states: dict[str, str] = {}
    uuids: dict[str, str] = {}
    for name in ("aiagent-control", "aiagent-exec"):
        ok, xml = run_readonly("virsh", "--readonly", "--connect", "qemu:///system", "dumpxml", name)
        if not ok or not xml:
            return {"kind": "static_topology", "status": "BLOCKED_HOST_UNAVAILABLE",
                    "checks": {"libvirt_dumpxml": False}, "hostId": host_id(), "recordedAt": now_iso()}
        xmls[name] = xml
        state_ok, state = run_readonly("virsh", "--readonly", "--connect", "qemu:///system", "domstate", name)
        uuid_ok, uid = run_readonly("virsh", "--readonly", "--connect", "qemu:///system", "domuuid", name)
        states[name] = state.strip().lower() if state_ok else "unknown"
        uuids[name] = uid.strip() if uuid_ok else ""
    try:
        control, execution = (ET.fromstring(xmls[name]) for name in ("aiagent-control", "aiagent-exec"))
    except ET.ParseError:
        return {"kind": "static_topology", "status": "BLOCKED_HOST_UNAVAILABLE",
                "checks": {"valid_libvirt_xml": False}, "hostId": host_id(), "recordedAt": now_iso()}

    def sources(root: ET.Element) -> set[str]:
        return {s for disk in root.findall("./devices/disk") for node in disk.findall("source")
                if (s := node.get("file") or node.get("dev"))}

    def cid(root: ET.Element) -> str | None:
        node = root.find("./devices/vsock/cid")
        return node.get("address") if node is not None and node.get("auto") == "no" else None

    checks = {
        "both_kvm_domains": all(root.get("type") == "kvm" for root in (control, execution)),
        "distinct_live_guests": all(s == "running" for s in states.values())
                                and bool(uuids["aiagent-control"])
                                and bool(uuids["aiagent-exec"])
                                and uuids["aiagent-control"] != uuids["aiagent-exec"],
        "correct_domain_names": control.findtext("name") == "aiagent-control"
                                and execution.findtext("name") == "aiagent-exec",
        "execution_has_no_nic": not execution.findall("./devices/interface"),
        "no_shared_file_devices": not control.findall("./devices/filesystem")
                                  and not execution.findall("./devices/filesystem"),
        "no_host_device_passthrough": not control.findall("./devices/hostdev")
                                      and not execution.findall("./devices/hostdev"),
        "different_disk_sources": bool(sources(control)) and bool(sources(execution))
                                  and not (sources(control) & sources(execution)),
        "distinct_static_vsock_cids": cid(control) == "2000" and cid(execution) == "3",
    }
    digest = "sha256:" + hashlib.sha256(
        (xmls["aiagent-control"] + "\n" + xmls["aiagent-exec"]).encode()
    ).hexdigest()
    return {
        "kind": "static_topology",
        "hostId": host_id(),
        "recordedAt": now_iso(),
        "topologyDigest": digest,
        "checks": checks,
        "status": "TOPOLOGY_READONLY_PASS" if all(checks.values()) else "BLOCKED_HOST_UNAVAILABLE",
        "note": "Libvirt XML/state do not prove guest kernel isolation, egress, fail-closed or grant revocation.",
    }


def _valid_record(
    check: object, identity: object, digest: object, fresh_time: dt.datetime, evidence_dir: Path | None,
) -> bool:
    required = {
        "id", "status", "execution", "evidenceSha256", "evidenceFile",
        "hostId", "topologyDigest", "recordedAt",
    }
    if not isinstance(check, dict) or set(check) != required or evidence_dir is None:
        return False
    if check["status"] != "PASS" or check["execution"] not in ("CI", "REAL_HOST", "OPERATOR"):
        return False
    proof, name = check["evidenceSha256"], check["evidenceFile"]
    valid_sha = lambda x: isinstance(x, str) and len(x) == 71 and x.startswith("sha256:") and all(
        c in "0123456789abcdef" for c in x[7:]
    )
    if not valid_sha(check["hostId"]) or not valid_sha(check["topologyDigest"]):
        return False
    if not isinstance(proof, str) or len(proof) != 64 or any(c not in "0123456789abcdef" for c in proof):
        return False
    if not isinstance(name, str) or len(name) > 120 or name in (".", "..") or Path(name).name != name:
        return False
    try:
        artifact = evidence_dir / name
        if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size > MAX_EVIDENCE_BYTES:
            return False
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != proof:
            return False
        when = dt.datetime.fromisoformat(check["recordedAt"].replace("Z", "+00:00"))
        if not when.tzinfo or abs((fresh_time - when).total_seconds()) > 86400:
            return False
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    if check["execution"] in ("REAL_HOST", "OPERATOR"):
        return check["hostId"] == identity and check["topologyDigest"] == digest
    # CI artifacts come from a different machine, and may use a digest of the CI runner image.
    return True


def evaluate_evidence(
    evidence: object, live: dict[str, object] | None = None, evidence_dir: Path | None = None,
) -> dict[str, object]:
    """Evaluate checked artifacts; never promote simulated 2VM results to HOST_VERIFIED.

    sha256 verifies that a referenced artifact was not accidentally changed; it is not a
    cryptographic signature or a substitute for operator review of the test's real outcome.
    """
    if live is None:
        live = topology_report()
    identity = live.get("hostId", "")
    digest = live.get("topologyDigest", "")
    current = dt.datetime.now(dt.UTC)
    records: dict[str, dict] = {}
    duplicates: set[str] = set()
    if isinstance(evidence, dict) and isinstance(evidence.get("checks"), list):
        for item in evidence["checks"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            key = item["id"]
            if key in records or key in duplicates:
                duplicates.add(key)
                records.pop(key, None)
                continue
            if _valid_record(item, identity, digest, current, evidence_dir):
                records[key] = item

    def passed(ids: tuple[str, ...], execution: str | None = None) -> bool:
        return all(k in records and (execution is None or records[k]["execution"] == execution) for k in ids)

    code = passed(BASE, "CI")
    integrated = code and passed(CI, "CI")
    host = integrated and live.get("status") == "TOPOLOGY_READONLY_PASS" and passed(HOST, "REAL_HOST")
    prod = host and passed(PROD[:-1], "REAL_HOST") and passed(PROD[-1:], "OPERATOR")
    gates = {"CODE_READY": code, "INTEGRATED": integrated,
             "HOST_VERIFIED": host, "PRODUCTION_READY": prod}
    reached = next((g for g in reversed(tuple(gates)) if gates[g]), "NOT_RUN")
    required_execution = {**dict.fromkeys(BASE + CI, "CI"), **dict.fromkeys(HOST + PROD[:-1], "REAL_HOST"),
                          PROD[-1]: "OPERATOR"}
    pending = [k for k, mode in required_execution.items()
               if k not in records or records[k]["execution"] != mode]
    return {
        "kind": "acceptance_gate",
        "status": reached,
        "gates": gates,
        "missingOrInvalid": pending,
        "hostId": identity,
        "topologyDigest": digest,
        "recordedAt": now_iso(),
        "productionCutoverAuthorized": False,
        "note": "Operator review of raw evidence and separate cutover approval are mandatory. No auto-start.",
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="command", required=True)
    s.add_parser("capabilities")
    s.add_parser("topology")
    e = s.add_parser("evaluate")
    e.add_argument("--evidence", type=Path, required=True)
    a = p.parse_args(argv)
    if a.command == "capabilities":
        result = capability_report()
    elif a.command == "topology":
        result = topology_report()
    else:
        try:
            if a.evidence.stat().st_size > MAX_EVIDENCE_BYTES:
                raise ValueError("evidence too large")
            evidence = json.loads(a.evidence.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "NOT_RUN", "error": "invalid evidence file"}))
            return 2
        result = evaluate_evidence(evidence, evidence_dir=a.evidence.parent)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") not in ("NOT_RUN", "BLOCKED_HOST_UNAVAILABLE") else 2


if __name__ == "__main__":
    raise SystemExit(main())
