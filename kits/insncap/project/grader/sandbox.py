"""Run a program with its standard input and output and little else, under a memory, CPU-time and
wall-clock cap: the grader's two kinds of run (standard library only). Part of the instruction-count
kit (sdk/insncap, vendored verbatim into each project that uses it, as grader/sandbox.py).

- `run_emulated`: the worker's program, a static x86-64 executable (`check_static`), under
  qemu-x86_64 with the insncap plugin (native/insncap.c), which counts its instructions, stops it
  over its cap, refuses a second thread and answers its system calls (below). The plugin reports
  on PLUGIN_FD, a pipe of this module's: `Outcome.report` (`parse_report`).
- `run_framed`: the same, on items: its input is a pipe this module writes, one item a frame, the
  next only after the program's answer to the last (a line of its output), and the report counts
  each item's start (insncap.c, "Framed input").
- `run_tree`: a program of the grader's own that reads a tree of files (an interpreter: python3,
  its libraries and the worker's code), under the emulator with insncap's `tree` profile, on items
  as `run_framed`; the tree may be read, listed and executed, and looks the same on every host.
- `run_static`: a static program of the grader's own (a reference decoder, a checker) on the
  worker's untrusted output.

Either runs in a child process that, before the program starts:

- has only file descriptors 0 (its input: a regular file, open read-only, or for items a pipe this
  module writes), 1 (a pipe this module reads), 2 (/dev/null), PLUGIN_FD (a pipe this module reads)
  and EXEC_FD (the program to start, close-on-exec);
- has RLIMIT_AS (address space) and RLIMIT_CPU (CPU seconds) set to the caps, and dumps no core;
- sets PR_SET_NO_NEW_PRIVS;
- restricts itself with Landlock: nothing may be written, created or removed anywhere, and only the
  profile's files may be read or executed (`Profile.readable`): the program alone for a static
  run; for the emulator, qemu-x86_64, the plugin, the program and the system's shared libraries;
  for a tree, also the tree's directories, listed too (`Profile.listable`). Where
  the kernel's Landlock has them, also no TCP or UDP, no unix socket by path, and no signals or
  abstract unix sockets outside the sandbox;
- installs a seccomp filter (`bpf_program`): the profile's system calls run, `execveat` runs only
  on EXEC_FD with AT_EMPTY_PATH (the launch itself), `clone` only to start a thread (CLONE_THREAD:
  the emulator's own; a thread of the program's is refused by the plugin), `clone3` fails with
  ENOSYS (so the C library falls back to `clone`), every other call fails with EPERM, and a call
  through another ABI (x32, i386) kills the process;

and then executes the program with `execveat(EXEC_FD, "", ..., AT_EMPTY_PATH)`. A step that fails
is reported to the parent on a close-on-exec pipe (REPORT_FD), and the run raises
SandboxSetupFailed: the platform's failure, never the program's.

No profile has socket, fork (a clone without CLONE_THREAD), execve, ptrace, io_uring or any call
that writes a file: the programs can read their input, write their output, allocate memory, and,
for the emulator, read its own libraries.

Under the emulator, the program's system calls are insncap's first: it lets through only reads of
its input, writes of its output, memory management and its own signal handling (and for a tree,
read-only files of the tree, as the same tree on every host), answers the clocks from the
instruction count and getrandom from a fixed seed, and refuses the rest (processes, sockets,
timers). What it lets through, the emulator makes under the same filter and Landlock. So
everything the program can observe is the same on every run, and so is its count. And the program
can't reach the emulator's own memory, where the count is: the emulator checks every load and
store of the program's against its reserved space before it happens (native/qemu.patch), and
ends a run that goes past it with status 135 (EXIT_ESCAPE) and no report of the plugin's, which
`_run` reports as the plugin's `escape` line.
"""

