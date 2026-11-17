"""STEP 3-E Runtime Cell enforcement - Linux kernel features (run in a Linux container or the Untrusted VM).

docker run --rm --network none --cap-add SYS_ADMIN --security-opt seccomp=unconfined -v <backend>:/w -w /w \
  python:3.13-slim sh -c "pip -q install pydantic jsonschema pytest && PYTHONPATH=packages/domain/src:packages/sandbox/src \
  python -m pytest -q tests/linux"
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs Linux kernel (rlimits/namespaces)")

from aiagent_domain import uuid7  # noqa: E402
from aiagent_sandbox.grant import Grant, GrantError, sign  # noqa: E402

KEY = os.urandom(32)


def grant(
    argv: list[str],
    cwd: str = ".",
    *,
    wall_ms: int = 5000,
    mem: int = 256 * 1024 * 1024,
    ttl: int = 60,
    projected: bool = False,
) -> dict:
    return sign(
        KEY,
        Grant(
            uuid.uuid4().hex,
            "t1",
            "task1",
            "run1",
            "intent1",
            3,
            tuple(argv),
            cwd,
            wall_ms,
            mem,
            int(time.time()) + ttl,
            attempt_id=uuid.uuid4().hex,
            attempt_binding_version=1,
            execution_binding_version=1 if projected else None,
            execution_id=uuid7() if projected else None,
            execution_binding_sha256="a" * 64 if projected else None,
        ),
    )


def sup(tmp_path: Path, netns: bool = True):  # noqa: ANN201
    from aiagent_sandbox.supervisor import Limits, Supervisor

    uid = 20001 if os.geteuid() == 0 else None  # dedicated: RLIMIT_NPROC is per-uid kernel-wide
    return Supervisor(
        tmp_path / "cells", KEY, limits=Limits(cpu_s=5, nproc=32), require_netns=netns, cell_uid=uid, cell_gid=uid
    )


def run(c):  # noqa: ANN001, ANN201
    return asyncio.run(c)


def test_runs_command_in_workspace_with_scrubbed_env(tmp_path: Path) -> None:
    os.environ["AIAGENT_SECRET_SHOULD_NOT_LEAK"] = "s3cr3t"
    s = sup(tmp_path)
    argv = [sys.executable, "-c", "import os; print(os.getcwd()); print(sorted(os.environ)); print(6*7)"]
    out = run(s.execute(grant(argv), argv, "."))
    assert out["status"] == "EXITED" and out["exitCode"] == 0, out
    assert "42" in out["stdout"] and "s3cr3t" not in out["stdout"] and "AIAGENT" not in out["stdout"]
    assert str(tmp_path / "cells" / "t1" / "task1") in out["stdout"]


def test_grant_single_use_binding_expiry_signature(tmp_path: Path) -> None:
    s = sup(tmp_path)
    argv = ["/bin/true"]
    g = grant(argv)
    run(s.execute(g, argv, "."))
    with pytest.raises(GrantError, match="REPLAYED"):
        run(s.execute(g, argv, "."))
    with pytest.raises(GrantError, match="REPLAYED"):  # survives a supervisor restart
        run(sup(tmp_path).execute(g, argv, "."))
    with pytest.raises(GrantError, match="BINDING"):
        run(s.execute(grant(argv), ["/bin/sh", "-c", "id"], "."))
    with pytest.raises(GrantError, match="EXPIRED"):
        run(s.execute(grant(argv, ttl=-1), argv, "."))
    forged = grant(argv)
    forged["grant"]["argv"] = ["/bin/sh"]
    with pytest.raises(GrantError, match="BAD_SIGNATURE"):
        run(s.execute(forged, ["/bin/sh"], "."))


def test_refuses_root_without_cell_uid(tmp_path: Path) -> None:
    from aiagent_sandbox.supervisor import Supervisor

    if os.geteuid() != 0:
        pytest.skip("only meaningful when the supervisor runs as root")
    with pytest.raises(RuntimeError, match="root"):
        Supervisor(tmp_path / "x", KEY)


def test_runs_as_unprivileged_uid(tmp_path: Path) -> None:
    s = sup(tmp_path)
    argv = ["/usr/bin/id", "-u"]
    out = run(s.execute(grant(argv), argv, "."))
    assert out["stdout"].strip() != "0", out


def test_cwd_escape_refused(tmp_path: Path) -> None:
    s = sup(tmp_path)
    with pytest.raises(GrantError, match="CWD_ESCAPE"):
        run(s.execute(grant(["/bin/true"], "../../etc"), ["/bin/true"], "../../etc"))
    ws = tmp_path / "cells" / "t1" / "task1"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "link").symlink_to("/etc")
    with pytest.raises(GrantError, match="CWD_ESCAPE"):
        run(s.execute(grant(["/bin/true"], "link"), ["/bin/true"], "link"))


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    s = sup(tmp_path)
    ws, _ = s.workspace("t1", "task1", ".")
    marker = ws / "survivor"
    argv = ["/bin/sh", "-c", f"(sleep 3; touch {marker}) & sleep 30"]
    t0 = time.monotonic()
    out = run(s.execute(grant(argv, wall_ms=500), argv, "."))
    assert out["timedOut"] and time.monotonic() - t0 < 5, out
    time.sleep(3.5)
    assert not marker.exists()  # the background child was killed with its group


def test_memory_and_process_limits(tmp_path: Path) -> None:
    s = sup(tmp_path)
    hog = [sys.executable, "-c", "x = bytearray(1024*1024*1024)"]
    out = run(s.execute(grant(hog, mem=128 * 1024 * 1024), hog, "."))
    assert out["exitCode"] != 0 and "MemoryError" in out["stderr"]
    bomb = ["/bin/sh", "-c", "for i in $(seq 1 200); do sleep 2 & done; wait"]
    out = run(s.execute(grant(bomb, wall_ms=4000), bomb, "."))
    assert "fork" in out["stderr"].lower() or "resource" in out["stderr"].lower(), out


def test_no_network_inside_cell(tmp_path: Path) -> None:
    s = sup(tmp_path, netns=True)
    code = (
        "import socket\n"
        "try:\n socket.create_connection(('1.1.1.1', 53), timeout=2); print('CONNECTED')\n"
        "except OSError as e:\n print('BLOCKED', e.errno)\n"
        "try:\n open('/proc/net/dev').read(); print('PROC_LEAK')\n"
        "except OSError:\n print('PROC_DENIED')"
    )
    argv = [sys.executable, "-c", code]
    out = run(s.execute(grant(argv), argv, "."))
    if out["status"] == "REFUSED":  # kernel refused namespaces: the command did NOT run (fail closed)
        assert out["reason"] == "PID_OR_NETNS_UNAVAILABLE"
        pytest.skip("user+net namespaces unavailable here; fail-closed refusal verified, isolation not exercised")
    assert "CONNECTED" not in out["stdout"] and "BLOCKED" in out["stdout"], out
    assert "PROC_DENIED" in out["stdout"] and "PROC_LEAK" not in out["stdout"], out


def test_landlock_blocks_other_workspaces_and_proc(tmp_path: Path) -> None:
    """Real child process, same UID, sibling Task and Tenant paths, symlink and /proc escape."""
    s = sup(tmp_path, netns=False)
    own, _ = s.workspace("t1", "task1", ".")
    other_task, _ = s.workspace("t1", "task2", ".")
    other_tenant, _ = s.workspace("t2", "task1", ".")
    (other_task / "private").write_text("SIBLING_SECRET")
    (other_tenant / "private").write_text("TENANT_SECRET")
    (own / "escape").symlink_to(other_task / "private")
    targets = [
        str(other_task / "private"),
        str(other_tenant / "private"),
        str(own / "escape"),
        str(s.root / ".consumed-grants"),
        "/proc/self/environ",
        "/etc/passwd",
    ]
    script = (
        "from pathlib import Path\n"
        "paths = " + repr(targets) + "\n"
        "for p in paths:\n"
        " try:\n"
        "  Path(p).read_bytes(); print('LEAK', p)\n"
        " except OSError:\n"
        "  print('BLOCKED', p)\n"
        "Path('mine').write_text('OK')\n"
        "print(Path('mine').read_text())\n"
    )
    argv = [sys.executable, "-c", script]
    out = run(s.execute(grant(argv), argv, "."))
    if out["status"] == "REFUSED":
        assert out["reason"] == "FS_ISOLATION_UNAVAILABLE"
        pytest.skip("Landlock is unavailable: execution failed closed; no filesystem isolation claim")
    assert out["exitCode"] == 0, out
    assert "LEAK" not in out["stdout"], out
    assert out["stdout"].count("BLOCKED") == len(targets), out
    assert "OK" in out["stdout"] and (own / "mine").read_text() == "OK"


def test_same_uid_process_inspection_blocked_across_exec(tmp_path: Path) -> None:
    """seccomp, unlike dumpability, persists across execve of normal binaries."""
    s = sup(tmp_path, netns=False)
    code = (
        "import ctypes, errno, os, platform\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "arch = platform.machine()\n"
        "nr = 310 if arch == 'x86_64' else 270\n"
        "ctypes.set_errno(0)\n"
        "ret = libc.syscall(nr, os.getpid(), 0, 1, 0, 1, 0)\n"
        "print(ret, ctypes.get_errno())\n"
    )
    argv = [sys.executable, "-c", code]
    out = run(s.execute(grant(argv), argv, "."))
    assert out["status"] == "EXITED" and out["exitCode"] == 0, out
    assert out["stdout"].strip() == "(-1, 1)" or out["stdout"].strip() == "-1 1", out


def test_task_pid_namespace_blocks_cross_task_signals_and_preserves_child_management(tmp_path: Path) -> None:
    """Real simultaneous child processes: SIGKILL -1 from Task A cannot reach Task B."""
    s = sup(tmp_path, netns=False)
    attack = (
        "import os, signal, time\n"
        "assert os.getpid() == 1, os.getpid()\n"
        "time.sleep(0.2)\n"
        "try:\n os.kill(-1, signal.SIGKILL)\n"
        "except OSError: pass\n"
        "print('ATTACK_FINISHED', flush=True)\n"
    )
    victim = (
        "import os, time\nassert os.getpid() == 1, os.getpid()\ntime.sleep(1.0)\nprint('VICTIM_ALIVE', flush=True)\n"
    )
    a_argv, b_argv = [sys.executable, "-c", attack], [sys.executable, "-c", victim]

    def task_grant(task: str, argv: list[str]) -> dict:
        return sign(
            KEY,
            Grant(
                uuid.uuid4().hex,
                "t1",
                task,
                "run1",
                "intent1",
                3,
                tuple(argv),
                ".",
                5000,
                256 * 1024 * 1024,
                int(time.time()) + 60,
                attempt_id=uuid.uuid4().hex,
                attempt_binding_version=1,
            ),
        )

    async def concurrently() -> tuple[dict, dict]:
        a, b = await asyncio.gather(
            s.execute(task_grant("task-a", a_argv), a_argv, "."),
            s.execute(task_grant("task-b", b_argv), b_argv, "."),
        )
        return a, b

    a, b = run(concurrently())
    assert a["status"] == "EXITED" and a["exitCode"] == 0, a
    assert b["status"] == "EXITED" and b["exitCode"] == 0 and "VICTIM_ALIVE" in b["stdout"], b

    manage = (
        "import os, signal, subprocess\n"
        "p = subprocess.Popen(['/bin/sleep', '10'])\n"
        "os.kill(p.pid, signal.SIGTERM)\n"
        "assert p.wait(timeout=2) == -signal.SIGTERM\n"
        "print('OWN_CHILD_STOPPED')\n"
    )
    args = [sys.executable, "-c", manage]
    result = run(s.execute(task_grant("task-c", args), args, "."))
    assert result["status"] == "EXITED" and result["exitCode"] == 0, result
    assert "OWN_CHILD_STOPPED" in result["stdout"], result


def test_trusted_source_descriptor_is_read_only_in_real_supervisor(tmp_path: Path) -> None:
    """Internal source-FD hook only: no authenticated Source Broker is claimed."""
    import tempfile

    s = sup(tmp_path, netns=False)
    with tempfile.TemporaryDirectory(prefix="codex-cell-source-") as source_path:
        source = Path(source_path)
        source.chmod(0o755)
        (source / "source.py").write_text("value=42\n", encoding="utf-8")
        (source / "source.py").chmod(0o666)
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            code = (
                "from pathlib import Path\n"
                f"p=Path({str(source / 'source.py')!r})\n"
                "assert p.read_text() == 'value=42\\n'\n"
                f"assert Path('/proc/self/fd/{source_fd}/source.py').read_text() == 'value=42\\n'\n"
                "try:\n p.write_text('changed')\n"
                "except PermissionError: print('SOURCE_READ_ONLY')\n"
                "else: print('WRITABLE')\n"
                "Path('output').write_text('ok')\n"
            )
            argv = [sys.executable, "-c", code]
            result = run(s._execute(grant(argv, projected=True), argv, ".", source_fd=source_fd))
        finally:
            os.close(source_fd)
        assert result["status"] == "EXITED" and result["exitCode"] == 0, result
        assert "SOURCE_READ_ONLY" in result["stdout"] and "WRITABLE" not in result["stdout"]
        assert (source / "source.py").read_text(encoding="utf-8") == "value=42\n"
        assert (s.workspace("t1", "task1", ".")[0] / "output").read_text() == "ok"


def test_staged_source_descriptor_through_real_supervisor(tmp_path: Path) -> None:
    """Synthetic in-process test: not a network Source Broker or authenticated Execution."""
    import hashlib
    import io
    import struct
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "development" / "src"))
    from aiagent_development.code_intelligence.models import Coverage, Identity
    from aiagent_development.code_intelligence.projection import ProjectionFile, ProjectionManifest
    from aiagent_development.code_intelligence.projection_stage import stage_projection
    from aiagent_development.code_intelligence.projection_stream import (
        _MAGIC,
        ProjectionStreamReceipt,
        _field,
        _header,
    )

    content = b"value=42\n"
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
    item = ProjectionFile("input.py", blob, hashlib.sha256(content).hexdigest(), len(content))
    identity = Identity("fixture-tenant", "fixture-owner", "fixture-repo", "fixture-rev", "a" * 40)
    manifest = ProjectionManifest(identity, "b" * 40, (item,), len(content), Coverage.PARTIAL, ("fixture",))
    prefix = _MAGIC + b"".join(
        _field(value, 1024) for value in (identity.tenant_id, identity.owner_id, identity.binding_id, identity.revision)
    )
    prefix += _field(identity.commit, 64) + _field(manifest.tree, 64) + struct.pack(">I", 1)
    wire = prefix + _header(item) + content
    receipt = ProjectionStreamReceipt(hashlib.sha256(wire).hexdigest(), len(wire), 1)
    s = sup(tmp_path, netns=False)
    with tempfile.TemporaryDirectory(prefix="codex-stage-parent-") as parent:
        with stage_projection(
            Path(parent), io.BytesIO(wire), manifest, receipt, deadline=time.monotonic() + 10
        ) as root_fd:
            path = f"/proc/self/fd/{root_fd}/input.py"
            code = (
                "from pathlib import Path\n"
                f"p=Path({path!r})\n"
                "assert p.read_text() == 'value=42\\n'\n"
                "try:\n p.write_text('changed')\n"
                "except PermissionError: print('SOURCE_READ_ONLY')\n"
                "else: print('WRITABLE')\n"
                "Path('output').write_text('ok')\n"
            )
            argv = [sys.executable, "-c", code]
            result = run(s._execute(grant(argv, projected=True), argv, ".", source_fd=root_fd))
            assert result["status"] == "EXITED" and result["exitCode"] == 0, result
            assert "SOURCE_READ_ONLY" in result["stdout"] and "WRITABLE" not in result["stdout"]
            fd = os.open("input.py", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                assert os.read(fd, 100) == content
            finally:
                os.close(fd)
        assert not list(Path(parent).iterdir())


def test_source_descriptor_requires_privileged_supervisor_and_distinct_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = sup(tmp_path, netns=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    argv = ["/bin/true"]
    with pytest.raises(GrantError, match="SOURCE_UID_ISOLATION_UNAVAILABLE"):
        run(s._execute(grant(argv), argv, ".", source_fd=-1))


def test_source_descriptor_requires_supervisor_owned_directory(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("requires root to test distinct source owner")
    s = sup(tmp_path, netns=False)
    source = tmp_path / "foreign-source"
    source.mkdir()
    os.chown(source, 20001, 20001)
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        argv = ["/bin/true"]
        with pytest.raises(GrantError, match="SOURCE_OWNER_MISMATCH"):
            run(s._execute(grant(argv), argv, ".", source_fd=fd))
    finally:
        os.close(fd)


def test_projected_execution_rejects_wrong_output_descriptor(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("projected execution requires a privileged Supervisor")
    s = sup(tmp_path, netns=False)
    source = tmp_path / "trusted-source"
    other = tmp_path / "other-output"
    source.mkdir()
    other.mkdir()
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    output_fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        argv = ["/bin/true"]
        with pytest.raises(GrantError, match="OUTPUT_DESCRIPTOR_MISMATCH"):
            run(s._execute(grant(argv, projected=True), argv, ".", source_fd=source_fd, output_fd=output_fd))
    finally:
        os.close(source_fd)
        os.close(output_fd)


def test_cell_source_traversal_restores_mode_on_failure(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("requires root for Cell group isolation")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "development" / "src"))
    from aiagent_development.code_intelligence.models import ReadError
    from aiagent_development.code_intelligence.projection_stage import cell_source_traversal

    parent = tmp_path / "private-source"
    parent.mkdir(mode=0o700)
    gid = parent.stat().st_gid
    with pytest.raises(RuntimeError, match="fixture"):
        with cell_source_traversal(parent, cell_gid=20001):
            assert parent.stat().st_mode & 0o777 == 0o710 and parent.stat().st_gid == 20001
            raise RuntimeError("fixture")
    assert parent.stat().st_mode & 0o777 == 0o700 and parent.stat().st_gid == gid
    parent.chmod(0o755)
    with pytest.raises(ReadError, match="not private"):
        with cell_source_traversal(parent, cell_gid=20001):
            pytest.fail("non-private source parent accepted")


def test_projected_execution_requires_bound_grant(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("projected execution requires privileged Supervisor")
    s = sup(tmp_path, netns=False)
    source = tmp_path / "source-binding-test"
    source.mkdir()
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    argv = ["/bin/true"]
    try:
        with pytest.raises(GrantError, match="PROJECTED_BINDING_REQUIRED"):
            run(s._execute(grant(argv), argv, ".", source_fd=fd))
        with pytest.raises(GrantError, match="PROJECTED_SOURCE_REQUIRED"):
            run(s.execute(grant(argv, projected=True), argv, "."))
    finally:
        os.close(fd)
