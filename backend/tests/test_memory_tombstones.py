"""Pure local tests for the external Memory tombstone log and sealed watermark."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest
from aiagent_core.memory_tombstones import GENESIS, TombstoneError, TombstoneLog


def _append(log: TombstoneLog, n: int):
    return log.append(
        kind="revoked",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        memory_id=f"memory-{n}",
        revision=2,
        recorded_at=f"2026-11-12T00:00:{n:02d}+00:00",
    )


def test_initialization_and_sealed_hmac_chain(tmp_path: Path) -> None:
    key = b"t" * 32
    log = TombstoneLog(tmp_path / "memory.jsonl", integrity_key=key)
    log.initialize()
    assert log.head() == (0, GENESIS)
    first = _append(log, 1)
    assert first.generation == 1
    assert log.head() == (1, first.chain_head)
    assert TombstoneLog(log.path, integrity_key=key).read() == [first]
    with pytest.raises(TombstoneError, match="authentication failed|HMAC chain broken"):
        TombstoneLog(log.path, integrity_key=b"x" * 32).read()


def _append_process(path: str, key: bytes, n: int) -> None:
    _append(TombstoneLog(path, integrity_key=key), n)


def test_concurrent_appends_are_serialized_and_contiguous(tmp_path: Path) -> None:
    log = TombstoneLog(tmp_path / "memory.jsonl", integrity_key=b"k" * 32)
    log.initialize()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: _append(log, i), range(1, 41)))
    entries = log.read()
    assert len(entries) == 40
    assert [e.generation for e in entries] == list(range(1, 41))
    assert log.head() == (40, entries[-1].chain_head)


def test_missing_or_rolled_back_external_head_fails_closed(tmp_path: Path) -> None:
    log = TombstoneLog(tmp_path / "memory.jsonl", integrity_key=b"z" * 32)
    log.initialize()
    first = _append(log, 1)
    log.head_path.unlink()
    with pytest.raises(TombstoneError, match="head seal is missing"):
        log.read()
    # A missing seal does not prove this is an interrupted final append; operator
    # recovery must fetch the independently retained seal/log instead.
    with pytest.raises(TombstoneError, match="head seal is missing"):
        log.reseal_validated()
    assert first.generation == 1


def test_crash_between_log_fsync_and_head_replace_requires_operator_reseal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = TombstoneLog(tmp_path / "memory.jsonl", integrity_key=b"r" * 32)
    log.initialize()

    def fail_seal(generation: int, chain_head: str) -> None:
        raise OSError("simulated power loss before sidecar replace")

    monkeypatch.setattr(log, "_write_seal_unlocked", fail_seal)
    with pytest.raises(OSError, match="simulated power loss"):
        _append(log, 1)
    with pytest.raises(TombstoneError, match="high-watermark does not match"):
        log.read()
    monkeypatch.undo()
    assert log.reseal_validated()[0] == 1
    assert len(log.read()) == 1


def test_reinitialize_never_overwrites_existing_tombstones(tmp_path: Path) -> None:
    log = TombstoneLog(tmp_path / "memory.jsonl", integrity_key=b"a" * 32)
    log.initialize()
    _append(log, 1)
    with pytest.raises(TombstoneError, match="refusing reinitialize"):
        log.initialize()
    assert len(log.read()) == 1


def test_truncated_log_cannot_be_resealed_from_a_valid_prefix(tmp_path: Path) -> None:
    log = TombstoneLog(tmp_path / "memory.jsonl", integrity_key=b"q" * 32)
    log.initialize()
    _append(log, 1)
    _append(log, 2)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text(lines[0] + "\n", encoding="utf-8")
    with pytest.raises(TombstoneError, match="high-watermark does not match"):
        log.read()
    with pytest.raises(TombstoneError, match="generation gap"):
        log.reseal_validated()


def test_separate_process_appends_share_the_file_lock(tmp_path: Path) -> None:
    path = tmp_path / "multi-process.jsonl"
    key = b"p" * 32
    log = TombstoneLog(path, integrity_key=key)
    log.initialize()
    ctx = get_context("spawn")
    processes = [ctx.Process(target=_append_process, args=(str(path), key, i)) for i in range(1, 7)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    assert all(not process.is_alive() and process.exitcode == 0 for process in processes)
    entries = log.read()
    assert [entry.generation for entry in entries] == list(range(1, 7))


def test_operator_init_command_creates_genesis_without_database_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiagent_core import admin

    log_path = tmp_path / "operator-log.jsonl"
    monkeypatch.setenv("AIAGENT_PROFILE", "test")
    monkeypatch.setenv("AIAGENT_MIGRATE_DSN", "postgresql://unused")
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_LOG", str(log_path))
    monkeypatch.setenv("AIAGENT_MEMORY_TOMBSTONE_HMAC_KEY", (b"h" * 32).hex())
    assert admin.main(["memory-tombstone-init"]) == 0
    assert TombstoneLog(log_path, integrity_key=b"h" * 32).head() == (0, GENESIS)