import ctypes
import fcntl
import os
import resource
import select
import signal
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, NoReturn

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"  # dockerfile.grader
QEMU = BIN / "qemu-x86_64"
PLUGIN = BIN / "insncap.so"
LIBRARIES = Path("/usr/lib/x86_64-linux-gnu")  # the emulator's (Debian: /lib is /usr/lib)
# What makes a run count the same every time, besides insncap's answers to its system calls and the
# patched time-stamp counter (native/qemu.patch). The reserved guest space (-R, 2 GiB:
# the program's memory) is laid out by the emulator alone, so the program's addresses don't follow
# the host's randomised mappings; the emulated CPU fixes CPUID, so runtime dispatch takes the same
# path everywhere; the seed fixes AT_RANDOM. `run_emulated` adds argv[0], the program's name (its
# path is the grader's to fix).
EMULATOR_ARGS = ("-R", "0x80000000", "-cpu", "Haswell-noTSX", "-seed", "0")

# x86-64 system call numbers.
STATIC_CALLS: dict[str, int] = {
    "read": 0, "write": 1, "close": 3, "fstat": 5, "lseek": 8, "mmap": 9, "mprotect": 10,
    "munmap": 11, "brk": 12, "rt_sigaction": 13, "rt_sigprocmask": 14, "rt_sigreturn": 15,
    "ioctl": 16, "pread64": 17, "readv": 19, "writev": 20, "mremap": 25, "madvise": 28,
    "getpid": 39, "exit": 60, "sigaltstack": 131, "arch_prctl": 158, "gettid": 186,
    "futex": 202, "set_tid_address": 218, "clock_gettime": 228, "exit_group": 231,
    "tgkill": 234, "newfstatat": 262, "set_robust_list": 273, "prlimit64": 302,
    "getrandom": 318, "rseq": 334,
}  # fmt: skip
# The emulator's own needs on top (traced running the baseline): loading its libraries and the
# program (openat, access, readlink), its threads (clone, below; futex), and what it asks of the
# system at startup.
EMULATOR_CALLS: dict[str, int] = STATIC_CALLS | {
    "poll": 7, "access": 21, "sched_yield": 24, "nanosleep": 35, "uname": 63, "readlink": 89,
    "getuid": 102, "getgid": 104, "geteuid": 107, "getegid": 108, "sysinfo": 99, "prctl": 157,
    "sched_getaffinity": 204, "clock_nanosleep": 230, "openat": 257, "ppoll": 271,
    "membarrier": 324,
}  # fmt: skip
# And for a tree, what insncap does on the host for the program (insncap.c, `tree_call`): opening,
# listing and reading the tree's files and links, and their descriptors' flags.
TREE_CALLS: dict[str, int] = EMULATOR_CALLS | {
    "fcntl": 72, "getcwd": 79, "getdents64": 217, "readlinkat": 267, "faccessat": 269,
    "faccessat2": 439,
}  # fmt: skip
CLONE, CLONE3, EXECVEAT = 56, 435, 322
CLONE_THREAD = 0x00010000
PLUGIN_FD = 3  # the plugin's report (insncap.c's fd=), a pipe to the parent
EXEC_FD = 4  # the program's descriptor in the child: the only one execveat may name
REPORT_FD = 5  # the child's report of a failed step, to the parent; closed by the launch
AT_EMPTY_PATH = 0x1000
RULE_FD_BASE = 256  # where the parent keeps the Landlock rules' descriptors, clear of 0-5

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM, ENOSYS = 1, 38
# Classic BPF: load a word of seccomp_data (nr at 0, arch at 4, args[i] at 16 + 8 i: the low 32
# bits), compare, test bits, return.
BPF_LD_W_ABS, BPF_JEQ_K, BPF_JGE_K, BPF_JSET_K, BPF_RET_K = 0x20, 0x15, 0x35, 0x45, 0x06
ARG0, ARG4 = 16, 48
PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 38, 22, 2

