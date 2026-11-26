"""Execution Supervisor - runs INSIDE the Untrusted Execution VM (separate guest kernel; no DB, no secrets, no route
to the Internet). It executes generated commands for admitted `sandbox_exec` intents only:

* authority = a signed, single-use, short-lived grant bound to argv/cwd/run/lease/intent (grant.py);
* per-task workspace confined by mandatory Landlock (system runtime read-only, no other task's files); scrubbed environment;
* Linux kernel enforcement per execution: new session/process group, RLIMIT_CPU/AS/NPROC/NOFILE/FSIZE/CORE, optional
  new user+network namespace (`require_netns`: fail closed if the kernel refuses), wall-clock timeout, kill of the whole
  process group afterwards (no surviving children), output caps;
* consumed grant ids are persisted so a restart cannot replay a grant.

Transport: authenticated frames (aiagent_domain.rpcwire) over vsock in production (`vsock:CID:PORT`), TCP/Unix in dev.
`python -m aiagent_sandbox.supervisor` (Linux only).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from aiagent_domain.rpcwire import (
    NonceCache,
    RpcAuthError,
    decode_length,
    encode_frame,
    sign_response,
    verify_request,
)

from .grant import GrantError, verify, verify_preview_capability
from .landlock import block_cross_task_process_access, disable_dumpability, restrict_to_workspace

MAX_OUTPUT = 64 * 1024
log = logging.getLogger("aiagent.sandbox")
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
CLONE_NEWPID = 0x20000000

# This trusted bootstrap stays in the parent PID namespace. Only the forked
# untrusted child becomes PID 1 in the per-Task PID namespace, with no view of
# (or ability to signal) sibling Task processes.
_PID_NAMESPACE_EXEC = """
import os
import sys

try:
    pid = os.fork()
except OSError:
    os._exit(125)
if pid == 0:
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    except OSError:
        os._exit(127)
_, status = os.waitpid(pid, 0)
if os.WIFEXITED(status):
    os._exit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status):
    os._exit(128 + os.WTERMSIG(status))
os._exit(125)
"""


# Projected runs additionally fail closed on Supervisor/launcher death. pidfd
# closes the fork-to-prctl race for PID 1, whose external parent is invisible
# through getppid() inside its PID namespace.
_PROJECTED_PID_NAMESPACE_EXEC = """
import ctypes
import os
import select
import signal
import sys

try:
    parent_fd = os.pidfd_open(os.getpid(), 0)
    pid = os.fork()
except (OSError, AttributeError):
    os._exit(125)
if pid == 0:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(125)
    poller = select.poll()
    poller.register(parent_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    if poller.poll(0):
        os._exit(125)
    os.close(parent_fd)
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    except OSError:
        os._exit(127)
os.close(parent_fd)
_, status = os.waitpid(pid, 0)
if os.WIFEXITED(status):
    os._exit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status):
    os._exit(128 + os.WTERMSIG(status))
