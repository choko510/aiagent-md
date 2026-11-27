"""Skill registry service: idempotent operations over an explicit store contract."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from aiagent_domain.canonical import canonical_json, sha256_hex
from aiagent_domain.ids import utc_instant, uuid7

from .approval import create_approval, verify_approval
from .bundle import inspect_zip
from .contracts import (
    CatalogEntry,
    DependencyPin,
    InstallationState,
    OperationReceipt,
    ReleaseState,
    RunSkillPin,
    SkillApproval,
    SkillDependency,
    SkillInstallation,
    SkillRelease,
    SkillSource,
    SkillText,
)
from .dependency import DependencyResolution, DependencyResolver
from .digest import capability_digest, dependency_digest, sha256_bytes
from .errors import (
    IdempotencyKeyReused,
    InstallationConflict,
    LifecycleConflict,
    QuarantineUnavailable,
    ReleaseRevoked,
    SkillError,
    SkillNotFound,
    SkillScopeViolation,
)
from .identity import assert_same_lineage
from .lifecycle import (
    abort_update,
    begin_disable,
    begin_enable,
    begin_remove,
    begin_rollback,
    begin_update,
    finish_enable,
    finish_install,
    finish_remove,
    finish_rollback,
    finish_update,
    new_installation,
    revoke_installation,
)
from .pinning import (
    EnabledSkillCatalog,
    ImmutableSkillBackend,
    create_run_pin,
    dependency_snapshot_digest,
    disclose_text,
    verify_run_pin,
)
from .policy import validate_requested
from .quarantine import QuarantineStore
from .validation import assert_manifest_consistent, assert_release_installable, build_release

RunAuthorizer = Callable[[str, str, str], bool]

MAX_PAGE = 100


class SkillStore(Protocol):
    def atomic(self, tenant_id: str) -> AbstractContextManager[None]: ...
    def put_release(self, release: SkillRelease) -> None: ...
    def get_release(self, tenant_id: str, release_id: str) -> SkillRelease: ...
    def find_release_by_digest(self, tenant_id: str, digest: str) -> SkillRelease | None: ...
    def list_releases(self, tenant_id: str) -> tuple[SkillRelease, ...]: ...
    def revoke_release(self, tenant_id: str, release_id: str, now: str) -> None: ...
    def put_installation(self, installation: SkillInstallation) -> None: ...
    def get_installation(self, tenant_id: str, installation_id: str) -> SkillInstallation: ...
    def find_installation_by_name(self, tenant_id: str, owner_user_id: str, name: str) -> SkillInstallation | None: ...
    def list_installations(self, tenant_id: str, owner_user_id: str) -> tuple[SkillInstallation, ...]: ...
    def add_history(self, tenant_id: str, installation_id: str, release_id: str) -> None: ...
    def list_history(self, tenant_id: str, installation_id: str) -> tuple[str, ...]: ...
    def get_receipt(self, tenant_id: str, actor_user_id: str, request_id: str) -> OperationReceipt | None: ...
    def put_receipt(self, receipt: OperationReceipt) -> None: ...
    def record_audit(
        self,
        tenant_id: str,
        actor_user_id: str,
        action: str,
        *,
        installation_id: str | None,
        release_id: str | None,
        detail: dict[str, Any],
    ) -> None: ...
    def put_pin(self, pin: RunSkillPin) -> None: ...
    def get_pin(self, tenant_id: str, run_id: str, installation_id: str) -> RunSkillPin | None: ...


def _pin_identity(pin: RunSkillPin) -> tuple[object, ...]:
    return (
        pin.release_id,
        pin.content_digest,
        pin.activation_generation,
        pin.revocation_epoch,
        pin.policy_revision,
        pin.capability_digest,
        pin.dependency_digest,
        pin.dependency_snapshot_digest,
        pin.skill_surface_digest,
    )


class InMemorySkillStore:
    """Single-process test store. Production uses PostgreSQL with tenant RLS."""

    def __init__(self) -> None:
        self._releases: dict[tuple[str, str], SkillRelease] = {}
        self._installations: dict[tuple[str, str], SkillInstallation] = {}
        self._history: dict[tuple[str, str], list[str]] = {}
        self._receipts: dict[tuple[str, str, str], OperationReceipt] = {}
        self._pins: dict[tuple[str, str, str], RunSkillPin] = {}
        self._audit: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    @contextmanager
    def atomic(self, tenant_id: str) -> Iterator[None]:
        """Mirror PostgreSQL rollback semantics so a failed operation leaves no partial in-memory state."""
        snapshot = (
            dict(self._releases),
            dict(self._installations),
            {key: list(value) for key, value in self._history.items()},
            dict(self._receipts),
            dict(self._pins),
            list(self._audit),
        )
        try:
            yield
        except BaseException:
            self._releases, self._installations, self._history, self._receipts, self._pins, self._audit = snapshot
            raise

    def put_release(self, release: SkillRelease) -> None:
        if release.tenant_id is None:
            raise SkillScopeViolation("release must be tenant-scoped")
        with self._lock:
            key = (release.tenant_id, release.id)
            existing = self._releases.get(key)
            if existing is not None and existing.content_digest != release.content_digest:
                raise LifecycleConflict("release is immutable")
            self._releases[key] = release

    def get_release(self, tenant_id: str, release_id: str) -> SkillRelease:
        with self._lock:
            try:
                return self._releases[(tenant_id, release_id)]
            except KeyError:
                raise SkillNotFound("release not found") from None

    def find_release_by_digest(self, tenant_id: str, digest: str) -> SkillRelease | None:
        with self._lock:
            for (owner, _), release in self._releases.items():
                if owner == tenant_id and release.content_digest == digest:
                    return release
            return None

    def list_releases(self, tenant_id: str) -> tuple[SkillRelease, ...]:
        with self._lock:
            rows = [r for (t, _), r in self._releases.items() if t == tenant_id]
        return tuple(sorted(rows, key=lambda r: r.created_at))

    def revoke_release(self, tenant_id: str, release_id: str, now: str) -> None:
        with self._lock:
            release = self.get_release(tenant_id, release_id)
            self._releases[(tenant_id, release_id)] = SkillRelease(
                **{**release.__dict__, "state": ReleaseState.REVOKED}
            )

    def put_installation(self, installation: SkillInstallation) -> None:
        with self._lock:
            self._installations[(installation.tenant_id, installation.id)] = installation

    def get_installation(self, tenant_id: str, installation_id: str) -> SkillInstallation:
        with self._lock:
            try:
                return self._installations[(tenant_id, installation_id)]
            except KeyError:
                raise SkillNotFound("installation not found") from None

    def find_installation_by_name(self, tenant_id: str, owner_user_id: str, name: str) -> SkillInstallation | None:
        with self._lock:
            for installation in self._installations.values():
                if (
                    installation.tenant_id == tenant_id
                    and installation.owner_user_id == owner_user_id
                    and installation.skill_name == name
                    and installation.effective is not InstallationState.REMOVED
                ):
                    return installation
            return None

    def list_installations(self, tenant_id: str, owner_user_id: str) -> tuple[SkillInstallation, ...]:
        with self._lock:
            rows = [
                i for i in self._installations.values() if i.tenant_id == tenant_id and i.owner_user_id == owner_user_id
            ]
        return tuple(sorted(rows, key=lambda i: i.created_at))

    def add_history(self, tenant_id: str, installation_id: str, release_id: str) -> None:
        with self._lock:
            self._history.setdefault((tenant_id, installation_id), []).append(release_id)

    def list_history(self, tenant_id: str, installation_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._history.get((tenant_id, installation_id), ()))

    def get_receipt(self, tenant_id: str, actor_user_id: str, request_id: str) -> OperationReceipt | None:
        with self._lock:
            return self._receipts.get((tenant_id, actor_user_id, request_id))

    def put_receipt(self, receipt: OperationReceipt) -> None:
        with self._lock:
            self._receipts[(receipt.tenant_id, receipt.actor_user_id, receipt.request_id)] = receipt

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
        with self._lock:
            self._audit.append(
                {
                    "tenantId": tenant_id,
                    "actorUserId": actor_user_id,
                    "action": action,
                    "installationId": installation_id,
                    "releaseId": release_id,
                    "detail": detail,
                }
            )

    def put_pin(self, pin: RunSkillPin) -> None:
        with self._lock:
            key = (pin.tenant_id, pin.run_id, pin.installation_id)
            existing = self._pins.get(key)
            if existing is not None:
                if _pin_identity(existing) != _pin_identity(pin):
                    raise LifecycleConflict("run pin is immutable")
                return
            self._pins[key] = pin

    def get_pin(self, tenant_id: str, run_id: str, installation_id: str) -> RunSkillPin | None:
        with self._lock:
            return self._pins.get((tenant_id, run_id, installation_id))


class SkillRegistry:
    def __init__(
        self,
        store: SkillStore,
        *,
        quarantine: QuarantineStore | None = None,
        run_authorizer: RunAuthorizer | None = None,
    ) -> None:
        self.store = store
        self.quarantine = quarantine
        self.run_authorizer = run_authorizer
        self._lock = threading.RLock()

    def _receipt(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        request_id: str,
        action: str,
        payload: dict[str, Any],
        operation: Callable[[], tuple[str | None, str | None, dict[str, Any]]],
    ) -> OperationReceipt:
        if not request_id or len(request_id) > 128:
            raise SkillError("Idempotency-Key is required")
        payload_hash = sha256_hex(canonical_json(payload))
        with self._lock, self.store.atomic(tenant_id):
            existing = self.store.get_receipt(tenant_id, actor_user_id, request_id)
            if existing is not None:
                if existing.action != action or existing.payload_sha256 != payload_hash:
                    raise IdempotencyKeyReused("Idempotency-Key was reused with a different payload")
                return existing
            installation_id, release_id, result = operation()
            receipt = OperationReceipt(
                id=uuid7(),
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                request_id=request_id,
                payload_sha256=payload_hash,
                action=action,
                installation_id=installation_id,
                release_id=release_id,
                result=result,
                created_at=utc_instant(),
            )
            self.store.put_receipt(receipt)
            self.store.record_audit(
                tenant_id,
                actor_user_id,
                action,
                installation_id=installation_id,
                release_id=release_id,
                detail={"receiptId": receipt.id, "requestId": request_id, "payloadSha256": payload_hash},
            )
            return receipt

    def ingest(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        source: SkillSource,
        archive: bytes,
        request_id: str,
        now: str | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()
        payload = {"source": source.to_wire(), "archiveSha256": sha256_bytes(archive)}

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            bundle = inspect_zip(archive)
            if self.quarantine is not None:
                self.quarantine.put(bundle)
            release, scan = build_release(
                release_id=uuid7(),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                source=source,
                bundle=bundle,
                now=timestamp,
            )
            for existing in self.store.list_releases(tenant_id):
                if (
                    existing.owner_user_id == owner_user_id
                    and existing.manifest.name == release.manifest.name
                    and existing.manifest.version == release.manifest.version
                ):
                    raise LifecycleConflict("this Skill version already exists and is immutable")
            self.store.put_release(release)
            return (
                None,
                release.id,
                {
                    "release": release.to_wire(),
                    "scan": scan.to_wire(),
                    "bundleDigest": bundle.digest,
                },
            )

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="ingest",
            payload=payload,
            operation=operation,
        )

    def _approval(
        self,
        *,
        approval: SkillApproval,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        action: str,
        release: SkillRelease,
        capability_digest_value: str,
        policy_revision: str,
        expected_revision: int,
        now: str,
        request_id: str,
        installation_id: str | None = None,
        target_release_id: str | None = None,
        enable_after: bool | None = None,
        reenable: bool | None = None,
    ) -> None:
        verify_approval(
            approval,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            action=action,
            release=release,
            capability_digest=capability_digest_value,
            policy_revision=policy_revision,
            expected_installation_revision=expected_revision,
            now=now,
            request_id=request_id,
            installation_id=installation_id,
            target_release_id=target_release_id,
            enable_after=enable_after,
            reenable=reenable,
        )

    def install(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        release_id: str,
        approval: SkillApproval,
        capability_digest_value: str,
        policy_revision: str,
        request_id: str,
        now: str | None = None,
        request_policy_revision: str | None = None,
        request_ttl_seconds: int | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            release = self.store.get_release(tenant_id, release_id)
            if release.owner_user_id not in (None, owner_user_id):
                raise SkillScopeViolation("release belongs to another owner")
            if release.state is ReleaseState.REVOKED:
                raise ReleaseRevoked("release is revoked")
            if release.state is not ReleaseState.STAGED_FOR_REVIEW:
                raise LifecycleConflict("release is not ready for installation")
            if release.scan is not None:
                assert_release_installable(release, release.scan)
            self.resolve_dependencies(tenant_id, owner_user_id, release)
            existing = self.store.find_installation_by_name(tenant_id, owner_user_id, release.manifest.name)
            if existing is not None:
                raise InstallationConflict("Skill is already installed in this owner scope")
            self._approval(
                approval=approval,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
                action="install",
                release=release,
                capability_digest_value=capability_digest_value,
                policy_revision=policy_revision,
                expected_revision=0,
                now=timestamp,
                request_id=request_id,
            )
            installation = new_installation(
                installation_id=uuid7(),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                release=release,
                policy_revision=policy_revision,
                now=timestamp,
            )
            installation = finish_install(installation, now=timestamp)
            self.store.put_installation(installation)
            self.store.add_history(tenant_id, installation.id, release.id)
            return installation.id, release.id, {"installation": installation.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="install",
            payload={
                "releaseId": release_id,
                "expectedContentDigest": approval.content_digest,
                "policyRevision": request_policy_revision,
                "ttlSeconds": request_ttl_seconds,
            },
            operation=operation,
        )

    def enable(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        installation_id: str,
        approval: SkillApproval,
        capability_digest_value: str,
        policy_revision: str,
        expected_revision: int,
        request_id: str,
        now: str | None = None,
        request_policy_revision: str | None = None,
        request_ttl_seconds: int | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            installation = self.store.get_installation(tenant_id, installation_id)
            assert_owner(installation, owner_user_id)
            release = self.store.get_release(tenant_id, installation.release_id)
            if release.scan is not None:
                assert_release_installable(release, release.scan)
            self.resolve_dependencies(tenant_id, owner_user_id, release)
            self._approval(
                approval=approval,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
                action="enable",
                release=release,
                capability_digest_value=capability_digest_value,
                policy_revision=policy_revision,
                expected_revision=expected_revision,
                now=timestamp,
                request_id=request_id,
                installation_id=installation_id,
            )
            updated = begin_enable(
                installation,
                release,
                expected_revision=expected_revision,
                approved_release_digest=approval.content_digest,
                policy_revision=policy_revision,
                now=timestamp,
            )
            updated = finish_enable(updated, now=timestamp)
            self.store.put_installation(updated)
            return updated.id, updated.release_id, {"installation": updated.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="enable",
            payload={
                "installationId": installation_id,
                "expectedRevision": expected_revision,
                "expectedContentDigest": approval.content_digest,
                "policyRevision": request_policy_revision,
                "ttlSeconds": request_ttl_seconds,
            },
            operation=operation,
        )

    def disable(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        installation_id: str,
        expected_revision: int,
        request_id: str,
        now: str | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            installation = self.store.get_installation(tenant_id, installation_id)
            assert_owner(installation, owner_user_id)
            updated = begin_disable(installation, expected_revision=expected_revision, now=timestamp)
            self.store.put_installation(updated)
            return updated.id, updated.release_id, {"installation": updated.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="disable",
            payload={"installationId": installation_id, "expectedRevision": expected_revision},
            operation=operation,
        )

    def update(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        installation_id: str,
        target_release_id: str,
        approval: SkillApproval,
        capability_digest_value: str,
        policy_revision: str,
        expected_revision: int,
        enable_after: bool,
        request_id: str,
        now: str | None = None,
        request_policy_revision: str | None = None,
        request_ttl_seconds: int | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            installation = self.store.get_installation(tenant_id, installation_id)
            assert_owner(installation, owner_user_id)
            current = self.store.get_release(tenant_id, installation.release_id)
            target = self.store.get_release(tenant_id, target_release_id)
            if target.owner_user_id not in (None, owner_user_id):
                raise SkillScopeViolation("release belongs to another owner")
            if target.manifest.name != installation.skill_name:
                raise LifecycleConflict("update target has a different Skill name")
            assert_same_lineage(current.manifest.source, target.manifest.source, owner_user_id)
            if target.state is ReleaseState.REVOKED:
                raise ReleaseRevoked("release is revoked")
            if target.scan is not None:
                assert_release_installable(target, target.scan)
            self.resolve_dependencies(tenant_id, owner_user_id, target)
            self._approval(
                approval=approval,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
                action="update",
                release=target,
                capability_digest_value=capability_digest_value,
                policy_revision=policy_revision,
                expected_revision=expected_revision,
                now=timestamp,
                request_id=request_id,
                installation_id=installation_id,
                target_release_id=target_release_id,
                enable_after=enable_after,
            )
            started = begin_update(
                installation,
                current,
                target,
                expected_revision=expected_revision,
                now=timestamp,
            )
            try:
                finished = finish_update(
                    started,
                    target,
                    enable=enable_after,
                    approved_release_digest=approval.content_digest,
                    policy_revision=policy_revision,
                    now=timestamp,
                    suspended=installation.effective is InstallationState.SUSPENDED,
                    was_enabled=installation.effective is InstallationState.ENABLED,
                    previous_policy_revision=installation.policy_revision,
                )
            except Exception:
                self.store.put_installation(abort_update(started, now=timestamp, restore=installation.effective))
                raise
            self.store.put_installation(finished)
            self.store.add_history(tenant_id, finished.id, target.id)
            return finished.id, target.id, {"installation": finished.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="update",
            payload={
                "installationId": installation_id,
                "targetReleaseId": target_release_id,
                "expectedRevision": expected_revision,
                "expectedContentDigest": approval.content_digest,
                "enableAfter": enable_after,
                "policyRevision": request_policy_revision,
                "ttlSeconds": request_ttl_seconds,
            },
            operation=operation,
        )

    def rollback(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        installation_id: str,
        target_release_id: str,
        approval: SkillApproval,
        capability_digest_value: str,
        policy_revision: str,
        expected_revision: int,
        reenable: bool,
        request_id: str,
        now: str | None = None,
        request_policy_revision: str | None = None,
        request_ttl_seconds: int | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            installation = self.store.get_installation(tenant_id, installation_id)
            assert_owner(installation, owner_user_id)
            target = self.store.get_release(tenant_id, target_release_id)
            if target.owner_user_id not in (None, owner_user_id):
                raise SkillScopeViolation("release belongs to another owner")
            if target.manifest.name != installation.skill_name:
                raise LifecycleConflict("rollback target has a different Skill name")
            current = self.store.get_release(tenant_id, installation.release_id)
            assert_same_lineage(current.manifest.source, target.manifest.source, owner_user_id)
            if target.id == installation.release_id:
                raise LifecycleConflict("installation already points at the rollback target")
            if target.scan is not None:
                assert_release_installable(target, target.scan)
            self.resolve_dependencies(tenant_id, owner_user_id, target)
            history = self.store.list_history(tenant_id, installation_id)
            if target_release_id not in history:
                raise LifecycleConflict("rollback target is not in this installation history")
            self._approval(
                approval=approval,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                actor_user_id=actor_user_id,
                action="rollback",
                release=target,
                capability_digest_value=capability_digest_value,
                policy_revision=policy_revision,
                expected_revision=expected_revision,
                now=timestamp,
                request_id=request_id,
                installation_id=installation_id,
                target_release_id=target_release_id,
                reenable=reenable,
            )
            started = begin_rollback(installation, target, expected_revision=expected_revision, now=timestamp)
            finished = finish_rollback(
                started,
                target,
                reenable=reenable,
                policy_revision=policy_revision,
                now=timestamp,
                suspended=installation.effective is InstallationState.SUSPENDED,
                was_enabled=installation.effective is InstallationState.ENABLED,
                previous_policy_revision=installation.policy_revision,
            )
            self.store.put_installation(finished)
            return finished.id, target.id, {"installation": finished.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="rollback",
            payload={
                "installationId": installation_id,
                "targetReleaseId": target_release_id,
                "expectedRevision": expected_revision,
                "expectedContentDigest": approval.content_digest,
                "reenable": reenable,
                "policyRevision": request_policy_revision,
                "ttlSeconds": request_ttl_seconds,
            },
            operation=operation,
        )

    def remove(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        installation_id: str,
        expected_revision: int,
        request_id: str,
        now: str | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            installation = self.store.get_installation(tenant_id, installation_id)
            assert_owner(installation, owner_user_id)
            started = begin_remove(installation, expected_revision=expected_revision, now=timestamp)
            finished = finish_remove(started, now=timestamp)
            self.store.put_installation(finished)
            return finished.id, finished.release_id, {"installation": finished.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="remove",
            payload={"installationId": installation_id, "expectedRevision": expected_revision},
            operation=operation,
        )

    def revoke(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        actor_user_id: str,
        installation_id: str,
        request_id: str,
        now: str | None = None,
    ) -> OperationReceipt:
        timestamp = now or utc_instant()

        def operation() -> tuple[str | None, str | None, dict[str, Any]]:
            installation = self.store.get_installation(tenant_id, installation_id)
            assert_owner(installation, owner_user_id)
            if installation.effective is InstallationState.REVOKED:
                # Repeat revoke is a no-op receipt; do not CAS an unchanged row or bump the revision.
                return installation.id, installation.release_id, {"installation": installation.to_wire()}
            # Revoke the installation first: a concurrent pin then fails closed even if release revocation is
            # still in flight. The enclosing transaction rolls both back if release revocation fails.
            updated = revoke_installation(installation, now=timestamp)
            self.store.put_installation(updated)
            self.store.revoke_release(tenant_id, installation.release_id, timestamp)
            return updated.id, updated.release_id, {"installation": updated.to_wire()}

        return self._receipt(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
            action="revoke",
            payload={"installationId": installation_id},
            operation=operation,
        )

    def catalog(self, tenant_id: str, owner_user_id: str) -> EnabledSkillCatalog:
        catalog = EnabledSkillCatalog()
        for installation in self.store.list_installations(tenant_id, owner_user_id):
            if installation.effective is not InstallationState.ENABLED:
                continue
            release = self.store.get_release(tenant_id, installation.release_id)
            if release.state is ReleaseState.REVOKED:
                continue
            try:
                self.resolve_dependencies(tenant_id, owner_user_id, release)
            except SkillError:
                # A skill whose required dependency is disabled, revoked or moved is not discoverable.
                continue
            catalog.add(installation, release)
        return catalog

    def search(self, tenant_id: str, owner_user_id: str, query: str) -> tuple[CatalogEntry, ...]:
        return self.catalog(tenant_id, owner_user_id).search(query)

    def resolve_dependencies(self, tenant_id: str, owner_user_id: str, release: SkillRelease) -> DependencyResolution:
        """Resolve required dependencies against enabled, non-revoked releases only."""
        enabled: dict[tuple[str, str], SkillRelease] = {}
        enabled_installations: dict[tuple[str, str], SkillInstallation] = {}
        for installation in self.store.list_installations(tenant_id, owner_user_id):
            if installation.effective is not InstallationState.ENABLED:
                continue
            dependency_release = self.store.get_release(tenant_id, installation.release_id)
            if dependency_release.state is ReleaseState.REVOKED:
                continue
            key = (dependency_release.manifest.name, dependency_release.manifest.version)
            enabled[key] = dependency_release
            enabled_installations[key] = installation
        catalog: dict[str, list[str]] = {}
        for name, version in enabled:
            catalog.setdefault(name, []).append(version)

        def edges(name: str, version: str) -> tuple[SkillDependency, ...]:
            return enabled[(name, version)].manifest.dependencies

        resolution = DependencyResolver(catalog, edges).resolve(release.manifest.dependencies)
        exact = tuple(
            DependencyPin(
                skill_name=name,
                release_id=enabled[(name, version)].id,
                content_digest=enabled[(name, version)].content_digest,
                installation_id=enabled_installations[(name, version)].id,
                activation_generation=enabled_installations[(name, version)].activation_generation,
                version=version,
            )
            for name, version in sorted(resolution.pins.items())
        )
        return DependencyResolution(
            pins=resolution.pins,
            order=resolution.order,
            optional_missing=resolution.optional_missing,
            digest=resolution.digest,
            exact=exact,
        )

    def _authorize_run(self, tenant_id: str, run_id: str, installation: SkillInstallation) -> None:
        if self.run_authorizer is None:
            raise SkillScopeViolation("run ownership cannot be verified; refusing an unowned run pin")
        if not self.run_authorizer(tenant_id, run_id, installation.owner_user_id):
            raise SkillScopeViolation("run does not belong to this tenant and owner")

    def _assert_dependencies_current(self, tenant_id: str, owner_user_id: str, pin: RunSkillPin) -> None:
        """Fail closed if any dependency in the snapshot was disabled, revoked, or moved."""
        if dependency_digest({item.skill_name: item.version for item in pin.dependencies}) != pin.dependency_digest:
            raise SkillScopeViolation("pinned dependency digest is inconsistent with the snapshot")
        if dependency_snapshot_digest(pin.dependencies) != pin.dependency_snapshot_digest:
            raise SkillScopeViolation("pinned dependency snapshot digest is inconsistent with the closure")
        names = [item.skill_name for item in pin.dependencies]
        if names != sorted(names) or len(names) != len(set(names)):
            raise SkillScopeViolation("pinned dependency closure is not canonical")
        for dependency in pin.dependencies:
            installation = self.store.get_installation(tenant_id, dependency.installation_id)
            if installation.owner_user_id != owner_user_id:
                raise SkillScopeViolation("pinned dependency belongs to another owner")
            release = self.store.get_release(tenant_id, dependency.release_id)
            if installation.effective is not InstallationState.ENABLED or release.state is ReleaseState.REVOKED:
                raise ReleaseRevoked("pinned dependency is no longer enabled")
            if (
                installation.release_id != dependency.release_id
                or installation.skill_name != dependency.skill_name
                or installation.activation_generation != dependency.activation_generation
                or release.content_digest != dependency.content_digest
                or release.manifest.name != dependency.skill_name
                or release.manifest.version != dependency.version
            ):
                raise SkillScopeViolation("pinned dependency was updated or replaced")

    def _assert_live_installation(self, installation: SkillInstallation, pin: RunSkillPin) -> None:
        if installation.effective is not InstallationState.ENABLED:
            raise ReleaseRevoked("pinned Skill is no longer enabled")
        # disable/remove/revoke increment this epoch; re-enabling must not revive an earlier Run's pin.
        if installation.revocation_epoch != pin.revocation_epoch:
            raise ReleaseRevoked("pinned Skill was revoked after this Run started")
        if installation.policy_revision != pin.policy_revision:
            raise ReleaseRevoked("pinned Skill policy revision changed")

    def pin_for_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        installation_id: str,
        capability_digest_value: str,
        dependency_digest_value: str,
        now: str | None = None,
    ) -> RunSkillPin:
        timestamp = now or utc_instant()
        with self.store.atomic(tenant_id):
            installation = self.store.get_installation(tenant_id, installation_id)
            self._authorize_run(tenant_id, run_id, installation)
            existing = self.store.get_pin(tenant_id, run_id, installation_id)
            if existing is not None:
                if (
                    existing.capability_digest != capability_digest_value
                    or existing.dependency_digest != dependency_digest_value
                    or existing.activation_generation != installation.activation_generation
                    or existing.revocation_epoch != installation.revocation_epoch
                    or existing.policy_revision != installation.policy_revision
                    or existing.release_id != installation.release_id
                ):
                    raise LifecycleConflict("run pin no longer matches the live installation")
                self._assert_dependencies_current(tenant_id, installation.owner_user_id, existing)
                return existing
            release = self.store.get_release(tenant_id, installation.release_id)
            if installation.effective is not InstallationState.ENABLED:
                raise ReleaseRevoked("installation is not enabled")
            if release.state is ReleaseState.REVOKED:
                raise ReleaseRevoked("release is revoked")
            catalog = self.catalog(tenant_id, installation.owner_user_id)
            if installation.skill_name not in {entry.name for entry in catalog.all_entries()}:
                raise SkillScopeViolation("Skill is not enabled")
            requested = validate_requested(release.manifest.requested_capabilities)
            if capability_digest_value != capability_digest(requested):
                raise SkillScopeViolation("pinned capability digest does not match the release request")
            resolution = self.resolve_dependencies(tenant_id, installation.owner_user_id, release)
            if dependency_digest_value != resolution.digest:
                raise SkillScopeViolation("pinned dependency digest does not match the enabled dependency set")
            pin = create_run_pin(
                tenant_id=tenant_id,
                run_id=run_id,
                installation=installation,
                release=release,
                capability_digest=capability_digest_value,
                dependency_digest=dependency_digest_value,
                skill_surface_digest=catalog.surface_digest(),
                dependencies=resolution.exact,
                now=timestamp,
            )
            self.store.put_pin(pin)
            return pin

    def load_pinned_body(
        self,
        *,
        tenant_id: str,
        run_id: str,
        installation_id: str,
        offset: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> SkillText:
        """Read one pinned body from quarantine; callers cannot substitute file bytes."""
        installation = self.store.get_installation(tenant_id, installation_id)
        pin = self.store.get_pin(tenant_id, run_id, installation_id)
        if pin is None:
            raise SkillScopeViolation("run has no Skill pin")
        self._authorize_run(tenant_id, run_id, installation)
        # The pin names the exact release this Run started with. An update must not silently swap the body.
        release = self.store.get_release(tenant_id, pin.release_id)
        self._assert_live_installation(installation, pin)
        if release.state is ReleaseState.REVOKED:
            raise ReleaseRevoked("release is revoked")
        self._assert_dependencies_current(tenant_id, installation.owner_user_id, pin)
        if self.quarantine is None:
            raise QuarantineUnavailable("quarantine is not configured")
        bundle_digest_value = str(release.manifest.provenance.get("bundleDigest", ""))
        if not bundle_digest_value:
            raise QuarantineUnavailable("release has no quarantined bundle")
        files = self.quarantine.read(bundle_digest_value)
        return self.load_body(
            tenant_id=tenant_id,
            run_id=run_id,
            installation_id=installation_id,
            files=files,
            offset=offset,
            max_bytes=max_bytes,
            pinned_release=release,
        )

    def load_body(
        self,
        *,
        tenant_id: str,
        run_id: str,
        installation_id: str,
        files: dict[str, bytes],
        offset: int = 0,
        max_bytes: int = 16 * 1024,
        pinned_release: SkillRelease | None = None,
    ) -> SkillText:
        installation = self.store.get_installation(tenant_id, installation_id)
        release = pinned_release or self.store.get_release(tenant_id, installation.release_id)
        pin = self.store.get_pin(tenant_id, run_id, installation_id)
        if pin is None:
            raise SkillScopeViolation("run has no Skill pin")
        self._authorize_run(tenant_id, run_id, installation)
        self._assert_live_installation(installation, pin)
        if release.state is ReleaseState.REVOKED:
            raise ReleaseRevoked("pinned Skill is revoked")
        verify_run_pin(pin, installation=installation, release=release, snapshot=True)
        self._assert_dependencies_current(tenant_id, installation.owner_user_id, pin)
        assert_manifest_consistent(release, files)
        backend = ImmutableSkillBackend(files)
        body = backend.read_bytes(release.manifest.entry)
        return disclose_text(
            skill_id=release.id,
            path=release.manifest.entry,
            body=body,
            content_digest_value=release.content_digest,
            offset=offset,
            max_bytes=max_bytes,
        )


def assert_owner(installation: SkillInstallation, owner_user_id: str) -> None:
    if installation.owner_user_id != owner_user_id:
        raise SkillScopeViolation("installation belongs to another owner")


def make_approval(
    *,
    tenant_id: str,
    owner_user_id: str,
    actor_user_id: str,
    action: str,
    release: SkillRelease,
    capability_digest_value: str,
    policy_revision: str,
    request_id: str,
    expected_installation_revision: int,
    now: str | None = None,
    ttl_seconds: int = 3600,
    installation_id: str | None = None,
    target_release_id: str | None = None,
    enable_after: bool = False,
    reenable: bool = False,
) -> SkillApproval:
    return create_approval(
        approval_id=uuid7(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        action=action,
        release=release,
        capability_digest=capability_digest_value,
        policy_revision=policy_revision,
        request_id=request_id,
        expected_installation_revision=expected_installation_revision,
        now=now or utc_instant(),
        ttl_seconds=ttl_seconds,
        installation_id=installation_id,
        target_release_id=target_release_id,
        enable_after=enable_after,
        reenable=reenable,
    )


__all__ = [
    "InMemorySkillStore",
    "SkillRegistry",
    "SkillStore",
    "assert_owner",
    "make_approval",
]
