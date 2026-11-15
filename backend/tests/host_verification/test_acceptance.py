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


@pytest.fixture(scope="module")
def dyn_probe(hv: ModuleType) -> ModuleType:
    sys.modules["hv_acceptance_dep"] = hv
    return module("dynamic_probe")


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
    monkeypatch.setattr(hv, "run_readonly", lambda *args, **_kwargs: (False, ""))
    result = hv.capability_report()
    assert result["status"] == "BLOCKED_HOST_UNAVAILABLE"
    assert result["checks"]["kvm_api_open_and_version"] is False


def test_bad_live_topology_is_blocked(hv: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hv, "run_readonly", lambda *args, **_kwargs: (False, ""))
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

    def runner(*args: str, **_kwargs: object) -> tuple[bool, str]:
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

    def runner(*args: str, **_kwargs: object) -> tuple[bool, str]:
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
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["hostEvidenceCompleteNotAttested"] is True
    live = {**fake_live(hv), "topologyDigest": "sha256:" + "f" * 64}
    assert hv.evaluate_evidence({"checks": entries}, live, tmp_path)["hostEvidenceCompleteNotAttested"] is False


def test_production_requires_manual_operator_signoff(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI]
    entries += [add_evidence(hv, tmp_path, k, execution="REAL_HOST") for k in hv.HOST + hv.PROD[:-1]]
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["status"] == "INTEGRATED"
    entries += [add_evidence(hv, tmp_path, hv.PROD[-1], execution="OPERATOR")]
    result = hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)
    assert result["status"] == "INTEGRATED"
    assert result["hostEvidenceCompleteNotAttested"] is True
    assert result["productionEvidenceCompleteNotAttested"] is True
    assert result["gates"]["PRODUCTION_READY"] is False
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
    monkeypatch.setattr(queue_gate.sys, "platform", "linux")
    args = queue_gate.arguments(["--rows", "1000000", "--execute"])
    assert queue_gate.plan(args, "")["status"] == "BLOCKED"
    args = queue_gate.arguments(["--rows", "1000", "--execute"])
    good = "postgresql://postgres:pw@127.0.0.1:55432/aiagent_hv_disposable"
    assert queue_gate.plan(args, good)["status"] == "BLOCKED"
    monkeypatch.setenv("AIAGENT_HV_ALLOW_WRITE", "YES")
    args = queue_gate.arguments(["--rows", "1000", "--execute", "--ack", "ALLOW_DISPOSABLE_DB_WRITE"])
    assert queue_gate.plan(args, good)["status"] == "READY_TO_EXECUTE"
    assert "pw" not in json.dumps(queue_gate.plan(args, good))
    assert queue_gate.safe_target(good + "?host=evil.example") is False
    assert queue_gate.safe_target(good + "#frag") is False
    monkeypatch.delenv(queue_gate.ENV_VAR, raising=False)
    assert queue_gate.main(["--rows", "1000"]) == 2


def test_restore_requires_quarantine(restore: ModuleType) -> None:
    safe = {
        "incarnation": 2,
        "dispatch_paused": True,
        "active_queues": 0,
        "claimed_jobs": 0,
        "unrevoked_permits": 0,
        "sending_intents": 0,
        "sending_effects": 0,
        "live_leases": 0,
        "stale_active_runs": 0,
        "unknown_effects": 0,
    }
    assert restore.safety_checks(safe)["status"] == "QUARANTINE_READONLY_PASS"
    assert restore.safety_checks({**safe, "active_queues": 1})["status"] == "BLOCKED"
    assert restore.safety_checks({**safe, "claimed_jobs": 1})["status"] == "BLOCKED"
    assert restore.safety_checks({**safe, "unrevoked_permits": 1})["status"] == "BLOCKED"
    assert restore.safety_checks({**safe, "live_leases": 1})["status"] == "BLOCKED"
    assert restore.safety_checks({**safe, "stale_active_runs": 1})["status"] == "BLOCKED"
    unknown_res = restore.safety_checks({**safe, "unknown_effects": 3})
    assert unknown_res["status"] == "BLOCKED"
    assert unknown_res["unknownEffectsRequireReconcile"] == 3
    assert restore.safe_target("postgresql://postgres:pw@127.0.0.1:5432/aiagent_hv_restored_quarantine") is True
    assert (
        restore.safe_target("postgresql://postgres:pw@127.0.0.1:5432/aiagent_hv_restored_quarantine?host=evil") is False
    )


