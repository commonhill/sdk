# The instruction-count kit

This kit is for count-first projects whose compute budget is a number of instructions. The grader runs a program under QEMU's user-mode emulator and gets three guarantees:
- the count is exact;
- the count is capped;
- the program sees nothing that changes from run to run.

The program is one of two kinds:
- **A static x86-64 program the worker hands over.** It runs on each held-out input, or on items. This is how oxipng (PER-372) and graphviz-crossings (PER-375) work.
- **An interpreter of the grader's own, such as python3, running the worker's code.** It reads a read-only tree of files that looks the same on every host. The AlgoTune projects (PER-378) work this way.

**For a new speed project with a static program, use the cycles kit (`kits/cycles`) instead:** it estimates cycles, weighting each instruction by its cost and pricing cache misses, and takes peak memory, with the same entry points. This kit stays the simpler option, and the only one for an interpreter in a tree.

`project/` is laid out as a project's files, and each project vendors it verbatim with `python3 tools/vendor.py copy --kits-only --kit insncap DIR`. `tools/vendor.py check --release N --kit insncap DIR` fails when a copy differs.

| file | what it is |
| -- | -- |
| `grader/native/insncap.c` | The TCG plugin. Its comment is the specification. See the bullets below. |
| `grader/native/qemu.patch` | Patches QEMU 11.1.2: a time-stamp counter that steps by 1024 a read, TSC_AUX (rdtscp, rdpid) fixed at 0, x86-64 guests under `-R` (no vsyscall page), and a range check before every guest load and store (below). |
| `grader/sandbox.py` | The sandbox: Landlock, seccomp, rlimits and file descriptors. It has these entry points: `run_emulated` (a static program on a file), `run_framed` (on items), `run_tree` (an interpreter on items, in a tree), `run_static` (the grader's own helpers, natively), and `parse_report`. |

The plugin (`insncap.c`) does the following:
- **Counts and caps.** It counts executed instructions, plus each memory access of a `rep` string instruction. It stops the program over its cap (exit 137) or at a second thread (exit 136).
- **Frames items.** With `frames=1`, each item is a frame the grader sends only after the answer to the one before, and the report gives each item's starting count.
- **Answers system calls** under one of two profiles:
  - `stdio`: clocks from the count, getrandom from a fixed seed, a constant thread id, and descriptors 0, 1 and 2 only.
  - `tree`: on top of `stdio`'s answers, read-only files with a normalised stat (no inode, device, owner or time of the host's), sorted directory listings, and constant identities. Only the tree is there: its roots (`roots=`, from `run_tree`'s tree), what is under them, and the directories above them, which stat as directories but can't be opened (Landlock). Every other path, and every failed resolution outside the tree, is ENOENT.

A project's `dockerfile.grader` does the rest:
1. build QEMU from the release tarball, pinned by its SHA-256, with the patch applied (`patch -p1 --forward`) and `--target-list=x86_64-linux-user --enable-plugins`;
2. compile the plugin against the tarball's `include/plugins`;
3. put both, `qemu-x86_64` and `insncap.so`, in `grader/bin/`.

oxipng's `dockerfile.grader` and its README ("How a run is scored") are the worked example. `probe.dockerfile` builds the kit alone for its tests.

**The program's memory:** in user-mode emulation a guest address becomes `guest_base + address`, and QEMU checks it only where it chooses to. So a program that can make an arbitrary pointer could otherwise read or write the emulator's own memory, where the count is. That includes C, unsafe Rust, and Python through ctypes or numpy. The kit's guest space is QEMU's reserved one (`-R 0x80000000`, `GUEST_SPACE` in insncap.c), and the emulator checks every guest access against it *before* the access happens (`qemu.patch`, part 3). An access outside it ends the process with status 135 at once, and nothing of it lands. The program can't catch it. `sandbox.py` reports it as `escape`.

A project needs no language rule of its own for this, though one (oxipng's safe Rust) does no harm. Counts don't change. The check costs two host instructions per access, which is cheaper than the plugin callback it replaces on every store.

Every place in QEMU 11.1.2's user-mode x86-64 emulator that turns a guest address into a host one (`g2h` and its variants), with what covers it:

| where | what | covered by |
| -- | -- | -- |
| `tcg/x86_64/tcg-target.c.inc`, `prepare_host_addr` | every load and store of translated code | the patch: `cmp addr, last - size + 1` / `ja` to the slow path |
| `accel/tcg/user-exec.c`, `cpu_mmu_lookup` | the slow path (`helper_ld*_mmu`, `helper_st*_mmu`) and every helper's access through `cpu_ld*`, `cpu_st*` | the patch: range check, exit 135 |
| `user-exec.c`, `atomic_mmu_lookup` | atomics: `cmpxchg`, `cmpxchg16b`, locked read-modify-writes | the patch: range check, exit 135 |
| `user-exec.c`, `tlb_vaddr_to_host` | a host pointer for a helper (no x86 user) | the patch: NULL outside the range, so callers take the checked path |
| `user-exec.c`, `probe_access*`, `get_page_addr_code_hostp` | instruction fetch, and helpers that probe a range and then use the host pointer (fxsave, xsave and their restores) | QEMU's own: `guest_addr_valid` on the start, and a probe stays within one page, and the space's end (page-aligned) cannot fall inside that page; otherwise a SIGSEGV the guest may catch, before any access |
| `user-exec.c`, `cpu_memory_rw_debug` | the plugin's `qemu_plugin_read_memory_vaddr` and `_write_` (clocks, getrandom, stat, paths) | QEMU's own: page flags, and there are no pages outside the range |
| `linux-user/uaccess.c`, `lock_user` | every system call's buffers and structures: read, write, readv, writev, rt_sigaction, sigaltstack, prlimit64, the signal frame | QEMU's own: `access_ok` (`guest_range_valid`, `page_check_range`) |
| `linux-user/mmap.c` | mmap, munmap, mprotect, mremap, madvise | QEMU's own: `guest_range_valid` before any `g2h` |
| `linux-user/syscall.c`, `do_futex` | futex's words, which the host's kernel follows itself, unchecked | the plugin: only WAIT, WAKE and their BITSET forms, on a word inside `GUEST_SPACE` (else EFAULT); the rest ENOSYS |
| `syscall.c`: msync, mlock, mlock2, munlock, mount, shmat, shmdt, modify_ldt, set_thread_area | unchecked or loosely checked addresses | unreachable: the plugin refuses them (ENOSYS), and the seccomp filter has none |
| `syscall.c`, exit's `clear_child_tid` futex | the thread-id word at a thread's exit | unreachable: set_tid_address is the plugin's (a constant), and a second thread ends the run |
| `linux-user/i386/cpu_loop.c`, `main.c`, `syscall.c` (gdt, idt, ldt) | QEMU's own descriptor tables, which it allocates in the guest's space | QEMU's own addresses, not the guest's |
| `linux-user/elfload.c`, `user-exec.c` (`page_set_flags`'s mprotect), the gdbstub | the loader before the guest runs, page bookkeeping, debugging | not guest-chosen; no gdbstub in the sandbox |

The architecture-specific files of other guests (`linux-user/hppa`, `arm`, `ppc` and so on) aren't built: the kit builds `x86_64-linux-user` only.

**Changing the kit:** change `project/`, then re-vendor it into each project that uses it. That changes every vendoring project's frozen grader files, so it is a new reward version for each of them.

The tests:
- `tests/test_insncap_kit_docker.py` covers escapes (stores and loads into the emulator's data and code buffer, in-range accesses up to the last byte), framed items, the tree profile, and the emulator, the count and the stdio profile's system calls;
- `tests/test_insncap_kit.py` covers the report and the sandbox's static checks.