# Landlock (linux/landlock.h): system calls, and the rights each ABI version handles.
LANDLOCK_CREATE_RULESET, LANDLOCK_ADD_RULE, LANDLOCK_RESTRICT_SELF = 444, 445, 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
FS_EXECUTE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 2, 1 << 3
# Every filesystem right, by the ABI that added the last: 2 REFER, 3 TRUNCATE, 5 IOCTL_DEV,
# 9 RESOLVE_UNIX.
FS_RIGHTS = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 5: (1 << 16) - 1, 9: (1 << 17) - 1}
NET_RIGHTS = {4: 0b11, 10: 0b1111}  # 4: bind and connect TCP; 10: bind and send UDP too
SCOPES = 0b11  # ABI 6: abstract unix sockets and signals
O_PATH = 0o10000000

CHUNK = 1 << 20  # the most of the program's output read at once
MAX_PLUGIN_REPORT = 1 << 20  # "count <N>\n" and the like, and an "item <N>\n" an item
FRAME_HEADER = 8  # an item's frame: its length, 8 bytes little-endian, then the item
WAIT_POLL_S = 0.05  # how often to check whether a program whose output ended has exited
HIGH_FD = 64  # where the child parks its descriptors before putting them in place
CLOSE_RANGE = 436
SETUP_FAILED = 126  # the child's exit status when it couldn't confine itself or start the program
EXIT_ESCAPE = 135  # the emulator's, for a guest access outside its space (native/qemu.patch)
MAX_REPORT_BYTES = 256

# ELF (elf.h)
PT_DYNAMIC, PT_INTERP, DT_NULL, DT_NEEDED = 2, 3, 0, 1
ET_EXEC, ET_DYN, EM_X86_64 = 2, 3, 62
PHDR_BYTES, DYN_BYTES = 56, 16
MAX_PHDRS = 256


class NotStatic(ValueError):
    """The program isn't a static x86-64 Linux executable."""


class SandboxUnavailable(RuntimeError):
    """The kernel lacks Landlock, so no program can be confined: the platform's problem."""


class SandboxSetupFailed(RuntimeError):
    """The child couldn't confine itself or start the program (its step and errno): the
    platform's problem, never the program's, which didn't run."""


