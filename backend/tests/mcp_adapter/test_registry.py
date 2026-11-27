from __future__ import annotations

from datetime import timedelta

import pytest
from aiagent_mcp_adapter.contracts import utc_now
from aiagent_mcp_adapter.errors import ErrorCode, McpError
from aiagent_mcp_adapter.registry_snapshot import SnapshotKey, SnapshotStore, ref_from_entry

from .helpers import entry, limits, snapshot


async def test_publish_is_cas_and_monotonic() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    with pytest.raises(McpError) as err:
        await store.publish(snapshot(entry(generation=2), generation=2), expected_generation=0)
    assert err.value.code is ErrorCode.BINDING_MISMATCH
    second = snapshot(entry(generation=2), generation=2)
    await store.publish(second, expected_generation=1)
    assert store.current(SnapshotKey("tenant-a", "owner-a", "demo")).generation == 2


async def test_stale_generation_ref_is_rejected_after_publish() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    ref = ref_from_entry(first.entries[0])
    assert store.resolve(ref).alias == first.entries[0].alias
    await store.publish(snapshot(entry(generation=2), generation=2), expected_generation=1)
    with pytest.raises(McpError) as err:
        store.resolve(ref)
    assert err.value.code is ErrorCode.STALE_TOOL_REF


async def test_pinned_run_completes_on_its_generation_but_revocation_stops_it() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    ref = ref_from_entry(first.entries[0])
    await store.pin_run(first, run_id="run-1")
    await store.publish(snapshot(entry(generation=2), generation=2), expected_generation=1)
    assert store.resolve_pinned("run-1", ref).tool_id == ref.tool_id
    await store.revoke_run("run-1")
    with pytest.raises(McpError) as err:
        store.resolve_pinned("run-1", ref)
    assert err.value.code is ErrorCode.POLICY_DENIED


async def test_provider_revocation_invalidates_pins() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    ref = ref_from_entry(first.entries[0])
    await store.pin_run(first, run_id="run-2")
    await store.revoke_provider(SnapshotKey("tenant-a", "owner-a", "demo"))
    with pytest.raises(McpError):
        store.resolve_pinned("run-2", ref)
    with pytest.raises(McpError):
        store.resolve(ref)


async def test_expired_snapshot_and_pin_are_rejected() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry(), ttl_s=1.0)
    await store.publish(first, expected_generation=0)
    ref = ref_from_entry(first.entries[0])
    await store.pin_run(first, run_id="run-3", ttl_s=1.0)
    later = utc_now() + timedelta(seconds=5)
    with pytest.raises(McpError) as err:
        store.resolve(ref, now=later)
    assert err.value.code is ErrorCode.SNAPSHOT_EXPIRED
    with pytest.raises(McpError) as err:
        store.resolve_pinned("run-3", ref, now=later)
    assert err.value.code is ErrorCode.SNAPSHOT_EXPIRED


async def test_schema_mutation_is_detected_at_resolve() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    ref = ref_from_entry(first.entries[0])
    key = SnapshotKey("tenant-a", "owner-a", "demo")
    stored = store._current[key]  # integrity test: simulate in-place corruption of the stored snapshot
    stored.entries[0].input_schema["properties"]["injected"] = {"type": "string"}
    with pytest.raises(McpError) as err:
        store.resolve(ref)
    assert err.value.code in (ErrorCode.SCHEMA_INVALID, ErrorCode.STALE_TOOL_REF)


async def test_run_pin_is_immutable_and_revocation_survives_sweep() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    await store.pin_run(first, run_id="run-immutable")
    second = snapshot(entry(generation=2), generation=2)
    await store.publish(second, expected_generation=1)
    with pytest.raises(McpError) as err:
        await store.pin_run(second, run_id="run-immutable")
    assert err.value.code is ErrorCode.BINDING_MISMATCH

    await store.revoke_run("run-immutable")
    later = utc_now() + timedelta(seconds=5)
    store.sweep(now=later)
    with pytest.raises(McpError) as err:
        await store.pin_run(first, run_id="run-immutable")
    assert err.value.code is ErrorCode.POLICY_DENIED


async def test_revoked_provider_requires_explicit_admin_reenable() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    key = SnapshotKey("tenant-a", "owner-a", "demo")
    await store.revoke_provider(key)
    with pytest.raises(McpError) as err:
        await store.publish(snapshot(entry(), generation=1), expected_generation=0)
    assert err.value.code is ErrorCode.POLICY_DENIED
    with pytest.raises(McpError):
        await store.pin_run(first, run_id="run-after-revoke")
    await store.unrevoke_provider(key)
    await store.publish(snapshot(entry(generation=2), generation=2), expected_generation=1)


async def test_generation_never_restarts_after_ttl_expiry() -> None:
    store = SnapshotStore(limits())
    first = snapshot(entry())
    await store.publish(first, expected_generation=0)
    later = utc_now() + timedelta(seconds=601)
    assert store.sweep(now=later) == 1
    with pytest.raises(McpError) as err:
        await store.publish(snapshot(entry()), expected_generation=0)
    assert err.value.code is ErrorCode.BINDING_MISMATCH
    await store.publish(snapshot(entry(generation=2), generation=2), expected_generation=1)


async def test_expired_pin_is_not_silently_re_pinned() -> None:
    store = SnapshotStore(limits())
    snap = snapshot(entry())
    await store.publish(snap, expected_generation=0)
    start = utc_now()
    await store.pin_run(snap, run_id="run-expiring", ttl_s=1.0, now=start)
    with pytest.raises(McpError) as err:
        await store.pin_run(snap, run_id="run-expiring", ttl_s=1.0, now=start + timedelta(seconds=2))
    assert err.value.code is ErrorCode.SNAPSHOT_EXPIRED
    assert store.sweep(now=start + timedelta(seconds=2)) == 0
    with pytest.raises(McpError) as err2:
        await store.pin_run(snap, run_id="run-expiring", ttl_s=1.0, now=start + timedelta(seconds=3))
    assert err2.value.code is ErrorCode.SNAPSHOT_EXPIRED
