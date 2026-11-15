"""The instruction-count kit (kits/insncap) in its own image (kits/insncap/probe.dockerfile), on
programs of the tests' own:

- a program that loads or stores outside its reserved space (2 GiB) ends as `escape` (exit 135)
  with no count, before the access lands: machine code scanning upward with a SIGSEGV handler,
  plain loads and stores, an 8-byte store that crosses the space's end, and Python's ctypes, in
  the sandbox; and with address randomisation off (so the test knows where they are), ctypes
  aimed at the emulator's code buffer and its data, both loads and stores. Accesses inside its
  space, up to its last byte, count as always, the same every run;
- framed items: the program is sent each item after its answer to the one before, reads only the
  current frame, and the report counts each item's start, the same every run;
- the `tree` profile: Python counts the same on its own tree, on a copy of its standard library in
  a volume, and on a copy in a tmpfs made in reverse order with a file touched; nothing outside the
  tree is there (open, stat, access and readlink all ENOENT, whatever the host has), the
  directories above it stat as directories but can't be listed, it can't open a file to write, and
  every identity is a constant;
- hand-assembled static programs (test_insncap_kit.py's) are counted the same on every run, stopped
  over their cap (exit 137) or at a second thread, and can't open a file, write one, open a socket,
  start a process, write the plugin's report or learn their pid; time, chance and ids are the same
  every run, and a rep string instruction counts its memory accesses.
"""

import hashlib
import importlib.util
import json
import struct
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
KIT = ROOT / "kits" / "insncap"
TIMEOUT_S = 3600
OUTPUT_TAIL = 8000
STDLIB = "/usr/local/lib/python3.13"
# What Python under the emulator reads: its own files, the C library, the dynamic loader's cache,
# and the tests' scripts.
TREE = ["/usr/local", "/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/etc/ld.so.cache", "/w"]


def _helpers() -> Any:
    """test_insncap_kit.py, for its hand-made static programs (tests aren't a package)."""
    spec = importlib.util.spec_from_file_location(
        "insncap_kit_test_helpers", Path(__file__).with_name("test_insncap_kit.py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPERS = _helpers()
cat, elf, probe, thread = HELPERS.cat, HELPERS.elf, HELPERS.probe, HELPERS.thread


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
    """The kit's image, built once per session from kits/insncap/project and probe.dockerfile
    (PER-344: `build_image` fetches its pinned downloads first, and fails rather than hangs)."""
    digest = _digest(KIT)
    tag = f"arh-insncap-probe:{digest}"

    def build(path: Path) -> None:
        build_image("-f", str(KIT / "probe.dockerfile"), "-t", tag, str(KIT / "project"), keyed=True)
        path.write_text(tag)

    build_once("insncap-probe", [digest], build)
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


@pytest.fixture
def volume() -> Iterator[str]:
    name = f"arh-insncap-{uuid.uuid4().hex[:12]}"
    docker("volume", "create", name)
    try:
        yield name
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, check=False)


# The runner in the image: each job of jobs.json run twice, as [exit, report, output].
RUNNER = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, "/kit")
import sandbox
limits = sandbox.Limits(1 << 32, 600, 600.0)
results = {}
for name, job in json.loads(Path("/w/jobs.json").read_text()).items():
    if "program" in job:  # an executable copy (the tests' files are mounted read-only, not +x)
        copy = Path("/tmp/kit") / Path(job["program"]).name
        copy.parent.mkdir(exist_ok=True)
        copy.write_bytes(Path(job["program"]).read_bytes())
        copy.chmod(0o500)
        job["program"] = str(copy)
    runs = []
    for _ in range(2):
        out = bytearray()
        keep = lambda c: out.extend(c) or True
        items = [bytes.fromhex(i) for i in job.get("items", [])]
        if job["kind"] == "static":
            with open("/w/input", "rb") as source:
                o = sandbox.run_emulated(Path(job["program"]), 10**9, source, keep, limits)
        elif job["kind"] == "framed":
            o = sandbox.run_framed(Path(job["program"]), 10**9, items, keep, limits)
        else:
            env = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
            tree = [Path(p) for p in job["tree"]]
            o = sandbox.run_tree(Path("/usr/local/bin/python3"), ["-I", job["script"]], env, tree, 10**11,
                                 items, keep, limits)
        runs.append([o.exit_code, o.report.decode(), out.decode(errors="replace")])
    results[name] = runs
