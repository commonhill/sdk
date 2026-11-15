# arh-testkit: the tests' Docker build harness

A pytest plugin with the Docker build harness every repository's Docker-heavy tests share: the
package mirrors every build and pull goes through, the pinned downloads fetched before a build, the
watchdog and network retries of every build and pull, the build turn, busybox and the compose jobs
on it, and scripted HTTP workers.

## Use

Add it as a dev dependency. In this workspace:

```toml
[dependency-groups]
dev = ["arh-testkit"]

[tool.uv.sources]
arh-testkit = { workspace = true }
```

The plugin loads itself (its `pytest11` entry point): its fixtures are there in every test, and at
startup it puts `bin/docker` first on `PATH`. It needs its source tree, since `bin/docker` lives
beside `src/`: install it editable (a workspace member, or `uv add --editable`), not as a wheel.

## Fixtures

- `build_image(*args, cwd=None, keyed=False)`: `docker build`, its pinned downloads warmed and its
  base images pulled first; `keyed=True` takes the registry's copy of a build of the same inputs.
- `docker_compose(root, project, *args, run_s=...)`, `compose(...)` (its output): `docker compose`,
  its project built first under the watchdog; a command that still builds fails.
- `compose_built(root, project)`: a compose project's builds done by `docker_build` first.
- `compose_build(*args, cwd=None)`: a `docker compose ... build`, watched and retried.
- `pull_image(image)`: an image pulled with retries.
- `build_once(name, inputs, build)`: a file built once per session, shared by the xdist workers.
- `shared_dir`: the directory the session's xdist workers share.
- `busybox_archive`, `busybox_unloaded`, `busybox_image`, `busybox_base`: busybox's archive and
  its per-worker tag.
- `hello_compose`, `make_compose_job(command)`: compose jobs on busybox.
- `scripted_http(script)`, `worker_scripts`: HTTP servers that answer as a test scripts them.

## Functions

Importable from `arh_testkit`: `docker_build`, `built`, `pulled`, `base_images`, `warmed`,
`pinned_downloads`, `PinnedDownload`, `run_compose`, `built_once`, `session_dir`,
`mirror_build_args`, `registry_mirror`, `docker_turns`, `load_busybox`, `compose_job`,
`http_answer`, and the constants `BUSYBOX`, `BUSYBOX_UPSTREAM`, `HELLO_COMPOSE`, `XDIST_WORKER`,
`BUILD_OUTPUT_TAIL`, `DOCKER_TURNS_BIN`. The rest is in `arh_testkit.docker`, `arh_testkit.busybox`
and `arh_testkit.scripted`.

## Environment

- `ARH_TEST_PIP_INDEX`, `ARH_TEST_PYPI_FILES`, `ARH_TEST_CARGO_MIRROR`: the package mirrors, as
  every build's args `PIP_INDEX_URL` and `PIP_TRUSTED_HOST`, `PYPI_FILES_MIRROR`, `CARGO_MIRROR_URL`.
- `ARH_DOCKER_REGISTRY_MIRROR`: Docker Hub's mirror for the Docker daemons tests start; the
  daemon's own first mirror if unset (set as `ARH_LOCAL_REGISTRY_MIRROR` too).
- `ARH_TEST_IMAGE_CACHE`, `ARH_TEST_IMAGE_CACHE_PUSH`: the registry repository keyed images are
  taken from, and whether a run pushes what it built.
- `PYTEST_XDIST_WORKER`: the worker's busybox tag and session directory.
- Set by the plugin for the processes it starts: `ARH_REAL_DOCKER`, `ARH_BUILD_TURN_LOCK`,
  `ARH_BUILD_TURN_GATE`, `ARH_TEST_BUILD_ARGS`. Read by `bin/docker`: those, `ARH_BUILD_TURN`
  and `ARH_BUILD_TURN_WAIT_S` (default 1800).

Needs `docker` (with BuildKit and the compose plugin) and `flock` (util-linux) on `PATH`.

## Licence

Apache-2.0 (`LICENSE`).
