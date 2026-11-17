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
import contextlib
import json
import os
import signal
import sys
import time
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

from .grant import GrantError, verify
from .landlock import block_cross_task_process_access, disable_dumpability, restrict_to_workspace

MAX_OUTPUT = 64 * 1024
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


@dataclass
class Limits:
    cpu_s: int = 30
    memory_bytes: int = 512 * 1024 * 1024
    nproc: int = 64
    nofile: int = 256
    fsize_bytes: int = 64 * 1024 * 1024


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
    _consumed: set[str] = field(default_factory=set)
    _active: dict[str, tuple[str, str, str, asyncio.Event]] = field(default_factory=dict)
    _heartbeats: dict[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("the execution supervisor requires Linux (kernel rlimits/namespaces)")
        if os.geteuid() == 0 and self.cell_uid in (None, 0):
            raise RuntimeError("refusing to run generated code as root: configure an unprivileged cell uid/gid")
        self.root.mkdir(parents=True, exist_ok=True)
        self.consumed_path = self.consumed_path or self.root / ".consumed-grants"
        if self.consumed_path.exists():
            self._consumed = set(self.consumed_path.read_text().split())

    def _consume(self, gid: str) -> None:
        if gid in self._consumed:
            raise GrantError("REPLAYED")
        self._consumed.add(gid)
        assert self.consumed_path is not None
        with open(self.consumed_path, "a", encoding="utf-8") as f:
            f.write(gid + "\n")
            f.flush()
            os.fsync(f.fileno())

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

    def _preexec(self, wall_ms: int, memory_bytes: int, workspace: Path, source_fd: int | None = None) -> Any:
        import resource

        lim = self.limits
        require_netns = self.require_netns
        uid, gid = self.cell_uid, self.cell_gid if self.cell_gid is not None else self.cell_uid
        expected_parent = os.getpid()

        def fn() -> None:
            os.setsid()  # new session + process group: the whole tree is killable
            resource.setrlimit(resource.RLIMIT_CPU, (lim.cpu_s, lim.cpu_s))
            mem = min(memory_bytes, lim.memory_bytes)
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_NPROC, (lim.nproc, lim.nproc))
            resource.setrlimit(resource.RLIMIT_NOFILE, (lim.nofile, lim.nofile))
            resource.setrlimit(resource.RLIMIT_FSIZE, (lim.fsize_bytes, lim.fsize_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # Filesystem confinement is MANDATORY even in development / when netns is disabled.
            # Parent/cwd validation alone cannot prevent access to another tenant's files.
            try:
                restrict_to_workspace(workspace, source_fd=source_fd)
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
        if output_fd is not None and (source_fd is None or output_fd == source_fd):
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
            ws, cwd = self.workspace(g.tenant_id, g.task_id, cwd_rel)
            if output_fd is not None:
                try:
                    actual, expected = os.fstat(output_fd), os.stat(cwd, follow_symlinks=False)
                    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                        raise GrantError("OUTPUT_DESCRIPTOR_MISMATCH")
                except OSError as exc:
                    raise GrantError("OUTPUT_DESCRIPTOR_INVALID") from exc
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


async def serve(sup: Supervisor, keys: dict[str, bytes], endpoint: str) -> None:
    nonces = NonceCache()

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            n = decode_length(await r.readexactly(4))
            env = json.loads(await r.readexactly(n))
            try:
                caller, method, body, nonce = verify_request(keys, env, nonces)
            except RpcAuthError:
                w.write(encode_frame({"type": "error", "code": "UNAUTHENTICATED"}))
                return
            key = keys[caller]
            if caller != "tool_gateway" or method not in ("exec.run", "exec.cancel", "exec.heartbeat"):
                w.write(encode_frame(sign_response(key, nonce, {"type": "error", "code": "FORBIDDEN_METHOD"})))
                return
            try:
                if method in ("exec.cancel", "exec.heartbeat"):
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
                else:
                    res = await sup.execute(
                        body.get("grant"), list(body.get("argv", [])), str(body.get("cwdRelative", ""))
                    )
                frame = {"type": "result", "data": res}
            except GrantError as e:
                frame = {"type": "error", "code": f"GRANT_{e}"}
            w.write(encode_frame(sign_response(key, nonce, frame)))
            await w.drain()
        except Exception:
            pass
        finally:
            w.close()

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
    async with srv:
        await srv.serve_forever()


def main() -> None:
    key_hex = Path(os.environ["AIAGENT_SANDBOX_GRANT_KEY_FILE"]).read_text().strip()
    rpc = Path(os.environ["AIAGENT_SANDBOX_RPC_KEY_FILE"]).read_text().strip()
    uid = os.environ.get("AIAGENT_SANDBOX_CELL_UID")
    sup = Supervisor(
        Path(os.environ.get("AIAGENT_SANDBOX_ROOT", "/var/lib/aiagent/cells")),
        bytes.fromhex(key_hex),
        require_netns=os.environ.get("AIAGENT_SANDBOX_REQUIRE_NETNS", "1") == "1",
        cell_uid=int(uid) if uid else None,
        cell_gid=int(uid) if uid else None,
    )
    asyncio.run(
        serve(
            sup, {"tool_gateway": bytes.fromhex(rpc)}, os.environ.get("AIAGENT_SANDBOX_LISTEN", "vsock:4294967295:7000")
        )
    )


if __name__ == "__main__":
    main()