print(json.dumps(results))
"""


def run_jobs(
    image: str, tmp_path: Path, jobs: dict[str, Any], *mounts: str, before: str = ""
) -> dict[str, Any]:
    (tmp_path / "jobs.json").write_text(json.dumps(jobs))
    (tmp_path / "runner.py").write_text(RUNNER)
    (tmp_path / "input").write_bytes(b"x" * 100)
    readable(tmp_path)
    command = f"{before}exec python3 /w/runner.py"
    out = docker(
        "run", "--rm", "--network", "none", "-v", f"{tmp_path}:/w:ro", *mounts, image, "sh", "-c", command
    )
    return json.loads(out)


def program(tmp_path: Path, name: str, code: bytes) -> str:
    (tmp_path / f"{name}.elf").write_bytes(code)
    return f"/w/{name}.elf"


EXIT0 = bytes.fromhex("b8e700000031ff0f05")  # exit_group(0)
# Assembled (GNU as, x86-64): install a SIGSEGV handler that steps over the faulting store (3 bytes),
# then store a byte to each page from 0x555555554000 up, 4096 pages: where the emulator's own image
# is when address randomisation is off. The first store is outside the guest's space, so the run
# ends there, whatever is mapped.
SCAN = bytes.fromhex(
    "4883ec40488d055f0000004889042448c744240804000004488d0554000000488944241048c744241800000000b80d00"
    "0000bf0b0000004889e631d241ba080000000f0548bb0040555555550000b900100000c603014881c300100000ffc975"
    "f2b8e700000031ff0f05488382a800000003c3b80f0000000f05"
)
MOV_RBX = "48bb"  # mov rbx, imm64
OUTSIDE = (0x555555554000).to_bytes(8, "little").hex()
LOAD = bytes.fromhex(MOV_RBX + OUTSIDE + "8a03") + EXIT0  # mov al, [rbx]
STORE = bytes.fromhex(MOV_RBX + OUTSIDE + "c60301") + EXIT0  # mov byte [rbx], 1
# mmap(0x7ffff000, 4096, RW, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED): the space's last page.
LAST_PAGE = "b809000000bf00f0ff7fbe00100000ba0300000041ba3200000049c7c0ffffffff4531c90f05"
# Then mov [rbx], rbx and mov rax, [rbx]: 8 bytes at the address.
LAST = bytes.fromhex(LAST_PAGE + MOV_RBX + (0x7FFFFFF8).to_bytes(8, "little").hex() + "48891b488b03") + EXIT0
CROSSING = bytes.fromhex(LAST_PAGE + MOV_RBX + (0x7FFFFFF9).to_bytes(8, "little").hex() + "48891b") + EXIT0
TREE_ROOTS = ":".join(TREE)
# The emulator's code buffer (its rwx mapping) and the first writable mapping of its own image,
# address randomisation off, found from a sleeping emulator's maps; then Python's ctypes aimed at
# each, a load and a store, its one item what to do. (In the sandbox the emulator's layout is
# random, so this runs it directly: the check is the emulator's own.)
ESCAPES = rf"""
q="setarch x86_64 -R /kit/bin/qemu-x86_64 -R 0x80000000 -cpu Haswell-noTSX -seed 0"
p="-plugin /kit/bin/insncap.so,limit=100000000000,fd=3,profile=tree,roots={TREE_ROOTS},frames=1"
$q /bin/sleep 3 & sleep 1
code=$(grep -m1 ' rwxp ' /proc/$!/maps | cut -d- -f1)
data=$(grep qemu-x86_64 /proc/$!/maps | grep -m1 rw-p | cut -d- -f1)
echo "code $code"
echo "data $data"
for what in "store $code" "load $code" "store $data" "load $data"; do
  python3 /w/frame.py "$what" > in
  $q $p /usr/local/bin/python3 -I /w/ctypes_escape.py < in 3>report > out
  echo "$what: $? $(grep -c count report) $(cat out)"
