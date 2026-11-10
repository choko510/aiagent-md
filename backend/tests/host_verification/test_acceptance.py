"""AI-05 pure unit/negative tests. No KVM, PostgreSQL, VM creation, networking or host modification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "deploy" / "kvm"


def module(name: str) -> ModuleType:
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location("hv_" + name, path)
    assert spec and spec.loader
    instance = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = instance
    spec.loader.exec_module(instance)
    return instance


@pytest.fixture(scope="module")
def hv() -> ModuleType:
    return module("acceptance")


@pytest.fixture(scope="module")
def queue_gate() -> ModuleType:
    return module("queue_gate")


@pytest.fixture(scope="module")
def restore() -> ModuleType:
    return module("restore_probe")


def fake_live(hv: ModuleType) -> dict:
    return {"status": "TOPOLOGY_READONLY_PASS", "hostId": "sha256:" + "a" * 64, "topologyDigest": "sha256:" + "b" * 64}


def add_evidence(
    hv: ModuleType,
    directory: Path,
    kind: str,
    *,
    execution: str = "CI",
    host: str = "sha256:" + "a" * 64,
    digest: str = "sha256:" + "b" * 64,
) -> dict:
    body = json.dumps({"test": kind, "note": "synthetic test data"}).encode()
    filename = kind + ".json"
    (directory / filename).write_bytes(body)
    return {
        "id": kind,
        "status": "PASS",
        "execution": execution,
        "evidenceSha256": hashlib.sha256(body).hexdigest(),
        "evidenceFile": filename,
        "hostId": host,
        "topologyDigest": digest,
        "recordedAt": hv.now_iso(),
    }


def test_missing_kvm_is_blocked(hv: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hv, "kvm_api_available", lambda: False)
    monkeypatch.setattr(hv, "run_readonly", lambda *args: (False, ""))
    result = hv.capability_report()
    assert result["status"] == "BLOCKED_HOST_UNAVAILABLE"
    assert result["checks"]["kvm_api_open_and_version"] is False


def test_bad_live_topology_is_blocked(hv: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hv, "run_readonly", lambda *args: (False, ""))
    result = hv.topology_report()
    assert result["status"] == "BLOCKED_HOST_UNAVAILABLE"


def test_live_xml_checked_without_changing_vms(hv: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    control = """<domain type='kvm'><name>aiagent-control</name><devices>
        <disk><source file='/tmp/control.qcow2'/></disk>
        <interface type='network'><source network='default'/></interface>
        <vsock><cid auto='no' address='2000'/></vsock></devices></domain>"""
    execution = """<domain type='kvm'><name>aiagent-exec</name><devices>
        <disk><source file='/tmp/exec.qcow2'/></disk>
        <vsock><cid auto='no' address='3'/></vsock></devices></domain>"""
    commands = []

    def runner(*args: str) -> tuple[bool, str]:
        commands.append(args)
        name = args[-1]
        if "dumpxml" in args:
            return True, control if name == "aiagent-control" else execution
        if "domstate" in args:
            return True, "running\n"
        if "domuuid" in args:
            return True, "123" if name == "aiagent-control" else "456"
        return False, ""

    monkeypatch.setattr(hv, "run_readonly", runner)
    result = hv.topology_report()
    assert result["status"] == "TOPOLOGY_READONLY_PASS"
    assert all(v for v in result["checks"].values())
    assert all("--readonly" in args for args in commands)
    assert not any({"start", "destroy", "define", "undefine"} & set(args) for args in commands)


def test_static_topology_rejects_execution_nic(hv: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """<domain type='kvm'><name>aiagent-exec</name><devices>
        <disk><source file='/tmp/exec.qcow2'/></disk>
        <interface type='network'/><vsock><cid auto='no' address='3'/></vsock></devices></domain>"""
    control = (
        xml.replace("aiagent-exec", "aiagent-control")
        .replace("/tmp/exec.qcow2", "/tmp/control.qcow2")
        .replace("address='3'", "address='2000'")
    )

    def runner(*args: str) -> tuple[bool, str]:
        n = args[-1]
        if "dumpxml" in args:
            return True, control if n == "aiagent-control" else xml
        if "domstate" in args:
            return True, "running"
        return True, "one" if n == "aiagent-control" else "two"

    monkeypatch.setattr(hv, "run_readonly", runner)
    result = hv.topology_report()
    assert result["status"] == "BLOCKED_HOST_UNAVAILABLE"
    assert result["checks"]["execution_has_no_nic"] is False


def test_ci_code_ready_without_kvm(hv: ModuleType, tmp_path: Path) -> None:
    live = {"status": "BLOCKED_HOST_UNAVAILABLE", "hostId": "sha256:" + "c" * 64}
    entries = [add_evidence(hv, tmp_path, name, host="sha256:" + "d" * 64) for name in hv.BASE + hv.CI]
    result = hv.evaluate_evidence({"checks": entries}, live, tmp_path)
    assert result["status"] == "INTEGRATED"
    assert not result["gates"]["HOST_VERIFIED"]


def test_mock_kvm_evidence_never_host_verified(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI + hv.HOST]
    result = hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)
    assert result["status"] == "INTEGRATED"
    assert set(hv.HOST) <= set(result["missingOrInvalid"])


def test_host_checks_require_real_host_and_exact_digest(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI]
    entries += [add_evidence(hv, tmp_path, k, execution="REAL_HOST") for k in hv.HOST]
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["status"] == "HOST_VERIFIED"
    live = {**fake_live(hv), "topologyDigest": "sha256:" + "f" * 64}
    assert hv.evaluate_evidence({"checks": entries}, live, tmp_path)["status"] == "INTEGRATED"


def test_production_requires_manual_operator_signoff(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI]
    entries += [add_evidence(hv, tmp_path, k, execution="REAL_HOST") for k in hv.HOST + hv.PROD[:-1]]
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["status"] == "HOST_VERIFIED"
    entries += [add_evidence(hv, tmp_path, hv.PROD[-1], execution="OPERATOR")]
    result = hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)
    assert result["status"] == "PRODUCTION_READY"
    assert result["productionCutoverAuthorized"] is False


def test_missing_or_tampered_evidence_and_duplicates_are_rejected(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI]
    (tmp_path / (hv.BASE[0] + ".json")).write_text("tampered")
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["status"] == "NOT_RUN"
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI]
    entries += [entries[0]]
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["status"] == "NOT_RUN"


def test_stale_evidence_and_traversal_rejected(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE]
    entries[0]["recordedAt"] = "2020-01-01T00:00:00Z"
    entries[1]["evidenceFile"] = "../etc/passwd"
    result = hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)
    assert result["status"] == "NOT_RUN"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("postgresql://postgres:pw@127.0.0.1:55432/aiagent_hv_disposable", True),
        ("postgresql://postgres:pw@remote:5432/aiagent_hv_disposable", False),
        ("postgresql://postgres:pw@127.0.0.1:5432/production", False),
        ("postgresql://postgres:pw@127.0.0.1/aiagent_hv_disposable", False),
        ("file:///tmp/aiagent_hv_disposable", False),
        ("", False),
    ],
)
def test_benchmark_refuses_non_disposable_url(queue_gate: ModuleType, url: str, ok: bool) -> None:
    assert queue_gate.safe_target(url) is ok


def test_load_limits_and_write_confirmation(queue_gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    args = queue_gate.arguments(["--rows", "1000000", "--execute"])
    assert queue_gate.plan(args, "")["status"] == "BLOCKED"
    args = queue_gate.arguments(["--rows", "1000", "--execute"])
    good = "postgresql://postgres:pw@127.0.0.1:55432/aiagent_hv_disposable"
    assert queue_gate.plan(args, good)["status"] == "BLOCKED"
    monkeypatch.setenv("AIAGENT_HV_ALLOW_WRITE", "YES")
    args = queue_gate.arguments(["--rows", "1000", "--execute", "--ack", "ALLOW_DISPOSABLE_DB_WRITE"])
    assert queue_gate.plan(args, good)["status"] == "READY_TO_EXECUTE"
    assert "pw" not in json.dumps(queue_gate.plan(args, good))


def test_restore_requires_quarantine(restore: ModuleType) -> None:
    safe = {
        "incarnation": 2,
        "dispatch_paused": True,
        "active_queues": 0,
        "unrevoked_permits": 0,
        "sending_intents": 0,
        "sending_effects": 0,
        "live_leases": 0,
        "unknown_effects": 3,
    }
    assert restore.safety_checks(safe)["status"] == "QUARANTINE_READONLY_PASS"
    assert restore.safety_checks({**safe, "active_queues": 1})["status"] == "BLOCKED"
    assert restore.safety_checks({**safe, "unrevoked_permits": 1})["status"] == "BLOCKED"
    assert restore.safety_checks({**safe, "live_leases": 1})["status"] == "BLOCKED"


def test_python_syntax_all_host_scripts() -> None:
    for name in ("acceptance", "queue_gate", "restore_probe"):
        path = SCRIPTS / (name + ".py")
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
