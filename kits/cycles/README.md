# The cycles kit

This kit is for speed projects. It measures a static x86-64 program twice, under Valgrind, and gives two numbers that are the same on every run and every host:

- **estimated cycles**, the score: each executed instruction weighted by its cost on a real CPU, plus a cost for each simulated cache miss. CodSpeed's CPU simulation estimates cycles the same way, with the same engine;
- **peak memory**: the most memory the program had mapped at any time.

Its report also says why the estimate moved: instructions, the instructions' own cycles, L1 and last-level misses, mispredicted branches.

It is the successor to the instruction-count kit (`kits/insncap`). An instruction count is exact too, but it counts a `div` the same as a `mov` and can't see a cache miss, so a program can trade work it doesn't count for work it does. Plain counts stay the simpler option, and existing projects keep them until their authors move.

`project/` is laid out as a project's files, and each project vendors it verbatim with `python3 tools/vendor.py copy --kits-only --kit cycles DIR`. `tools/vendor.py check --release N --kit cycles DIR` fails when a copy differs.

| file | what it is |
| -- | -- |
| `grader/sandbox.py` | The sandbox and the measurement: Landlock, seccomp, rlimits and file descriptors, the two passes, the report (`parse_report`) and the formula. Entry points: `run_emulated` (a static program on a file), `run_framed` (on items) and `run_static` (the grader's own helpers, natively). Its module doc is the specification. |
| `grader/native/valgrind.patch` | Commonhill's patch to CodSpeed's Valgrind fork: cycle estimation in Cachegrind, the profile, the memory guard and the report (below). |

## The engine

[valgrind-codspeed](https://github.com/CodSpeedHQ/valgrind-codspeed) is CodSpeed's fork of Valgrind 3.26 (GPL-3.0-or-later), pinned at release `3.26.0-0codspeed13` (commit `346abce5`), with its Capstone submodule (commit `02257584`). CodSpeed's own callgrind decodes each instruction with Capstone and looks it up in a generated table of Zen 4 latency and reciprocal throughput (`callgrind/x86_caps_lut.inc`). The patch compiles that decoder and table into Cachegrind, because Cachegrind simulates the same caches about 3 times faster than callgrind (callgrind spends the difference on its call graph), and changes one thing in them: how x87 is priced (below).

A project's `dockerfile.grader` builds it the way `probe.dockerfile` does, step for step (so the two share Docker's cache):

1. fetch both archives from GitHub (`codeload.github.com`, pinned by SHA-256; add it to `[build] egress`);
2. apply the patch (`patch -p1 --forward`), `./autogen.sh`, `./configure --enable-only64bit`, then build only what the two tools need: `make include/vgversion.h` and `make` in `third_party`, `include`, `VEX`, `coregrind`, `cachegrind` and `massif`;
3. copy the two static tools, `cachegrind/cachegrind-amd64-linux` and `massif/massif-amd64-linux`, into `grader/bin/valgrind/` (`sandbox.py`'s `VALGRIND`).

### x87

Every x87 row in the table is its default, 100/100 centi-cycles (one cycle), whatever the instruction (`FXSAVE` aside); why isn't published, as its generator isn't. So `FDIV` cost an eighth of `DIVSD` (500/1300), `FSQRT` a thirteenth of `SQRTSD` (840/2100), `FILD` a fraction of `CVTSI2SD` (147/800), `FSIN` the same as a `MOV`. A program that did its divisions, roots, conversions or transcendentals in x87 (with the precision control at double, `FDIV` and `FSQRT` round exactly as `DIVSD` and `SQRTSD` do) wrote the same bytes for far fewer estimated cycles (PER-492). Since release 9 the patch (`callgrind/cycledecode.c`, `x87_row`) prices:

- **x87 arithmetic and conversions at their SSE2 twin's row,** the same work on a double: `FADD`, `FSUB(R)(P)`, `FIADD` and `FISUB(R)` as `ADDSD`/`SUBSD`, `FMUL(P)` and `FIMUL` as `MULSD`, `FDIV(R)(P)` and `FIDIV(R)` as `DIVSD`, `FSQRT` as `SQRTSD`, `FRNDINT` as `ROUNDSD`, `FILD` as `CVTSI2SD`, `FIST(P)` as `CVTSD2SI` and `FISTTP` as `CVTTSD2SI`;
- **the rest that compute at a measured cost,** which have no twin: `FSIN`, `FCOS`, `FSINCOS`, `FPTAN`, `FPATAN`, `F2XM1`, `FYL2X`, `FYL2XP1`, `FPREM`, `FPREM1`, `FSCALE`, `FXTRACT`, `FBLD` and `FBSTP`. No Zen 4 measurement of them is published under a licence the kit can use (LLVM's Zen 4 model gives them a 100-cycle placeholder; uops.info lists them as microcode, without numbers), so the costs are Commonhill's own, **measured on Zen 3** (a Ryzen 5 5600X), by `x87/measure.py`: reciprocal throughput and latency in whole cycles, each at one mid-range argument (`FSIN` 38/156 at 1.5; their latency varies with the argument). Its docstring has them all and how to measure them again;
- loads and stores of floats, exchanges, compares, constants and control as before, at one cycle each.

A program that executes none of the repriced instructions gets the same estimate as under release 8 to the cycle.

## The formula

`cycles-v1`, in `sandbox.py` (`FORMULA`, `L1_COST`, `LL_COST`, `Counts.cycles`):

```
icycles   = floor(sqrt(Ct * Cl) / 100)       # CodSpeed's: the throughput- and latency-bound sums, in centi-cycles
l1_misses = I1mr + D1mr + D1mw                # misses in L1 (instructions and data)
ll_misses = ILmr + DLmr + DLmw                # misses in the last-level cache, served by memory
cycles    = icycles + 20 * (l1_misses - ll_misses) + 200 * ll_misses
```

The caches are CodSpeed's, fixed whatever the host has: L1 instruction and data 32 KiB 8-way, last level 8 MiB 16-way, 64-byte lines. The miss costs were fixed before the validation, inside CodSpeed's published ranges (10 to 40 cycles for L2/L3, 100 and up for memory); they weren't fitted to it.

**A reward version pins all of it:** the formula's lines, the patch and the fork's commit are frozen grader files. Changing any of them changes every vendoring project's frozen files, so it is a new reward version for each.

Mispredicted branches (Valgrind's simple predictor) are reported, not scored.

## The cap

`cap` is in estimated cycles. A run whose estimate is over it gets an `over` line after the run, deterministically. While it runs, the patch ends it once it has run `INSTRUCTIONS_PER_CYCLE` (8) instructions for each estimated cycle of its cap (exit 137, `over`), so a program that never ends is stopped too. Real programs run about one instruction an estimated cycle, far below eight, so in practice the running cap ends only runs that are over their cap anyway.

## Memory

The second pass is Massif with `--pages-as-heap=yes`: the peak of the bytes the program had mapped (its heap, anonymous mappings, its stack and its own image), at page granularity, exactly (the patch tracks the peak itself rather than Massif's snapshots). Massif's malloc-level heap can't work here: Valgrind hooks malloc through a preloaded library, and a static program loads none.

The memory pass sees the same program, input, clocks and randomness, so it must write the same output and exit the same way; `sandbox.py` checks both and raises `PassesDiverged` (the platform's problem) when they differ. It runs only for a run that ended itself within its cap.

Report it as a side metric and guard it against the incumbent (`[[guards]]`), or fold it into the score.

## The report

`parse_report(outcome.report)` gives a `Report`:

- `totals`: the counts at the program's end (`Counts`; None if Valgrind ended it: over the running cap, a second thread, an escape); `cycles` is the score. A program a signal killed has its counts up to then, but no memory pass, and its exit is minus the signal;
- `items`: the counts at each item's start (framed runs), and `item_counts()` each item's own;
- `peak_memory_bytes` (None if the memory pass didn't run);
- `over`, `thread`, `escape`;
- `side_metrics()`: `cycles`, `instructions`, `instruction_cycles`, `l1_misses`, `ll_misses`, `mispredicts` and `peak_memory_bytes`, for `write_result(secondary=...)`.

An item's estimate is its own counts' `cycles`. The items' estimates needn't add up to the run's, because `icycles` is a geometric mean: score a framed run on its totals.

## Determinism

Everything the program can observe is the same on every run and under both tools:

- **CPUID** is Valgrind's fixed model (a Haswell i7-4910MQ: AVX2, BMI, FMA, F16C, RDRAND, no AVX-512, which Valgrind doesn't have), so runtime dispatch takes the same path everywhere. Valgrind picks that model, and which instructions it decodes, from the host's features; the patch pins them to Haswell's set, hiding newer ones (RDSEED), and a host without one of them can't run the kit (`m_machine.c`). The cost table is Zen 4's: the code paths are fixed by one model and the costs by another, both fixed.
- **Time** (clock_gettime, gettimeofday, time, clock_getres) moves on by a microsecond a read, the realtime clocks from 2026-01-01T00:00:00Z; sleeping returns at once; the time-stamp counter steps by 1024 a read and TSC_AUX is 0.
- **Randomness:** getrandom from a fixed seed, and `--deterministic-rng` (the fork's) for AT_RANDOM and RDRAND.
- **The program's start:** argv is the one name `program`, the environment is empty, and the auxiliary vector is a fixed one (`initimg-linux.c`: the executable's own entries, page size 4096, ids 1000, AT_HWCAP the CPUID model's, AT_RANDOM's bytes from the fixed seed, platform `x86_64`), not a copy of the host kernel's, whose entries and values follow its version, the CPU and the account.
- **The layout:** Valgrind places the program's memory itself: the image, then the brk segment, then its mappings, from the bottom of its space, and its stack at the top. The fork fixes the startup stack's layout whatever the arguments and environment, and the patch sizes the stack and the brk segment at 8 MiB each, not from the host's RLIMIT_STACK and RLIMIT_DATA. Cache misses follow addresses, so these are what make the misses, and the estimate, the same on every host.
- **The program itself:** the estimate is of these bytes. Code and data that lie elsewhere miss differently, with the same instructions: a build that embeds its paths (Rust's panic locations name cargo's registry directory, whose name follows the crates.io mirror the build used) gives a different estimate. A project's build makes the same bytes on the platform, in CI and on an author's machine (AUTHOR.md, "Speed, counted").
- **Threads:** a second thread ends the run (exit 136, `thread`). Valgrind runs threads one after another, so a parallel speedup wouldn't show anyway.

Measured: the whole report (every count and the peak) and the program's output were the same on two hosts (Xeon E5-2620 v3 and E5-2670 v3, different L3 sizes) for seven oxipng sample images, and on repeat runs. Stock Valgrind (which passes the clocks to the host) isn't: an older oxipng build gave three different instruction counts in six runs on one image under it, and one count in four runs under the kit. Two fresh builds of the tools are the same bytes, as `grader_build` requires. CI checks the start and the layout, and a run's whole report, against values recorded on fuji-1 (`test_the_programs_start_and_layout_are_the_same_on_every_host`).

## The program's reach

A run's integrity is the counts, and they live in Valgrind's memory, in the program's own process. A program that can make any pointer (C, unsafe Rust) could otherwise write them, and any code can make one of Valgrind's client requests, which would let it stop the count or run Valgrind's own functions. The patch closes each way in:

| what | how | covered by |
| -- | -- | -- |
| the program's loads, stores, CAS, LL/SC and helpers' memory accesses | IR the program runs | the guard (`m_translate.c`): before the tool sees a block, every access gets a check that it ends below `VG_CLIENT_SPACE_END` (1.75 GiB); one that doesn't ends the run with status 135 and no report, `escape` |
| where the program's memory is | Valgrind's address-space manager | the client's stack ends at `VG_CLIENT_SPACE_END`, every client mapping the advisor grants ends below it (else the mapping fails with EINVAL, Valgrind's answer when its advisor refuses), Valgrind's own image is loaded there (`configure.ac`) and its own mappings are far above |
| addresses system calls follow (read's buffer, iovecs, futex words, sigaltstack's stack, where Valgrind would write signal frames, and the ranges of mprotect, munmap, mremap and madvise, whose MADV_DONTNEED would zero Valgrind's memory: its own wrapper doesn't check madvise's) | the kernel or Valgrind | the profile (`syswrap-hill.c`): each must be in the space, else EFAULT |
| system calls | the kernel | the profile: reads of descriptor 0, writes to 1 and 2, anonymous mappings or of descriptor 0, memory management and the program's own signal handling; clocks, getrandom, poll and set_tid_address answered; huge pages, whose answers would tell the host's setup, refused (`MAP_HUGETLB`: EINVAL) or ignored (madvise's `MADV_HUGEPAGE`, `MADV_NOHUGEPAGE` and `MADV_COLLAPSE` succeed and do nothing); everything else ENOSYS. Then `sandbox.py`'s seccomp filter and Landlock, under which Valgrind itself runs |
| the report | a descriptor | moved out of the program's range at startup (`VG_(safe_fd)`), and the profile allows writes to 1 and 2 only |
| client requests (`RUNNING_ON_VALGRIND`, `CACHEGRIND_STOP_INSTRUMENTATION`, `CLIENT_CALL*`) | a magic instruction sequence | ignored, before their arguments are read: the program's own default comes back, as on a CPU |

## Overhead

On oxipng 10.2.1 (preset 2, the project's static harness), on a shared Xeon E5-2670 v3 box: the cycles pass alone about 36 times native (diagram-000: 4.4 s against 0.12 s), the memory pass about 6 times, and a whole measurement in the sandbox, both passes, 28 to 50 times native on seven sample images (diagram-000: 4.7 s; photo-000: 16.4 s against 0.58 s). The instruction-count kit runs about 6 times native; CodSpeed's callgrind about 105 times. So a held-out set that takes 28 s natively takes about 20 core-minutes; items are independent, so a grader can measure several at once (`cpu-m` and up).

## Validation

Does the estimate rank changes the way wall time does, and better than a plain count? 16 before/after pairs, each a static program on fixed inputs (PER-458):

- **7 real oxipng changes** from upstream's history between 9.0 and 10.2.1, built the project's way and run on 7 sample images;
- **2 real openvector changes**;
- **7 synthetic C pairs**, five of them built so that the variant with fewer instructions is the slower one (cache misses, a division, prefetching), and one (branchy against branchless) that no estimate here can get right.

Wall time: 30 interleaved runs of each variant on one core of a Haswell Xeon (E5-2670 v3), and 15 of each synthetic variant on a Zen 3 desktop (Ryzen 5600X); a pair is inside the noise when the 95% bootstrap interval of its change contains 0. Δ = (B − A) / A; the estimates are the kit's.

To reproduce: build both variants of each pair as static x86-64 programs (oxipng before and after each upstream change named below, built the project's way, run by its harness on its sample images). Time each natively on one idle core, alternating A and B. Measure each once with `run_emulated` and read `instructions`, `instruction_cycles` and `cycles` from `side_metrics()`; the estimates are deterministic, so once is enough. Then, over the pairs outside the noise, compute Spearman's ρ and Kendall's τ between Δ wall and each metric's Δ, and count the pairs where each metric's Δ has the sign of Δ wall.

| pair | Δ wall, Haswell | Δ wall, Zen 3 | Δ instructions | Δ instruction cycles | Δ cycles |
| -- | -- | -- | -- | -- | -- |
| oxipng: filter heuristics stop early | +0.4% (noise) | | −0.6% | −0.5% | −0.4% |
| oxipng #804 bigrams buffer | −0.6% (noise) | | −3.0% | −3.9% | −3.3% |
| oxipng #676 `chunks_exact` | +0.1% (noise) | | −0.1% | −0.1% | −0.1% |
| oxipng #713 re-filter in evaluation | +0.9% | | +0.9% | +0.9% | +0.8% |
| oxipng #830 filter strategies | −7.7% | | −9.0% | −8.5% | −7.6% |
| oxipng #842 preset rebalance | +0.7% | | +0.5% | +0.5% | +0.5% |
| oxipng #650 all-zero lines | −0.5% | | −0.1% | −0.1% | −0.1% |
| openvector: shifts for constant divisions | −1.0% | | −0.9% | −0.6% | −0.8% |
| openvector: series in fixed 64-bit limbs | −39.7% | | −45.2% | −41.4% | −38.4% |
| array of structs → struct of arrays | −33.8% | −48.3% | +0.0% | −0.9% | −77.7% |
| branchy → branchless | −71.0% | −81.9% | +33.3% | +55.0% | +46.3% |
| `div` → multiply by reciprocal | −75.7% | −22.7% | +44.4% | −13.2% | −13.2% |
| chained → open-addressing hash | −12.9% | −4.2% (noise) | +23.8% | +24.8% | −26.8% |
| linked list → array | −96.8% | −97.8% | −66.6% | −81.1% | −87.4% |
| matrix multiply ijk → ikj | −78.6% | −87.5% | −42.3% | −47.3% | −75.0% |
| binary search → Eytzinger layout | −66.7% | −65.0% | +66.6% | +103.7% | −54.0% |

Agreement with Haswell wall time, over the pairs outside the noise (Spearman ρ, Kendall τ, and how many pairs each picks the faster variant of):

| pairs | instructions | instruction cycles | cycles |
| -- | -- | -- | -- |
| all 16 (13 outside the noise) | ρ 0.11, τ 0.12, 8 of 13 | ρ 0.43, τ 0.38, 10 of 13 | **ρ 0.69, τ 0.63, 12 of 13** |
| the 9 real ones (6) | ρ 0.92, τ 0.78, 6 of 6 | ρ 0.92, τ 0.78, 6 of 6 | ρ 0.93, τ 0.83, 6 of 6 |
| the 7 synthetic ones (7) | ρ 0.43, τ 0.33, 2 of 7 | ρ 0.71, τ 0.62, 4 of 7 | ρ 0.29, τ 0.24, 6 of 7 |

What it says:

- **Where a change trades instructions for memory traffic or for dearer instructions, only the full estimate gets the direction right:** 6 of the 7 synthetic pairs against 2 for the plain count. It misses the branchy/branchless pair, as expected: mispredictions aren't priced.
- **The real changes here are instruction-bound,** so all three metrics pick all 6 measurable ones right and rank them almost alike; the estimate's lead there is one fewer discordant pair. They show the estimate costs nothing where counts already work.
- **Magnitudes are rougher than directions:** a `div` costs far more on Haswell than the Zen 4 table says (the Zen 3 desktop is close to it), and streaming access the hardware prefetches is overpriced. That is why the synthetic pairs' ρ is low while their picks are right.
- **The miss costs barely matter:** over L1 10, 20 or 40 and last level 100, 200 or 300, ρ stays 0.69 to 0.71 and the picks 12 of 13.

## Limits

- Threads run one after another, so parallel speedups aren't rewarded; a second thread ends the run.
- I/O, system calls and branch mispredictions cost nothing in the estimate.
- Static x86-64 programs only. Interpreters reading a tree of files (Python, as AlgoTune's projects do) stay on the instruction-count kit, which emulates their file system calls.
- The program has 1.75 GiB of memory, not the instruction-count kit's 2 GiB: Valgrind's tools must be linked below 2 GiB, and its image sits right above the program's space. A project's PROGRAM.md says so.
- The host needs Haswell's features (AVX2, BMI, FMA, F16C, RDRAND, LZCNT): every runner has them. On one without, Valgrind stops before the program starts.
- The program sees no environment and is named `program`. One that needs a variable sets it in its own code.

## Changing the kit

Change `project/`, then re-vendor it into each project that uses it. That changes every vendoring project's frozen grader files, so it is a new reward version for each.

The tests:

- `tests/test_cycles_kit.py`: the formula, the report, the running cap and the patch's layout, without Docker (`tools/tests/test_vendor.py` checks the vendoring);
- `tests/test_cycles_kit_docker.py`: in the kit's image (`probe.dockerfile`), escapes (Valgrind's own image, the space's end, a store across it, system call addresses), the profile, huge pages, ignored client requests, the clocks, threads, both caps, framed items, the memory pass, and the program's start and layout against values recorded on fuji-1, from three paths under three stack limits. A change to the kit that moves them updates the recorded values (`LAYOUT_OUTPUT_SHA256`, `LAYOUT_REPORT`).

## Licence

Valgrind and the fork are GPL-3.0-or-later, and so is `native/valgrind.patch`. `sandbox.py` runs Valgrind as a separate program and links nothing of it; it is GPL-2.0-or-later, as the instruction-count kit's is (`REUSE.toml`).
