# Commonhill SDK

What a Commonhill project's author builds on, and vendors into the project:

| Path | What it is |
| -- | -- |
| `grader/` | The grader SDK, package `arh-grader` (`arh_grader`): the worker client that treats every response as hostile, the run's seed, timings, `write_result` and `items.json`, head-to-head matches, and the view. A project vendors it as `grader/arh_grader`. Its README is its documentation, releases included. |
| `kits/insncap/` | The instruction-count kit: a QEMU plugin and the sandbox a speed project's grader runs the worker's program in, counting its instructions. |
| `kits/cycles/` | The cycles kit: CodSpeed's Valgrind, patched to estimate cycles, and its sandbox. |
| `tools/vendor.py` | Vendoring: `copy` writes the current SDK release and a project's kits into it; `check --release N [--kit KIT]... DIR` says whether DIR has exactly that release and those kits. |
| `testkit/` | `arh-testkit`, the pytest plugin every Commonhill repository's Docker-heavy tests share: package mirrors, pinned downloads, build retries and the build turn, busybox, compose jobs, scripted workers. |

## Releases

A tag `v<N>.0.0` is release N of the grader SDK (`arh_grader.RELEASE`); the kits and the tools are
versioned with it. A patch version changes the repository's tooling and leaves every vendored tree
the same. A project pins the release it vendored until its next reward version, and its CI checks
its copy:

```sh
git clone --depth 1 --branch v8.0.0 https://github.com/commonhill/sdk /tmp/sdk
python3 /tmp/sdk/tools/vendor.py check --release 8 .
```

## Working on it

You need [mise](https://mise.jdx.dev/) and Docker. `mise run lint ::: typecheck ::: test` runs the
checks CI runs (`.github/workflows/ci.yml`); the kits' Docker tests build QEMU and Valgrind.

## Licence

Apache-2.0 (`LICENSE`, `NOTICE`), except the files `REUSE.toml` names: the kits' sandboxes and
`insncap.c` are GPL-2.0-or-later, `qemu.patch` GPL-2.0-only and `valgrind.patch` GPL-3.0-or-later.
Security reports: `SECURITY.md`.