def check_static(fd: int) -> None:
    """Raises NotStatic unless the file open on `fd` is a 64-bit little-endian x86-64 ELF
    executable (ET_EXEC or ET_DYN, a static PIE) with no PT_INTERP and no DT_NEEDED."""
    header = os.pread(fd, 64, 0)
    if len(header) < 64 or header[:6] != b"\x7fELF\x02\x01":
        raise NotStatic("not a 64-bit little-endian ELF")
    e_type, machine = struct.unpack_from("<HH", header, 16)
    phoff = struct.unpack_from("<Q", header, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", header, 54)
    if machine != EM_X86_64 or e_type not in (ET_EXEC, ET_DYN):
        raise NotStatic("not an x86-64 executable")
    if phentsize != PHDR_BYTES or not 0 < phnum <= MAX_PHDRS:
        raise NotStatic("unexpected program headers")
    table = os.pread(fd, PHDR_BYTES * phnum, phoff)
    if len(table) != PHDR_BYTES * phnum:
        raise NotStatic("truncated program headers")
    for i in range(phnum):
        p_type, _, p_offset, _, _, p_filesz = struct.unpack_from("<IIQQQQ", table, i * PHDR_BYTES)
        if p_type == PT_INTERP:
            raise NotStatic("it names a program interpreter")
        if p_type == PT_DYNAMIC:
            dynamic = os.pread(fd, p_filesz, p_offset)
            for at in range(0, len(dynamic) - DYN_BYTES + 1, DYN_BYTES):
                tag = struct.unpack_from("<q", dynamic, at)[0]
                if tag == DT_NULL:
                    break
                if tag == DT_NEEDED:
                    raise NotStatic("it needs a shared library")


def bpf_program(allowed: dict[str, int]) -> bytes:
    """The filter, as `struct sock_filter` entries (module doc): kill another ABI; execveat on
    EXEC_FD with AT_EMPTY_PATH, clone with CLONE_THREAD, and `allowed` run; clone3 gets ENOSYS;
    the rest EPERM."""

    def op(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
        return struct.pack("=HBBI", code, jt, jf, k)

    numbers = sorted(set(allowed.values()) - {CLONE, CLONE3, EXECVEAT})
    n = len(numbers)

    # The layout: 0-5 the ABI checks; 6-10 execveat; 11-13 clone; 14-15 clone3; 16 to 15 + n the
    # allowed calls; then EPERM (16 + n) and ALLOW (17 + n). A jump counts from the next
    # instruction; `allow`/`deny` give the offsets from instruction `at`.
    def allow(at: int) -> int:
        return 17 + n - at - 1

    def deny(at: int) -> int:
        return 16 + n - at - 1

    program = [
        op(BPF_LD_W_ABS, 4),
        op(BPF_JEQ_K, AUDIT_ARCH_X86_64, 1, 0),
        op(BPF_RET_K, SECCOMP_RET_KILL_PROCESS),
        op(BPF_LD_W_ABS, 0),
        op(BPF_JGE_K, X32_SYSCALL_BIT, 0, 1),
        op(BPF_RET_K, SECCOMP_RET_KILL_PROCESS),
        op(BPF_JEQ_K, EXECVEAT, 0, 4),  # 6: not execveat: on to clone
        op(BPF_LD_W_ABS, ARG0),  # 7
        op(BPF_JEQ_K, EXEC_FD, 0, deny(8)),  # 8: another descriptor: EPERM
        op(BPF_LD_W_ABS, ARG4),  # 9
        op(BPF_JEQ_K, AT_EMPTY_PATH, allow(10), deny(10)),  # 10
        op(BPF_JEQ_K, CLONE, 0, 2),  # 11: not clone: on to clone3
        op(BPF_LD_W_ABS, ARG0),  # 12: clone's flags
        op(BPF_JSET_K, CLONE_THREAD, allow(13), deny(13)),  # 13
        op(BPF_JEQ_K, CLONE3, 0, 1),  # 14: not clone3: on to the allowed calls
        op(BPF_RET_K, SECCOMP_RET_ERRNO | ENOSYS),  # 15
    ]
    program += [op(BPF_JEQ_K, nr, allow(16 + i), 0) for i, nr in enumerate(numbers)]
    program += [op(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM), op(BPF_RET_K, SECCOMP_RET_ALLOW)]
    return b"".join(program)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_char_p)]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


@dataclass(frozen=True)
class Profile:
    """What a kind of run may do: its system calls (beyond execveat and clone, always handled as
    the module doc says), the files it may read and execute besides the program itself, and the
    directories it may also list."""

    calls: dict[str, int]
    readable: tuple[Path, ...]
    listable: tuple[Path, ...] = ()


STATIC = Profile(STATIC_CALLS, ())


def emulator_profile(program: Path) -> Profile:
    """The emulator's profile for the program at `program`."""
    return Profile(EMULATOR_CALLS, (PLUGIN, program, LIBRARIES))


def tree_profile(tree: Sequence[Path]) -> Profile:
    """The emulator's profile for a program in `tree`, the directories (and files) it may read,
    list and execute."""
    return Profile(TREE_CALLS, (PLUGIN, LIBRARIES), tuple(tree))


@dataclass(frozen=True)
class Report:
    """What insncap reported (`Outcome.report`): the count at the program's exit (None if it didn't
    exit itself), the count at each item's start (framed runs), and whether the program ended over
    its cap, at a second thread, or at an access outside its memory (the emulator's own end,
    `_escaped`)."""

    count: int | None
    items: tuple[int, ...]
    over: bool
    thread: bool
    escape: bool


def parse_report(report: bytes) -> Report | None:
    """`report` read, or None if it isn't insncap's (a line it never writes, a count twice)."""
    count: int | None = None
    items: list[int] = []
    flags = {"over": False, "thread": False, "escape": False}
    for line in report.decode(errors="replace").splitlines():
        word, _, value = line.partition(" ")
        if word in ("count", "item") and value.isdigit() and value == str(int(value)):
            if word == "item":
                items.append(int(value))
            elif count is None:
                count = int(value)
            else:
                return None
        elif word == "over" and value.isdigit():
            flags["over"] = True
        elif line in ("thread", "escape"):
            flags[line] = True
        else:
            return None
    return Report(count, tuple(items), **flags)


