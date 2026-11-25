"""PostgreSQL store for Skill lifecycle. RLS is set by Database.tx(tenant) on every access."""

from __future__ import annotations

import json
from typing import Any

import psycopg
from aiagent_core.db import Database, Row
from aiagent_domain.ids import utc_instant

from .contracts import (
    DesiredState,
    InstallationState,
    OperationReceipt,
    ReleaseState,
    RunSkillPin,
    ScanResult,
    SecurityFinding,
    Severity,
    SkillInstallation,
    SkillRelease,
    TrustLevel,
)
from .errors import IdempotencyKeyReused, InstallationConflict, LifecycleConflict, SkillNotFound, StaleRevision
from .manifest import manifest_from_wire


def _instant(value: Any) -> str | None:
    if value is None:
        return None
    return utc_instant(value)


class PostgresSkillStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def put_release(self, release: SkillRelease) -> None:
        if release.tenant_id is None:
            raise LifecycleConflict("release must be tenant-scoped")
        try:
            with self.db.tx(release.tenant_id) as conn:
                conn.execute(
                    """INSERT INTO agent.skill_releases
                       (id, tenant_id, owner_user_id, skill_name, version, content_digest, state, trust_level,
                        scan_digest, scan, manifest, publisher_id, publisher_handle, publisher_verified, signature_status,
                        source_kind, source_commit_sha, artifact_digest, created_at, validated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        release.id,
                        release.tenant_id,
                        release.owner_user_id,
                        release.manifest.name,
                        release.manifest.version,
                        release.content_digest,
                        release.state.value,
                        release.trust_level.value,
                        release.scan_digest,
                        json.dumps(release.scan.to_wire()) if release.scan is not None else json.dumps({}),
                        json.dumps(release.manifest.to_wire()),
                        release.manifest.publisher_id,
                        release.manifest.publisher_handle,
                        release.publisher_verified,
                        release.signature_status,
                        release.manifest.source.kind.value,
                        release.source_commit_sha,
                        release.artifact_digest,
                        release.created_at,
                        release.validated_at,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise LifecycleConflict("release already exists (immutable identity or digest)") from exc

    def get_release(self, tenant_id: str, release_id: str) -> SkillRelease:
        with self.db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM agent.skill_releases WHERE tenant_id=%s AND id=%s", (tenant_id, release_id)
            ).fetchone()
        if row is None:
            raise SkillNotFound("release not found")
        return self._release(row)

    def find_release_by_digest(self, tenant_id: str, digest: str) -> SkillRelease | None:
        with self.db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM agent.skill_releases WHERE tenant_id=%s AND content_digest=%s ORDER BY created_at DESC LIMIT 1",
                (tenant_id, digest),
            ).fetchone()
        return self._release(row) if row is not None else None

    def list_releases(self, tenant_id: str) -> tuple[SkillRelease, ...]:
        with self.db.tx(tenant_id) as conn:
            rows = conn.execute(
                "SELECT * FROM agent.skill_releases WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 200",
                (tenant_id,),
            ).fetchall()
        return tuple(self._release(row) for row in rows)

    def revoke_release(self, tenant_id: str, release_id: str, now: str) -> None:
        with self.db.tx(tenant_id) as conn:
            cur = conn.execute(
                "UPDATE agent.skill_releases SET state=%s, revoked_at=COALESCE(revoked_at, %s) WHERE tenant_id=%s AND id=%s",
                (ReleaseState.REVOKED.value, now, tenant_id, release_id),
            )
        if cur.rowcount == 0:
            raise SkillNotFound("release not found")

    def put_installation(self, installation: SkillInstallation) -> None:
        try:
            with self.db.tx(installation.tenant_id) as conn:
                cur = conn.execute(
                    """INSERT INTO agent.skill_installations
                       (id, tenant_id, owner_user_id, skill_name, release_id, desired, effective, record_version,
                        activation_generation, policy_revision, approved_release_digest, created_at, updated_at, revoked_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, id) DO UPDATE SET
                         release_id=EXCLUDED.release_id, desired=EXCLUDED.desired, effective=EXCLUDED.effective,
                         record_version=EXCLUDED.record_version, activation_generation=EXCLUDED.activation_generation,
                         policy_revision=EXCLUDED.policy_revision, approved_release_digest=EXCLUDED.approved_release_digest,
                         updated_at=EXCLUDED.updated_at, revoked_at=EXCLUDED.revoked_at
                       WHERE agent.skill_installations.record_version = EXCLUDED.record_version - 1""",
                    (
                        installation.id,
                        installation.tenant_id,
                        installation.owner_user_id,
                        installation.skill_name,
                        installation.release_id,
                        installation.desired.value,
                        installation.effective.value,
                        installation.record_version,
                        installation.activation_generation,
                        installation.policy_revision,
                        installation.approved_release_digest,
                        installation.created_at,
                        installation.updated_at,
                        installation.revoked_at,
                    ),
                )
                if cur.rowcount != 1:
                    raise StaleRevision("installation record version changed concurrently")
        except psycopg.errors.UniqueViolation as exc:
            raise InstallationConflict("Skill is already installed in this owner scope") from exc

    def get_installation(self, tenant_id: str, installation_id: str) -> SkillInstallation:
        with self.db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM agent.skill_installations WHERE tenant_id=%s AND id=%s", (tenant_id, installation_id)
            ).fetchone()
        if row is None:
            raise SkillNotFound("installation not found")
        return self._installation(row)

    def find_installation_by_name(self, tenant_id: str, owner_user_id: str, name: str) -> SkillInstallation | None:
        with self.db.tx(tenant_id) as conn:
            row = conn.execute(
                """SELECT * FROM agent.skill_installations
                   WHERE tenant_id=%s AND owner_user_id=%s AND skill_name=%s AND effective<>%s LIMIT 1""",
                (tenant_id, owner_user_id, name, InstallationState.REMOVED.value),
            ).fetchone()
        return self._installation(row) if row is not None else None

    def list_installations(self, tenant_id: str, owner_user_id: str) -> tuple[SkillInstallation, ...]:
        with self.db.tx(tenant_id) as conn:
            rows = conn.execute(
                "SELECT * FROM agent.skill_installations WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at",
                (tenant_id, owner_user_id),
            ).fetchall()
        return tuple(self._installation(row) for row in rows)

    def add_history(self, tenant_id: str, installation_id: str, release_id: str) -> None:
        with self.db.tx(tenant_id) as conn:
            conn.execute(
                """INSERT INTO agent.skill_installation_history (tenant_id, installation_id, release_id)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                (tenant_id, installation_id, release_id),
            )

    def list_history(self, tenant_id: str, installation_id: str) -> tuple[str, ...]:
        with self.db.tx(tenant_id) as conn:
            rows = conn.execute(
                "SELECT release_id FROM agent.skill_installation_history WHERE tenant_id=%s AND installation_id=%s ORDER BY replaced_at",
                (tenant_id, installation_id),
            ).fetchall()
        return tuple(str(row["release_id"]) for row in rows)

    def get_receipt(self, tenant_id: str, actor_user_id: str, request_id: str) -> OperationReceipt | None:
        with self.db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM agent.skill_receipts WHERE tenant_id=%s AND actor_user_id=%s AND request_id=%s",
                (tenant_id, actor_user_id, request_id),
            ).fetchone()
        return self._receipt(row) if row is not None else None

    def put_receipt(self, receipt: OperationReceipt) -> None:
        with self.db.tx(receipt.tenant_id) as conn:
            cur = conn.execute(
                """INSERT INTO agent.skill_receipts
                   (id, tenant_id, actor_user_id, request_id, payload_sha256, action, installation_id, release_id, result, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT (tenant_id, actor_user_id, request_id) DO NOTHING""",
                (
                    receipt.id,
                    receipt.tenant_id,
                    receipt.actor_user_id,
                    receipt.request_id,
                    receipt.payload_sha256,
                    receipt.action,
                    receipt.installation_id,
                    receipt.release_id,
                    json.dumps(receipt.result),
                    receipt.created_at,
                ),
            )
            if cur.rowcount == 0:
                existing = conn.execute(
                    "SELECT payload_sha256, action FROM agent.skill_receipts WHERE tenant_id=%s AND actor_user_id=%s AND request_id=%s",
                    (receipt.tenant_id, receipt.actor_user_id, receipt.request_id),
                ).fetchone()
                if (
                    existing is None
                    or existing["payload_sha256"] != receipt.payload_sha256
                    or existing["action"] != receipt.action
                ):
                    raise IdempotencyKeyReused("Idempotency-Key was reused with a different payload")

    def put_pin(self, pin: RunSkillPin) -> None:
        with self.db.tx(pin.tenant_id) as conn:
            cur = conn.execute(
                """INSERT INTO agent.skill_run_pins
                   (tenant_id, run_id, installation_id, release_id, content_digest, activation_generation,
                    policy_revision, capability_digest, dependency_digest, skill_surface_digest, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    pin.tenant_id,
                    pin.run_id,
                    pin.installation_id,
                    pin.release_id,
                    pin.content_digest,
                    pin.activation_generation,
                    pin.policy_revision,
                    pin.capability_digest,
                    pin.dependency_digest,
                    pin.skill_surface_digest,
                    pin.created_at,
                ),
            )
            if cur.rowcount == 0:
                stored = conn.execute(
                    "SELECT * FROM agent.skill_run_pins WHERE tenant_id=%s AND run_id=%s AND installation_id=%s",
                    (pin.tenant_id, pin.run_id, pin.installation_id),
                ).fetchone()
                if stored is None or (
                    str(stored["release_id"]),
                    str(stored["content_digest"]),
                    int(stored["activation_generation"]),
                    str(stored["capability_digest"]),
                    str(stored["dependency_digest"]),
                    str(stored["skill_surface_digest"]),
                ) != (
                    pin.release_id,
                    pin.content_digest,
                    pin.activation_generation,
                    pin.capability_digest,
                    pin.dependency_digest,
                    pin.skill_surface_digest,
                ):
                    raise LifecycleConflict("run pin is immutable")

    def get_pin(self, tenant_id: str, run_id: str, installation_id: str) -> RunSkillPin | None:
        with self.db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM agent.skill_run_pins WHERE tenant_id=%s AND run_id=%s AND installation_id=%s",
                (tenant_id, run_id, installation_id),
            ).fetchone()
        if row is None:
            return None
        return RunSkillPin(
            tenant_id=str(row["tenant_id"]),
            run_id=str(row["run_id"]),
            installation_id=str(row["installation_id"]),
            release_id=str(row["release_id"]),
            content_digest=str(row["content_digest"]),
            activation_generation=int(row["activation_generation"]),
            policy_revision=str(row["policy_revision"]),
            capability_digest=str(row["capability_digest"]),
            dependency_digest=str(row["dependency_digest"]),
            skill_surface_digest=str(row["skill_surface_digest"]),
            created_at=_instant(row["created_at"]) or "",
        )

    def record_audit(
        self,
        tenant_id: str,
        actor_user_id: str,
        action: str,
        *,
        installation_id: str | None,
        release_id: str | None,
        detail: dict[str, Any],
    ) -> None:
        with self.db.tx(tenant_id) as conn:
            conn.execute(
                """INSERT INTO agent.skill_audit_events
                   (tenant_id, actor_user_id, action, installation_id, release_id, detail)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                (tenant_id, actor_user_id, action, installation_id, release_id, json.dumps(detail)),
            )

    @staticmethod
    def _release(row: Row) -> SkillRelease:
        return SkillRelease(
            id=str(row["id"]),
            manifest=manifest_from_wire(row["manifest"]),
            content_digest=str(row["content_digest"]),
            state=ReleaseState(row["state"]),
            trust_level=TrustLevel(row["trust_level"]),
            scan_digest=str(row["scan_digest"]),
            created_at=_instant(row["created_at"]) or "",
            validated_at=_instant(row["validated_at"]),
            publisher_verified=bool(row["publisher_verified"]),
            signature_status=str(row["signature_status"]),
            source_commit_sha=row["source_commit_sha"],
            artifact_digest=row["artifact_digest"],
            tenant_id=str(row["tenant_id"]),
            owner_user_id=str(row["owner_user_id"]),
            scan=PostgresSkillStore._scan(row),
        )

    @staticmethod
    def _scan(row: Row) -> ScanResult | None:
        data = row.get("scan")
        if not isinstance(data, dict) or "findings" not in data:
            return None
        findings = tuple(
            SecurityFinding(
                rule_id=str(item["ruleId"]),
                severity=Severity(str(item["severity"])),
                path=str(item["path"]),
                line=item.get("line"),
                column=item.get("column"),
                excerpt=str(item.get("excerpt", "")),
                rationale=str(item.get("rationale", "")),
                recommendation=str(item.get("recommendation", "")),
                engine_version=str(item.get("engineVersion", "skill-scan/1")),
                heuristic=bool(item.get("heuristic", True)),
            )
            for item in data["findings"]
        )
        return ScanResult(
            findings=findings,
            engine_version=str(data.get("engineVersion", "skill-scan/1")),
            digest=str(data.get("digest", row["scan_digest"])),
            blocking=bool(data.get("blocking", False)),
        )

    @staticmethod
    def _installation(row: Row) -> SkillInstallation:
        return SkillInstallation(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            owner_user_id=str(row["owner_user_id"]),
            skill_name=str(row["skill_name"]),
            release_id=str(row["release_id"]),
            desired=DesiredState(row["desired"]),
            effective=InstallationState(row["effective"]),
            record_version=int(row["record_version"]),
            activation_generation=int(row["activation_generation"]),
            policy_revision=str(row["policy_revision"]),
            approved_release_digest=row["approved_release_digest"],
            created_at=_instant(row["created_at"]) or "",
            updated_at=_instant(row["updated_at"]) or "",
            revoked_at=_instant(row["revoked_at"]),
        )

    @staticmethod
    def _receipt(row: Row) -> OperationReceipt:
        return OperationReceipt(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            actor_user_id=str(row["actor_user_id"]),
            request_id=str(row["request_id"]),
            payload_sha256=str(row["payload_sha256"]),
            action=str(row["action"]),
            installation_id=str(row["installation_id"]) if row["installation_id"] else None,
            release_id=str(row["release_id"]) if row["release_id"] else None,
            result=dict(row["result"]),
            created_at=_instant(row["created_at"]) or "",
        )
