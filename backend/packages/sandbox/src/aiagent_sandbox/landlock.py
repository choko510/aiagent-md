"""Per-execution Landlock filesystem confinement.

No path outside the current task workspace is writable, and no other task/tenant workspace
is readable. The small system allowlist contains executable/runtime files only. This is a
mandatory layer, NOT a substitute for the separate execution-VM kernel and network namespace.
Requires Linux Landlock; unsupported kernels fail closed rather than running unrestricted.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

# The Landlock syscall numbers are shared by supported Linux architectures (x86_64/aarch64).
_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446
_CREATE_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13  # ABI 2
_TRUNCATE = 1 << 14  # ABI 3
_IOCTL_DEV = 1 << 15  # ABI 5

_READ = _READ_FILE | _READ_DIR
_WRITE = (
    _WRITE_FILE
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_CHAR
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_BLOCK
    | _MAKE_SYM
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _syscall(libc: ctypes.CDLL, nr: int, *args: object) -> int:
    result = libc.syscall(nr, *args)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(result)


def restrict_to_workspace(workspace: Path) -> None:
    """Invoke in the already-forked child before exec; raises if confinement cannot be applied.

    No /proc, parent workspace directory, /root, /run/secrets, or broad /etc access.
    The dynamic loader and Python standard library can read their system runtime paths.
    """
    if os.uname().machine not in ("x86_64", "aarch64"):
        raise OSError("unsupported Landlock syscall architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _syscall(libc, _SYS_CREATE, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_VERSION))
    handled = _EXECUTE | _READ | _WRITE
    if abi >= 2:
        handled |= _REFER
    if abi >= 3:
        handled |= _TRUNCATE
    if abi >= 5:
        handled |= _IOCTL_DEV
    ruleset = _RulesetAttr(handled)
    fd = _syscall(libc, _SYS_CREATE, ctypes.byref(ruleset), ctypes.sizeof(ruleset), ctypes.c_uint32(0))
    try:

        def allow(path: Path, rights: int) -> None:
            if not path.exists():
                return
            # O_PATH does not grant the child access to the opened target after close.
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                attr = _PathBeneathAttr(rights, pfd)
                _syscall(
                    libc,
                    _SYS_ADD,
                    ctypes.c_int(fd),
                    ctypes.c_int(_RULE_PATH_BENEATH),
                    ctypes.byref(attr),
                    ctypes.c_uint32(0),
                )
            finally:
                os.close(pfd)

        # Executables, standard libraries and runtime files only. Never allow the
        # supervisor's parent cell directory or a sibling workspace.
        for system in ("/usr", "/bin", "/lib", "/lib64", "/sbin"):
            allow(Path(system), _READ | _EXECUTE)
        for runtime_file in ("/etc/ld.so.cache", "/etc/localtime", "/dev/null", "/dev/urandom"):
            allow(Path(runtime_file), _READ_FILE | _WRITE_FILE)
        rights = _READ | _WRITE | _EXECUTE
        if abi >= 2:
            rights |= _REFER
        if abi >= 3:
            rights |= _TRUNCATE
        if abi >= 5:
            rights |= _IOCTL_DEV
        allow(workspace, rights)
        # Sibling cells share a numeric UID; block ptrace/process_vm_readv even if
        # their PIDs can be guessed, independently of the host's Yama policy.
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE = 4
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        _syscall(libc, _SYS_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0))
    finally:
        os.close(fd)
