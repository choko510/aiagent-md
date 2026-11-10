"""Production start-up gates: without the VM-topology attestation, with inline secrets, with a FakeModel-only Model
Gateway, or with a development-only egress allowance, services must refuse to start."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiagent_core import config

ENTRY = {
    "api": ["-m", "aiagent_api.app"],
    "runtime": ["-m", "aiagent_runtime.worker"],
    "model_gateway": ["-m", "aiagent_gateways.servers", "model"],
    "tool_gateway": ["-m", "aiagent_gateways.servers", "tool"],
    "sentinel": ["-m", "aiagent_sentinel.main"],
    "relay": ["-m", "aiagent_core.relay_main"],
}


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("AIAGENT_")}
    return subprocess.run([sys.executable, *args], env={**base, **env}, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("service", sorted(ENTRY))
def test_refuses_without_isolation_attestation(service: str, tmp_path: Path) -> None:
    r = run(ENTRY[service], {"AIAGENT_PROFILE": "production",
                             "AIAGENT_ISOLATION_ATTESTATION_FILE": str(tmp_path / "missing.json")})
    assert r.returncode != 0
    assert "production safety requirements not met" in r.stderr and "isolation attestation" in r.stderr, r.stderr[-800:]


def test_attestation_must_be_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "iso.json"
    f.write_text(json.dumps({"trusted_control_vm": True, "untrusted_execution_vm_separate_kernel": True,
                             "egress_only_via_sentinel": True, "no_docker_socket": False}))
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    monkeypatch.setenv("AIAGENT_ISOLATION_ATTESTATION_FILE", str(f))
    with pytest.raises(config.ConfigError, match="no_docker_socket"):
        config.require_isolation("x")
    f.write_text(json.dumps({k: True for k in config.REQUIRED_ATTESTATION}))
    config.require_isolation("x")  # complete attestation passes


def test_inline_secrets_refused_in_production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    monkeypatch.setenv("AIAGENT_X", "inline-value")
    with pytest.raises(config.ConfigError, match="_FILE"):
        config.secret("AIAGENT_X")
    p = tmp_path / "s"
    p.write_text("from-file\n")
    monkeypatch.setenv("AIAGENT_X_FILE", str(p))
    assert config.secret("AIAGENT_X").reveal() == "from-file"  # type: ignore[union-attr]
    assert "from-file" not in repr(config.secret("AIAGENT_X"))


def test_model_gateway_refuses_fake_only_in_production(tmp_path: Path) -> None:
    att = tmp_path / "iso.json"
    att.write_text(json.dumps({k: True for k in config.REQUIRED_ATTESTATION}))
    for name, val in (("keys", "runtime=" + "11" * 32), ("dsn", "postgresql://x:y@127.0.0.1:1/z")):
        (tmp_path / name).write_text(val)
    r = run(ENTRY["model_gateway"], {
        "AIAGENT_PROFILE": "production", "AIAGENT_ISOLATION_ATTESTATION_FILE": str(att),
        "AIAGENT_MODEL_GATEWAY_RPC_KEYS_FILE": str(tmp_path / "keys"), "AIAGENT_MODEL_GATEWAY_DSN_FILE": str(tmp_path / "dsn"),
        "AIAGENT_MODEL_GATEWAY_ENDPOINT": "tcp:127.0.0.1:0"})
    assert r.returncode == 2 and "FakeModel only" in r.stderr, r.stderr[-800:]
