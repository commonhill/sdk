"""The cycles kit (kits/cycles) in its own image (kits/cycles/probe.dockerfile), on hand-assembled
programs of the tests' own (GNU as, x86-64, Intel syntax: each comment is its source), each run
twice in the sandbox, which must give the same exit, report and output:

- a load or store outside the program's space (1.75 GiB), at the space's end or in Valgrind's own
  image, or one 8-byte store that crosses the end, ends the run as `escape` (exit 135) with no
  report, before it lands; the space's last 8 bytes count as always, and a fault inside the space
  is the program's own (SIGSEGV);
- an address a system call would follow outside the space fails the call (EFAULT): read's buffer,
  sigaltstack's stack, madvise's range; and a call outside the profile (open) fails with ENOSYS;
- huge pages, whose answers would follow the host's setup: MAP_HUGETLB fails with EINVAL, and
  madvise's huge page advice succeeds and does nothing;
- Valgrind's client requests are ignored: RUNNING_ON_VALGRIND returns the program's own default,
  and CACHEGRIND_STOP_INSTRUMENTATION doesn't stop the count;
- the clocks and the time-stamp counter read the same on every run, from the patch's fixed start;
- a second thread ends the run (exit 136, `thread`); a run over its cap ends as `over`, either at
  the running cap (exit 137) or on its estimate after the run;
- framed items: each item's start has its counts, the same every run;
- the memory pass sees a 64 MiB mapping;
- the program's start (argv, environment, auxiliary vector) and layout (stack, image, brk segment,
  mappings) are the same from any path and under other rlimits, and are the values recorded on
  fuji-1, so CI's hosts are checked against them;
- x87 arithmetic costs what its SSE2 twin does (FDIV as DIVSD, FSQRT as SQRTSD), and FSIN its
  measured cost, not the table's one-cycle default (PER-492).
"""

import hashlib
import importlib.util
import json
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
KIT = ROOT / "kits" / "cycles"
TIMEOUT_S = 3600
OUTPUT_TAIL = 8000


def _helpers() -> Any:
    """test_insncap_kit.py, for its hand-made static programs (tests aren't a package)."""
    spec = importlib.util.spec_from_file_location(
        "insncap_kit_test_helpers", ROOT / "kits" / "insncap" / "tests" / "test_insncap_kit.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPERS = _helpers()
cat, elf = HELPERS.cat, HELPERS.elf


def _kit() -> Any:
    spec = importlib.util.spec_from_file_location(
        "cycles_kit_sandbox", KIT / "project" / "grader" / "sandbox.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SANDBOX = _kit()


def docker(*args: str) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, timeout=TIMEOUT_S, check=False)
    if done.returncode != 0:
        tail = (done.stdout + done.stderr).decode(errors="replace")[-OUTPUT_TAIL:]
        raise AssertionError(f"docker {' '.join(args[:3])}: {done.returncode}\n{tail}")
    return done.stdout.decode()


@pytest.fixture(scope="session")
def image(
    build_once: Callable[[str, object, Callable[[Path], None]], Path],
    build_image: Callable[..., str],
) -> str:
    """The kit's image, built once per session from kits/cycles/project and probe.dockerfile
    (PER-344: `build_image` fetches its pinned downloads first, and fails rather than hangs)."""
    digest = _digest(KIT)
    tag = f"arh-cycles-probe:{digest}"

    def build(path: Path) -> None:
        build_image("-f", str(KIT / "probe.dockerfile"), "-t", tag, str(KIT / "project"), keyed=True)
        path.write_text(tag)

    build_once("cycles-probe", [digest], build)
    return tag


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16]


def readable(path: Path) -> Path:
    """`path` and everything in it readable by anyone (tmp_path is private)."""
    for entry in [path, *path.rglob("*")]:
        if not entry.is_symlink():
            entry.chmod(0o755 if entry.is_dir() else 0o644)
    return path


