# commonhill-testkit: the tests' Docker build harness

A pytest plugin that holds the Docker build harness every repository's Docker-heavy tests share. It
covers:

- the package mirrors that every build and pull goes through
- the pinned downloads fetched before a build
- the watchdog and network retries of every build and pull
- the build turn
- busybox and the compose jobs on it
- scripted HTTP workers

## Use

Add it as a dev dependency. In this workspace:

```toml
[dependency-groups]
dev = ["commonhill-testkit"]

[tool.uv.sources]
commonhill-testkit = { workspace = true }
```

The plugin loads itself (its `pytest11` entry point): its fixtures are there in every test, and at
startup it puts `bin/docker` first on `PATH`. It needs its source tree, since `bin/docker` lives
beside `src/`: install it editable (a workspace member, or `uv add --editable`), not as a wheel.

## Fixtures

- `build_image(*args, cwd=None, keyed=False)`: `docker build`, its pinned downloads warmed and its
  base images pulled first; `keyed=True` takes the registry's copy of a build with the same inputs.
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

Importable from `commonhill_testkit`: `docker_build`, `built`, `pulled`, `base_images`, `warmed`,
`pinned_downloads`, `PinnedDownload`, `run_compose`, `built_once`, `session_dir`,
`mirror_build_args`, `registry_mirror`, `docker_turns`, `load_busybox`, `compose_job`,
`http_answer`, and the constants `BUSYBOX`, `BUSYBOX_UPSTREAM`, `HELLO_COMPOSE`, `XDIST_WORKER`,
`BUILD_OUTPUT_TAIL`, `DOCKER_TURNS_BIN`. The rest is in `commonhill_testkit.docker`,
`commonhill_testkit.busybox` and `commonhill_testkit.scripted`.

## Environment

- `COMMONHILL_TEST_PIP_INDEX`, `COMMONHILL_TEST_PYPI_FILES`, `COMMONHILL_TEST_CARGO_MIRROR`: the
  package mirrors, passed to every build as the args `PIP_INDEX_URL` and `PIP_TRUSTED_HOST`,
  `PYPI_FILES_MIRROR` and `CARGO_MIRROR_URL`.
- `ARH_DOCKER_REGISTRY_MIRROR`: Docker Hub's mirror for the Docker daemons tests start; the
  daemon's own first mirror if unset (set as `ARH_LOCAL_REGISTRY_MIRROR` too). These two are the
  platform core's settings and keep its names until the core renames them.
- `COMMONHILL_TEST_IMAGE_CACHE`, `COMMONHILL_TEST_IMAGE_CACHE_PUSH`: the registry repository that
  keyed images are taken from, and whether a run pushes what it built.
- `PYTEST_XDIST_WORKER`: the worker's busybox tag and session directory.
- Set by the plugin for the processes it starts: `COMMONHILL_REAL_DOCKER`,
  `COMMONHILL_BUILD_TURN_LOCK`, `COMMONHILL_BUILD_TURN_GATE`, `COMMONHILL_TEST_BUILD_ARGS`. Read by
  `bin/docker`: those, `COMMONHILL_BUILD_TURN` and `COMMONHILL_BUILD_TURN_WAIT_S` (default 1800).

Needs `docker` (with BuildKit and the compose plugin) and `flock` (util-linux) on `PATH`.

## Licence

Apache-2.0 (`LICENSE`).
