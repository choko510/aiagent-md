"""Generation-fenced, immutable registry snapshots.

Only this store may turn a ToolRef into an executable entry. A ref is accepted only if its generation,
schema digest, policy revision and credential version all match the exact snapshot it names. There is
no fallback to an older generation for new calls; a run may complete on the generation it pinned, and
revocation invalidates both.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import AdapterLimits
from .contracts import RegistrySnapshot, ToolEntry, ToolRef, compute_surface_digest, utc_now
from .errors import ErrorCode, McpError


@dataclass(frozen=True, order=True)
class SnapshotKey:
    tenant_id: str
    owner_user_id: str
    provider_id: str


@dataclass(frozen=True)
class RunPin:
    run_id: str
    key: SnapshotKey
    generation: int
    surface_digest: str
    policy_revision: int
    credential_version: int
    pinned_at: datetime
    expires_at: datetime
    endpoint_fingerprint: str = ""
    invalidation_epoch: int = 0
    revoked: bool = False


@dataclass
class SnapshotStore:
    limits: AdapterLimits
    _current: dict[SnapshotKey, RegistrySnapshot] = field(default_factory=dict)
    _history: dict[SnapshotKey, list[RegistrySnapshot]] = field(default_factory=dict)
    _pins: dict[str, RunPin] = field(default_factory=dict)
    _revoked_keys: set[SnapshotKey] = field(default_factory=set)
    _last_generation: dict[SnapshotKey, int] = field(default_factory=dict)
    _invalidation_epoch: dict[SnapshotKey, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history_depth: int = 4

    async def publish(self, snapshot: RegistrySnapshot, *, expected_generation: int) -> RegistrySnapshot:
        key = SnapshotKey(snapshot.tenant_id, snapshot.owner_user_id, snapshot.provider_id)
        if snapshot.is_expired():
            raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "cannot_publish_expired_snapshot")
        async with self._lock:
            if key in self._revoked_keys:
                raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked")
            _validate_snapshot(snapshot)
            current = self._live(key)
            if snapshot.invalidation_epoch != self._invalidation_epoch.get(key, 0):
                # The scan that produced this snapshot began before an invalidation. Publishing it
                # would resurrect exactly the surface that list_changed/revocation fenced.
                raise McpError(ErrorCode.BINDING_MISMATCH, "stale_scan_invalidation_epoch")
            last_generation = self._last_generation.get(key, 0)
            if current is None:
                # Expiry, revocation and a fresh process must never restart the sequence at 1.
                if expected_generation != last_generation or snapshot.generation != last_generation + 1:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "generation_cas_failed")
            else:
                if current.generation != expected_generation:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "generation_cas_failed")
                if snapshot.generation != current.generation + 1:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "generation_not_monotonic")
            frozen = snapshot.model_copy(deep=True)
            self._current[key] = frozen
            self._last_generation[key] = frozen.generation
            history = self._history.setdefault(key, [])
            history.append(frozen)
            del history[: max(0, len(history) - self.history_depth)]
            return frozen.model_copy(deep=True)

    def current(self, key: SnapshotKey, *, now: datetime | None = None) -> RegistrySnapshot | None:
        snapshot = self._live(key, now=now)
        return snapshot.model_copy(deep=True) if snapshot is not None else None

    def _live(self, key: SnapshotKey, *, now: datetime | None = None) -> RegistrySnapshot | None:
        snapshot = self._current.get(key)
        if snapshot is None:
            return None
        if snapshot.is_expired(now):
            self._current.pop(key, None)
            self._invalidation_epoch[key] = self._invalidation_epoch.get(key, 0) + 1
            return None
        return snapshot

    def resolve(self, ref: ToolRef, *, now: datetime | None = None) -> ToolEntry:
        key = SnapshotKey(ref.tenant_id, ref.owner_user_id, ref.provider_id)
        snapshot = self._live(key, now=now)
        if snapshot is None:
            raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "snapshot_missing_or_expired")
        if snapshot.generation != ref.generation:
            raise McpError(ErrorCode.STALE_TOOL_REF, "generation_mismatch")
        return _entry_for(snapshot, ref)

    async def pin_run(
        self,
        snapshot: RegistrySnapshot,
        *,
        run_id: str,
        ttl_s: float | None = None,
        now: datetime | None = None,
    ) -> RunPin:
        if not run_id:
            raise McpError(ErrorCode.BINDING_MISMATCH, "run_id_required")
        _validate_snapshot(snapshot)
        key = SnapshotKey(snapshot.tenant_id, snapshot.owner_user_id, snapshot.provider_id)
        pinned_at = now or utc_now()
        pin = RunPin(
            run_id=run_id,
            key=SnapshotKey(snapshot.tenant_id, snapshot.owner_user_id, snapshot.provider_id),
            generation=snapshot.generation,
            surface_digest=snapshot.surface_digest,
            policy_revision=snapshot.policy_revision,
            credential_version=snapshot.credential_version,
            pinned_at=pinned_at,
            expires_at=pinned_at + timedelta(seconds=ttl_s or self.limits.snapshot_ttl_s),
            endpoint_fingerprint=snapshot.endpoint_fingerprint,
            invalidation_epoch=snapshot.invalidation_epoch,
        )
        async with self._lock:
            existing = self._pins.get(run_id)
            if pin.key in self._revoked_keys:
                raise McpError(ErrorCode.POLICY_DENIED, "provider_revoked")
            if existing is not None:
                if existing.revoked:
                    raise McpError(ErrorCode.POLICY_DENIED, "run_revoked")
                if existing.key != pin.key:
                    raise McpError(ErrorCode.BINDING_MISMATCH, "run_scope_mismatch")
                if (
                    existing.generation != pin.generation
                    or existing.surface_digest != pin.surface_digest
                    or existing.endpoint_fingerprint != pin.endpoint_fingerprint
                ):
                    # A run must never be silently re-pinned to a newer tool surface or endpoint.
                    raise McpError(ErrorCode.BINDING_MISMATCH, "run_pin_immutable")
                if pinned_at >= existing.expires_at:
                    raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "run_pin_expired")
                return existing
            current = self._live(key, now=pinned_at)
            if (
                current is None
                or current.generation != snapshot.generation
                or current.surface_digest != snapshot.surface_digest
                or current.endpoint_fingerprint != snapshot.endpoint_fingerprint
                or current.invalidation_epoch != snapshot.invalidation_epoch
            ):
                raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "run_pin_requires_current_snapshot")
            self._pins[run_id] = pin
        return pin

    def resolve_pinned(self, run_id: str, ref: ToolRef, *, now: datetime | None = None) -> ToolEntry:
        pin = self._pins.get(run_id)
        if pin is None:
            raise McpError(ErrorCode.BINDING_MISMATCH, "run_not_pinned")
        moment = now or utc_now()
        if pin.revoked:
            raise McpError(ErrorCode.POLICY_DENIED, "run_revoked")
        if moment >= pin.expires_at:
            raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "run_pin_expired")
        key = SnapshotKey(ref.tenant_id, ref.owner_user_id, ref.provider_id)
        if key != pin.key:
            raise McpError(ErrorCode.BINDING_MISMATCH, "run_scope_mismatch")
        if ref.generation != pin.generation:
            raise McpError(ErrorCode.STALE_TOOL_REF, "generation_mismatch")
        snapshot = self._snapshot_for_generation(pin.key, pin.generation)
        if (
            snapshot is None
            or snapshot.surface_digest != pin.surface_digest
            or snapshot.endpoint_fingerprint != pin.endpoint_fingerprint
        ):
            raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "pinned_snapshot_missing")
        if snapshot.is_expired(moment):
            raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "pinned_snapshot_expired")
        # A pure same-descriptor refresh may keep an old pin executable. A deletion, descriptor
        # change, policy/credential change or invalidation epoch change must not.
        current = self._live(pin.key, now=moment)
        if current is None:
            raise McpError(ErrorCode.SNAPSHOT_EXPIRED, "current_snapshot_missing")
        if current.invalidation_epoch != pin.invalidation_epoch:
            raise McpError(ErrorCode.POLICY_DENIED, "pin_invalidated_epoch")
        pinned_entry = _entry_for(snapshot, ref)
        current_entry = _entry_for(current, ref)
        if (
            current_entry.tool_id != pinned_entry.tool_id
            or current_entry.source_name != pinned_entry.source_name
            or current_entry.descriptor_revision != pinned_entry.descriptor_revision
            or current_entry.schema_digest() != pinned_entry.schema_digest()
        ):
            raise McpError(ErrorCode.STALE_TOOL_REF, "tool_removed_or_changed")
        return pinned_entry

    async def invalidate_pins(self, key: SnapshotKey) -> int:
        """Fence active runs and the current surface. A new Run must re-discover and re-pin."""
        count = 0
        async with self._lock:
            self._invalidation_epoch[key] = self._invalidation_epoch.get(key, 0) + 1
            self._current.pop(key, None)
            for run_id, pin in list(self._pins.items()):
                if pin.key == key and not pin.revoked:
                    self._pins[run_id] = RunPin(
                        run_id=pin.run_id,
                        key=pin.key,
                        generation=pin.generation,
                        surface_digest=pin.surface_digest,
                        policy_revision=pin.policy_revision,
                        credential_version=pin.credential_version,
                        pinned_at=pin.pinned_at,
                        expires_at=pin.expires_at,
                        endpoint_fingerprint=pin.endpoint_fingerprint,
                        invalidation_epoch=pin.invalidation_epoch,
                        revoked=True,
                    )
                    count += 1
        return count

    def invalidation_epoch(self, key: SnapshotKey) -> int:
        return self._invalidation_epoch.get(key, 0)

    def pinned_endpoint_fingerprint(self, run_id: str, ref: ToolRef) -> str:
        pin = self._pins.get(run_id)
        if pin is None:
            raise McpError(ErrorCode.BINDING_MISMATCH, "run_not_pinned")
        if pin.key != SnapshotKey(ref.tenant_id, ref.owner_user_id, ref.provider_id):
            raise McpError(ErrorCode.BINDING_MISMATCH, "run_scope_mismatch")
        if pin.revoked:
            raise McpError(ErrorCode.POLICY_DENIED, "run_revoked")
        return pin.endpoint_fingerprint

    def _snapshot_for_generation(self, key: SnapshotKey, generation: int) -> RegistrySnapshot | None:
        for snapshot in reversed(self._history.get(key, [])):
            if snapshot.generation == generation:
                return snapshot
        return None

    async def revoke_run(self, run_id: str) -> None:
        async with self._lock:
            pin = self._pins.get(run_id)
            if pin is not None:
                self._pins[run_id] = RunPin(
                    run_id=pin.run_id,
                    key=pin.key,
                    generation=pin.generation,
                    surface_digest=pin.surface_digest,
                    policy_revision=pin.policy_revision,
                    credential_version=pin.credential_version,
                    pinned_at=pin.pinned_at,
                    expires_at=pin.expires_at,
                    endpoint_fingerprint=pin.endpoint_fingerprint,
                    invalidation_epoch=pin.invalidation_epoch,
                    revoked=True,
                )

    async def revoke_provider(self, key: SnapshotKey) -> None:
        async with self._lock:
            self._revoked_keys.add(key)
            self._invalidation_epoch[key] = self._invalidation_epoch.get(key, 0) + 1
            self._current.pop(key, None)
            for run_id, pin in list(self._pins.items()):
                if pin.key == key:
                    self._pins[run_id] = RunPin(
                        run_id=pin.run_id,
                        key=pin.key,
                        generation=pin.generation,
                        surface_digest=pin.surface_digest,
                        policy_revision=pin.policy_revision,
                        credential_version=pin.credential_version,
                        pinned_at=pin.pinned_at,
                        expires_at=pin.expires_at,
                        endpoint_fingerprint=pin.endpoint_fingerprint,
                        invalidation_epoch=pin.invalidation_epoch,
                        revoked=True,
                    )

    async def unrevoke_provider(self, key: SnapshotKey) -> None:
        """Explicit admin re-enable; publishing after revocation otherwise fails closed."""
        async with self._lock:
            self._revoked_keys.discard(key)

    def sweep(self, *, now: datetime | None = None) -> int:
        moment = now or utc_now()
        removed = 0
        for key, snapshot in list(self._current.items()):
            if snapshot.is_expired(moment):
                self._current.pop(key, None)
                self._invalidation_epoch[key] = self._invalidation_epoch.get(key, 0) + 1
                removed += 1
        # Keep expired and revoked tombstones. Removing one would let a later prepare() re-pin
        # the same run identifier after TTL expiry. A separate retention job may prune them only
        # after the run is terminally settled.
        return removed


def _entry_for(snapshot: RegistrySnapshot, ref: ToolRef) -> ToolEntry:
    entry = snapshot.entry_by_tool_id(ref.tool_id)
    if entry.alias != ref.alias:
        raise McpError(ErrorCode.STALE_TOOL_REF, "alias_mismatch")
    if entry.descriptor_revision != ref.descriptor_revision:
        raise McpError(ErrorCode.STALE_TOOL_REF, "descriptor_revision_mismatch")
    if entry.schema_digest() != ref.schema_digest:
        raise McpError(ErrorCode.STALE_TOOL_REF, "schema_digest_mismatch")
    if entry.policy_revision != ref.policy_revision or snapshot.policy_revision != ref.policy_revision:
        raise McpError(ErrorCode.STALE_TOOL_REF, "policy_revision_mismatch")
    if entry.credential_version != ref.credential_version or snapshot.credential_version != ref.credential_version:
        raise McpError(ErrorCode.CREDENTIAL_STALE, "credential_version_mismatch")
    if entry.risk_class != ref.risk_class:
        raise McpError(ErrorCode.POLICY_DENIED, "risk_class_mismatch")
    entry.verify_digests()
    return entry.model_copy(deep=True)


def ref_from_entry(entry: ToolEntry) -> ToolRef:
    return ToolRef(
        tenant_id=entry.tenant_id,
        owner_user_id=entry.owner_user_id,
        provider_id=entry.provider_id,
        tool_id=entry.tool_id,
        alias=entry.alias,
        generation=entry.generation,
        descriptor_revision=entry.descriptor_revision,
        schema_digest=entry.schema_digest(),
        risk_class=entry.risk_class,
        policy_revision=entry.policy_revision,
        credential_version=entry.credential_version,
    )


def _validate_snapshot(snapshot: RegistrySnapshot) -> None:
    if not snapshot.endpoint_fingerprint:
        raise McpError(ErrorCode.BINDING_MISMATCH, "endpoint_fingerprint_missing")
    if compute_surface_digest(snapshot.entries) != snapshot.surface_digest:
        raise McpError(ErrorCode.SCHEMA_INVALID, "surface_digest_mismatch")
    for entry in snapshot.entries:
        if (
            entry.tenant_id != snapshot.tenant_id
            or entry.owner_user_id != snapshot.owner_user_id
            or entry.provider_id != snapshot.provider_id
            or entry.generation != snapshot.generation
        ):
            raise McpError(ErrorCode.BINDING_MISMATCH, "entry_scope_mismatch")
        if entry.policy_revision != snapshot.policy_revision:
            raise McpError(ErrorCode.BINDING_MISMATCH, "policy_revision_mismatch")
        if entry.credential_version != snapshot.credential_version:
            raise McpError(ErrorCode.CREDENTIAL_STALE, "credential_version_mismatch")
        entry.verify_digests()
