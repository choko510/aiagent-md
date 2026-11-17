"""Verify Supervisor-signed capture claims against the canonical Execution row.

This is a cryptographic verifier, not proof of VM/image attestation. The signer
and authenticated Cell transfer are not wired by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from aiagent_core import control
from aiagent_core.db import Database
from aiagent_domain import canonical_json

from .execution_artifacts import ExecutionArtifactError


class HmacCaptureVerifier:
    def __init__(self, db: Database, key: bytes, *, max_age_seconds: int = 120) -> None:
        if len(key) < 32 or not 1 <= max_age_seconds <= 300:
            raise ValueError("invalid capture verifier configuration")
        self._db, self._key, self._max_age = db, key, max_age_seconds

    def verify(self, claim: control.Claim, execution_id: str, digest: str, size: int, proof: bytes) -> None:
        self._verify(claim, execution_id, digest, size, proof, ("FINISHED", "PUBLISHED"))

    def verify_running(self, claim: control.Claim, execution_id: str, digest: str, size: int, proof: bytes) -> str:
        """Authenticate the receipt before a separately fenced FINISHED transition."""
        return self._verify(claim, execution_id, digest, size, proof, ("RUNNING", "FINISHED"))

    def _verify(
        self,
        claim: control.Claim,
        execution_id: str,
        digest: str,
        size: int,
        proof: bytes,
        states: tuple[str, ...],
    ) -> str:
        if not isinstance(proof, bytes) or not 1 <= len(proof) <= 16384:
            raise ExecutionArtifactError("CAPTURE_PROOF_INVALID")
        try:
            envelope = json.loads(proof)
            if not isinstance(envelope, dict) or set(envelope) != {"payload", "mac"}:
                raise ValueError
            payload, mac = envelope["payload"], envelope["mac"]
            if (
                not isinstance(payload, dict)
                or not isinstance(mac, str)
                or set(payload)
                != {
                    "version",
                    "tenant",
                    "owner",
                    "task",
                    "run",
                    "goal",
                    "lease",
                    "execution",
                    "attempt",
                    "binding_sha256",
                    "artifact_sha256",
                    "artifact_bytes",
                    "issued_at",
                }
            ):
                raise ValueError
            expected = hmac.new(self._key, canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
            now = int(time.time())
            if (
                not hmac.compare_digest(mac, expected)
                or type(payload["version"]) is not int
                or payload["version"] != 1
                or type(payload["issued_at"]) is not int
                or not now - self._max_age <= payload["issued_at"] <= now + 5
                or type(payload["goal"]) is not int
                or type(payload["lease"]) is not int
                or type(payload["artifact_bytes"]) is not int
                or payload["tenant"] != claim.tenant_id
                or payload["owner"] != claim.owner_user_id
                or payload["task"] != claim.task_id
                or payload["run"] != claim.run_id
                or payload["goal"] != claim.goal_revision
                or payload["lease"] != claim.lease_epoch
                or payload["execution"] != execution_id
                or payload["artifact_sha256"] != digest
                or payload["artifact_bytes"] != size
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ExecutionArtifactError("CAPTURE_PROOF_INVALID") from exc
        with self._db.tx(tenant=claim.tenant_id) as c:
            c.execute("SELECT set_config('statement_timeout', '5000ms', true)")
            c.execute("SELECT set_config('lock_timeout', '5000ms', true)")
            control.check_current(c, claim)
            row = c.execute(
                "SELECT tool_attempt_id,binding_sha256,state FROM agent.execution_instances"
                " WHERE tenant_id=%s AND id=%s AND owner_user_id=%s AND task_id=%s AND run_id=%s",
                (claim.tenant_id, execution_id, claim.owner_user_id, claim.task_id, claim.run_id),
            ).fetchone()
        if (
            row is None
            or row["state"] not in states
            or str(row["tool_attempt_id"]) != payload["attempt"]
            or str(row["binding_sha256"]).strip() != payload["binding_sha256"]
        ):
            raise ExecutionArtifactError("CAPTURE_PROOF_INVALID")

        return str(row["binding_sha256"]).strip()