os._exit(125)
"""


async def _bounded_output(proc: asyncio.subprocess.Process, wall_seconds: float) -> tuple[bytes, bytes, bool, bool]:
    """Drain both pipes concurrently with bounded retention; kill on overflow or timeout."""
    assert proc.stdout is not None and proc.stderr is not None
    overflow = asyncio.Event()
    buffers = (bytearray(), bytearray())

    async def drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
        while chunk := await stream.read(8192):
            remaining = MAX_OUTPUT - len(buf)
            buf.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()

    tasks = [asyncio.create_task(drain(proc.stdout, buffers[0])), asyncio.create_task(drain(proc.stderr, buffers[1]))]

    async def complete() -> None:
        await asyncio.gather(proc.wait(), *tasks)

    finished = asyncio.create_task(complete())
    wait_overflow = asyncio.create_task(overflow.wait())
    timed_out = False
    try:
        done, _ = await asyncio.wait(
            {finished, wait_overflow}, timeout=wall_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        timed_out = not done
        if timed_out or overflow.is_set():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        await asyncio.shield(finished)
    finally:
        # Also stop children on caller cancellation or unexpected pipe errors.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        wait_overflow.cancel()
        await asyncio.gather(wait_overflow, return_exceptions=True)
        await asyncio.gather(finished, *tasks, return_exceptions=True)
    return bytes(buffers[0]), bytes(buffers[1]), timed_out, overflow.is_set()


async def _run_with_revocation(
    proc: asyncio.subprocess.Process, wall_seconds: float, revoked: asyncio.Event
) -> tuple[bytes, bytes, bool, bool, bool]:
    """Kill and reap on revocation; event source must be a trusted authority bridge."""
    output = asyncio.create_task(_bounded_output(proc, wall_seconds))
    stopped = asyncio.create_task(revoked.wait())
    try:
        await asyncio.wait({output, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if revoked.is_set():
            output.cancel()
            await asyncio.gather(output, return_exceptions=True)
            return b"", b"", False, False, True
        out, err, timed_out, truncated = await output
        return out, err, timed_out, truncated, False
    finally:
        stopped.cancel()
        if not output.done():
            output.cancel()
        await asyncio.gather(stopped, output, return_exceptions=True)


def _collect_browser_ro_paths() -> tuple[tuple[Path, ...], Path]:
    extra_ro: list[Path] = []
    for candidate in (
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path(sys.executable).resolve().parent.parent,
        Path(__file__).resolve().parents[4],
    ):
        if candidate.exists():
            extra_ro.append(candidate)
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / ".cache" / "ms-playwright")
    pw_dir = Path(pw_path).resolve()
    if pw_dir.exists():
        extra_ro.append(pw_dir)
    for sys_path_entry in sys.path:
        if sys_path_entry:
            sp = Path(sys_path_entry).resolve()
            if sp.exists() and sp != Path("/"):
                extra_ro.append(sp)
    return tuple(dict.fromkeys(extra_ro)), pw_dir


@dataclass
class Limits:
    cpu_s: int = 30
    memory_bytes: int = 512 * 1024 * 1024
    nproc: int = 64
    nofile: int = 256
    fsize_bytes: int = 64 * 1024 * 1024


@dataclass
class _BrowserCgroup:
    """Per-cell cgroup v2 resource controller for Browser Cells (R3-04: memory.max, pids.max, cpu.max)."""

    path: Path

    @classmethod
    def create(
        cls,
        *,
        name: str,
        memory_bytes: int,
        pids_max: int,
        cpu_quota_us: int,
        cgroup_root: Path | None = None,
    ) -> _BrowserCgroup | None:
        base = cgroup_root
        if base is None:
            cg_root = Path("/sys/fs/cgroup")
            if not (cg_root / "cgroup.controllers").exists():
                return None
            rel = ""
            with contextlib.suppress(OSError):
                for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
                    if line.startswith("0::"):
                        rel = line[3:].strip().lstrip("/")
                        break
            base = (cg_root / rel) if rel else cg_root
        cell_dir = base / name
        try:
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "memory.max").write_text(f"{memory_bytes}\n", encoding="utf-8")
            swap_file = cell_dir / "memory.swap.max"
            if swap_file.exists():
                with contextlib.suppress(OSError):
                    swap_file.write_text("0\n", encoding="utf-8")
            (cell_dir / "pids.max").write_text(f"{pids_max}\n", encoding="utf-8")
            (cell_dir / "cpu.max").write_text(f"{cpu_quota_us} 100000\n", encoding="utf-8")
        except OSError:
            with contextlib.suppress(OSError):
                cell_dir.rmdir()
            return None
        return cls(cell_dir)

    def attach(self, pid: int) -> bool:
        try:
            (self.path / "cgroup.procs").write_text(f"{pid}\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def cleanup(self) -> None:
        kill_file = self.path / "cgroup.kill"
        if kill_file.exists():
            with contextlib.suppress(OSError):
                kill_file.write_text("1\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            self.path.rmdir()


def _process_group_rss_bytes(pgid: int) -> int:
    """Sum VmRSS across all processes in `pgid` from /proc as fallback memory enforcement in unprivileged dev/test."""
    total = 0
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if os.getpgid(pid) != pgid:
                continue
            for line in (entry / "status").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        total += int(parts[1]) * 1024
                    break
        except OSError:
            continue
    return total


@dataclass
class Supervisor:
    root: Path
    grant_key: bytes
    limits: Limits = field(default_factory=Limits)
    require_netns: bool = True
    # Unprivileged identity for generated code. Must be DEDICATED to cells on this kernel: RLIMIT_NPROC is not enforced
    # for root and is counted per uid kernel-wide, so sharing a uid with any service would let either exhaust the other.
    cell_uid: int | None = None
    cell_gid: int | None = None
    consumed_path: Path | None = None
    preview_capability_key: bytes | None = None
    require_browser_cgroup: bool = False
    cgroup_root: Path | None = None
    _active: dict[str, tuple[str, str, str, asyncio.Event]] = field(default_factory=dict)
    _active_previews: dict[str, tuple[str, asyncio.Event]] = field(default_factory=dict)
    _heartbeats: dict[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("the execution supervisor requires Linux (kernel rlimits/namespaces)")
        if os.geteuid() == 0 and self.cell_uid in (None, 0):
            raise RuntimeError("refusing to run generated code as root: configure an unprivileged cell uid/gid")
        if self.preview_capability_key is not None:
            if len(self.preview_capability_key) < 32:
                raise RuntimeError("preview_capability_key must be >= 32 bytes")
            if self.preview_capability_key == self.grant_key:
                raise RuntimeError("preview_capability_key must be distinct from grant_key")
        self.root.mkdir(parents=True, exist_ok=True)
        self.consumed_path = self.consumed_path or self.root / ".consumed-grants"

    def _consume(self, gid: str) -> None:
        # The journal is a local replay safeguard, not the canonical Authority.
        # Serialize independent Supervisor processes and fail closed when busy,
        # corrupt or too large; never read an unbounded journal into memory.
        import fcntl
        import stat

        assert self.consumed_path is not None
        try:
            fd = os.open(self.consumed_path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+b") as journal:
                try:
                    fcntl.flock(journal.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise GrantError("REPLAY_JOURNAL_BUSY") from exc
                info = os.fstat(journal.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022
                    or info.st_size > 64 * 1024 * 1024
                ):
                    raise GrantError("REPLAY_JOURNAL_INVALID")
                deadline = time.monotonic() + 1
                while True:
                    line = journal.readline(258)
                    if not line:
                        break
                    if time.monotonic() > deadline or len(line) > 257 or not line.endswith(b"\n"):
                        raise GrantError("REPLAY_JOURNAL_INVALID")
                    if line[:-1] == gid.encode("ascii"):
                        raise GrantError("REPLAYED")
                journal.write(gid.encode("ascii") + b"\n")
                journal.flush()
                os.fsync(journal.fileno())
                parent_fd = os.open(self.consumed_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
        except OSError as exc:
            raise GrantError("REPLAY_JOURNAL_UNAVAILABLE") from exc

    def workspace(self, tenant: str, task: str, cwd_rel: str) -> tuple[Path, Path]:
        ws = (self.root / tenant / task).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        cwd = (ws / cwd_rel).resolve()
        if cwd != ws and ws not in cwd.parents:
            raise GrantError("CWD_ESCAPE")
        cwd.mkdir(parents=True, exist_ok=True)
        if self.cell_uid is not None and os.geteuid() == 0:
            for p in (ws, cwd):
                os.chown(p, self.cell_uid, self.cell_gid if self.cell_gid is not None else self.cell_uid)
        return ws, cwd

    def cancel_projected(self, tenant: str, execution: str, attempt: str, binding: str) -> bool:
        """Signal only the exact active signed execution; not an Authority check."""
        active = self._active.get(execution)
        if active is None or active[:3] != (tenant, attempt, binding):
            return False
        active[3].set()
        return True

    def renew_projected(self, tenant: str, execution: str, attempt: str, binding: str) -> bool:
        active, heartbeat = self._active.get(execution), self._heartbeats.get(execution)
        if active is None or active[:3] != (tenant, attempt, binding) or heartbeat is None or active[3].is_set():
            return False
        deadline, period = heartbeat
        if time.monotonic() >= deadline:
            active[3].set()
            return False
        self._heartbeats[execution] = (time.monotonic() + period, period)
        return True

    async def _watch_heartbeat(self, execution: str, revoked: asyncio.Event) -> None:
        while not revoked.is_set():
            heartbeat = self._heartbeats.get(execution)
            if heartbeat is None or time.monotonic() >= heartbeat[0]:
                revoked.set()
                return
            await asyncio.sleep(min(0.1, max(0.001, heartbeat[0] - time.monotonic())))

    def _preexec(
        self,
        wall_ms: int,
        memory_bytes: int,
        workspace: Path,
        source_fd: int | None = None,
        *,
        extra_ro_exec_paths: tuple[Path, ...] = (),
        extra_rw_files: tuple[Path, ...] = (),
        browser_mode: bool = False,
    ) -> Any:
        import resource

        lim = self.limits
        require_netns = self.require_netns
        uid, gid = self.cell_uid, self.cell_gid if self.cell_gid is not None else self.cell_uid
        expected_parent = os.getpid()

        def fn() -> None:
            os.setsid()  # new session + process group: the whole tree is killable
            resource.setrlimit(resource.RLIMIT_CPU, (max(lim.cpu_s, 60) if browser_mode else lim.cpu_s,) * 2)
            if not browser_mode:
                mem = min(memory_bytes, lim.memory_bytes)
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
                resource.setrlimit(resource.RLIMIT_NPROC, (lim.nproc, lim.nproc))
                resource.setrlimit(resource.RLIMIT_NOFILE, (lim.nofile, lim.nofile))
            else:
                resource.setrlimit(resource.RLIMIT_NPROC, (max(lim.nproc, 256), max(lim.nproc, 256)))
                resource.setrlimit(resource.RLIMIT_NOFILE, (max(lim.nofile, 1024), max(lim.nofile, 1024)))
            resource.setrlimit(resource.RLIMIT_FSIZE, (lim.fsize_bytes, lim.fsize_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # Filesystem confinement is MANDATORY even in development / when netns is disabled.
            # Parent/cwd validation alone cannot prevent access to another tenant's files.
            try:
                restrict_to_workspace(
                    workspace,
                    source_fd=source_fd,
                    extra_ro_exec_paths=extra_ro_exec_paths,
                    extra_rw_files=extra_rw_files,
                )
            except (OSError, ValueError) as e:
                os.write(2, f"landlock_setup: {type(e).__name__}: {e}\n".encode()[:400])
                os._exit(98)  # fail closed; no generated command was executed
            # Install the Landlock rules before dropping credentials; the supervisor
            # can traverse protected root-owned parent directories while configuring
            # the rule, but the generated program only ever runs with the cell UID.
            if uid is not None and os.geteuid() == 0:
                os.setgroups([])
                os.setgid(gid)  # type: ignore[arg-type]
                os.setuid(uid)
            try:
                os.unshare(CLONE_NEWUSER | CLONE_NEWPID | (CLONE_NEWNET if require_netns else 0))
            except (OSError, AttributeError):
                os._exit(97)  # fail closed: mandatory PID namespace unavailable
            if browser_mode and require_netns:
                # Inside the fresh CLONE_NEWNET + CLONE_NEWUSER namespace (before execve drops namespace caps),
                # bring up the child's PRIVATE `lo` interface so Chromium can reach ONLY the local Unix bridge.
                # No external interface or host loopback exists in this netns.
                import fcntl
                import socket as _socket
                import struct

                _lo_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                try:
                    _ifreq = struct.pack("16sH14s", b"lo", 0, b"")
                    _res = fcntl.ioctl(_lo_sock.fileno(), 0x8913, _ifreq)  # SIOCGIFFLAGS
                    _flags = struct.unpack("16sH14s", _res)[1]
                    if not (_flags & 0x1):  # IFF_UP
                        fcntl.ioctl(_lo_sock.fileno(), 0x8914, struct.pack("16sH14s", b"lo", _flags | 0x1, b""))
                except OSError:
                    os._exit(96)
                finally:
                    _lo_sock.close()
            # setuid and the NEWUSER transition may reset dumpability. Sibling
            # commands run with the same numeric UID; a ptrace window is unsafe.
            try:
                disable_dumpability()  # defense in depth; execve resets this alone
                block_cross_task_process_access(preserve_parent_death=source_fd is not None)
            except OSError:
                os._exit(98)
            if source_fd is not None:
                import ctypes

                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected_parent:
                    os._exit(98)

        return fn

    def cancel_preview(self, tenant_id: str, capability_id: str) -> bool:
        entry = self._active_previews.get(capability_id)
        if entry is None or entry[0] != tenant_id:
            return False
        entry[1].set()
        return True

    async def debug_preview(self, capability_token: Any) -> dict[str, Any]:
        """Run Browser Debugger inside full Sandbox Cell isolation using a dedicated PreviewCapability (R3-02/R3-04/R3-05).

        - Verifies PreviewCapability signed with `preview_capability_key` (distinct from `grant_key`).
        - Binds target_port, target_url, endpoint_ref, commit_sha, preview_instance_id, instance_generation,
          lease_id, resource caps, and single-use capability_id exclusively from the signed capability.
        - Enforces per-cell cgroup v2 (`memory.max`, `pids.max`, `cpu.max`) with production fail-closed and
          process-group RSS watcher fallback in unprivileged dev/test environments.
        - Monitors capability expiry and live `preview.cancel` revocation during execution.
        """
        if self.preview_capability_key is None:
            raise GrantError("PREVIEW_CAPABILITY_UNAVAILABLE")
        cap = verify_preview_capability(self.preview_capability_key, capability_token)
        self._consume(f"prv:{cap.capability_id}")
        target_port = cap.target_port
        ws, cwd = self.workspace(cap.tenant_id, cap.task_id, ".")
        tmp_dir = ws / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        cgroup = _BrowserCgroup.create(
            name=f"aiagent-prv-{cap.capability_id[:16]}",
            memory_bytes=cap.memory_bytes,
            pids_max=cap.pids_max,
            cpu_quota_us=cap.cpu_quota_us,
            cgroup_root=self.cgroup_root,
        )
        if cgroup is None and self.require_browser_cgroup:
            raise GrantError("BROWSER_CGROUP_REQUIRED")

        # Keep AF_UNIX path short (<108 bytes Linux sun_path limit); Landlock grants access ONLY to this exact socket file.
        bridge_sock = Path("/tmp") / f"aiag-prv-{cap.capability_id[:16]}.sock"  # noqa: S108
        if bridge_sock.exists():
            bridge_sock.unlink()

        async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while chunk := await src.read(16384):
                    dst.write(chunk)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    dst.close()

        async def _bridge_conn(ur: asyncio.StreamReader, uw: asyncio.StreamWriter) -> None:
            try:
                tr, tw = await asyncio.open_connection("127.0.0.1", target_port)
            except OSError:
                uw.close()
                return
            t1 = asyncio.create_task(_pipe(ur, tw))
            t2 = asyncio.create_task(_pipe(tr, uw))
            await asyncio.gather(t1, t2, return_exceptions=True)

        bridge_srv = await asyncio.start_unix_server(_bridge_conn, path=str(bridge_sock))
        if self.cell_uid is not None and os.geteuid() == 0:
            os.chown(bridge_sock, self.cell_uid, self.cell_gid if self.cell_gid is not None else self.cell_uid)
            os.chmod(bridge_sock, 0o600)
        else:
            os.chmod(bridge_sock, 0o600)

        extra_ro_paths, pw_dir = await asyncio.to_thread(_collect_browser_ro_paths)

        clean_env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(ws),
            "TMPDIR": str(tmp_dir),
            "XDG_CONFIG_HOME": str(tmp_dir),
            "XDG_CACHE_HOME": str(tmp_dir),
            "PLAYWRIGHT_BROWSERS_PATH": str(pw_dir),
            "OPENSSL_CONF": "/dev/null",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.environ.get("PYTHONPATH"):
            clean_env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        worker_cmd = [
            sys.executable,
            "-m",
            "aiagent_development.browser_debug_worker",
            "--bridge-sock",
            str(bridge_sock),
        ]
        bootstrap = [sys.executable, "-c", _PID_NAMESPACE_EXEC, *worker_cmd]
        payload = json.dumps(
            {"targetUrl": cap.target_url, "leasedPort": target_port, "commitSha": cap.commit_sha}
        ).encode("utf-8")

        revoked = asyncio.Event()
        self._active_previews[cap.capability_id] = (cap.tenant_id, revoked)
        watcher: asyncio.Task[None] | None = None
        memory_exceeded = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *bootstrap,
                cwd=str(cwd),
                env=clean_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec(
                    cap.wall_ms,
                    cap.memory_bytes,
                    ws,
                    extra_ro_exec_paths=extra_ro_paths,
                    extra_rw_files=(bridge_sock,),
                    browser_mode=True,
                ),
            )
            if cgroup is not None and not cgroup.attach(proc.pid) and self.require_browser_cgroup:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                raise GrantError("BROWSER_CGROUP_ATTACH_FAILED")

            async def _watch_limits_and_expiry() -> None:
                nonlocal memory_exceeded
                while proc.returncode is None and not revoked.is_set():
                    if time.time() >= cap.expires:
                        revoked.set()
                        return
                    if cgroup is None and _process_group_rss_bytes(proc.pid) > cap.memory_bytes:
                        memory_exceeded = True
                        revoked.set()
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(_watch_limits_and_expiry())
            assert proc.stdin is not None
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
            effective_wall = min(max(cap.wall_ms / 1000.0, 15.0), max(1.0, cap.expires - time.time()))
            out, err, timed_out, truncated, was_revoked = await _run_with_revocation(proc, effective_wall, revoked)
            return {
                "status": "REVOKED" if was_revoked else "EXITED",
                "attemptId": cap.attempt_id,
                "capabilityId": cap.capability_id,
                "exitCode": proc.returncode if proc.returncode is not None else -1,
                "timedOut": timed_out,
                "truncated": truncated,
                "memoryExceeded": memory_exceeded,
                "cgroupEnforced": cgroup is not None,
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace"),
            }
        finally:
            self._active_previews.pop(cap.capability_id, None)
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if cgroup is not None:
                cgroup.cleanup()
            bridge_srv.close()
            await bridge_srv.wait_closed()
            with contextlib.suppress(OSError):
                bridge_sock.unlink()

    async def execute(self, token: Any, argv: list[str], cwd_rel: str) -> dict[str, Any]:
        return await self._execute(token, argv, cwd_rel, source_fd=None)

    async def _execute(
        self,
        token: Any,
        argv: list[str],
        cwd_rel: str,
        *,
        source_fd: int | None,
        output_fd: int | None = None,
        output_path: Path | None = None,
        source_cwd: bool = False,
        revoked: asyncio.Event | None = None,
        heartbeat_seconds: float | None = None,
    ) -> dict[str, Any]:
        # source_fd is reserved for a future trusted projection broker. It is
        # deliberately not accepted from RPC and is not an authorization token.
        if source_fd is not None:
            if os.geteuid() != 0 or self.cell_uid in (None, 0):
                raise GrantError("SOURCE_UID_ISOLATION_UNAVAILABLE")
            try:
                if os.fstat(source_fd).st_uid != os.geteuid():
                    raise GrantError("SOURCE_OWNER_MISMATCH")
            except OSError as exc:
                raise GrantError("SOURCE_DESCRIPTOR_INVALID") from exc
        if heartbeat_seconds is not None and (
            source_fd is None or isinstance(heartbeat_seconds, bool) or not 0.1 <= heartbeat_seconds <= 10
        ):
            raise GrantError("INVALID_HEARTBEAT")
        if source_cwd and (source_fd is None or output_fd is None):
            raise GrantError("SOURCE_CWD_INVALID")
        if (output_fd is None) != (output_path is None):
            raise GrantError("OUTPUT_DESCRIPTOR_INVALID")
        if output_fd is not None and (source_fd is None or output_fd == source_fd or cwd_rel != "."):
            raise GrantError("OUTPUT_DESCRIPTOR_INVALID")
        g = verify(self.grant_key, token, argv, cwd_rel)
        if source_fd is not None and g.execution_binding_version != 1:
            raise GrantError("PROJECTED_BINDING_REQUIRED")
        if source_fd is None and g.execution_binding_version is not None:
            raise GrantError("PROJECTED_SOURCE_REQUIRED")
        if source_fd is not None and heartbeat_seconds is None:
            raise GrantError("HEARTBEAT_REQUIRED")
        self._consume(g.grant_id)
        execution = g.execution_id
        if source_fd is not None:
            assert execution is not None
            self._consume("execution:" + execution)
        if source_fd is not None:
            assert execution is not None and g.attempt_id is not None and g.execution_binding_sha256 is not None
            if execution in self._active:
                raise GrantError("EXECUTION_ALREADY_ACTIVE")
            revoked = revoked or asyncio.Event()
            self._active[execution] = (g.tenant_id, g.attempt_id, g.execution_binding_sha256, revoked)
        heartbeat_task: asyncio.Task[None] | None = None
        if execution is not None and heartbeat_seconds is not None:
            self._heartbeats[execution] = (time.monotonic() + heartbeat_seconds, heartbeat_seconds)
            assert revoked is not None
            heartbeat_task = asyncio.create_task(self._watch_heartbeat(execution, revoked))
        try:
            if revoked is not None and revoked.is_set():
                return {"status": "REVOKED", "attemptId": g.attempt_id, "executionId": g.execution_id}
            if output_path is None:
                ws, cwd = self.workspace(g.tenant_id, g.task_id, cwd_rel)
            else:
                ws = cwd = output_path
            if output_fd is not None:
                try:
                    actual, expected = os.fstat(output_fd), os.stat(cwd, follow_symlinks=False)
                    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                        raise GrantError("OUTPUT_DESCRIPTOR_MISMATCH")
                except OSError as exc:
                    raise GrantError("OUTPUT_DESCRIPTOR_INVALID") from exc
            if source_cwd:
                assert source_fd is not None
                try:
                    source_path = Path(os.readlink(f"/proc/self/fd/{source_fd}"))
                    source_actual = os.fstat(source_fd)
                    source_named = os.stat(source_path, follow_symlinks=False)
                    if not source_path.is_absolute() or (source_actual.st_dev, source_actual.st_ino) != (
                        source_named.st_dev,
                        source_named.st_ino,
                    ):
                        raise GrantError("SOURCE_DESCRIPTOR_INVALID")
                    cwd = source_path
                except OSError as exc:
                    raise GrantError("SOURCE_DESCRIPTOR_INVALID") from exc
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(ws), "LANG": "C.UTF-8", "TMPDIR": str(ws)}
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                _PID_NAMESPACE_EXEC if source_fd is None else _PROJECTED_PID_NAMESPACE_EXEC,
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec(g.wall_ms, g.memory_bytes, ws, source_fd),
                close_fds=True,
                pass_fds=tuple(fd for fd in (source_fd, output_fd) if fd is not None),
            )
            if revoked is None:
                out, err, timed_out, truncated = await _bounded_output(proc, g.wall_ms / 1000)
            else:
                out, err, timed_out, truncated, was_revoked = await _run_with_revocation(
                    proc, g.wall_ms / 1000, revoked
                )
                if was_revoked:
                    return {"status": "REVOKED", "attemptId": g.attempt_id, "executionId": g.execution_id}
            rc = proc.returncode
            if rc == 98:
                return {
                    "status": "REFUSED",
                    "reason": "FS_ISOLATION_UNAVAILABLE",
                    "detail": err[:400].decode("utf-8", "replace"),
                }
            if rc == 97:
                return {"status": "REFUSED", "reason": "PID_OR_NETNS_UNAVAILABLE"}
            return {
                "status": "EXITED",
                "attemptId": g.attempt_id,
                "executionId": g.execution_id,
                "exitCode": rc,
                "timedOut": timed_out,
                "wallMs": int((time.monotonic() - started) * 1000),
                "stdout": out[:MAX_OUTPUT].decode("utf-8", "replace"),
                "stderr": err[:MAX_OUTPUT].decode("utf-8", "replace"),
                "truncated": truncated,
            }
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if source_fd is not None and execution is not None:
                self._heartbeats.pop(execution, None)
                self._active.pop(execution, None)

    @contextlib.asynccontextmanager
    async def _execute_and_capture(
        self,
        token: Any,
        argv: list[str],
        cwd_rel: str,
        *,
        source_fd: int,
        output_parent: Path,
        binding: dict[str, Any],
        receipt_key: bytes,
        private_parent: Path,
        deadline: float,
        heartbeat_seconds: float,
        revoked: asyncio.Event,
        prepared_output_fd: int | None = None,
        source_cwd: bool = False,
    ) -> AsyncIterator[tuple[dict[str, Any], int, bytes]]:
        """Private prototype: capture/sign only after this Supervisor reports clean exit.

        This does not authenticate runtime image, transport or canonical authority;
        no RPC endpoint exposes it, and final publication must recheck the DB.
        """
        from aiagent_development.code_intelligence.artifact_capture import capture_scip

        from .capture_signing import sign_capture, validate_capture_binding
        from .projected_output import projected_output, validate_projected_output

        g = verify(self.grant_key, token, argv, cwd_rel)
        if g.execution_binding_version != 1 or self.cell_uid is None:
            raise GrantError("PROJECTED_BINDING_REQUIRED")
        validate_capture_binding(receipt_key, g, binding)
        if not math.isfinite(deadline) or not time.monotonic() < deadline <= time.monotonic() + 300:
            raise GrantError("CAPTURE_DEADLINE")
        assert g.execution_id is not None
        cell_gid = self.cell_gid if self.cell_gid is not None else self.cell_uid
        output = (
            projected_output(output_parent, g, cell_uid=self.cell_uid, cell_gid=cell_gid)
            if prepared_output_fd is None
            else contextlib.nullcontext(prepared_output_fd)
        )
        with output as output_fd:
            validate_projected_output(output_parent, g, output_fd, cell_uid=self.cell_uid, cell_gid=cell_gid)
            output_path = output_parent / g.execution_id
            result = await self._execute(
                token,
                argv,
                cwd_rel,
                source_fd=source_fd,
                output_fd=output_fd,
                output_path=output_path,
                source_cwd=source_cwd,
                revoked=revoked,
                heartbeat_seconds=heartbeat_seconds,
            )
            if (
                result.get("status") != "EXITED"
                or type(result.get("exitCode")) is not int
                or result.get("exitCode") != 0
                or result.get("timedOut") is not False
                or result.get("truncated") is not False
                or revoked.is_set()
            ):
                raise GrantError("PROJECTED_EXECUTION_INCOMPLETE")
            stopped = threading.Event()

            def active() -> bool:
                return not stopped.is_set() and not revoked.is_set()

            capture = capture_scip(
                output_fd, private_parent, expected_uid=self.cell_uid, deadline=deadline, active=active
            )

            def take_capture() -> Any:
                captured = capture.__enter__()
                try:
                    proof = sign_capture(
                        receipt_key,
                        g,
                        binding,
                        captured.fd,
                        expected_sha256=captured.sha256,
                        size=captured.byte_length,
                        deadline=deadline,
                        active=active,
                    )
                    return captured, proof
                except BaseException:
                    capture.__exit__(*sys.exc_info())
                    raise

            worker = asyncio.create_task(asyncio.to_thread(take_capture))
            try:
                captured, proof = await asyncio.shield(worker)
            except asyncio.CancelledError:
                stopped.set()
                try:
                    await worker
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(capture.__exit__, None, None, None)
                raise
            try:
                if revoked.is_set():
                    raise GrantError("PROJECTED_EXECUTION_REVOKED")
                yield result, captured.fd, proof
            finally:
                stopped.set()
                await asyncio.to_thread(capture.__exit__, None, None, None)


async def serve(sup: Supervisor, keys: dict[str, bytes], endpoint: str, *, source_sessions: Any = None) -> None:
    nonces = NonceCache()
    connections: set[asyncio.Task[Any]] = set()

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            connections.add(task)
        method = ""
        body: dict[str, Any] = {}
        claimed_source: Any = None
        try:
            n = decode_length(await r.readexactly(4))
            env = json.loads(await r.readexactly(n))
            try:
                caller, method, body, nonce = verify_request(keys, env, nonces)
            except RpcAuthError:
                w.write(encode_frame({"type": "error", "code": "UNAUTHENTICATED"}))
                return
            key = keys[caller]
            if (
                caller not in ("tool_gateway", "api")
                or (caller == "api" and method != "preview.debug")
                or method
                not in (
                    "exec.run",
                    "exec.cancel",
                    "exec.heartbeat",
                    "preview.debug",
                    "source.receive",
                    "source.register",
                    "source.heartbeat",
                    "source.cancel",
                )
            ):
                w.write(encode_frame(sign_response(key, nonce, {"type": "error", "code": "FORBIDDEN_METHOD"})))
                return
            try:
                if method in ("source.receive", "source.register", "source.heartbeat", "source.cancel"):
                    if source_sessions is None:
                        raise GrantError("SOURCE_BROKER_UNAVAILABLE")
                    try:
                        if method == "source.register":
                            res = source_sessions.register_authorized(body, sup._consume)
                            session_tag = hashlib.sha256(res["sessionId"].encode()).hexdigest()[:16]
                            log.info(
                                "source registered session=%s execution=%s",
                                session_tag,
                                body["ticket"]["payload"]["execution"],
                            )
                        elif method == "source.heartbeat":
                            renewed = source_sessions.renew(body)
                            if renewed:
                                sup.renew_projected(
                                    body["tenantId"], body["executionId"], body["attemptId"], body["bindingSha256"]
                                )
                            res = {"renewed": renewed}
                        elif method == "source.cancel":
                            if set(body) == {"ticket"}:
                                scope = source_sessions.cancel_ticket(body, sup._consume)
                                cancelled = True
                                sup.cancel_projected(*scope)
                            else:
                                cancelled = source_sessions.cancel(body)
                                if cancelled:
                                    sup.cancel_projected(
                                        body["tenantId"], body["executionId"], body["attemptId"], body["bindingSha256"]
                                    )
                            res = {"cancelled": cancelled}
                            if cancelled:
                                log.info("source session cancellation accepted")
                        else:
                            claimed_source = source_sessions.claim(body)
                            res = await source_sessions.receive(claimed_source, r)
                    except Exception as exc:
                        raise GrantError("SOURCE_TRANSFER_REJECTED") from exc
                elif method in ("exec.cancel", "exec.heartbeat"):
                    fields = (
                        body.get("tenantId"),
                        body.get("executionId"),
                        body.get("attemptId"),
                        body.get("bindingSha256"),
                    )
                    if set(body) != {"tenantId", "executionId", "attemptId", "bindingSha256"} or not all(
                        isinstance(value, str) and 1 <= len(value) <= 128 for value in fields
                    ):
                        raise GrantError("MALFORMED")
                    scoped = cast(tuple[str, str, str, str], fields)
                    if method == "exec.cancel":
                        res = {"cancelled": sup.cancel_projected(*scoped)}
                    else:
                        res = {"renewed": sup.renew_projected(*scoped)}
                elif method == "preview.debug":
                    res = await sup.debug_preview(body.get("capability"))
                elif method == "preview.cancel":
                    t_id, cap_id = body.get("tenantId"), body.get("capabilityId")
                    if not isinstance(t_id, str) or not isinstance(cap_id, str) or not t_id or not cap_id:
                        raise GrantError("MALFORMED")
                    res = {"cancelled": sup.cancel_preview(t_id, cap_id)}
                else:
                    res = await sup.execute(
                        body.get("grant"), list(body.get("argv", [])), str(body.get("cwdRelative", ""))
                    )
                artifact_result: Any = None
                if method == "source.receive":
                    from .source_rpc import ArtifactTransfer

                    if isinstance(res, ArtifactTransfer):
                        artifact_result = res
                if artifact_result is not None:
                    from .binary_transfer import MAX_CHUNK, encode_chunks

                    res = artifact_result
                    if not res.active() or time.monotonic() >= res.deadline:
                        raise GrantError("SOURCE_TRANSFER_REJECTED")
                    artifact = res.artifact
                    frame = {
                        "type": "artifact",
                        "data": {
                            "size": len(artifact.data),
                            "sha256": hashlib.sha256(artifact.data).hexdigest(),
                            "proof": base64.b64encode(artifact.proof).decode("ascii"),
                        },
                    }
                    w.write(encode_frame(sign_response(key, nonce, frame)))
                    for block in encode_chunks(
                        res.key,
                        res.binding,
                        (artifact.data[i : i + MAX_CHUNK] for i in range(0, len(artifact.data), MAX_CHUNK)),
                    ):
                        remaining = res.deadline - time.monotonic()
                        if remaining <= 0 or not res.active():
                            raise GrantError("SOURCE_TRANSFER_REJECTED")
                        w.write(block)
                        draining = asyncio.create_task(w.drain())
                        try:
                            while not draining.done():
                                remaining = res.deadline - time.monotonic()
                                if remaining <= 0 or not res.active():
                                    raise GrantError("SOURCE_TRANSFER_REJECTED")
                                try:
                                    await asyncio.wait_for(asyncio.shield(draining), timeout=min(remaining, 0.1))
                                except TimeoutError:
                                    continue
                            await draining
                        finally:
                            if not draining.done():
                                draining.cancel()
                                await asyncio.gather(draining, return_exceptions=True)
                    session_tag = hashlib.sha256(str(body.get("sessionId", "")).encode()).hexdigest()[:16]
                    log.info("source artifact transferred session=%s", session_tag)
                    return
                frame = {"type": "result", "data": res}
            except GrantError as e:
                frame = {"type": "error", "code": f"GRANT_{e}"}
            w.write(encode_frame(sign_response(key, nonce, frame)))
            await w.drain()
        except Exception as exc:
            # Never log request bodies, argv, keys, source, output or receipts.
            log.warning("sandbox RPC failed: %s", type(exc).__name__)
        finally:
            w.close()
            if claimed_source is not None and source_sessions is not None:
                source_sessions.end(claimed_source)
            if task is not None:
                connections.discard(task)

    kind, _, rest = endpoint.partition(":")
    if kind == "vsock":
        import socket

        cid, port = rest.split(":")
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        sock.bind((int(cid), int(port)))
        srv = await asyncio.start_server(handle, sock=sock)
    elif kind == "unix":
        srv = await asyncio.start_unix_server(handle, path=rest)
    else:
        host, port = rest.rsplit(":", 1)
        srv = await asyncio.start_server(handle, host, int(port))
    try:
        # serve_forever() waits for existing connections on cancellation in
        # Python 3.12. Cancel handlers first so staged contexts can unwind.
        await asyncio.Future[None]()
    finally:
        srv.close()
        for connection in tuple(connections):
            connection.cancel()
        if connections:
            await asyncio.gather(*tuple(connections), return_exceptions=True)
        await srv.wait_closed()


def _configured_source_sessions(sup: Supervisor, rpc_key: bytes) -> Any:
    """Load an explicitly provisioned source broker or leave it disabled.

    This validates local configuration, not VM/image attestation or durable
    storage. Those production gates remain the operator's responsibility.
    """
    mode = os.environ.get("AIAGENT_SANDBOX_SOURCE_MODE", "disabled")
    if mode == "disabled":
        return None
    if mode != "enabled" or os.geteuid() != 0 or sup.cell_uid in (None, 0) or not sup.require_netns:
        raise RuntimeError("source broker isolation unavailable")
    import stat

    from aiagent_development.code_intelligence.projection_stage import _open_trusted_parent

    from .scip_execution import scip_registration_factory
    from .source_rpc import SourceSessions

    def checked_directory(path: Path, mode_bits: int, gid: int | None = None) -> None:
        if not path.is_absolute() or any(part in (".", "..") for part in path.parts[1:]):
            raise RuntimeError("source broker directory unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                ancestor = os.fstat(fd)
                permissions = stat.S_IMODE(ancestor.st_mode)
                if ancestor.st_uid != 0 or (permissions & 0o022 and not permissions & stat.S_ISVTX):
                    raise RuntimeError("source broker directory unavailable")
            info = os.fstat(fd)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode_bits or (gid is not None and info.st_gid != gid):
                raise RuntimeError("source broker directory unavailable")
        finally:
            os.close(fd)

    def read_key(name: str) -> bytes:
        path = Path(os.environ[name])
        if not path.is_absolute():
            raise RuntimeError("source broker key unavailable")
        checked_directory(path.parent, 0o700)
        parent_fd = _open_trusted_parent(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_size > 4096
                ):
                    raise RuntimeError("source broker key unavailable")
                raw = os.read(fd, 4097)
                after = os.fstat(fd)
                if len(raw) != info.st_size or (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise RuntimeError("source broker key unavailable")
                value = bytes.fromhex(raw.decode("ascii").strip())
                if len(value) < 32:
                    raise RuntimeError("source broker key unavailable")
                return value
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    base = Path(os.environ["AIAGENT_SANDBOX_SOURCE_ROOT"])
    gid = sup.cell_gid if sup.cell_gid is not None else sup.cell_uid
    assert gid is not None
    checked_directory(base, 0o710, gid)
    source, output, capture = (base / name for name in ("source", "output", "capture"))
    for path in (source, output, capture):
        checked_directory(path, 0o700)
    journal = Path(os.environ["AIAGENT_SANDBOX_REPLAY_JOURNAL"])
    if not journal.is_absolute() or not journal.name:
        raise RuntimeError("source replay journal unavailable")
    checked_directory(journal.parent, 0o700)
    ticket_key = read_key("AIAGENT_SANDBOX_SOURCE_TICKET_KEY_FILE")
    capture_key = read_key("AIAGENT_SANDBOX_CAPTURE_KEY_FILE")
    if (
        read_key("AIAGENT_SANDBOX_GRANT_KEY_FILE") != sup.grant_key
        or read_key("AIAGENT_SANDBOX_RPC_KEY_FILE") != rpc_key
    ):
        raise RuntimeError("source broker key unavailable")
    if len({sup.grant_key, rpc_key, ticket_key, capture_key}) != 4:
        raise RuntimeError("source broker keys must be distinct")
    sup.consumed_path = journal
    return SourceSessions(
        source,
        ticket_key=ticket_key,
        registration_factory=scip_registration_factory(sup, source, output, capture, capture_key),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    key_hex = Path(os.environ["AIAGENT_SANDBOX_GRANT_KEY_FILE"]).read_text().strip()
    rpc = Path(os.environ["AIAGENT_SANDBOX_RPC_KEY_FILE"]).read_text().strip()
    uid = os.environ.get("AIAGENT_SANDBOX_CELL_UID")
    limits = Limits()
    if os.environ.get("AIAGENT_SANDBOX_SOURCE_MODE", "disabled") == "enabled":
        limits.memory_bytes = int(os.environ["AIAGENT_SANDBOX_MEMORY_LIMIT_BYTES"])
        limits.cpu_s = int(os.environ["AIAGENT_SANDBOX_CPU_LIMIT_SECONDS"])
        if not 512 * 1024**2 <= limits.memory_bytes <= 8 * 1024**3 or not 1 <= limits.cpu_s <= 300:
            raise RuntimeError("invalid source broker limits")
    preview_cap_key: bytes | None = None
    preview_cap_file = os.environ.get("AIAGENT_PREVIEW_CAPABILITY_KEY_FILE")
    if preview_cap_file:
        preview_cap_key = bytes.fromhex(Path(preview_cap_file).read_text().strip())
    require_browser_cgroup = (
        os.environ.get(
            "AIAGENT_SANDBOX_REQUIRE_BROWSER_CGROUP",
            "1" if os.environ.get("AIAGENT_PROFILE") == "production" else "0",
        )
        == "1"
    )
    sup = Supervisor(
        Path(os.environ.get("AIAGENT_SANDBOX_ROOT", "/var/lib/aiagent/cells")),
        bytes.fromhex(key_hex),
        limits=limits,
        require_netns=os.environ.get("AIAGENT_SANDBOX_REQUIRE_NETNS", "1") == "1",
        cell_uid=int(uid) if uid else None,
        cell_gid=int(uid) if uid else None,
        preview_capability_key=preview_cap_key,
        require_browser_cgroup=require_browser_cgroup,
    )
    rpc_key = bytes.fromhex(rpc)
    source_sessions = _configured_source_sessions(sup, rpc_key)
    asyncio.run(
        serve(
            sup,
            {"tool_gateway": rpc_key},
            os.environ.get("AIAGENT_SANDBOX_LISTEN", "vsock:4294967295:7000"),
            source_sessions=source_sessions,
        )
    )


if __name__ == "__main__":
    main()