done
"""
FRAME = """import struct, sys
item = sys.argv[1].encode() + b"\\n"
sys.stdout.buffer.write(struct.pack("<Q", len(item)) + item)
"""
CTYPES = """import ctypes, sys
what, address = sys.stdin.readline().split()
if what == "store":
    ctypes.memset(int(address, 16), 0, 1)
else:
    ctypes.string_at(int(address, 16), 1)
print("survived", flush=True)
"""


def test_an_access_outside_the_programs_memory_ends_the_run_as_escape(image: str, tmp_path: Path) -> None:
    """Every load and store outside the guest's space ends the run before it lands, as `escape`
    with no count, the same both runs; accesses inside, up to the last byte, count as always."""
    (tmp_path / "ctypes_escape.py").write_text(CTYPES)
    (tmp_path / "frame.py").write_text(FRAME)
    static = {"scan": SCAN, "load": LOAD, "store": STORE, "crossing": CROSSING, "last": LAST}
    jobs: dict[str, Any] = {
        name: {"kind": "static", "program": program(tmp_path, name, elf(code))}
        for name, code in static.items()
    }
    # mov [rsp], rsp: a store inside its own memory
    jobs["stack"] = {
        "kind": "static",
        "program": program(tmp_path, "stack", elf(bytes.fromhex("48892424") + EXIT0)),
    }
    for what in ("store", "load"):
        jobs[f"ctypes {what}"] = {
            "kind": "tree",
            "tree": TREE,
            "script": "/w/ctypes_escape.py",
            "items": [f"{what} {0x555555554000:x}\n".encode().hex()],
        }
    runs = run_jobs(image, tmp_path, jobs)
    for name in ("scan", "load", "store", "crossing"):
        assert runs[name] == [[135, "escape\n", ""]] * 2, name
    for what in ("store", "load"):
        first, second = runs[f"ctypes {what}"]
        assert first == second
        exit_code, report, out = first
        assert (exit_code, out) == (135, "")
        assert report.splitlines()[-1] == "escape", report
        assert "count" not in report, report
    for name in ("stack", "last"):
        assert runs[name][0] == runs[name][1], name
        assert runs[name][0][0] == 0, name
        assert str(runs[name][0][1]).startswith("count "), name

    readable(tmp_path)
    out = docker(
        "run", "--rm", "--network", "none", "--security-opt", "seccomp=unconfined", "-v", f"{tmp_path}:/w:ro",
        image, "sh", "-c", ESCAPES,
    )  # fmt: skip
    lines = out.splitlines()
    code, data = (int(line.split()[1], 16) for line in lines[:2])
    assert code >= 0x80000000, "the code buffer is outside the guest's space"
    assert data >= 0x80000000, "the emulator's data is outside the guest's space"
    assert lines[2:] == [
        f"store {code:x}: 135 0 ",
        f"load {code:x}: 135 0 ",
        f"store {data:x}: 135 0 ",
        f"load {data:x}: 135 0 ",
    ]


ECHO = """import sys
for line in sys.stdin:
    print(line.strip().upper(), flush=True)
