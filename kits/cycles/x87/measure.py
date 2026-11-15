"""The costs the patch charges the x87 instructions that have no SSE2 twin (PER-492,
valgrind.patch's x87_row in callgrind/cycledecode.c), measured natively: for each, its latency
(each result chained into the next: x <- x0 + 0 * f(x), so the value stays x0 and the dependency
stays) and its reciprocal throughput (x0 reloaded each time), in cycles of user time (`perf stat
-e cycles:u`, best of 9), less a baseline of the glue alone. Each runs at one mid-range argument
(`OPS`): their latency varies with it. FBLD and FBSTP go through memory, so no result chains into
the next: the patch charges the lesser of their two loops' cycles for both.

    python3 kits/cycles/x87/measure.py    needs gcc and perf, on an idle x86-64 core

The patch's numbers are this script's on a Ryzen 5 5600X (Zen 3), rounded to whole cycles:

    fsin 156/38, fcos 182/134, fsincos 149/52, fptan 110/31, fpatan 186/187, f2xm1 86/20,
    fyl2x 120/42, fyl2xp1 136/137, fprem 27/15, fprem1 28/17, fscale 11/2, fxtract 7/4,
    fbld 48 and fbstp 166 (both loops' lesser)
    (latency/reciprocal throughput; fdiv 15/5 and fsqrt 22/10 for scale). Repeat runs on that
    host, under load, agreed to a cycle or two, except fcos's and fyl2x's throughput (to ~15).
"""

import subprocess
import sys
import tempfile
from pathlib import Path

N = 2_000_000
# name: (x0, below x0 on the stack, the op on st0 = x leaving st0 = f(x)); st(1) = 1.0 where the op
# takes a second operand.
OPS = {
    "none": ("1.5", "", ""),
    "fsin": ("1.5", "", "fsin"),
    "fcos": ("1.5", "", "fcos"),
    "fsincos": ("1.5", "", "fsincos\n  fstp st(0)"),
    "fptan": ("0.7", "", "fptan\n  fstp st(0)"),
    "fpatan": ("0.7", "", "fld1\n  fpatan"),
    "f2xm1": ("0.5", "", "f2xm1"),
    "fyl2x": ("1.5", "", "fld1\n  fxch\n  fyl2x"),
    "fyl2xp1": ("0.2", "", "fld1\n  fxch\n  fyl2xp1"),
    "fprem": ("5.3", "fld1", "fprem"),
    "fprem1": ("5.3", "fld1", "fprem1"),
    "fscale": ("1.5", "fld1", "fscale"),
    "fxtract": ("1.5", "", "fxtract\n  fstp st(1)"),
    "fbld": ("1.5", "", "fstp st(0)\n  fbld tbyte ptr [rip + bcd]"),
    "fbstp": ("1.5", "", "fld st(0)\n  fbstp tbyte ptr [rip + buf]"),
    "fsqrt": ("1.5", "", "fsqrt"),
    "fdiv": ("1.5", "fld1", "fdiv st(0), st(1)"),
}
GLUE = {
    "latency": "fmul qword ptr [rip + zero]\n  fadd qword ptr [rip + x0]",
    "throughput": "fstp st(0)\n  fld qword ptr [rip + x0]",
}
PROGRAM = """.intel_syntax noprefix
.globl _start
_start:
  {below}
  fld qword ptr [rip + x0]
  mov ecx, {n}
1:
  {op}
  {glue}
  dec ecx
  jnz 1b
  mov eax, 231
  xor edi, edi
  syscall
.data
x0: .double {x0}
zero: .double 0.0
bcd: .byte 0x45, 0x23, 0x01, 0, 0, 0, 0, 0, 0, 0
buf: .zero 16
"""


def cycles(program: Path) -> int:
    """The program's user-time cycles, the least of nine runs (a busy sibling thread only adds)."""
    best = None
    for _ in range(9):
        command = ["perf", "stat", "-x,", "-e", "cycles:u", str(program)]
        done = subprocess.run(command, capture_output=True, text=True, check=True)
        count = int(done.stderr.strip().splitlines()[-1].split(",")[0])
        best = count if best is None else min(best, count)
    assert best is not None
    return best


def main() -> int:
    measured: dict[tuple[str, str], int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, (x0, below, op) in OPS.items():
            for mode, glue in GLUE.items():
                source = Path(tmp) / f"{name}-{mode}.S"
                source.write_text(PROGRAM.format(below=below or "nop", x0=x0, n=N, op=op or "nop", glue=glue))
                program = source.with_suffix("")
                subprocess.run(["gcc", "-nostdlib", "-static", "-o", str(program), str(source)], check=True)
                measured[name, mode] = cycles(program)
    print(f"{'op':10} {'latency':>8} {'recip tp':>8}")
    for name in OPS:
        if name != "none":
            latency, throughput = (
                (measured[name, mode] - measured["none", mode]) / N for mode in ("latency", "throughput")
            )
            print(f"{name:10} {latency:8.1f} {throughput:8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
