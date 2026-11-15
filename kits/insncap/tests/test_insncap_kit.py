"""The instruction-count kit (kits/insncap; a project's CI checks its copy with tools/vendor.py),
without Docker: its files, the plugin's guest space is the emulator's reserved space, the
tree profile's system calls, the plugin's report read, items fed one frame an answer, and the
sandbox's static profile (the emulator's own) on tiny hand-assembled x86-64 programs: it copies,
refuses a process or a socket, lets a thread start and answers clone3 with ENOSYS
(test_insncap_kit_docker.py runs the kit). The hand-made programs here (`elf`, `cat`, `probe`,
`thread`) are the kit Docker tests' too."""

import importlib.util
import os
import re
import struct
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
KIT = ROOT / "kits" / "insncap" / "project"
FILES = sorted(p.relative_to(KIT) for p in KIT.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_the_kit_is_the_plugin_the_patch_and_the_sandbox() -> None:
    assert [str(p) for p in FILES] == [
        "grader/native/insncap.c",
        "grader/native/qemu.patch",
        "grader/sandbox.py",
    ]


def _sandbox() -> ModuleType:
    spec = importlib.util.spec_from_file_location("insncap_kit_sandbox", KIT / "grader" / "sandbox.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SANDBOX = _sandbox()


def test_the_plugins_guest_space_is_the_emulators_reserved_space() -> None:
    plugin = (KIT / "grader" / "native" / "insncap.c").read_text()
    space = re.search(r"#define GUEST_SPACE UINT64_C\((0x[0-9a-f]+)\)", plugin)
    assert space is not None
    args = list(SANDBOX.EMULATOR_ARGS)
    assert args[args.index("-R") + 1] == space.group(1)


def test_the_tree_profile_lets_the_plugin_open_list_and_read_the_tree() -> None:
    calls = SANDBOX.TREE_CALLS
    assert SANDBOX.EMULATOR_CALLS.items() <= calls.items()
    for name in ("openat", "newfstatat", "getdents64", "readlink", "readlinkat", "getcwd", "fcntl"):
        assert name in calls, name
    for name in ("socket", "execve", "unlink", "rename", "mkdir", "kill"):
        assert name not in calls, name


@pytest.mark.parametrize(
    ("report", "parsed"),
    [
        (b"count 12\n", (12, (), False, False, False)),
        (b"item 5\nitem 9\ncount 40\n", (40, (5, 9), False, False, False)),
        (b"item 5\nover 100\n", (None, (5,), True, False, False)),
        (b"thread\n", (None, (), False, True, False)),
        (b"escape\n", (None, (), False, False, True)),
        (b"", (None, (), False, False, False)),
    ],
    ids=["count", "items", "over", "thread", "escape", "nothing"],
)
def test_a_report_is_parsed(report: bytes, parsed: tuple[object, ...]) -> None:
    got = SANDBOX.parse_report(report)
    assert got is not None
    assert (got.count, got.items, got.over, got.thread, got.escape) == parsed


@pytest.mark.parametrize("report", [b"count 1\ncount 2\n", b"count -1\n", b"count 01\n", b"hello\n"])
def test_a_report_insncap_never_writes_is_none(report: bytes) -> None:
    assert SANDBOX.parse_report(report) is None


def test_items_are_sent_one_frame_an_answer_then_the_end() -> None:
    source, sink = os.pipe()
    feed = SANDBOX._Feed(sink, [b"a\n", b"bb\n"])  # pyright: ignore[reportPrivateUsage]
    try:
        frame = struct.pack("<Q", 2) + b"a\n"
        assert feed.writable() == sink
        feed.write()
        assert os.read(source, 100) == frame
        assert feed.writable() is None  # nothing more until the answer
        feed.answered(b"A")
        assert feed.writable() is None  # an answer is a line
        feed.answered(b"\n")
        feed.write()
        assert os.read(source, 100) == struct.pack("<Q", 3) + b"bb\n" + struct.pack("<Q", 0)
        assert feed.writable() is None  # all sent: the pipe closed
        assert os.read(source, 100) == b""
    finally:
        os.close(source)
        feed.close()


def outcome(exit_code: int, report: bytes, cap: str | None = None, stopped: bool = False) -> object:
    return SANDBOX.Outcome(
        exit_code=exit_code, cap=cap, stopped=stopped, cpu_s=0.0, peak_rss_bytes=0, wall_s=0.0, report=report
    )


def test_the_emulators_own_escape_is_reported_as_the_plugins() -> None:
    """qemu.patch ends a run whose program reaches outside its space with status 135, before the
    plugin reports a count: `_escaped` adds the `escape` line. A program that exits 135 itself has
    its count, and a run killed at a cap or stopped is that, whatever its status."""
    escaped = SANDBOX._escaped  # pyright: ignore[reportPrivateUsage]
    assert escaped(outcome(135, b"item 7\n")).report == b"item 7\nescape\n"
    assert SANDBOX.parse_report(escaped(outcome(135, b"")).report).escape
    for kept in (
        outcome(135, b"count 12\n"),
        outcome(0, b"count 12\n"),
        outcome(137, b"over 5\n"),
        outcome(-9, b"", cap="timeout"),
        outcome(135, b"", stopped=True),
    ):
        assert escaped(kept) == kept


def test_the_patch_checks_every_access_before_it_lands() -> None:
    """qemu.patch's range check is in the translated code's fast path, the slow path and helpers'
    lookup, the atomics' lookup and tlb_vaddr_to_host, each ending the run before the access."""
    patch = (KIT / "grader" / "native" / "qemu.patch").read_text()
    files = re.findall(r"^\+\+\+ b/(\S+)", patch, re.MULTILINE)
    assert files == [
        "target/i386/tcg/misc_helper.c",
        "linux-user/x86_64/elfload.c",
        "tcg/x86_64/tcg-target.c.inc",
        "accel/tcg/user-exec.c",
    ]
    assert "+            jcc = tcg_out_cmp(s, TCG_COND_GTU, addr, last, true, P_REXW);" in patch
    assert patch.count("+    arh_check_range(addr,") == 2  # cpu_mmu_lookup and atomic_mmu_lookup
    assert "+        _exit(135);" in patch
    assert str(SANDBOX.EXIT_ESCAPE) == "135"


def keep(chunk: bytes) -> bool:
    return True


def test_a_tree_path_the_plugins_arguments_cant_carry_is_refused() -> None:
    for path in ("/a:b", "/a,b"):
        with pytest.raises(ValueError, match="can't contain"):
            SANDBOX.run_tree(Path("/x"), [], {}, [Path(path)], 1, [], keep, SANDBOX.Limits(1, 1, 1.0))


# --- hand-made static programs (the kit Docker tests load them from here) -----------------------


def elf(code: bytes) -> bytes:
    """A static executable: one loadable segment (read, execute) holding the headers and `code`."""
    base, headers = 0x400000, 64 + 56
    size = headers + len(code)
    ident = b"\x7fELF" + bytes([2, 1, 1, 0, 0]) + bytes(7)
    ehdr = ident + struct.pack("<HHIQQQIHHHHHH", 2, 0x3E, 1, base + headers, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    return ehdr + struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, size, size, 0x1000) + code


def cat() -> bytes:
    """Copies its input to its output; exits 0 at the end."""
    loop = bytes.fromhex(
        "31c0" "31ff" "4889e6" "ba00000100" "0f05"  # read(0, rsp, 65536)
        "4885c0" "7e14"  # <= 0: done
        "4889c2" "b801000000" "bf01000000" "4889e6" "0f05"  # write(1, rsp, n)
        "ebd9"  # again
    )  # fmt: skip
    done = bytes.fromhex("4889c748f7dfb83c0000000f05")  # exit(-result)
    return elf(bytes.fromhex("4881ec00000100") + loop + done)


# syscall; exit(rax < 0)
SYSCALL_EXIT = bytes.fromhex("0f05" "31ff" "4885c0" "400f9cc7" "b83c000000" "0f05")  # fmt: skip


def probe(number: int, rdi: int = 0, rsi: int = 0) -> bytes:
    """System call `number` with two arguments (and zeros); exits 1 if it failed, else 0."""
    code = (
        b"\xb8"
        + struct.pack("<I", number)
        + b"\xbf"
        + struct.pack("<I", rdi)
        + b"\xbe"
        + struct.pack("<I", rsi)
    )
    return elf(code + bytes.fromhex("31d2" "4531d2" "4531c0") + SYSCALL_EXIT)  # fmt: skip


def thread(wait: bool = False) -> bytes:
    """Starts a thread with clone (CLONE_VM | FS | FILES | SIGHAND | THREAD | SYSVSEM) on a stack
    below its own, which exits at once; exits 0 if clone worked, else 1. With `wait`, sleeps 10 s
    first (nanosleep), so the thread surely starts before the program ends."""
    start = bytes.fromhex(
        "bf000f0500" "488db42400f0ffff" "31d2" "4531d2" "4531c0" "b8380000000f05"
    )  # fmt: skip
    # sub rsp, 16; [rsp] = {10 s, 0 ns}; nanosleep(rsp, NULL)
    nanosleep = bytes.fromhex(
        "4883ec10" "48c704240a000000" "48c744240800000000" "4889e7" "31f6" "b8230000000f05"
    )  # fmt: skip
    sleep = nanosleep if wait else b""
    parent = sleep + bytes.fromhex("31ffb8e70000000f05")  # (nanosleep({10, 0})), exit_group(0)
    failed = bytes.fromhex("bf01000000b8e70000000f05")  # exit_group(1)
    child = bytes.fromhex("31ffb83c0000000f05")  # exit(0)
    # zero: the thread (past parent and failed); negative: failed (past parent)
    test = bytes.fromhex("4885c0") + bytes([0x74, 2 + len(parent) + len(failed), 0x78, len(parent)])
    return elf(start + test + parent + failed + child)


SOCKET, FORK, CLONE, CLONE3 = 41, 57, 56, 435
SIGCHLD = 17


# --- the sandbox's static profile (the emulator's) -----------------------------------------------


def run_static(tmp_path: Path, code: bytes, data: bytes = b"") -> tuple[Any, bytes]:
    path = tmp_path / "program"
    path.write_bytes(code)
    path.chmod(0o755)
    source = tmp_path / "input"
    source.write_bytes(data)
    output = bytearray()

    def take(chunk: bytes) -> bool:
        output.extend(chunk)
        return True

    with source.open("rb") as stdin:
        result = SANDBOX.run_static(path, stdin, take, SANDBOX.Limits(1 << 30, 10, 10.0))
    return result, bytes(output)


def test_cat_round_trips_in_the_static_sandbox(tmp_path: Path) -> None:
    data = bytes(range(256)) * 1000
    result, output = run_static(tmp_path, cat(), data)
    assert result.ok, result
    assert output == data


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("socket", probe(SOCKET, 2, 1)),
        ("fork", probe(FORK)),
        ("clone_a_process", probe(CLONE, SIGCHLD)),
    ],
    ids=["socket", "fork", "clone_a_process"],
)
def test_a_process_or_a_socket_is_refused(tmp_path: Path, name: str, code: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(code)
    path.chmod(0o755)
    assert subprocess.run([path], check=False).returncode == 0, f"{name} works outside"
    result, _ = run_static(tmp_path, code)
    assert result.exit_code == 1, f"{name}: {result}"


def test_clone3_gets_enosys_so_the_c_library_falls_back_to_clone(tmp_path: Path) -> None:
    errno = b"\xb8" + struct.pack("<I", CLONE3) + bytes.fromhex("31ff31f60f0548f7d889c7b83c0000000f05")
    result, _ = run_static(tmp_path, elf(errno))  # exit(-clone3(NULL, 0))
    assert result.exit_code == 38, result  # ENOSYS, not EPERM


def test_a_thread_may_start_the_emulators_need_it(tmp_path: Path) -> None:
    """clone with CLONE_THREAD passes the filter (the emulator's own threads; the plugin refuses
    the program's), and works outside the sandbox too."""
    path = tmp_path / "thread"
    path.write_bytes(thread())
    path.chmod(0o755)
    assert subprocess.run([path], check=False).returncode == 0
    result, _ = run_static(tmp_path, thread())
    assert result.exit_code == 0, result


def test_the_emulators_calls_name_no_process_socket_or_file_writer() -> None:
    refused = {"socket", "connect", "fork", "vfork", "execve", "ptrace", "io_uring_setup", "memfd_create",
               "unlink", "rename", "mkdir", "openat2", "process_vm_writev", "kill"}  # fmt: skip
    assert not refused & set(SANDBOX.EMULATOR_CALLS)
    assert SANDBOX.CLONE not in SANDBOX.EMULATOR_CALLS.values()
