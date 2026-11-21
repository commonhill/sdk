"""commonhill_testkit: the Docker build harness every repository's Docker-heavy tests share, as a pytest
plugin (it loads itself: the `pytest11` entry point). The package mirrors every build and pull goes
through, the pinned downloads fetched before a build, the watchdog and network retries of every
build and pull, the build turn (bin/docker, first on the tests' PATH), the tests' `docker
compose`, the registry's copies of keyed images, busybox and the compose jobs on it, and scripted
HTTP workers. Its fixtures are this module's; its functions are importable from it.

It needs its source tree: bin/docker lives beside src/, so the package is installed editable from
it (a uv workspace member), not as a wheel."""

import pytest

# pytest rewrites the asserts of a plugin's modules only when it finds them in its distribution's
# files, which an editable install lists none of; so they are named before they are imported.
pytest.register_assert_rewrite(
    "commonhill_testkit.busybox", "commonhill_testkit.docker", "commonhill_testkit.scripted"
)

from commonhill_testkit.busybox import (  # noqa: E402
    BUSYBOX,
    BUSYBOX_UPSTREAM,
    HELLO_COMPOSE,
    XDIST_WORKER,
    busybox_archive,
    busybox_base,
    busybox_image,
    busybox_unloaded,
    compose_job,
    hello_compose,
    load_busybox,
    make_compose_job,
)
from commonhill_testkit.docker import (  # noqa: E402
    BUILD_OUTPUT_TAIL,
    DOCKER_TURNS_BIN,
    PinnedDownload,
    base_images,
    build_image,
    build_once,
    built,
    built_once,
    compose,
    compose_build,
    compose_built,
    docker_build,
    docker_compose,
    docker_turns,
    mirror_build_args,
    pinned_downloads,
    pull_image,
    pulled,
    registry_mirror,
    run_compose,
    session_dir,
    shared_dir,
    warmed,
)
from commonhill_testkit.scripted import (  # noqa: E402
    Script,
    ScriptedServer,
    WorkerScripts,
    http_answer,
    scripted_http,
    worker_scripts,
)

__all__ = [
    "BUILD_OUTPUT_TAIL",
    "BUSYBOX",
    "BUSYBOX_UPSTREAM",
    "DOCKER_TURNS_BIN",
    "HELLO_COMPOSE",
    "XDIST_WORKER",
    "PinnedDownload",
    "Script",
    "ScriptedServer",
    "WorkerScripts",
    "base_images",
    "build_image",
    "build_once",
    "built",
    "built_once",
    "busybox_archive",
    "busybox_base",
    "busybox_image",
    "busybox_unloaded",
    "compose",
    "compose_build",
    "compose_built",
    "compose_job",
    "docker_build",
    "docker_compose",
    "docker_turns",
    "hello_compose",
    "http_answer",
    "load_busybox",
    "make_compose_job",
    "mirror_build_args",
    "pinned_downloads",
    "pull_image",
    "pulled",
    "pytest_configure",
    "registry_mirror",
    "run_compose",
    "scripted_http",
    "session_dir",
    "shared_dir",
    "warmed",
    "worker_scripts",
]


def pytest_configure() -> None:
    """The tests' docker first on PATH before anything runs Docker (`docker_turns`)."""
    docker_turns()