_FILTERS: dict[int, tuple[bytes, _SockFprog]] = {}
_libc = ctypes.CDLL(None, use_errno=True)
_libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
_libc.prctl.restype = ctypes.c_int
_libc.syscall.restype = ctypes.c_long


def _filter(calls: dict[str, int]) -> _SockFprog:
    """The compiled filter for `calls`, made before any fork (the child only calls prctl)."""
    key = id(calls)
    if key not in _FILTERS:
        program = bpf_program(calls)
        _FILTERS[key] = (program, _SockFprog(len(program) // 8, program))
    return _FILTERS[key][1]


def landlock_abi() -> int:
    """The kernel's Landlock ABI version; SandboxUnavailable without one."""
    abi = _libc.syscall(
        LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION)
    )
    if abi < 1:
        raise SandboxUnavailable(f"Landlock is unavailable: {os.strerror(ctypes.get_errno())}")
    return abi


def landlock_ruleset(abi: int) -> tuple[_RulesetAttr, int]:
    """The ruleset for `abi`: every filesystem right it has (and TCP, UDP and scopes, where it has
    them), and the size of the attributes that `abi` reads."""
    fs = FS_RIGHTS[max(v for v in FS_RIGHTS if v <= abi)]
    net = NET_RIGHTS[max(v for v in NET_RIGHTS if v <= abi)] if abi >= 4 else 0
    attr = _RulesetAttr(fs, net, SCOPES if abi >= 6 else 0)
    return attr, 24 if abi >= 6 else 16 if abi >= 4 else 8


@dataclass(frozen=True)
class Limits:
    memory_bytes: int  # RLIMIT_AS
    cpu_s: int  # RLIMIT_CPU
    wall_s: float


@dataclass(frozen=True)
class Outcome:
    """How a run ended. `cap` is "cpu" or "timeout" when it hit one (it was killed), else None;
    `stopped` when the output callback asked to stop it; `report` what arrived on PLUGIN_FD."""

    exit_code: int  # the exit status, or minus the signal that ended it
    cap: str | None
    stopped: bool
    cpu_s: float
    peak_rss_bytes: int
    wall_s: float
    report: bytes

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.cap is None and not self.stopped


def _fail(step: str) -> NoReturn:
    """In the child: report `step` and the errno it left on REPORT_FD, and exit."""
    try:
        os.write(REPORT_FD, f"{step}: errno {ctypes.get_errno()}".encode()[:MAX_REPORT_BYTES])
    finally:
        os._exit(SETUP_FAILED)


def _required(result: int, step: str) -> int:
    """The result of a step the child can't go on without; `_fail`s when it failed."""
    if result < 0:
        _fail(step)
    return result


def _child(
    fds: tuple[int, int, int, int, int, int],
    rule_fds: Sequence[tuple[int, int]],
    limits: Limits,
    abi: int,
    prog: _SockFprog,
    argv: ctypes.Array[ctypes.c_char_p],
    envp: ctypes.Array[ctypes.c_char_p],
) -> NoReturn:
    """After fork: the descriptors in place (`fds`: input, output, error, plugin, program,
    report), the caps, Landlock (a rule on each of `rule_fds`, with its rights), the filter, then
    the program. A step that fails is reported on REPORT_FD (`_fail`), so no program ever runs
    unconfined, and the parent tells the platform's failure from the program's."""
    try:
        parked = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, HIGH_FD) for fd in fds]
        for target, fd in enumerate(parked):
            os.dup2(fd, target, inheritable=target < EXEC_FD)
        os.chdir("/")
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s))
        # No core dump, also where the kernel pipes them to a helper (core_pattern "|..."), which
        # ignores a limit of 0 and keeps a dying program waiting on it: 1 is the kernel's "don't"
        # for pipes, and less than a page for files.
        resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
        _required(_libc.prctl(PR_SET_NO_NEW_PRIVS, 1, None, 0, 0), "no_new_privs")
        attr, size = landlock_ruleset(abi)
        ruleset = _required(
            _libc.syscall(LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), 0),
            "landlock_create_ruleset",
        )
        for fd, rights in ((EXEC_FD, FS_EXECUTE | FS_READ_FILE), *rule_fds):
            rule = _PathBeneathAttr(rights, fd)
            _required(
                _libc.syscall(LANDLOCK_ADD_RULE, ruleset, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0),
                "landlock_add_rule",
            )
        _required(_libc.syscall(LANDLOCK_RESTRICT_SELF, ruleset, 0), "landlock_restrict_self")
        # The system call itself: os.closerange may close descriptors one at a time, up to 2^31.
        # It also closes the ruleset and the rules' descriptors.
        _required(
            _libc.syscall(CLOSE_RANGE, ctypes.c_uint(REPORT_FD + 1), ctypes.c_uint(2**32 - 1), 0),
            "close_range",
        )
        _required(_libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(prog), 0, 0), "seccomp")
        _libc.syscall(EXECVEAT, EXEC_FD, b"", argv, envp, AT_EMPTY_PATH)
        _fail("execveat")
    except BaseException as exc:
        _fail(f"setup {type(exc).__name__}")
    finally:
        os._exit(SETUP_FAILED)


