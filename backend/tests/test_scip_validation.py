from __future__ import annotations

import pytest
from aiagent_gateways.execution_artifacts import ExecutionArtifactError
from aiagent_gateways.scip_validation import SnapshotScipVerifier


def test_local_index_verifier_cannot_be_enabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    with pytest.raises(ExecutionArtifactError, match="SCIP_VALIDATION_ISOLATION_REQUIRED"):
        SnapshotScipVerifier(None, None, None, lambda: None)  # type: ignore[arg-type,return-value]


def test_profile_change_cannot_reuse_local_verifier_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    verifier = SnapshotScipVerifier(None, None, None, lambda: None)  # type: ignore[arg-type,return-value]
    monkeypatch.setenv("AIAGENT_PROFILE", "production")
    with pytest.raises(ExecutionArtifactError, match="SCIP_VALIDATION_ISOLATION_REQUIRED"):
        verifier.verify(None, "execution", b"index")  # type: ignore[arg-type]
