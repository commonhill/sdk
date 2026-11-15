"""The cycles kit (kits/cycles), without Docker: its files, the formula, the report read, the
running cap, the patch's client space and items fed one frame an answer
(test_cycles_kit_docker.py runs the kit; a project's CI checks its copy with tools/vendor.py)."""

import importlib.util
import os
import re
import struct
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
KIT = ROOT / "kits" / "cycles" / "project"
FILES = sorted(p.relative_to(KIT) for p in KIT.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_the_kit_is_the_patch_and_the_sandbox() -> None:
    assert [str(p) for p in FILES] == ["grader/native/valgrind.patch", "grader/sandbox.py"]


def _sandbox() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cycles_kit_sandbox", KIT / "grader" / "sandbox.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SANDBOX = _sandbox()
PATCH = (KIT / "grader" / "native" / "valgrind.patch").read_text()


def counts(**values: int) -> Any:
    names = [f.name for f in SANDBOX.fields(SANDBOX.Counts)]
    return SANDBOX.Counts(*(values.get(name, 0) for name in names))


def test_the_estimate_is_codspeeds_instruction_cycles_plus_the_misses_costs() -> None:
    assert (SANDBOX.FORMULA, SANDBOX.L1_COST, SANDBOX.LL_COST) == ("cycles-v1", 20, 200)
    # sqrt(400 * 900) = 600 centi-cycles: 6 cycles; then 3 L1 misses the LL serves (5 - 2) and 2
    # LL misses.
    c = counts(ct=400, cl=900, i1_misses=1, d1_read_misses=3, d1_write_misses=1, dl_read_misses=2)
    assert (c.icycles, c.l1_misses, c.ll_misses) == (6, 5, 2)
    assert c.cycles == 6 + 20 * 3 + 200 * 2


def test_instruction_cycles_round_down_and_need_both_bounds() -> None:
    assert counts(ct=101, cl=101).icycles == 1
    assert counts(ct=199, cl=199).icycles == 1
    assert counts(ct=10**6, cl=0).icycles == 0


REPORT_ITEM = " ".join(str(i) for i in range(1, 16))
REPORT_TOTALS = " ".join(str(10 * i) for i in range(1, 16))


@pytest.mark.parametrize(
    ("report", "parsed"),
    [
        (f"totals {REPORT_TOTALS}\npeak 4096\n".encode(), (True, 0, 4096, False, False, False)),
        (
            f"item {REPORT_ITEM}\nitem {REPORT_ITEM}\ntotals {REPORT_TOTALS}\n".encode(),
            (True, 2, None, False, False, False),
        ),
        (f"item {REPORT_ITEM}\nover 100\n".encode(), (False, 1, None, True, False, False)),
        (f"totals {REPORT_TOTALS}\nover 100\n".encode(), (True, 0, None, True, False, False)),
        (b"thread\n", (False, 0, None, False, True, False)),
        (b"escape\n", (False, 0, None, False, False, True)),
        (b"", (False, 0, None, False, False, False)),
    ],
    ids=["totals", "items", "over", "over-the-estimate", "thread", "escape", "nothing"],
)
def test_a_report_is_parsed(report: bytes, parsed: tuple[object, ...]) -> None:
    got = SANDBOX.parse_report(report)
    assert got is not None
    assert (
        got.totals is not None,
        len(got.items),
        got.peak_memory_bytes,
        got.over,
        got.thread,
        got.escape,
    ) == parsed


@pytest.mark.parametrize(
    "report",
    [
        f"totals {REPORT_TOTALS}\ntotals {REPORT_TOTALS}\n".encode(),
        b"peak 1\npeak 2\n",
        b"totals 1 2 3\n",
        f"totals {REPORT_TOTALS} -1\n".encode(),
        b"peak 01\n",
        b"hello\n",
    ],
    ids=["totals-twice", "peak-twice", "short", "negative", "leading-zero", "other"],
)
def test_a_report_the_patch_never_writes_is_none(report: bytes) -> None:
    assert SANDBOX.parse_report(report) is None


def test_each_items_counts_run_to_the_next_items_start_and_the_last_to_the_exit() -> None:
    report = SANDBOX.parse_report(
        b"item" + b" 0" * 15 + b"\nitem" + b" 10" * 15 + b"\ntotals" + b" 25" * 15 + b"\n"
    )
    first, second = report.item_counts()
    assert (first.instructions, second.instructions) == (10, 15)
    assert (first.ct, second.cl) == (10, 15)


def test_the_side_metrics_say_why_the_estimate_moved() -> None:
    report = SANDBOX.parse_report(f"totals {REPORT_TOTALS}\npeak 8192\n".encode())
    assert report.side_metrics() == {
        "cycles": report.cycles,
        "instructions": 10,
        "instruction_cycles": report.totals.icycles,
        "l1_misses": 20 + 50 + 80,
        "ll_misses": 30 + 60 + 90,
        "mispredicts": 110 + 130,
        "peak_memory_bytes": 8192,
    }


def test_the_running_cap_allows_the_estimates_cap_and_more() -> None:
    """A run is ended at INSTRUCTIONS_PER_CYCLE instructions an estimated cycle of its cap; the cap
    itself is checked on the estimate after the run."""
    assert SANDBOX.max_instructions(1000) == 8000
    options = SANDBOX.cycles_options(1000)
    assert "--max-instructions=8000" in options
    assert {"--cache-sim=yes", "--cycle-estimation=yes", "--branch-sim=yes", *SANDBOX.CACHES} <= set(options)


def test_valgrind_runs_the_tool_directly_with_the_patchs_report_and_profile() -> None:
    argv = SANDBOX._valgrind_argv(SANDBOX.CACHEGRIND, Path("/p/prog"), ["--x"], framed=True)  # pyright: ignore[reportPrivateUsage]
    assert argv[0] == "cachegrind-amd64-linux"
    assert argv[-1] == "/p/prog"
    assert f"--hill-report-fd={SANDBOX.REPORT_FD}" in argv
    assert {"--deterministic-rng=yes", "--hill-frames=yes", "--vgdb=no"} <= set(argv)


def test_the_patchs_client_space_ends_below_valgrinds_image() -> None:
    """The patch keeps all of the program's memory below VG_CLIENT_SPACE_END and moves Valgrind's
    own image (the tools' load address) above it."""
    space = re.search(r"#define VG_CLIENT_SPACE_END \(\(Addr\)(0x[0-9a-fA-F]+)ULL\)", PATCH)
    load = re.search(r'\+\s+valt_load_address_pri_norml="(0x[0-9a-fA-F]+)"', PATCH)
    assert space is not None
    assert load is not None
    assert int(space.group(1), 16) == 0x70000000  # 1.75 GiB
    assert int(load.group(1), 16) >= int(space.group(1), 16)
    assert str(SANDBOX.EXIT_ESCAPE) in re.findall(r"#define HILL_EXIT_ESCAPE (\d+)", PATCH)


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


def test_the_patchs_own_escape_is_reported() -> None:
    """The patch ends a run whose program reaches outside its space with status 135 and no
    report; a program that exits 135 itself has its totals."""
    escaped = SANDBOX._escaped  # pyright: ignore[reportPrivateUsage]
    assert escaped(outcome(135, b"")).report == b"escape\n"
    kept = outcome(135, f"totals {REPORT_TOTALS}\n".encode())
    assert escaped(kept) == kept
    for other in (outcome(1, b""), outcome(135, b"", cap="timeout"), outcome(135, b"", stopped=True)):
        assert escaped(other) == other