def run_static(program: Path, stdin: BinaryIO, output: Callable[[bytes], bool], limits: Limits) -> Outcome:
    """Run the static `program` (no arguments but its name) on `stdin` (module doc, STATIC).
    Raises NotStatic for a program that isn't a static executable."""
    return _run(program, [program.name], STATIC, stdin, output, limits, static=True)


def _emulated(argv0: str, cap: int, options: str) -> list[str]:
    plugin = f"{PLUGIN},limit={cap},fd={PLUGIN_FD}{options}"
    return [QEMU.name, *EMULATOR_ARGS, "-0", argv0, "-plugin", plugin]


def run_emulated(
    program: Path, cap: int, stdin: BinaryIO, output: Callable[[bytes], bool], limits: Limits
) -> Outcome:
    """Run the program at `program` under qemu-x86_64 with insncap at `cap` instructions on
    `stdin`. Raises NotStatic, before anything runs, when the program isn't a static
    executable; the emulator is the grader's own."""
    with open(program, "rb") as executable:
        check_static(executable.fileno())
    argv = [*_emulated(program.name, cap, ""), str(program)]
    return _run(QEMU, argv, emulator_profile(program), stdin, output, limits, static=False)


def run_framed(
    program: Path, cap: int, items: Sequence[bytes], output: Callable[[bytes], bool], limits: Limits
) -> Outcome:
    """`run_emulated` on `items`: the program reads them as one input, framed (insncap.c), and is
    sent each after its answer to the one before, a line of its output; the report has each
    item's start (`Report.items`)."""
    with open(program, "rb") as executable:
        check_static(executable.fileno())
    argv = [*_emulated(program.name, cap, ",frames=1"), str(program)]
    return _run(QEMU, argv, emulator_profile(program), None, output, limits, static=False, items=items)


def run_tree(
    program: Path,
    args: Sequence[str],
    env: Mapping[str, str],
    tree: Sequence[Path],
    cap: int,
    items: Sequence[bytes],
    output: Callable[[bytes], bool],
    limits: Limits,
) -> Outcome:
    """The program at `program` (the grader's own, in `tree`: an interpreter, say) with `args` and
    `env`, under the emulator with insncap's tree profile, on `items` as `run_framed`. It may
    read, list and execute `tree` alone, as the same tree on every host (insncap.c)."""
    # argv[0] the program's path: an interpreter finds its own files from it. The plugin shows the
    # guest the tree's real paths alone (`roots=`); a path in it can't hold its separators.
    roots = [os.path.realpath(path) for path in tree]
    if any(":" in root or "," in root for root in roots):
        raise ValueError("a tree's paths can't contain ':' or ','")
    options = f",profile=tree,roots={':'.join(roots)},frames=1"
    argv = [*_emulated(str(program), cap, options), str(program), *args]
    envp = [f"{key}={value}" for key, value in env.items()]
    return _run(QEMU, argv, tree_profile(tree), None, output, limits, static=False, items=items, env=envp)