def test_acceptance_hardening_regressions(hv: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Missing fcntl on non-Linux hosts returns False instead of crashing.
    monkeypatch.setattr(hv, "fcntl", None)
    assert hv.kvm_api_available() is False

    # Oversized XML is rejected cleanly before ET.fromstring instead of truncating mid-tag.
    ok, _ = hv.run_readonly(sys.executable, "-c", "print('<root>' + 'a' * 100 + '</root>')", max_bytes=20)
    assert ok is False

    # Shared backingStore, forbidden shmem/channel, and multiple vsocks are rejected.
    base_control = """<domain type='kvm'><name>aiagent-control</name><devices>
        <disk><source file='/tmp/control.qcow2'/><backingStore><source file='/tmp/shared.qcow2'/></backingStore></disk>
        <vsock><cid auto='no' address='2000'/></vsock></devices></domain>"""
    bad_exec = """<domain type='kvm'><name>aiagent-exec</name><devices>
        <disk><source file='/tmp/exec.qcow2'/><backingStore><source file='/tmp/shared.qcow2'/></backingStore></disk>
        <channel type='unix'/><vsock><cid auto='no' address='3'/></vsock><vsock><cid auto='no' address='4'/></vsock>
        </devices></domain>"""

    def runner(*args: str, **_kwargs: object) -> tuple[bool, str]:
        n = args[-1]
        if "dumpxml" in args:
            return True, base_control if n == "aiagent-control" else bad_exec
        if "domstate" in args:
            return True, "running"
        return True, "one" if n == "aiagent-control" else "two"

    monkeypatch.setattr(hv, "run_readonly", runner)
    res = hv.topology_report()
    assert res["status"] == "BLOCKED_HOST_UNAVAILABLE"
    assert res["checks"]["different_disk_sources"] is False
    assert res["checks"]["execution_has_no_nic"] is False
    assert res["checks"]["distinct_static_vsock_cids"] is False

    # Future timestamp and oversized artifact fail validation.
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE]
    entries[0]["recordedAt"] = "2099-01-01T00:00:00Z"
    assert hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)["status"] == "NOT_RUN"


def test_queue_gate_metrics_type_and_rls_role_checks(
    queue_gate: ModuleType, restore: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_gate.sys, "platform", "linux")
    monkeypatch.setenv("AIAGENT_HV_ALLOW_WRITE", "YES")
    monkeypatch.setenv(queue_gate.ENV_VAR, "postgresql://postgres:pw@127.0.0.1:55432/aiagent_hv_disposable")
    monkeypatch.setattr(queue_gate.shutil, "which", lambda _name: "uv")

    class FakeProc:
        returncode = 0
        pid = 12345

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            return json.dumps(
                {
                    "rows": 1001,
                    "acked": True,
                    "duplicate_acks": 0,
                    "outbox_pending_after": 0,
                    "max_db_connections": None,
                    "wal_mb": 1.2,
                }
            ), ""

    monkeypatch.setattr(queue_gate.subprocess, "Popen", lambda *a, **kw: FakeProc())
    assert queue_gate.main(["--rows", "1000", "--execute", "--ack", "ALLOW_DISPOSABLE_DB_WRITE"]) == 2

    # Restore probe fails closed if connected DB role lacks BYPASSRLS/superuser.
    class FakeStatus:
        IDLE = "IDLE"

    class FakeCursor:
        def __init__(self, row: dict[str, object]) -> None:
            self._row = row

        def fetchone(self) -> dict[str, object]:
            return self._row

    class FakeTx:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

    class FakeConn:
        class info:
            transaction_status = FakeStatus.IDLE

        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def transaction(self) -> FakeTx:
            return FakeTx()

        def execute(self, sql: str) -> FakeCursor:
            if "SHOW transaction_read_only" in sql:
                return FakeCursor({"transaction_read_only": "on"})
            if "FROM pg_roles" in sql:
                return FakeCursor({"rolsuper": False, "rolbypassrls": False})
            raise AssertionError(f"unexpected query: {sql}")

    fake_psycopg = ModuleType("psycopg")
    fake_psycopg.pq = type("PQ", (), {"TransactionStatus": FakeStatus})()  # type: ignore[attr-defined]
    fake_psycopg.connect = lambda *a, **kw: FakeConn()  # type: ignore[attr-defined]
    fake_rows = ModuleType("psycopg.rows")
    fake_rows.dict_row = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)
    with pytest.raises(RuntimeError, match="BYPASSRLS"):
        restore.query_readonly("postgresql://u:p@127.0.0.1:5432/aiagent_hv_restored_quarantine")