"""


def test_items_are_framed_and_each_items_start_is_counted(image: str, tmp_path: Path) -> None:
    items = [b"go\n", b"one\n", b"two two\n", b"three\n"]
    (tmp_path / "echo.py").write_text(ECHO)
    jobs = {
        "static": {
            "kind": "framed",
            "program": program(tmp_path, "cat", cat()),
            "items": [i.hex() for i in items],
        },
        "tree": {"kind": "tree", "tree": TREE, "script": "/w/echo.py", "items": [i.hex() for i in items]},
    }
    results = run_jobs(image, tmp_path, jobs)
    assert results["static"][0][2] == "go\none\ntwo two\nthree\n"
    assert results["tree"][0][2] == "GO\nONE\nTWO TWO\nTHREE\n"
    for name in ("static", "tree"):
        first, second = results[name]
        assert first == second, name
        assert first[0] == 0
        lines = first[1].splitlines()
        starts = [int(line.split()[1]) for line in lines if line.startswith("item ")]
        assert len(starts) == len(items)
        assert starts == sorted(starts)
        assert len(set(starts)) == len(items)
        assert lines[-1].startswith("count ")
        assert int(lines[-1].split()[1]) > starts[-1]


PROBE = """import importlib.metadata, json, os, sys
sys.stdin.readline()
seen = {}
for path in ("/proc/self/maps", "/dev/null", "/kit/sandbox.py", "/w/probe.py"):
    try:
        with open(path) as f:
            seen[path] = "read"
    except OSError as e:
        seen[path] = type(e).__name__
try:
    open("/w/new", "w")
    seen["write"] = "opened"
except OSError as e:
    seen["write"] = type(e).__name__
outside = {}
for path in ("/kit/sandbox.py", "/kit/sandbox.py/x", "/w/../kit/sandbox.py", "/bin", "/kit", "/nonexistent"):
    found = []
    for call in (os.stat, os.lstat, os.readlink):
        try:
            call(path)
            found.append("ok")
        except OSError as e:
            found.append(type(e).__name__)
    found.append(os.access(path, os.F_OK))
    outside[path] = found
seen["outside"] = outside
links = {}
for path in ("/w/out", "/w/out/x", "/w/outdir/sandbox.py", "/w/outdir/none/x", "/w/intree"):
    found = []
    for call in (os.stat, os.lstat, os.readlink):
        try:
            call(path)
            found.append("ok")
        except OSError as e:
            found.append(type(e).__name__)
    found.append(os.access(path, os.F_OK))
    links[path] = found
seen["links"] = links
seen["above"] = [os.path.isdir("/"), os.path.isdir("/usr"), os.path.isdir("/usr/lib")]
try:
    os.listdir("/usr")
    seen["list above"] = "listed"
except OSError as e:
    seen["list above"] = type(e).__name__