def _run(
    program: Path,
    args: list[str],
    profile: Profile,
    stdin: BinaryIO | None,
    output: Callable[[bytes], bool],
    limits: Limits,
    *,
    static: bool,
    items: Sequence[bytes] = (),
    env: Sequence[str] = (),
) -> Outcome:
    """Run `program` with `args`, the environment `env` (none by default) and cwd /, on `stdin`, a
    regular file open read-only, or else on `items`, framed (`_Feed`), passing each chunk of its
    output to `output`, which returns False to stop it there. Raises SandboxUnavailable on a
    kernel without Landlock, and SandboxSetupFailed when the child couldn't confine itself or
    start the program."""
    if stdin is not None and fcntl.fcntl(stdin.fileno(), fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError("the program's input must be open read-only")
    abi = landlock_abi()
    prog = _filter(profile.calls)
    argv = (ctypes.c_char_p * (len(args) + 1))(*(a.encode() for a in args), None)
    envp = (ctypes.c_char_p * (len(env) + 1))(*(e.encode() for e in env), None)
    opened: list[int] = []
    rule_fds: list[tuple[int, int]] = []
    feed: _Feed | None = None
    try:
        executable = os.open(program, os.O_RDONLY | os.O_CLOEXEC)
        opened.append(executable)
        if static:
            check_static(executable)
        rules = [(path, FS_EXECUTE | FS_READ_FILE) for path in profile.readable]
        rules += [
            (path, FS_EXECUTE | FS_READ_FILE | (FS_READ_DIR if path.is_dir() else 0))
            for path in profile.listable
        ]
        for path, rights in rules:
            fd = os.open(path, O_PATH | os.O_CLOEXEC)
            opened.append(fd)
            high = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, RULE_FD_BASE)
            opened.append(high)
            rule_fds.append((high, rights))
        if stdin is None:
            source, sink = os.pipe()  # close-on-exec: the child's dup2 makes its 0 inheritable
            opened.append(source)
            feed = _Feed(sink, items)
        else:
            source = stdin.fileno()
        out, into = os.pipe()  # all close-on-exec
        plugin, plugging = os.pipe()
        report, reporting = os.pipe()
        try:
            errors = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
            try:
                started = time.monotonic()
                pid = os.fork()
                if pid == 0:
                    fds = (source, into, errors, plugging, executable, reporting)
                    _child(fds, rule_fds, limits, abi, prog, argv, envp)
            finally:
                os.close(errors)
        finally:
            os.close(into)
            os.close(plugging)
            os.close(reporting)
    except BaseException:
        if feed is not None:
            feed.close()
        raise
    finally:
        for fd in opened:
            os.close(fd)
    try:
        failed = _read_report(report)  # until the launch closes it, or the child reports
        if failed:
            os.waitpid(pid, 0)
            raise SandboxSetupFailed(failed)
        outcome = _supervise(pid, out, plugin, output, limits, started, feed)
    finally:
        os.close(out)
        os.close(plugin)
        if feed is not None:
            feed.close()
    return outcome if static else _escaped(outcome)


def _escaped(outcome: Outcome) -> Outcome:
    """An emulated run's outcome, with the emulator's own end for a guest access outside its space
    (status 135, before the plugin could report a count) reported as the plugin's `escape` line.
    A program that exits 135 itself has its count reported."""
    if outcome.exit_code != EXIT_ESCAPE or outcome.cap is not None or outcome.stopped:
        return outcome
    if any(line.startswith(b"count ") for line in outcome.report.splitlines()):
        return outcome
    return replace(outcome, report=outcome.report + b"escape\n")