# The runner in the image: each job of jobs.json run twice, as [exit, report, output (hex)], under
# the job's soft rlimits, if any (inherited by the sandbox and Valgrind).
RUNNER = r"""
import json, resource, sys
from pathlib import Path
sys.path.insert(0, "/kit")
import sandbox
limits = sandbox.Limits(1 << 32, 600, 600.0)
results = {}
for name, job in json.loads(Path("/w/jobs.json").read_text()).items():
    copy = Path("/tmp/kit") / Path(job["program"]).name  # executable (the tests' files aren't +x)
    copy.parent.mkdir(exist_ok=True)
    copy.write_bytes(Path(job["program"]).read_bytes())
    copy.chmod(0o500)
    saved = {}
    for kind, soft in job["rlimits"].items():
        number = getattr(resource, "RLIMIT_" + kind)
        saved[number] = resource.getrlimit(number)
        resource.setrlimit(number, (soft, saved[number][1]))
    runs = []
    for _ in range(2):
        out = bytearray()
        keep = lambda c: out.extend(c) or True
        if job["kind"] == "static":
            with open("/w/input", "rb") as source:
                o = sandbox.run_emulated(copy, job["cap"], source, keep, limits)
        else:
            items = [bytes.fromhex(i) for i in job["items"]]
            o = sandbox.run_framed(copy, job["cap"], items, keep, limits)
        runs.append([o.exit_code, o.report.decode(), out.hex()])
    results[name] = runs
    for number, old in saved.items():
        resource.setrlimit(number, old)
print(json.dumps(results))
"""

CAP = 10**12