st = os.stat("/w/probe.py")
seen["stat"] = [st.st_dev, st.st_nlink, st.st_uid, st.st_mtime, st.st_ino == os.stat("/w/probe.py").st_ino]
seen["listing"] = os.listdir("/usr/local/lib/python3.13/json")
seen["ids"] = [os.getpid(), os.getuid(), os.uname().nodename, sorted(os.sched_getaffinity(0)), os.getcwd()]
seen["dists"] = sorted(d.metadata["Name"] for d in importlib.metadata.distributions())
print(json.dumps(seen), flush=True)
"""


def test_a_tree_counts_the_same_wherever_its_files_are_and_hides_the_host(
    image: str, tmp_path: Path, volume: str
) -> None:
    (tmp_path / "probe.py").write_text(PROBE)
    # Links in the tree: out of it, to a file and to a directory the image has, and into it.
    (tmp_path / "out").symlink_to("/kit/sandbox.py")
    (tmp_path / "outdir").symlink_to("/kit")
    (tmp_path / "intree").symlink_to("/w/probe.py")
    jobs = {"probe": {"kind": "tree", "tree": TREE, "script": "/w/probe.py", "items": [b"go\n".hex()]}}
    docker("run", "--rm", "-v", f"{volume}:/out", image, "cp", "-a", f"{STDLIB}/.", "/out/")
    on_image = run_jobs(image, tmp_path, jobs)["probe"]
    on_volume = run_jobs(image, tmp_path, jobs, "-v", f"{volume}:{STDLIB}")["probe"]
    reversed_copy = (
        f"cd /src && find . -mindepth 1 -type d | sort | while read d; do mkdir -p {STDLIB}/$d; done"
        f" && find . -type f | sort -r | while read f; do cp -p $f {STDLIB}/$f; done"
        f" && find . -type l | while read l; do cp -P $l {STDLIB}/$l; done"
        f" && touch {STDLIB}/json/__init__.py && "
    )
    on_tmpfs = run_jobs(
        image,
        tmp_path,
        jobs,
        "-v",
        f"{volume}:/src:ro",
        "--tmpfs",
        f"{STDLIB}:exec,size=1g",
        before=reversed_copy,
    )["probe"]
    assert on_image[0] == on_image[1]
    assert on_image[0][0] == 0
    count = on_image[0][1]
    assert [run[1] for run in on_volume + on_tmpfs] == [count] * 4  # the same count, items and all
    seen = json.loads(on_image[0][2])
    assert seen["/proc/self/maps"] == "FileNotFoundError"
    assert seen["/dev/null"] == "FileNotFoundError"
    assert seen["/kit/sandbox.py"] == "FileNotFoundError"  # outside the tree
    not_there = ["FileNotFoundError"] * 3 + [False]
    assert seen["outside"] == dict.fromkeys(
        ["/kit/sandbox.py", "/kit/sandbox.py/x", "/w/../kit/sandbox.py", "/bin", "/kit", "/nonexistent"],
        not_there,
    )
    assert seen["above"] == [True, True, True]
    # A link out of the tree is there (lstat, readlink: it is the tree's), but nothing past it is,
    # whatever the host has there: ENOENT, never ENOTDIR or EACCES.
    assert seen["links"] == {
        "/w/out": ["FileNotFoundError", "ok", "ok", False],
        "/w/out/x": not_there,
        "/w/outdir/sandbox.py": not_there,
        "/w/outdir/none/x": not_there,
        "/w/intree": ["ok", "ok", "ok", True],
    }
    assert seen["list above"] == "PermissionError"  # Landlock: only the roots are listed
    assert seen["/w/probe.py"] == "read"
    assert seen["write"] == "PermissionError"
    assert seen["stat"] == [1, 1, 0, 1767225600.0, True]
    assert seen["listing"] == sorted(seen["listing"])
    assert seen["ids"] == [2, 0, "arh", [0], "/"]
    assert seen == json.loads(on_tmpfs[0][2])


# --- the plugin's caps, confinement and determinism on static programs ---------------------------


def spin() -> bytes:
    return elf(bytes.fromhex("ebfe"))  # jmp .


# Assembled from (GNU as, x86-64), at these offsets:
#   0x00 clock: sub $64,%rsp; clock_gettime(CLOCK_REALTIME, rsp); clock_gettime(CLOCK_MONOTONIC,
#        rsp+16); gettimeofday(rsp+32, NULL); rdtsc into rsp+48; getrandom(rsp+56, 8, 0);
#        write(1, rsp, 64); exit_group(0)
#   0x69 mapping: mmap(NULL, 2 MiB, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
#   0x8c movs: mov %rax,%rsi; lea 0x100000(%rax),%rdi; mov $N,%ecx; rep movsb
#   0x9d stos: mov %rax,%rdi; mov $N,%ecx; xor %eax,%eax; rep stosb
#   0xa9 cmps: mov %rax,%rsi; lea 0x100000(%rax),%rdi; mov $N,%ecx; repe cmpsb
#   0xba done: exit_group(0)
PROBES = bytes.fromhex(
    "4883ec40b8e400000031ff4889e60f05b8e4000000bf01000000488d7424100f05b860000000488d7c242031f60f050f318944243089542434b83e010000488d7c2438be0800000031d20f05b801000000bf010000004889e6ba400000000f05b8e700000031ff0f05b80900000031ffbe00002000ba0300000041ba2200000049c7c0ffffffff4531c90f054889c6488db800001000b944332211f3a44889c7b94433221131c0f3aa4889c6488db800001000b944332211f3a6b8e700000031ff0f05"
)
N = struct.pack("<I", 0x11223344)  # the placeholder for $N


def clock() -> bytes:
    """Writes what it can learn of time and chance: both clocks, gettimeofday, the time-stamp
    counter and 8 bytes of getrandom (64 bytes in all)."""
    return elf(PROBES[0x00:0x69])


def rep_string(kind: str, n: int, prefixes: str = "") -> bytes:
    """One rep-prefixed string instruction over `n` bytes of a fresh mapping: rep movsb, rep stosb or
    repe cmpsb (two zeroed buffers, so it runs to the end). With `prefixes` (hex), rep movsb is
    those bytes then a4: the same instruction, with REX bytes the CPU ignores among its prefixes."""
    body = {"movs": PROBES[0x8C:0x9D], "stos": PROBES[0x9D:0xA9], "cmps": PROBES[0xA9:0xBA]}[kind]
    assert body.count(N) == 1
    if prefixes:
        assert kind == "movs"
        assert body.endswith(bytes.fromhex("f3a4"))
        body = body[:-2] + bytes.fromhex(prefixes) + b"\xa4"
    return elf(PROBES[0x69:0x8C] + body.replace(N, struct.pack("<I", n)) + PROBES[0xBA:0xC3])


# Assembled the same way: sub $32,%rsp; set_tid_address(NULL) into rsp; rdtscp, its %rcx (TSC_AUX)
# into rsp+8; futex(rsp+24, FUTEX_LOCK_PI, 0, NULL) into rsp+16; write(1, rsp, 32); exit_group(0).
IDS = bytes.fromhex(
    "4883ec20b8da00000031ff0f05488904240f01f948894c2408c744241800000000b8ca000000488d7c2418be06000000"
    "31d24531d20f054889442410b801000000bf010000004889e6ba200000000f05b8e700000031ff0f05"
)
# rep movsb with REX bytes the CPU ignores: before the rep, two of them, and around a legacy prefix.
REX_PREFIXES = {
    "rex_before_rep": "40f3",
    "two_rexes": "f34040",
    "legacy_after_rex": "f34066",
    "rex_after_legacy": "f36640",
}


def open_file(path: bytes, flags: int) -> bytes:
    """openat(AT_FDCWD, path, flags); exits 1 if it failed, else 0."""
    code = b"\xb8" + struct.pack("<I", 257) + b"\xbf" + struct.pack("<I", 0xFFFFFF9C)
    rest = b"\xba" + struct.pack("<I", flags) + bytes.fromhex("0f0531ff4885c0400f9cc7b83c0000000f05")
    return elf(code + bytes.fromhex("488d35") + struct.pack("<i", len(rest)) + rest + path + b"\0")


# Each /w/*.elf run twice under the emulator with a cap of 10,000,000, as [exit, report, output
# length, output in hex].
EMULATED = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, "/kit")
import sandbox
results = {}
limits = sandbox.Limits(1 << 32, 60, 60.0)
for path in sorted(Path("/w").glob("*.elf")):
    program = Path("/tmp/kit") / path.stem
    program.parent.mkdir(exist_ok=True)
    program.write_bytes(path.read_bytes())
    program.chmod(0o500)
    runs = []
    for _ in range(2):
        out = bytearray()
        with open("/w/input", "rb") as source:
            o = sandbox.run_emulated(program, 10_000_000, source, lambda c: out.extend(c) or True, limits)
        runs.append([o.exit_code, o.report.decode(), len(out), out.hex()])
    results[path.stem] = runs
print(json.dumps(results))
"""


