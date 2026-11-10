"""Execution Supervisor - runs INSIDE the Untrusted Execution VM (separate guest kernel; no DB, no secrets, no route
to the Internet). It executes generated commands for admitted `sandbox_exec` intents only:

* authority = a signed, single-use, short-lived grant bound to argv/cwd/run/lease/intent (grant.py);
* per-task workspace; cwd must resolve inside it (symlink escapes refused); scrubbed environment (no inherited vars);
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
from typing import Any

from aiagent_domain.rpcwire import (
    NonceCache,
    RpcAuthError,
    decode_length,
    encode_frame,
    sign_response,
    verify_request,
)

from .grant import GrantError, verify

MAX_OUTPUT = 64 * 1024
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000


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
    cell_uid: int | None = None  # unprivileged identity for generated code (RLIMIT_NPROC is not enforced for root)
    cell_gid: int | None = None
    consumed_path: Path | None = None
    _consumed: set[str] = field(default_factory=set)

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

    def _preexec(self, wall_ms: int, memory_bytes: int) -> Any:
        import resource

        lim = self.limits
        require_netns = self.require_netns
        uid, gid = self.cell_uid, self.cell_gid if self.cell_gid is not None else self.cell_uid

        def fn() -> None:
            os.setsid()  # new session + process group: the whole tree is killable
            if uid is not None and os.geteuid() == 0:
                os.setgroups([])
                os.setgid(gid)  # type: ignore[arg-type]
                os.setuid(uid)
            resource.setrlimit(resource.RLIMIT_CPU, (lim.cpu_s, lim.cpu_s))
            mem = min(memory_bytes, lim.memory_bytes)
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_NPROC, (lim.nproc, lim.nproc))
            resource.setrlimit(resource.RLIMIT_NOFILE, (lim.nofile, lim.nofile))
            resource.setrlimit(resource.RLIMIT_FSIZE, (lim.fsize_bytes, lim.fsize_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if require_netns:
                try:
                    os.unshare(CLONE_NEWUSER | CLONE_NEWNET)  # empty network namespace: loopback down, no route
                except OSError:
                    os._exit(97)  # fail closed: never run without network isolation when it is required
        return fn

    async def execute(self, token: Any, argv: list[str], cwd_rel: str) -> dict[str, Any]:
        g = verify(self.grant_key, token, argv, cwd_rel)
        self._consume(g.grant_id)
        ws, cwd = self.workspace(g.tenant_id, g.task_id, cwd_rel)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(ws), "LANG": "C.UTF-8", "TMPDIR": str(ws)}
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, preexec_fn=self._preexec(g.wall_ms, g.memory_bytes), close_fds=True,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=g.wall_ms / 1000)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, err = await proc.communicate()
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)  # no surviving children after the execution ends
        rc = proc.returncode
        if rc == 97 and self.require_netns:
            return {"status": "REFUSED", "reason": "NETNS_UNAVAILABLE"}
        return {"status": "EXITED", "exitCode": rc, "timedOut": timed_out, "wallMs": int((time.monotonic() - started) * 1000),
                "stdout": out[:MAX_OUTPUT].decode("utf-8", "replace"), "stderr": err[:MAX_OUTPUT].decode("utf-8", "replace"),
                "truncated": len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT}


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
            if caller != "tool_gateway" or method != "exec.run":
                w.write(encode_frame(sign_response(key, nonce, {"type": "error", "code": "FORBIDDEN_METHOD"})))
                return
            try:
                res = await sup.execute(body.get("grant"), list(body.get("argv", [])), str(body.get("cwdRelative", "")))
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
    sup = Supervisor(Path(os.environ.get("AIAGENT_SANDBOX_ROOT", "/var/lib/aiagent/cells")), bytes.fromhex(key_hex),
                     require_netns=os.environ.get("AIAGENT_SANDBOX_REQUIRE_NETNS", "1") == "1",
                     cell_uid=int(uid) if uid else None, cell_gid=int(uid) if uid else None)
    asyncio.run(serve(sup, {"tool_gateway": bytes.fromhex(rpc)}, os.environ.get("AIAGENT_SANDBOX_LISTEN", "vsock:4294967295:7000")))


if __name__ == "__main__":
    main()