def run_jobs(image: str, tmp_path: Path, jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Each job ({"code": machine code, "cap": ..., "items": [...]}) run twice in the image; the
    two runs must be the same. Returns each job's first run as (exit, report, output)."""
    spec = {}
    for name, job in jobs.items():
        (tmp_path / f"{name}.elf").write_bytes(job.get("elf") or elf(job["code"]))
        spec[name] = {
            "kind": "framed" if "items" in job else "static",
            "program": f"/w/{name}.elf",
            "cap": job.get("cap", CAP),
            "items": [i.hex() for i in job.get("items", [])],
            "rlimits": job.get("rlimits", {}),
        }
    (tmp_path / "jobs.json").write_text(json.dumps(spec))
    (tmp_path / "runner.py").write_text(RUNNER)
    (tmp_path / "input").write_bytes(b"x" * 100)
    readable(tmp_path)
    out = docker(
        "run", "--rm", "--network", "none", "-v", f"{tmp_path}:/w:ro", image, "python3", "/w/runner.py"
    )
    results = json.loads(out)
    for name, (first, second) in results.items():
        assert first == second, name
    return {name: (runs[0][0], runs[0][1], bytes.fromhex(runs[0][2])) for name, runs in results.items()}


def report(text: str) -> Any:
    parsed = SANDBOX.parse_report(text.encode())
    assert parsed is not None, text
    return parsed


EXIT0 = "b8e700000031ff0f05"  # mov eax, 231; xor edi, edi; syscall: exit_group(0)
# exit(-rax): mov rdi, rax; neg rdi; mov eax, 231; syscall
EXIT_ERRNO = "4889c748f7dfb8e70000000f05"


def test_an_access_outside_the_programs_memory_ends_the_run_as_escape(image: str, tmp_path: Path) -> None:
    """Every load and store outside the program's space ends the run before it lands, as `escape`
    with no report, the same both runs; accesses inside, up to the last byte, count as always."""
    jobs = {
        # movabs rbx, 0x70100000; mov byte ptr [rbx], 1: in Valgrind's own image
        "store": {"code": bytes.fromhex("48bb0000107000000000c60301" + EXIT0)},
        # mov ebx, 0x70000000; mov al, byte ptr [rbx]: the first byte past the space
        "load": {"code": bytes.fromhex("bb000000708a03" + EXIT0)},
        # mov ebx, 0x6ffffff9; mov qword ptr [rbx], rbx: 8 bytes, the last past the space
        "crossing": {"code": bytes.fromhex("bbf9ffff6f48891b" + EXIT0)},
        # mov ebx, 0x6ffffff8; mov qword ptr [rbx], rbx; mov rax, qword ptr [rbx]: the last 8 bytes
        "last": {"code": bytes.fromhex("bbf8ffff6f48891b488b03" + EXIT0)},
        # mov byte ptr [0], 1: inside the space but unmapped: the program's own fault (SIGSEGV)
        "fault": {"code": bytes.fromhex("c604250000000001" + EXIT0)},
    }
    runs = run_jobs(image, tmp_path, jobs)
    for name in ("store", "load", "crossing"):
        assert runs[name] == (135, "escape\n", b""), name
    exit_code, text, _ = runs["last"]
    assert exit_code == 0
    assert report(text).totals is not None
    exit_code, text, out = runs["fault"]
    assert (exit_code, out, report(text).peak_memory_bytes) == (-11, b"", None)  # no memory pass


def test_an_address_a_system_call_would_follow_must_be_in_the_space(image: str, tmp_path: Path) -> None:
    jobs = {
        # xor eax, eax; xor edi, edi; movabs rsi, 0x70100000; mov edx, 16; syscall: read into
        # Valgrind's image
        "read": {"code": bytes.fromhex("31c031ff48be0000107000000000ba100000000f05" + EXIT_ERRNO)},
        # sub rsp, 32; movabs rax, 0x70100000; mov [rsp], rax; mov qword ptr [rsp + 8], 0;
        # mov qword ptr [rsp + 16], 0x10000; mov rdi, rsp; xor esi, esi; mov eax, 131; syscall:
        # sigaltstack on Valgrind's image, where Valgrind would write signal frames
        "altstack": {
            "code": bytes.fromhex(
                "4883ec2048b80000107000000000488904244"
                "8c74424080000000048c7442410000001004889e731f6b8830000000f05" + EXIT_ERRNO
            )
        },
        # push 0x2f; mov rdi, rsp; xor esi, esi; mov eax, 2; syscall: open("/"), outside the profile
        "open": {"code": bytes.fromhex("6a2f4889e731f6b8020000000f05" + EXIT_ERRNO)},
        # mov edi, 0x70100000; mov esi, 4096; mov edx, 4 (MADV_DONTNEED); mov eax, 28; syscall:
        # madvise on Valgrind's image, which would zero its memory
        "madvise": {"code": bytes.fromhex("bf00001070be00100000ba04000000b81c0000000f05" + EXIT_ERRNO)},
    }
    runs = run_jobs(image, tmp_path, jobs)
    assert runs["read"][0] == 14  # EFAULT
    assert runs["altstack"][0] == 14
    assert runs["madvise"][0] == 14
    assert runs["open"][0] == 38  # ENOSYS
    for name in jobs:
        assert report(runs[name][1]).totals is not None


# xor edi, edi; mov esi, 0x200000; mov edx, 3; mov r10d, FLAGS; mov r8, -1; xor r9d, r9d;
# mov eax, 9; syscall: mmap 2 MiB, read-write, with FLAGS (here private and anonymous, 0x22, or
# those and MAP_HUGETLB, 0x40022)
MAP_2M = "31ffbe00002000ba0300000041ba{flags}49c7c0ffffffff4531c9b8090000000f05"
# mov rdi, rax; mov esi, 0x200000; mov edx, ADVICE; mov eax, 28; syscall: madvise the mapping
ADVISE_2M = "4889c7be00002000ba{advice}b81c0000000f05"


def test_huge_pages_are_refused_or_ignored(image: str, tmp_path: Path) -> None:
    """Huge pages' answers follow the host's setup (its hugetlb pool, transparent huge pages):
    MAP_HUGETLB fails with EINVAL, and madvise's MADV_HUGEPAGE, MADV_NOHUGEPAGE and MADV_COLLAPSE
    succeed and do nothing, on every host."""
    plain = MAP_2M.format(flags="22000000")
    jobs = {
        "hugetlb": {"code": bytes.fromhex(MAP_2M.format(flags="22000400") + EXIT_ERRNO)},
        "hugepage": {"code": bytes.fromhex(plain + ADVISE_2M.format(advice="0e000000") + EXIT_ERRNO)},
        "nohugepage": {"code": bytes.fromhex(plain + ADVISE_2M.format(advice="0f000000") + EXIT_ERRNO)},
        "collapse": {"code": bytes.fromhex(plain + ADVISE_2M.format(advice="19000000") + EXIT_ERRNO)},
    }
    runs = run_jobs(image, tmp_path, jobs)
    assert runs["hugetlb"][0] == 22  # EINVAL
    for name in ("hugepage", "nohugepage", "collapse"):
        assert runs[name][0] == 0, name
    for name in jobs:
        assert report(runs[name][1]).totals is not None


# sub rsp, 64; mov qword ptr [rsp], 0x1001 (RUNNING_ON_VALGRIND); mov rax, rsp; mov edx, 7 (the
# default); the client request preamble (rol rdi, 3; rol rdi, 13; rol rdi, 61; rol rdi, 51;
# xchg rbx, rbx); mov r12, rdx; then mov qword ptr [rsp], 0x43470001
# (CACHEGRIND_STOP_INSTRUMENTATION); mov rax, rsp; xor edx, edx; the preamble; then
# mov ecx, 1000000; 1: dec ecx; jnz 1b; and exit(r12): mov rdi, r12; mov eax, 231; syscall
REQUESTS = bytes.fromhex(
    "4883ec4048c70424011000004889e0ba07000000"
    "48c1c70348c1c70d48c1c73d48c1c7334887db4989d4"
    "48c70424010047434889e031d2"
    "48c1c70348c1c70d48c1c73d48c1c7334887db"
    "b940420f00ffc975fc4c89e7b8e70000000f05"
)


def test_client_requests_are_ignored(image: str, tmp_path: Path) -> None:
    exit_code, text, _ = run_jobs(image, tmp_path, {"requests": {"code": REQUESTS}})["requests"]
    assert exit_code == 7  # RUNNING_ON_VALGRIND: the program's default, as on a CPU
    totals = report(text).totals
    assert totals.instructions > 2_000_000  # the loop, counted after STOP_INSTRUMENTATION
    assert totals.branches >= 1_000_000


# sub rsp, 64; clock_gettime(REALTIME, rsp); clock_gettime(MONOTONIC, rsp + 16); rdtsc into
# [rsp + 32]; rdtscp into [rsp + 40] and its aux into [rsp + 48]; zero [rsp + 52, rsp + 64);
# write(1, rsp, 64); exit_group(0)
CLOCKS = bytes.fromhex(
    "4883ec40b8e400000031ff4889e60f05b8e4000000bf01000000488d7424100f05"
    "0f3189442420895424240f01f9894424288954242c894c2430c744243400000000"
    "48c744243800000000b801000000bf010000004889e6ba400000000f05" + EXIT0
)


def test_the_clocks_and_the_time_stamp_counter_read_the_same_every_run(image: str, tmp_path: Path) -> None:
    """A clock read moves the clocks on by a microsecond: the realtime ones from
    2026-01-01T00:00:00Z, the others from 0. The counter steps by 1024 a read, TSC_AUX 0."""
    exit_code, _, out = run_jobs(image, tmp_path, {"clocks": {"code": CLOCKS}})["clocks"]
    assert exit_code == 0
    realtime, monotonic, tsc, tscp, aux = (
        struct.unpack_from("<qq", out, 0),
        struct.unpack_from("<qq", out, 16),
        struct.unpack_from("<Q", out, 32)[0],
        struct.unpack_from("<Q", out, 40)[0],
        struct.unpack_from("<I", out, 48)[0],
    )
    assert realtime == (1767225600, 1000)
    assert monotonic == (0, 2000)
    assert (tsc, tscp, aux) == (1024, 2048, 0)


# mov edi, 0x50f00 (CLONE_VM | FS | FILES | SIGHAND | THREAD | SYSVSEM); lea rsi, [rsp - 4096];
# xor edx, edx; xor r10d, r10d; xor r8d, r8d; mov eax, 56; syscall: clone
THREAD = bytes.fromhex("bf000f0500488db42400f0ffff31d24531d24531c0b8380000000f05" + EXIT0)
# mov ecx, 10000000; 1: dec ecx; jnz 1b: 20 M instructions
SPIN = bytes.fromhex("b980969800ffc975fc" + EXIT0)


def test_a_thread_or_a_run_over_its_cap_ends_it(image: str, tmp_path: Path) -> None:
    runs = run_jobs(
        image,
        tmp_path,
        {
            "thread": {"code": THREAD},
            "spin": {"code": SPIN},
            "spin capped": {"code": SPIN, "cap": 1_000_000},  # the running cap: 8 M instructions
        },
    )
    assert runs["thread"] == (136, "thread\n", b"")
    exit_code, text, _ = runs["spin capped"]
    assert (exit_code, report(text).over, report(text).totals) == (137, True, None)
    estimate = report(runs["spin"][1]).cycles
    assert estimate is not None
    assert estimate > 1_000_000

    # Within the running cap, but over the cap on its estimate: `over`, with its totals.
    exit_code, text, _ = run_jobs(image, tmp_path, {"spin": {"code": SPIN, "cap": estimate - 1}})["spin"]
    parsed = report(text)
    assert (exit_code, parsed.over, parsed.cycles, parsed.peak_memory_bytes) == (0, True, estimate, None)


def test_items_are_framed_and_each_items_start_is_counted(image: str, tmp_path: Path) -> None:
    items = [b"go\n", b"one\n", b"two two\n", b"three\n"]
    exit_code, text, out = run_jobs(image, tmp_path, {"cat": {"elf": cat(), "items": items}})["cat"]
    assert exit_code == 0
    assert out == b"".join(items)
    parsed = report(text)
    assert len(parsed.items) == len(items)
    starts = [c.instructions for c in parsed.items]
    assert starts == sorted(set(starts))
    assert all(c.instructions > 0 for c in parsed.item_counts())
    assert parsed.peak_memory_bytes is not None


# xor edi, edi; mov esi, 0x4000000; mov edx, 3; mov r10d, 0x22; mov r8, -1; xor r9d, r9d;
# mov eax, 9; syscall: mmap 64 MiB, read-write, private and anonymous; mov byte ptr [rax], 1
BIG_MAP = bytes.fromhex(
    "31ffbe00000004ba0300000041ba2200000049c7c0ffffffff4531c9b8090000000f05c60001" + EXIT0
)


def test_the_memory_pass_sees_every_mapped_page(image: str, tmp_path: Path) -> None:
    runs = run_jobs(image, tmp_path, {"small": {"code": bytes.fromhex(EXIT0)}, "big": {"code": BIG_MAP}})
    small, big = (report(runs[name][1]).peak_memory_bytes for name in ("small", "big"))
    assert small is not None
    assert big is not None
    assert abs((big - small) - (64 << 20)) <= 4096  # the 64 MiB mapping, to a page
    assert report(runs["big"][1]).side_metrics()["peak_memory_bytes"] == big


def pie(code: bytes) -> bytes:
    """`elf`, but position-independent (ET_DYN at 0), as Rust's static-pie programs are: Valgrind
    places it, like their later mappings, in the first free hole of the program's space."""
    headers = 64 + 56
    size = headers + len(code)
    ident = b"\x7fELF" + bytes([2, 1, 1, 0, 0]) + bytes(7)
    ehdr = ident + struct.pack("<HHIQQQIHHHHHH", 3, 0x3E, 1, headers, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    return ehdr + struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, size, size, 0x1000) + code


# mov rbx, rsp; mmap(0, 1 MiB, read-write, private and anonymous) into r12; mov byte ptr
# [r12 + 0x12345], 1; brk(0) into r13; push r13; push r12; push rbx; write(1, rsp, 24): where
# the stack, the mapping and the brk segment are; write(1, rbx, 512): argc, argv, envp, auxv;
# write(1, argv[0], 16); exit_group(0)
LAYOUT = pie(
    bytes.fromhex(
        "4889e331ffbe00001000ba0300000041ba2200000049c7c0ffffffff4531c9b8090000000f054989c4"
        "41c68424452301000131ffb80c0000000f054989c54155415453bf010000004889e6ba18000000b801"
        "0000000f05bf010000004889deba00020000b8010000000f05bf01000000488b7308ba10000000b801"
        "0000000f05b8e700000031ff0f05"
    )
)
# The auxiliary vector's entries, in order: AT_PHDR, AT_PHENT, AT_PHNUM, AT_PAGESZ, AT_BASE,
# AT_FLAGS, AT_ENTRY, AT_UID, AT_EUID, AT_GID, AT_EGID, AT_SECURE, AT_CLKTCK, AT_HWCAP, AT_RANDOM,
# AT_PLATFORM, AT_EXECFN.
AUXV_TYPES = [3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 23, 17, 16, 25, 15, 31]
# Recorded on fuji-1 (Xeon E5-2670 v3) with this kit: what LAYOUT writes (the image at 64 MiB,
# the brk segment right after it, the mapping 8 MiB on, the first stack pointer at 0x6f800000),
# and its report.
# CI's runners must give the same; a change to the kit that moves them changes these with it.
LAYOUT_OUTPUT_SHA256 = "a831d7d47c557fcf0d6eb461177cd511986ddfe4e501366c8d42d80fe49c67b0"
LAYOUT_REPORT = "totals 36 4 4 1 1 1 4 2 2 0 0 0 0 2019 2807\npeak 9457664\n"


def test_the_programs_start_and_layout_are_the_same_on_every_host(image: str, tmp_path: Path) -> None:
    """Where the program's stack, image, mappings and brk segment lie, and what its start holds
    (argv, an empty environment, a fixed auxiliary vector), don't depend on the host: not on the
    program's path, the inherited rlimits, the kernel or the CPU. Cache misses follow addresses,
    so the estimate is exact across hosts only if these are; CI checks them against the values
    recorded on fuji-1."""
    jobs = {
        "layout": {"elf": LAYOUT},
        "layout-from-a-longer-path-with-a-larger-stack": {"elf": LAYOUT, "rlimits": {"STACK": 64 << 20}},
        "l": {"elf": LAYOUT, "rlimits": {"STACK": 2 << 20}},
    }
    runs = run_jobs(image, tmp_path, jobs)
    assert runs["layout"] == runs["layout-from-a-longer-path-with-a-larger-stack"] == runs["l"]
    exit_code, text, out = runs["layout"]
    assert exit_code == 0
    stack, mapping, brk = struct.unpack("<3Q", out[:24])
    start = out[24:536]
    argc, argv0, argv_end, env_end = struct.unpack("<4Q", start[:32])
    assert (argc, argv_end, env_end) == (1, 0, 0)  # one argument, no environment
    types: list[int] = []
    for offset in range(32, len(start), 16):
        kind, _ = struct.unpack("<2Q", start[offset : offset + 16])
        if kind == 0:
            break
        types.append(kind)
    assert types == AUXV_TYPES
    assert out[536:544] == b"program\0"
    layout = f"stack {stack:#x}, mapping {mapping:#x}, brk {brk:#x}, argv[0] at {argv0:#x}"
    digest = hashlib.sha256(out).hexdigest()
    assert (digest, text) == (LAYOUT_OUTPUT_SHA256, LAYOUT_REPORT), f"{layout}: {digest} {text!r}"


X87_SETUP = "d9e8d9e8"  # fld1; fld1: st0 = st1 = 1.0, which each x87 op below keeps
# mov rax, 0x3ff0000000000000; movq xmm0, rax; movq xmm1, rax: xmm0 = xmm1 = 1.0
SSE_SETUP = "48b8000000000000f03f66480f6ec066480f6ec8"
REPEATS = 1000


def test_x87_costs_what_its_sse_twin_does_or_its_measured_cost(image: str, tmp_path: Path) -> None:
    """PER-492: the table has no x87 rows, so its default once priced FDIV, FSQRT and FSIN at one
    cycle each, under DIVSD's 5/13 and SQRTSD's 8.4/21: the same work, cheaper in x87. Each op is
    run 1,000 times after its setup; its own Ct and Cl (its program's less the setup's alone) are
    its twin's, and FSIN's are its measured 38 and 156 cycles."""
    ops = {
        "fdiv": (X87_SETUP, "d8f1"),  # fdiv st(0), st(1)
        "fsqrt": (X87_SETUP, "d9fa"),  # fsqrt
        "fsin": (X87_SETUP, "d9fe"),  # fsin
        "divsd": (SSE_SETUP, "f20f5ec1"),  # divsd xmm0, xmm1
        "sqrtsd": (SSE_SETUP, "f20f51c0"),  # sqrtsd xmm0, xmm0
    }
    jobs = {name: {"code": bytes.fromhex(setup + op * REPEATS + EXIT0)} for name, (setup, op) in ops.items()}
    jobs["x87"] = {"code": bytes.fromhex(X87_SETUP + EXIT0)}
    jobs["sse"] = {"code": bytes.fromhex(SSE_SETUP + EXIT0)}
    runs = run_jobs(image, tmp_path, jobs)
    totals = {name: report(text).totals for name, (_, text, _) in runs.items()}

    def each(name: str) -> tuple[float, float]:
        own = totals[name] - totals["x87" if ops[name][0] == X87_SETUP else "sse"]
        assert own.instructions == REPEATS, name
        return own.ct / REPEATS, own.cl / REPEATS

    assert each("divsd") == (500, 1300)
    assert each("sqrtsd") == (840, 2100)
    assert each("fdiv") == each("divsd")
    assert each("fsqrt") == each("sqrtsd")
    assert each("fsin") == (3800, 15600)