def test_programs_under_the_emulator_are_counted_capped_and_confined(image: str, tmp_path: Path) -> None:
    programs = {
        "cat": cat(),
        "spin": spin(),
        "thread": thread(wait=True),
        "fork": probe(56, 17),  # clone(SIGCHLD): a process
        "socket": probe(41, 2, 1),
        "read_eval": open_file(b"/eval/items.jsonl", 0),
        "read_kit": open_file(b"/kit/sandbox.py", 0),
        "write_tmp": open_file(b"/tmp/kit/x", 0o101),  # O_WRONLY | O_CREAT
        "read_self": open_file(b"/tmp/kit/read_self", 0),  # even its own file
        "write_report": probe(1, 3),  # write(3, NULL, 0): the plugin's report (PLUGIN_FD)
        "getpid": probe(39),
        "clock": clock(),
        **{f"{kind}_{n}": rep_string(kind, n) for kind in ("movs", "stos", "cmps") for n in (1, 1 << 20)},
        **{f"movs{label}_{1 << 20}": rep_string("movs", 1 << 20, p) for label, p in REX_PREFIXES.items()},
        "ids": elf(IDS),
    }
    for name, code in programs.items():
        (tmp_path / f"{name}.elf").write_bytes(code)
    (tmp_path / "input").write_bytes(b"x" * 1000)
    (tmp_path / "script.py").write_text(EMULATED)
    evaluation = tmp_path / "eval"
    evaluation.mkdir()
    (evaluation / "items.jsonl").write_text('{"name": "an item"}\n')
    readable(tmp_path)
    out = docker(
        "run", "--rm", "--network", "none", "-v", f"{tmp_path}:/w:ro", "-v", f"{evaluation}:/eval:ro",
        image, "python3", "/w/script.py",
    )  # fmt: skip
    results = json.loads(out)
    count = results["cat"][0][1]
    assert [run[:3] for run in results["cat"]] == [[0, count, 1000]] * 2  # copied, counted the same
    assert count.startswith("count ")
    assert [run[:3] for run in results["spin"]] == [[137, "over 10000000\n", 0]] * 2
    # insncap refuses what the program has no need of: a thread or a process (clone), a socket,
    # any file (its own too), the report's descriptor, the pid.
    for name in ("thread", "fork", "socket", "read_eval", "read_kit", "write_tmp", "read_self"):
        assert [run[0] for run in results[name]] == [1, 1], (name, results[name])
    for name in ("write_report", "getpid"):
        assert [run[0] for run in results[name]] == [1, 1], (name, results[name])
    # Time and chance are the same on every run: the clocks follow the count.
    first, second = results["clock"]
    assert first == second
    assert first[0] == 0
    seen = bytes.fromhex(first[3])
    realtime, monotonic = struct.unpack_from("<qq", seen, 0), struct.unpack_from("<qq", seen, 16)
    assert realtime[0] >= 1767225600  # 2026-01-01, insncap's BASE_S
    assert monotonic[0] == 0
    assert 0 < monotonic[1] < 1000  # a nanosecond an instruction so far
    # The thread id, rdtscp's TSC_AUX and a priority-inheritance futex: none tells the host apart.
    ids = [results["ids"][0][:3], results["ids"][1][:3]]
    assert ids[0] == ids[1]
    assert ids[0][0] == 0
    tid, aux, lock_pi, _ = struct.unpack("<qqqq", bytes.fromhex(results["ids"][0][3]))
    assert (tid, aux, lock_pi) == (2, 0, -38)  # insncap's TID, CPU 0, ENOSYS
    # A rep string instruction counts its memory accesses: a 1 MiB copy, fill or compare costs
    # about a million more than a 1-byte one, not a dozen.
    rep = [name for name in results if name[:4] in ("movs", "stos", "cmps")]
    counts = {name: int(results[name][0][1].split()[1]) for name in rep}
    big = (1 << 20) - 1
    assert counts["movs_1048576"] - counts["movs_1"] >= 2 * big
    assert counts["stos_1048576"] - counts["stos_1"] >= big
    assert counts["cmps_1048576"] - counts["cmps_1"] >= 2 * big
    for label in REX_PREFIXES:
        assert counts[f"movs{label}_1048576"] - counts["movs_1"] >= 2 * big, label
    for name in counts:
        assert results[name][0][:3] == results[name][1][:3], name
        assert results[name][0][0] == 0, name
