"""Per-execution Landlock filesystem confinement.

No path outside the current task workspace is writable, and no other task/tenant workspace
is readable. The small system allowlist contains executable/runtime files only. This is a
mandatory layer, NOT a substitute for the separate execution-VM kernel and network namespace.
Requires Linux Landlock; unsupported kernels fail closed rather than running unrestricted.
"""

from __future__ import annotations

import ctypes
import errno
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


def disable_dumpability() -> None:
    """Block same-UID ptrace after all credential and namespace changes.

    Linux can reset PR_SET_DUMPABLE on a UID/GID transition. Applying it before
    setuid is insufficient to isolate sibling Task processes sharing a UID.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE = 4
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFProg(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def block_cross_task_process_access() -> None:
    """Seccomp-BPF filter inherited across exec: deny same-UID process inspection.

    Dumpability is reset on execve of ordinary binaries, so PR_SET_DUMPABLE=0
    alone CANNOT prevent ptrace against sibling tasks. Fail closed if this
    persistent syscall filter is unavailable.
    """
    machine = os.uname().machine
    if machine == "x86_64":
        arch = 0xC000003E  # AUDIT_ARCH_X86_64
        blocked = (101, 310, 311, 312, 438, 303, 304)
    elif machine == "aarch64":
        arch = 0xC00000B7  # AUDIT_ARCH_AARCH64
        blocked = (117, 270, 271, 272, 438, 264, 265)
    else:
        raise OSError("unsupported seccomp architecture")

    # struct seccomp_data: nr at offset 0, arch at 4. Unknown ABI is fatal.
    prog: list[tuple[int, int, int, int]] = [
        (0x20, 0, 0, 4),  # BPF_LD | BPF_W | BPF_ABS: arch
        (0x15, 1, 0, arch),  # BPF_JMP | BPF_JEQ: jump over KILL on matching arch
        (0x06, 0, 0, 0x80000000),  # SECCOMP_RET_KILL_PROCESS
        (0x20, 0, 0, 0),  # load syscall number
    ]
    if machine == "x86_64":
        # The x32 ABI shares AUDIT_ARCH_X86_64; reject the entire x32 syscall
        # range so the filter cannot be bypassed with __X32_SYSCALL_BIT.
        prog += [
            (0x45, 0, 1, 0x40000000),  # BPF_JSET x32 bit -> KILL
            (0x06, 0, 0, 0x80000000),
        ]
    for number in blocked:
        prog.extend([
            (0x15, 0, 1, number),  # if syscall matches -> EPERM
            (0x06, 0, 0, 0x00050000 | errno.EPERM),  # SECCOMP_RET_ERRNO
        ])
    prog.append((0x06, 0, 0, 0x7FFF0000))  # SECCOMP_RET_ALLOW

    filters = (_SockFilter * len(prog))(*(_SockFilter(*entry) for entry in prog))
    program = _SockFProg(len(prog), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:  # PR_SET_SECCOMP / SECCOMP_MODE_FILTER
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


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
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        _syscall(libc, _SYS_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0))
    finally:
        os.close(fd)