def test_python_syntax_all_host_scripts() -> None:
    for name in ("acceptance", "queue_gate", "restore_probe", "dynamic_probe"):
        path = SCRIPTS / (name + ".py")
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_dynamic_probe_fails_closed_when_host_unavailable(
    dyn_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dyn_probe.acceptance, "capability_report", lambda: {"hostId": "sha256:" + "a" * 64, "checks": {}}
    )
    monkeypatch.setattr(
        dyn_probe.acceptance, "topology_report", lambda: {"status": "BLOCKED_HOST_UNAVAILABLE", "checks": {}}
    )
    monkeypatch.setattr(dyn_probe.acceptance, "run_readonly", lambda *a, **kw: (False, ""))
    report = dyn_probe.dynamic_probe()
    assert report["status"] == "BLOCKED_HOST_UNAVAILABLE"
    assert "kvm_api" in report["missingPrerequisites"]
    assert "nftables_egress_chain" in report["missingPrerequisites"]


def test_dynamic_probe_passes_only_with_all_isolated_host_checks(
    dyn_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dyn_probe.acceptance,
        "capability_report",
        lambda: {
            "hostId": "sha256:" + "a" * 64,
            "checks": {
                "linux_host": True,
                "kvm_api_open_and_version": True,
                "qemu_binary": True,
                "libvirt_readonly_access": True,
                "vsock_socket_api": True,
            },
        },
    )
    monkeypatch.setattr(
        dyn_probe.acceptance,
        "topology_report",
        lambda: {
            "status": "TOPOLOGY_READONLY_PASS",
            "topologyDigest": "sha256:" + "b" * 64,
            "checks": {"execution_has_no_nic": True},
        },
    )
    nft_json = json.dumps(
        {"nftables": [{"table": {"name": "aiagent_egress"}}, {"chain": {"table": "aiagent_egress", "name": "forward"}}]}
    )
    monkeypatch.setattr(dyn_probe.acceptance, "run_readonly", lambda *a, **kw: (True, nft_json))
    ok_report = dyn_probe.dynamic_probe(probe_vsock=False)
    assert ok_report["status"] == "DYNAMIC_PROBE_PASS"
    assert ok_report["missingPrerequisites"] == []

    monkeypatch.setattr(
        dyn_probe,
        "probe_vsock_endpoint",
        lambda cid, port: {"probed": True, "reachable": False, "reason": "ConnectionRefusedError"},
    )
    blocked = dyn_probe.dynamic_probe(probe_vsock=True)
    assert blocked["status"] == "BLOCKED_HOST_UNAVAILABLE"
    assert blocked["missingPrerequisites"] == ["vsock_control_reachable"]


def test_synthetic_artifacts_cannot_automatically_attest_host_or_production(hv: ModuleType, tmp_path: Path) -> None:
    entries = [add_evidence(hv, tmp_path, k) for k in hv.BASE + hv.CI]
    entries += [add_evidence(hv, tmp_path, k, execution="REAL_HOST") for k in hv.HOST + hv.PROD[:-1]]
    entries += [add_evidence(hv, tmp_path, hv.PROD[-1], execution="OPERATOR")]
    result = hv.evaluate_evidence({"checks": entries}, fake_live(hv), tmp_path)
    assert result["hostEvidenceCompleteNotAttested"] is True
    assert result["productionEvidenceCompleteNotAttested"] is True
    assert result["gates"]["HOST_VERIFIED"] is False
    assert result["gates"]["PRODUCTION_READY"] is False