class _Feed:
    """The items' frames for the program's input pipe (`sink`, the parent's end): the first at
    once, each next one once the program's output has as many lines as items it was sent, and
    after the last, the end (a frame of length 0) and the pipe closed."""

    def __init__(self, sink: int, items: Sequence[bytes]) -> None:
        os.set_blocking(sink, False)
        self.sink: int | None = sink
        self.items = list(items)
        self.sent = 0
        self.answers = 0
        self.pending = b""
        self._queue()

    def _queue(self) -> None:
        while self.sent <= self.answers and self.sent < len(self.items):
            item = self.items[self.sent]
            self.pending += struct.pack("<Q", len(item)) + item
            self.sent += 1
            if self.sent == len(self.items):
                self.pending += struct.pack("<Q", 0)

    def answered(self, chunk: bytes) -> None:
        """The program wrote `chunk`: count its answers, and queue the next frame."""
        self.answers += chunk.count(b"\n")
        self._queue()

    def writable(self) -> int | None:
        """The descriptor to wait on to write the next bytes, if any are waiting."""
        if self.sink is not None and not self.pending and self.sent == len(self.items):
            self.close()  # everything sent: the end
        return self.sink if self.pending else None

    def write(self) -> None:
        assert self.sink is not None
        try:
            done = os.write(self.sink, self.pending)
        except BlockingIOError:
            return
        except BrokenPipeError:  # the program is gone: what it didn't read doesn't matter
            self.pending = b""
            self.sent = len(self.items)
            return
        self.pending = self.pending[done:]

    def close(self) -> None:
        if self.sink is not None:
            os.close(self.sink)
            self.sink = None


def _read_report(report: int) -> str:
    """What the child reported on its way to the program: nothing once the launch closed it."""
    try:
        data = b""
        while chunk := os.read(report, MAX_REPORT_BYTES):
            data += chunk
        return data.decode(errors="replace")
    finally:
        os.close(report)


def _supervise(
    pid: int,
    out: int,
    plugin: int,
    output: Callable[[bytes], bool],
    limits: Limits,
    started: float,
    feed: _Feed | None,
) -> Outcome:
    """Read the program's output and the plugin's report until both end, a cap or the callback
    stops it, writing its items' frames as it answers (`feed`); reap it."""
    cap: str | None = None
    stopped = False
    report = b""
    open_fds = {out, plugin}
    while open_fds:
        left = limits.wall_s - (time.monotonic() - started)
        if left <= 0:
            cap = "timeout"
            break
        sink = feed.writable() if feed is not None else None
        ready, writable, _ = select.select(list(open_fds), [] if sink is None else [sink], [], left)
        if writable and feed is not None:
            feed.write()
        for fd in ready:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                open_fds.discard(fd)
            elif fd == plugin:
                report = (report + chunk)[:MAX_PLUGIN_REPORT]
            else:
                if feed is not None:
                    feed.answered(chunk)
                if not output(chunk):
                    stopped = True
        if stopped:
            break
    if cap is None and not stopped:  # its output ended: give it the rest of its time to exit
        while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if time.monotonic() - started >= limits.wall_s:
                cap = "timeout"
                break
            time.sleep(WAIT_POLL_S)
    if cap is not None or stopped:
        os.kill(pid, signal.SIGKILL)  # unreaped, so the pid is still the child's
    _, status, usage = os.wait4(pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    cpu_s = usage.ru_utime + usage.ru_stime
    # RLIMIT_CPU's soft and hard limits are equal, so the kernel ends it with SIGKILL at the cap
    # (accounted to the tick: allow a second).
    if (
        cap is None
        and not stopped
        and exit_code in (-signal.SIGXCPU, -signal.SIGKILL)
        and cpu_s >= limits.cpu_s - 1
    ):
        cap = "cpu"
    return Outcome(
        exit_code=exit_code,
        cap=cap,
        stopped=stopped,
        cpu_s=round(cpu_s, 3),
        peak_rss_bytes=usage.ru_maxrss * 1024,
        wall_s=round(time.monotonic() - started, 3),
        report=report,
    )
