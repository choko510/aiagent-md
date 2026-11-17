"""Real subprocess pipe tests; these do not establish Cell isolation."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from aiagent_sandbox.supervisor import MAX_OUTPUT, _bounded_output

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="process groups require Linux")


def test_bounded_simultaneous_output() -> None:
    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; [(os.write(1,b'x'*8192),os.write(2,b'y'*8192)) for _ in range(10000)]",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, err, timeout, truncated = await asyncio.wait_for(_bounded_output(proc, 5), 10)
        assert len(out) <= MAX_OUTPUT and len(err) <= MAX_OUTPUT
        assert truncated and not timeout and proc.returncode is not None

    asyncio.run(run())


def test_output_timeout_and_cancellation_reap_process() -> None:
    async def run() -> None:
        async def start() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

        proc = await start()
        _, _, timed_out, truncated = await asyncio.wait_for(_bounded_output(proc, 0.1), 5)
        assert timed_out and not truncated and proc.returncode is not None
        proc = await start()
        task = asyncio.create_task(_bounded_output(proc, 30))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)

    asyncio.run(run())


def test_exact_limit_is_not_truncated() -> None:
    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            f"import os; os.write(1,b'x'*{MAX_OUTPUT})",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, err, timed_out, truncated = await asyncio.wait_for(_bounded_output(proc, 5), 10)
        assert len(out) == MAX_OUTPUT and not err and not timed_out and not truncated

    asyncio.run(run())


@pytest.mark.parametrize("fd", [1, 2])
def test_one_stream_just_over_limit(fd: int) -> None:
    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            f"import os; os.write({fd},b'x'*{MAX_OUTPUT + 1})",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, err, timed_out, truncated = await asyncio.wait_for(_bounded_output(proc, 5), 10)
        assert truncated and not timed_out
        assert len(out if fd == 1 else err) == MAX_OUTPUT

    asyncio.run(run())


def test_nonzero_exit_is_retained() -> None:
    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "raise SystemExit(7)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        _, _, timed_out, truncated = await asyncio.wait_for(_bounded_output(proc, 5), 10)
        assert proc.returncode == 7 and not timed_out and not truncated

    asyncio.run(run())


def test_revocation_kills_process_and_discards_partial_output() -> None:
    from aiagent_sandbox.supervisor import _run_with_revocation

    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; print('partial',flush=True); time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        revoked = asyncio.Event()
        task = asyncio.create_task(_run_with_revocation(proc, 20, revoked))
        await asyncio.sleep(0.1)
        revoked.set()
        out, err, timed_out, truncated, was_revoked = await asyncio.wait_for(task, 5)
        assert was_revoked and out == err == b"" and not timed_out and not truncated
        assert proc.returncode is not None

    asyncio.run(run())
