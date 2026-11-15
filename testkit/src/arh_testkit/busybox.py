"""Busybox, the image the tests' jobs run: its archive, its per-worker tag, and the compose jobs
built on it."""

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from arh_testkit.docker import pulled

BUSYBOX_UPSTREAM = "busybox:1.37-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092"
# `docker load` restores tags but not registry digests, so jobs name their images by a local tag.
# Under pytest-xdist each worker has its own (`busybox_unloaded` removes it while other workers'
# jobs run theirs); the Docker daemon is shared.
XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
BUSYBOX = f"arh-test/busybox{'-' + XDIST_WORKER if XDIST_WORKER else ''}:1.37-musl"


# Two containers exchange a message over an internal network; the server writes it as the result.
HELLO_COMPOSE = f"""
services:
  server:
    image: {BUSYBOX}
    networks: [job]
    volumes: [result:/result]
    command:
      - sh
      - -c
      - |-
        nc -l -p 9000 > /tmp/m && printf '{{"message": "%s"}}' "$$(cat /tmp/m)" > /result/result.json
  client:
    image: {BUSYBOX}
    networks: [job]
    command: ["sh", "-c", "until echo hello | nc -w 1 server 9000; do sleep 0.2; done"]
networks:
  job: {{internal: true}}
volumes:
  result: {{}}
"""


def compose_job(command: str) -> str:
    """A one-service job running `command` (sh -c) with the result volume at /result."""
    escaped = command.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f"""
services:
  job:
    image: {BUSYBOX}
    network_mode: none
    volumes: [result:/result]
    command: ["sh", "-c", "{escaped}"]
volumes:
  result: {{}}
"""


@pytest.fixture(scope="session")
def busybox_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`docker save` of BUSYBOX, the only image the test jobs use."""
    path = tmp_path_factory.mktemp("images") / "busybox.tar"
    pulled(BUSYBOX_UPSTREAM)
    subprocess.run(["docker", "tag", BUSYBOX_UPSTREAM, BUSYBOX], check=True)
    subprocess.run(["docker", "save", "--output", str(path), BUSYBOX], check=True)
    return path


def load_busybox(archive: Path) -> None:
    """BUSYBOX's tag back from its archive (a no-op if it's there)."""
    subprocess.run(["docker", "load", "--quiet", "--input", str(archive)], check=True, capture_output=True)


@pytest.fixture
def busybox_unloaded(busybox_archive: Path) -> Iterator[Path]:
    """BUSYBOX's archive, with the tag removed from the local daemon so only `docker load` brings it
    back; loaded again afterwards, however the test ended, so no later test depends on whether
    this one ran (core's services/runner/tests/test_busybox_fixtures.py)."""
    subprocess.run(["docker", "image", "rm", BUSYBOX], capture_output=True)
    assert subprocess.run(["docker", "image", "inspect", BUSYBOX], capture_output=True).returncode != 0
    try:
        yield busybox_archive
    finally:
        load_busybox(busybox_archive)


@pytest.fixture(scope="session")
def busybox_image() -> str:
    return BUSYBOX


@pytest.fixture(scope="session")
def busybox_base() -> str:
    """BUSYBOX's registry reference, for Dockerfiles: BuildKit resolves FROM from the registry."""
    return BUSYBOX_UPSTREAM


@pytest.fixture(scope="session")
def hello_compose() -> str:
    return HELLO_COMPOSE


@pytest.fixture(scope="session")
def make_compose_job() -> Callable[[str], str]:
    return compose_job
