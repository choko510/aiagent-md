"""Environment-based configuration with file-mounted secrets and production fail-closed checks.

Secrets are read from `<NAME>_FILE` (Docker/systemd credentials) in production. Inline secret env vars are accepted
only in the development profile. Secret values are wrapped in `Secret` so they never appear in repr/logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Secret:
    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


PROFILES = ("production", "development", "test")


def profile() -> str:
    p = os.environ.get("AIAGENT_PROFILE", "production")
    if p not in PROFILES:
        raise ConfigError(f"unknown AIAGENT_PROFILE {p!r}")
    return p


def require_secret(name: str) -> str:
    s = secret(name)
    assert s is not None
    return s.reveal()


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise ConfigError(f"missing required setting {name}")
    return v


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.lstrip("-").isdigit():
        raise ConfigError(f"{name} must be an integer")
    return int(raw)


def secret(name: str, *, required: bool = True) -> Secret | None:
    """Read a secret: `<name>_FILE` first; inline `<name>` only outside production."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        p = Path(path)
        try:
            value = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError(f"cannot read secret file for {name}") from e
        if not value:
            raise ConfigError(f"secret file for {name} is empty")
        return Secret(value)
    inline = os.environ.get(name)
    if inline:
        if profile() == "production":
            raise ConfigError(f"{name} must be provided via {name}_FILE in production")
        return Secret(inline)
    if required:
        raise ConfigError(f"missing secret {name} (set {name}_FILE)")
    return None


def require_production_safety(component: str, checks: dict[str, bool]) -> None:
    """Fail closed at start-up when a production safety feature is missing.

    `checks` maps a human readable requirement to whether it is satisfied. In development/test the missing items
    are only reported by the caller; this function raises only in production.
    """
    missing = [k for k, ok in checks.items() if not ok]
    if missing and profile() == "production":
        raise ConfigError(f"{component}: production safety requirements not met: {', '.join(missing)}")


def isolation_attestation() -> dict[str, object]:
    """Provisioning (deploy/kvm) writes this file inside the Trusted Control VM after verifying the topology:
    Trusted Control VM / Untrusted Execution VM on separate guest kernels, egress only via Sentinel, no Docker socket.
    Its absence in production means the safety boundary is not established, so services refuse to start."""
    import json

    path = os.environ.get("AIAGENT_ISOLATION_ATTESTATION_FILE", "/etc/aiagent/isolation.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


REQUIRED_ATTESTATION = (
    "trusted_control_vm",
    "untrusted_execution_vm_separate_kernel",
    "egress_only_via_sentinel",
    "no_docker_socket",
)


def require_isolation(component: str) -> None:
    att = isolation_attestation()
    require_production_safety(
        component, {f"isolation attestation: {k}": att.get(k) is True for k in REQUIRED_ATTESTATION}
    )
