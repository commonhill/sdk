"""The tests' image builds can't hang (PER-344) and ride out a flaky mirror (PER-388), commonhill_testkit's
(testkit/): every download a Dockerfile makes is pinned, so the builders fetch it first; a `docker
build` that goes silent fails with its stalled step named; a stalled, trickling or wrong download
fails its attempts and then the fixture; a pinned download warmed from another URL is a cache hit
for a project's `ADD`, which then never fetches; a session's failed build fails its other asks at
once; a build that fails or stalls on the network is built again, and only then; and `docker
compose`'s own build, after the tests' build of its services, runs no step."""

import fcntl
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import commonhill_testkit.docker

ROOT = Path(__file__).parents[2]
TESTKIT_DOCKER = "testkit/src/commonhill_testkit/docker.py"  # where COMMONHILL_REAL_DOCKER is set
# A pinned download whose ADD runs over two lines, as enwik-fresh's worker fetches zig.
MULTILINE_ADD = Path(__file__).parent / "fixtures" / "multiline-add.dockerfile"


def downloading_dockerfiles() -> dict[str, list[str]]:
    """The repository's Dockerfiles with an `ADD` of a URL, and each such instruction (its lines)."""
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    found: dict[str, list[str]] = {}
    for name in listed.stdout.splitlines():
        if not re.search(r"(^|/)(dockerfile[^/]*|[^/]*\.dockerfile)$", name, re.IGNORECASE):
            continue
        text = (ROOT / name).read_text()
        adds = [a for a in re.findall(r"^ADD\b(?:[^\n]*\\\n)*[^\n]*", text, re.MULTILINE) if "://" in a]
        if adds:
            found[name] = adds
    return found


def test_every_download_a_dockerfile_makes_is_pinned_so_the_builders_fetch_it_first(
    harness: ModuleType,
) -> None:
    """`build_image` and `docker_compose` warm a Dockerfile's pinned downloads before its build;
    an unpinned one would be fetched by BuildKit, unbounded. SolReviewer344-2's R1: Hutter's ADDs
    (Zig's over two lines, enwik-fresh's now) were missed."""
    found = downloading_dockerfiles()
    assert "kits/insncap/probe.dockerfile" in found
    assert "testkit/tests/fixtures/multiline-add.dockerfile" in found
    for name, adds in found.items():
        downloads = harness.pinned_downloads(ROOT / name)
        assert len(harness.url_adds(ROOT / name)) == len(adds), name  # continuations joined
        assert {d.url for d in downloads} == {url for _, url in harness.url_adds(ROOT / name)}, name
    zig = harness.pinned_downloads(MULTILINE_ADD)
    assert [d.filename for d in zig] == ["zig-x86_64-linux-0.17.0.tar.xz"]


@pytest.fixture
def harness() -> ModuleType:
    """commonhill_testkit's builds, the module whose names the tests patch."""
    return commonhill_testkit.docker


@pytest.fixture(autouse=True)
def own_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """This module's builds stall and fail on purpose, on steps of their own (nonces, servers of
    their own), which no other test's build asks for: each test's builds take turns among
    themselves (PER-437), so its retries alone wait for no other test's builds, nor hold the
    others off."""
    monkeypatch.setenv("COMMONHILL_BUILD_TURN_LOCK", str(tmp_path / "build-turn.lock"))
    monkeypatch.setenv("COMMONHILL_BUILD_TURN_GATE", str(tmp_path / "build-turn.gate"))


# The stall limit the stall tests build under: past what a loaded runner keeps a build silent
# before the step under test even starts (docker's start, BuildKit's `[internal] load build
# definition` and the base's metadata: over 10 s on main's run 42410403253, 5 s on 41982492931).
STALL_S = 60
# `local_base`'s tag: in no registry, so BuildKit takes it from the daemon's store.
LOCAL_BASE = "commonhill-test/busybox-builds:1.37-musl"


class Stalling(http.server.BaseHTTPRequestHandler):
    """Sends a file's headers and its first bytes, then nothing (a stalled download), for longer
    than any stall limit."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        self.wfile.write(b"x" * 1000)
        self.wfile.flush()
        time.sleep(10 * STALL_S)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serving(handler: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def stalling() -> Iterator[str]:
    yield from serving(Stalling)


@pytest.fixture
def files(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A directory and a server of its files, as a project's download would come from."""
    directory = tmp_path / "served"
    directory.mkdir()

    class Files(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    for url in serving(Files):
        yield directory, url


def test_a_build_stalled_in_a_download_fails_naming_the_step(
    harness: ModuleType, stalling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BuildKit waits on a stalled body for good (an `ADD` of this server, under `timeout 240`,
    was still waiting at the end): `docker_build` kills it when it has printed nothing for
    BUILD_STALL_S and says which step it was in."""
    monkeypatch.setattr(harness, "BUILD_STALL_S", STALL_S)
    (tmp_path / "Dockerfile").write_text(
        f"FROM scratch\nADD --checksum=sha256:{'a' * 64} {stalling}/stalled-{uuid.uuid4().hex}.tar /x\n"
    )
    started = time.monotonic()
    with pytest.raises(AssertionError, match=f"printed nothing for {STALL_S} s") as failed:
        harness.docker_build(str(tmp_path))
    assert time.monotonic() - started < 3 * STALL_S, "killed at its stall, not left waiting"
    assert f"ADD --checksum=sha256:{'a' * 64} {stalling}/stalled-" in str(failed.value)


def test_a_stalled_or_wrong_download_fails_every_attempt_and_says_so(
    harness: ModuleType,
    stalling: str,
    files: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "FETCH_READ_TIMEOUT_S", 1)
    stalled = harness.PinnedDownload(f"{stalling}/qemu.tar.xz", "a" * 64, f"--checksum=sha256:{'a' * 64}")
    with pytest.raises(AssertionError, match=r"failed 3 times") as failed:
        harness.fetch(stalled, tmp_path / "stalled")
    assert str(failed.value).count("TimeoutError") == 3

    directory, url = files
    (directory / "wrong.tar.xz").write_bytes(b"not the pinned file")
    wrong = harness.PinnedDownload(f"{url}/wrong.tar.xz", "b" * 64, f"--checksum=sha256:{'b' * 64}")
    with pytest.raises(AssertionError, match=r"failed 3 times") as failed:
        harness.fetch(wrong, tmp_path / "wrong")
    assert str(failed.value).count("not the pinned " + "b" * 64) == 3


class Trickling(http.server.BaseHTTPRequestHandler):
    """Sends a file a byte at a time, each just inside any read timeout (SolReviewer344-2's R2)."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:  # the client gave up
            pass

    def log_message(self, format: str, *args: Any) -> None:
        pass


def test_a_trickling_download_ends_at_its_deadline(
    harness: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes arriving every 0.1 s never trip a 1 s read timeout; the 2 s deadline ends each of
    the 3 attempts anyway, at its first read past it."""
    monkeypatch.setattr(harness, "FETCH_READ_TIMEOUT_S", 1)
    monkeypatch.setattr(harness, "FETCH_DEADLINE_S", 2)
    for url in serving(Trickling):
        trickled = harness.PinnedDownload(f"{url}/zig.tar.xz", "c" * 64, f"--checksum=sha256:{'c' * 64}")
        started = time.monotonic()
        with pytest.raises(AssertionError, match=r"failed 3 times") as failed:
            harness.fetch(trickled, tmp_path / "trickled")
        assert time.monotonic() - started < 3 * 2 + 2
        assert str(failed.value).count("not done after 2 s") == 3


def test_a_warmed_download_is_a_cache_hit_for_a_projects_add(
    harness: ModuleType, files: tuple[Path, str], tmp_path: Path
) -> None:
    """Warmed (fetched here, added from another server's URL), a project's pinned `ADD` builds
    with its own URL gone: BuildKit's key for it is the file's name, flags and checksum."""
    directory, url = files
    content = uuid.uuid4().bytes * 1000  # never in the cache before
    (directory / "kit.tar.xz").write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    project = tmp_path / "project"
    project.mkdir()
    (project / "Dockerfile").write_text(
        f"FROM scratch\nADD --chmod=644 --checksum=sha256:{sha256} {url}/kit.tar.xz /opt/kit.tar.xz\n"
    )
    [download] = harness.pinned_downloads(project / "Dockerfile")
    assert (download.url, download.flags) == (f"{url}/kit.tar.xz", f"--chmod=644 --checksum=sha256:{sha256}")

    harness.warm_download(download, tmp_path / "warm")
    (directory / "kit.tar.xz").unlink()  # the project's URL now answers 404
    assert "CACHED" in harness.docker_build(str(project))


def test_a_failed_build_fails_the_sessions_other_asks_without_building_again(
    harness: ModuleType, tmp_path_factory: pytest.TempPathFactory
) -> None:
    name, builds = f"failing-{uuid.uuid4().hex}", list[Path]()

    def build(path: Path) -> None:
        builds.append(path)
        raise AssertionError("docker build: printed nothing for 600 s; ADD https://download.qemu.org/")

    for _ in range(2):
        with pytest.raises(
            AssertionError, match=re.escape("printed nothing for 600 s; ADD https://download.qemu.org/")
        ):
            harness.built_once(tmp_path_factory, name, [], build)
    assert len(builds) == 1


# --- network retries and compose's builds (PER-388) ----------------------------------------------


STALLED = "printed nothing for 600 s"
APT_RUN = "[stage-1 2/4] RUN apt-get update  && apt-get install -y --no-install-recommends libglib2.0-0"


def test_a_failed_steps_own_network_error_is_retried_and_any_other_failure_is_not(
    harness: ModuleType,
) -> None:
    """The lines run 42289113597 failed on, as `network_failure` reads a build's output, and
    failures that only mention the network (PR #279's review, R3) or that failed on something else
    after one (R4)."""
    apt = (
        f"#10 {APT_RUN}\n"
        "#10 195.4 E: Failed to fetch http://deb.debian.org/debian/pool/main/g/glib2.0/"
        "libglib2.0-0_2.74.6-2%2bdeb12u9_amd64.deb  Connection timed out [IP: 151.101.2.132 80]\n"
        "#10 195.4 E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?\n"
        '#10 ERROR: process "/bin/sh -c apt-get update ..." did not complete successfully: exit code: 100\n'
    )
    assert harness.network_failure(apt, "").startswith("#10 195.4 E: Unable to fetch some archives")
    gone = (  # Sol's round 3: a package gone from the mirror, the same next time
        f"#10 {APT_RUN}\n"
        "#10 2.1 Err:1 http://deb.debian.org/debian bookworm/main amd64 libglib2.0-0 amd64 2.74.6-2+deb12u8\n"
        "#10 2.1   404  Not Found [IP: 151.101.2.132 80]\n"
        "#10 2.2 E: Failed to fetch http://deb.debian.org/debian/pool/main/g/glib2.0/"
        "libglib2.0-0_2.74.6-2+deb12u8_amd64.deb  404  Not Found [IP: 151.101.2.132 80]\n"
        "#10 2.2 E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?\n"
        '#10 ERROR: process "/bin/sh -c apt-get update ..." did not complete successfully: exit code: 100\n'
    )
    assert harness.network_failure(gone, "") == ""
    mixed = (  # one fetch timed out, another is gone: the 404 comes back next time
        f"#10 {APT_RUN}\n"
        "#10 9.1 E: Failed to fetch http://deb.debian.org/a.deb  Connection timed out [IP: 151.101.2.132]\n"
        "#10 9.2 E: Failed to fetch http://deb.debian.org/b.deb  404  Not Found [IP: 151.101.2.132]\n"
        "#10 9.2 E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?\n"
        "#10 ERROR: exit code: 100\n"
    )
    assert harness.network_failure(mixed, "") == ""
    summary_only = (
        f"#10 {APT_RUN}\n#10 2.2 E: Some index files failed to download.\n#10 ERROR: exit code: 100\n"
    )
    assert harness.network_failure(summary_only, "") == ""
    # Sol's round-4 live apt runs, as one step's lines: an index's other compressions 404 (`Err:`)
    # before Packages.gz is found, then the archive's fetch fails.
    index = [
        "Ign:1 http://127.0.0.1:42741 ./ InRelease",
        "Ign:2 http://127.0.0.1:42741 ./ Release",
        *["Err:3 http://127.0.0.1:42741 ./ Packages", "  404  Not Found [IP: 127.0.0.1 42741]"] * 3,
        "Get:3 http://127.0.0.1:42741 ./ Packages [237 B]",
        "Need to get 12 B of archives.",
        "Err:1 http://127.0.0.1:42741 ./ fakereview 1.0",
    ]
    deb = "E: Failed to fetch http://127.0.0.1:42741/pool/fakereview_1.0_all.deb"
    summary = "E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?"

    def apt_step(*lines: str) -> str:
        return (
            f"#9 {APT_RUN}\n" + "".join(f"#9 4.6 {line}\n" for line in lines) + "#9 ERROR: exit code: 100\n"
        )

    failed = "  Connection failed [IP: 127.0.0.1 42741]"
    timed_out = apt_step(*index, failed, f"{deb}{failed}", summary)
    assert harness.network_failure(timed_out, "") == f"#9 4.6 {summary}"
    gone = "  404  Not Found [IP: 127.0.0.1 42741]"
    assert harness.network_failure(apt_step(*index, gone, f"{deb}{gone}", summary), "") == ""
    then_else = (  # Sol's R4: a fetch that failed and was let pass, then a deterministic error
        "#7 [2/2] RUN wget -O /dev/null http://127.0.0.1:9 || true; echo compiler: syntax error; exit 3\n"
        "#7 0.1 Connecting to 127.0.0.1:9 (127.0.0.1:9)\n"
        "#7 0.1 wget: can't connect to remote host (127.0.0.1): Connection refused\n"
        "#7 0.1 compiler: syntax error\n"
        '#7 ERROR: process "/bin/sh -c wget -O /dev/null http://127.0.0.1:9 || true; ..." did not complete\n'
    )
    assert harness.network_failure(then_else, "") == ""
    buildkit = (
        'ERROR: failed to solve: failed to load cache key: Get "http://x/y": read: connection reset by peer\n'
    )
    assert "connection reset by peer" in harness.network_failure(buildkit, "")
    add = '#5 [2/2] ADD https://x/kit.tar /kit.tar\n#5 ERROR: Get "https://x/kit.tar": EOF\n'
    assert harness.network_failure(add, "") == '#5 ERROR: Get "https://x/kit.tar": EOF'
    compile_error = "#7 [3/4] RUN make\n#7 3.1 main.c:4:1: error: expected ';'\n#7 ERROR: exit code: 1\n"
    assert harness.network_failure(compile_error, "") == ""
    echoed = (  # a command that prints a network error and exits: not a fetch that failed
        "#7 [2/2] RUN echo 'Connection refused' && exit 3\n#7 0.2 Connection refused\n"
        "#7 ERROR: process \"/bin/sh -c echo 'Connection refused' && exit 3\" did not complete successfully\n"
        "ERROR: failed to solve: process \"/bin/sh -c echo 'Connection refused' && exit 3\" failed\n"
    )
    assert harness.network_failure(echoed, "") == ""
    assert harness.network_failure(apt, "ran past 3600 s") == ""


def test_a_stall_is_retried_only_while_a_running_step_waits_on_the_network(harness: ModuleType) -> None:
    """Which steps were still running, read per step: BuildKit's lines interleave (PR #279's review,
    R2), and a RUN's own command isn't evidence (R3)."""
    silent = f"#11 {APT_RUN}\n"  # run 3's probe: apt connecting prints nothing
    assert harness.network_failure(silent, STALLED) == f"#11 {APT_RUN}"
    fetching = (
        f"#4 {APT_RUN}\n#4 30.6 Get:2 http://deb.debian.org/debian bookworm-updates InRelease [55.4 kB]\n"
    )
    assert harness.network_failure(fetching, STALLED).endswith(
        "Get:2 http://deb.debian.org/debian bookworm-updates InRelease [55.4 kB]"
    )
    interleaved = (  # the fetch, then another service's steps finishing after it
        f"#5 {APT_RUN}\n#5 0.575 Get:1 http://deb.debian.org/debian bookworm InRelease [151 kB]\n"
        "#6 [worker 3/5] COPY harness/ harness/\n#6 DONE 0.0s\n"
        "#7 [worker 4/5] RUN useradd worker\n#7 DONE 0.4s\n"
    )
    assert harness.network_failure(interleaved, STALLED).startswith(f"#5 {APT_RUN}: 0.575 Get:1")
    assert harness.network_failure(silent, "ran past 3600 s") == ""
    restarted = f"#5 {APT_RUN}\n#5 CANCELED\n\n#5 {APT_RUN}\n"  # the suite on the box: run again
    assert harness.network_failure(restarted, STALLED) == f"#5 {APT_RUN}"
    ci = (  # run 42346543224 attempt 2: deb.debian.org stalled the build stage's apt for 600 s
        f"#9 {APT_RUN}\n#9 0.214 Get:1 http://deb.debian.org/debian bookworm InRelease [151 kB]\n"
        "#9 30.24 Ign:1 http://deb.debian.org/debian bookworm InRelease\n"
        "#9 30.30 Get:3 http://deb.debian.org/debian-security bookworm-security InRelease [34.8 kB]\n"
        "#9 ...\n\n"
        "#10 [stage-1 2/6] RUN apt-get update\n#10 6.649 Successfully installed pypng\n#10 DONE 6.7s\n"
        f"#11 DONE 0.0s\n#9 {APT_RUN}\n"
    )
    got = harness.network_failure(ci, STALLED)
    assert got.startswith(f"#9 {APT_RUN}: 30.30 Get:3 http://deb.debian.org/debian-security"), got
    pulling = "#3 [1/2] FROM docker.io/library/debian:bookworm-slim\n#3 sha256:a1 1.2MB / 28MB 3.1s\n"
    assert harness.network_failure(pulling, STALLED).startswith("#3 [1/2] FROM")
    finished = f"#5 {APT_RUN}\n#5 0.575 Get:1 http://deb.debian.org/debian bookworm InRelease\n#5 DONE 9.1s\n"
    assert harness.network_failure(finished + "#9 [3/4] RUN make\n#9 12.0 cc -O2 x.c\n", STALLED) == ""
    assert harness.network_failure("#9 [3/4] RUN make\n", STALLED) == ""
    slept = "#7 [2/2] RUN sleep 120 && apt-get update\n"  # apt hadn't started
    assert harness.network_failure(slept, STALLED) == ""
    echoed = "#7 [2/2] RUN echo 'Connection refused' && sleep 999\n#7 0.2 Connection refused\n"
    assert harness.network_failure(echoed, STALLED) == ""


@pytest.fixture
def waits(harness: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, str]]:
    """`retry_wait`'s calls, each recorded instead of waited."""
    calls: list[tuple[float, str]] = []

    def wait(seconds: float, reason: str) -> None:
        calls.append((seconds, reason))

    monkeypatch.setattr(harness, "retry_wait", wait)
    return calls


class Resets:
    """A server of one file that resets every connection until it is told to serve: `url` and
    `content`, how many times it `served`, and the build's waits (`retry_wait`'s), the second of
    which turns it on."""

    def __init__(self, harness: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        self.content = uuid.uuid4().bytes * 100  # never in the cache before
        self.serving, self.served, self.waits = False, 0, list[tuple[float, str]]()
        monkeypatch.setattr(harness, "retry_wait", self.wait)

    def wait(self, seconds: float, reason: str) -> None:
        self.waits.append((seconds, reason))
        self.serving = len(self.waits) == 2

    def handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        resets = self

        class Resetting(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if not resets.serving:  # a reset, not a close: SO_LINGER 0
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.close_connection = True
                    return
                resets.served += 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(resets.content)))
                self.end_headers()
                self.wfile.write(resets.content)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Resetting


@pytest.fixture
def resets(harness: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Resets, str]]:
    server = Resets(harness, monkeypatch)
    for url in serving(server.handler()):
        yield server, url


def test_a_build_failing_on_the_network_is_built_again_until_it_fetches(
    harness: ModuleType, resets: tuple[Resets, str], tmp_path: Path
) -> None:
    """The server resets every connection until the second wait; the build's third attempt fetches."""
    server, url = resets
    checksum = hashlib.sha256(server.content).hexdigest()
    (tmp_path / "Dockerfile").write_text(
        f"FROM scratch\nADD --checksum=sha256:{checksum} {url}/kit.tar /kit.tar\n"
    )
    harness.docker_build(str(tmp_path))
    assert [seconds for seconds, _ in server.waits] == list(harness.BUILD_RETRY_WAITS_S)
    assert all(harness.NETWORK_FAILURE.search(reason) for _, reason in server.waits), server.waits
    assert server.served == 1


@pytest.fixture(scope="module")
def local_base(busybox_base: str) -> str:
    """BusyBox under a name no registry has, pulled once (again on a failure) and tagged: a build
    FROM it resolves its base from the daemon's own store, so the retry tests touch the network
    only in their step under test (a base image's metadata stalls and failures under load aren't
    theirs: main's run 42437620757)."""
    for attempt in range(3):
        if subprocess.run(["docker", "pull", "--quiet", busybox_base], capture_output=True).returncode == 0:
            break
        time.sleep(10 * (attempt + 1))
    subprocess.run(["docker", "tag", busybox_base, LOCAL_BASE], check=True)
    return LOCAL_BASE


@pytest.fixture
def outputs(harness: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each attempt's output (`watched`'s), in order: what ran in it, whatever its retry's label."""
    seen: list[str] = []
    watched = harness.watched

    def recorded(
        argv: list[str],
        cwd: Path | None,
        stall_s: float,
        deadline_s: float,
        env: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        output, why, code = watched(argv, cwd, stall_s, deadline_s, env)
        seen.append(output)
        return output, why, code

    monkeypatch.setattr(harness, "watched", recorded)
    return seen


def ran(nonce: str) -> tuple[str, str]:
    """A step's marker: the shell words it echoes, and what its run prints (never its header's)."""
    return f"ran-$((40+2))-{nonce}", f"ran-42-{nonce}"


def test_a_build_failing_otherwise_is_built_once(
    harness: ModuleType,
    waits: list[tuple[float, str]],
    outputs: list[str],
    tmp_path: Path,
    local_base: str,
) -> None:
    """Even if it prints a network error: the step that failed wasn't fetching. It runs once
    (counted by its marker, not by labels)."""
    echoed, printed = ran(uuid.uuid4().hex)
    (tmp_path / "Dockerfile").write_text(
        f'FROM {local_base}\nRUN echo "Connection refused {echoed}" && exit 3\n'
    )
    with pytest.raises(AssertionError, match="exit 1"):
        harness.docker_build(str(tmp_path))
    assert sum(printed in output for output in outputs) == 1, outputs
    assert [reason for _, reason in waits if "RUN echo" in reason] == []


def test_a_build_failing_after_a_fetch_it_let_fail_is_built_once(
    harness: ModuleType,
    waits: list[tuple[float, str]],
    outputs: list[str],
    tmp_path: Path,
    local_base: str,
) -> None:
    """The step fetches, lets the fetch fail, and then fails on something else (PR #279's review,
    R4: Sol's repro). It runs once (its marker counted in every attempt's output). The step's lines
    are kept apart in time, as a real build's fetch and compile are, so this tests what the step
    printed last, not two BuildKit races under load: it waits 5 s after the fetch (BuildKit merges
    stdout and stderr, and lines within one timestamp come in either order: 27 of 112 parallel
    builds printed the echo before wget's error, PER-428), and 1 s before it exits (a process
    that exits at once can have its last line logged after the step's ERROR)."""
    echoed, printed = ran(uuid.uuid4().hex)
    (tmp_path / "Dockerfile").write_text(
        f"FROM {local_base}\n"
        "RUN wget -O /dev/null http://127.0.0.1:9 || true; sleep 5; "
        f"echo compiler: syntax error {echoed}; sleep 1; exit 3\n"
    )
    with pytest.raises(AssertionError, match="exit 1"):
        harness.docker_build(str(tmp_path))
    assert sum(printed in output for output in outputs) == 1, outputs
    assert [reason for _, reason in waits if "RUN wget" in reason] == []


@dataclass
class Silent:
    """A URL a build's RUN reaches (on Docker's bridge gateway) whose server accepts and then never
    answers, the connections it accepted, and when (time.monotonic)."""

    url: str
    accepted: list[socket.socket]
    times: list[float]


@pytest.fixture
def silent() -> Iterator[Silent]:
    gateway = subprocess.run(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    listener = socket.create_server(("0.0.0.0", 0))
    held: list[socket.socket] = []
    times: list[float] = []

    def hold() -> None:
        while True:
            try:
                connection = listener.accept()[0]
            except OSError:
                return
            times.append(time.monotonic())
            held.append(connection)

    threading.Thread(target=hold, daemon=True).start()
    yield Silent(f"http://{gateway}:{listener.getsockname()[1]}/{uuid.uuid4().hex}", held, times)
    listener.close()
    for connection in held:
        connection.close()


def test_a_build_stalled_fetching_is_built_again_and_then_fails_saying_so(
    harness: ModuleType,
    waits: list[tuple[float, str]],
    outputs: list[str],
    tmp_path: Path,
    local_base: str,
    silent: Silent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RUN that starts by fetching, from a server that never answers: each of the 3 attempts
    stalls in that step, and is built again. The server took a fetch; not each attempt's, as on a
    loaded runner an attempt's container may not reach its fetch within the stall (two of three
    did, PER-477): test_..._fetches_afresh_when_built_again counts the retries' own fetches."""
    monkeypatch.setattr(harness, "BUILD_STALL_S", STALL_S)
    (tmp_path / "Dockerfile").write_text(f"FROM {local_base}\nRUN wget -q -O /dev/null {silent.url}\n")
    with pytest.raises(
        AssertionError, match=f"printed nothing for {STALL_S} s, on the network in all 3 attempts"
    ):
        harness.docker_build(str(tmp_path))
    assert len(outputs) == 3, outputs
    assert silent.accepted, "an attempt's wget reached the server"
    assert [reason for _, reason in waits if "RUN wget" in reason] == [reason for _, reason in waits]
    assert len(waits) == 2


def test_a_build_stalled_on_other_work_is_built_once(
    harness: ModuleType,
    waits: list[tuple[float, str]],
    outputs: list[str],
    tmp_path: Path,
    local_base: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step names a fetch, but it is sleeping before it (PR #279's review, R3). It is built once
    past its base: its step started in one attempt only (its header counted)."""
    monkeypatch.setattr(harness, "BUILD_STALL_S", STALL_S)
    step = f"RUN sleep 120 && apt-get update # {uuid.uuid4().hex}"
    (tmp_path / "Dockerfile").write_text(f"FROM {local_base}\n{step}\n")
    with pytest.raises(AssertionError, match=f"printed nothing for {STALL_S} s;"):
        harness.docker_build(str(tmp_path))
    assert sum(step in output for output in outputs) == 1, outputs
    assert [reason for _, reason in waits if "RUN sleep 120" in reason] == []


def test_a_build_stalled_on_a_step_another_build_waits_on_fetches_afresh_when_built_again(
    harness: ModuleType,
    waits: list[tuple[float, str]],
    tmp_path: Path,
    local_base: str,
    silent: Silent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PER-437: two builds of one step (the same base and command, in two contexts), the second
    started once the first's fetch stalled. BuildKit runs the step once for every build asking for
    it and kills it only once none waits on it, so an attempt built again while the other build
    still waited joined the hung fetch again. The attempt after a stall starts alone
    (commonhill_testkit's bin/docker): once both first attempts are gone, each build's second attempt fetches
    afresh, and both builds fail saying so.

    What it counts doesn't hang on timing (PER-457: on a loaded runner, 30 s didn't take the first
    build to its fetch): each attempt's start and end are recorded, and the fetches after the first
    attempts ended are the second attempts' own, whether or not the second build's first attempt
    joined the first's fetch. The first build's retry starts while the second's first attempt still
    waits, which is what an attempt alone must not join: the second's first attempt waits half a
    stall longer than the first's (its last line came after the first's fetch anyway, but a loaded
    runner took longer than that to end the first's attempt: PER-477)."""
    monkeypatch.setattr(harness, "BUILD_STALL_S", STALL_S)
    monkeypatch.setattr(harness, "BUILD_ATTEMPTS", 2)
    contexts = [tmp_path / "a", tmp_path / "b"]
    for context in contexts:
        context.mkdir()
        (context / "Dockerfile").write_text(f"FROM {local_base}\nRUN wget -q -O /dev/null {silent.url}\n")
    errors: list[str] = []
    spans: dict[str, list[tuple[float, float]]] = {"a": [], "b": []}
    watched = harness.watched

    def timed(
        argv: list[str],
        cwd: Path | None,
        stall_s: float,
        deadline_s: float,
        env: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        name = Path(argv[-1]).name
        if (name, len(spans[name])) == ("b", 0):
            stall_s += STALL_S / 2
        started = time.monotonic()
        try:
            return watched(argv, cwd, stall_s, deadline_s, env)
        finally:
            spans[name].append((started, time.monotonic()))

    monkeypatch.setattr(harness, "watched", timed)

    def build(context: Path) -> None:
        with pytest.raises(AssertionError) as failed:
            harness.docker_build(str(context))
        errors.append(str(failed.value))

    threads = [threading.Thread(target=build, args=(context,)) for context in contexts]
    threads[0].start()
    deadline = time.monotonic() + 10 * STALL_S
    while not silent.times and time.monotonic() < deadline:
        time.sleep(0.1)
    assert silent.times, "the first build's fetch reached the server"
    threads[1].start()
    for thread in threads:
        thread.join()

    assert len(errors) == 2, "each build failed"
    attempts = f"printed nothing for {STALL_S} s, on the network in all 2 attempts"
    assert all(attempts in error for error in errors), errors
    assert [len(spans["a"]), len(spans["b"])] == [2, 2], spans
    assert spans["a"][1][0] < spans["b"][0][1], "the first build's retry started while the second waited"
    firsts_gone = max(spans["a"][0][1], spans["b"][0][1])
    afresh = [at for at in silent.times if at > firsts_gone]
    assert len(afresh) == 2, ("one fetch per build's second attempt", silent.times, spans)
    assert len(waits) == 2


def test_every_docker_a_test_runs_is_the_one_that_takes_turns(harness: ModuleType) -> None:
    """PER-437: `docker` on the tests' PATH is commonhill_testkit's bin/docker, whatever runs it, and no
    code reaches Docker another way: no `docker` by its path, no Docker SDK, no COMMONHILL_REAL_DOCKER but
    commonhill_testkit's (which only hands it to its bin/docker)."""
    assert shutil.which("docker") == str(harness.DOCKER_TURNS_BIN / "docker")
    assert Path(os.environ["COMMONHILL_REAL_DOCKER"]).parent != harness.DOCKER_TURNS_BIN
    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True)
    bypass = re.compile(
        r"""^\s*(?:import|from)\s+docker\b|["'](?:/[\w.-]+)*/bin/docker["']|COMMONHILL_REAL_DOCKER""",
        re.MULTILINE,
    )
    found = {
        name: bypass.findall((ROOT / name).read_text())
        for name in listed.stdout.splitlines()
        if name not in (TESTKIT_DOCKER, "testkit/tests/test_image_builds_docker.py")
    }
    assert {name: hits for name, hits in found.items() if hits} == {}


@contextmanager
def locked(path: Path, mode: int) -> Generator[None]:
    with path.open("a") as lock:
        fcntl.flock(lock, mode)
        yield


def docker(tmp_path: Path, *args: str, turn: str = "", **env: str) -> subprocess.Popen[str]:
    """commonhill_testkit's bin/docker with `args`, its lock and gate in `tmp_path`, no build args
    unless `env` gives them, and, as the real docker, a script that prints its COMMONHILL_BUILD_TURN
    and arguments (a compose project's images: `img`, there once `tmp_path/there` is; with SLEEP
    set, it sleeps that long first)."""
    real = tmp_path / "real-docker"
    if not real.exists():
        real.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"config --images"*) echo img ;;\n'
            f'  *"image inspect"*) [ -f {tmp_path}/there ] ;;\n'
            '  *) sleep "${SLEEP:-0}"; echo "$COMMONHILL_BUILD_TURN $*" ;;\n'
            "esac\n"
        )
        real.chmod(0o755)
    wrapper = commonhill_testkit.docker.DOCKER_TURNS_BIN / "docker"
    return subprocess.Popen(
        [str(wrapper), *args],
        env={
            **os.environ,
            "COMMONHILL_REAL_DOCKER": str(real),
            "COMMONHILL_BUILD_TURN_LOCK": str(tmp_path / "lock"),
            "COMMONHILL_BUILD_TURN_GATE": str(tmp_path / "gate"),
            "COMMONHILL_BUILD_TURN": turn,
            "COMMONHILL_TEST_BUILD_ARGS": "",
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def waiting(*processes: subprocess.Popen[str]) -> bool:
    time.sleep(1)
    return all(process.poll() is None for process in processes)


def test_a_command_that_builds_waits_for_its_turn_and_any_other_runs_at_once(tmp_path: Path) -> None:
    """PER-437: while a build has the daemon alone, every command that builds waits (a raw `docker
    build`, buildx's, compose's `build`, and its `up` and `run` with `--build`, or without
    `--no-build` while one of the project's images is missing); any other command runs at once, as
    does a `docker` run by a build that has its turn (`held`, which the real docker's children get).
    A compose option's value is no subcommand: `--profile build up --no-build` builds nothing."""
    builds = [
        ("build", "."),
        ("buildx", "build", "."),
        ("compose", "build"),
        ("compose", "--profile", "build", "build"),
        ("compose", "up"),
        ("compose", "run", "w"),
    ]
    others = [
        ("ps",),
        ("compose", "up", "--no-build"),
        ("compose", "ps"),
        ("compose", "--profile", "build", "ps"),
    ]
    with locked(tmp_path / "lock", fcntl.LOCK_EX):
        waited = [docker(tmp_path, *args) for args in builds]
        ran = [docker(tmp_path, *args) for args in others] + [docker(tmp_path, "build", ".", turn="held")]
        assert [process.wait(10) for process in ran] == [0] * 5
        assert waiting(*waited)
    assert [process.wait(10) for process in waited] == [0] * 6
    assert [process.communicate()[0].split()[0] for process in waited] == ["held"] * 6


def test_a_compose_start_whose_images_are_there_runs_at_once_unless_told_to_build(tmp_path: Path) -> None:
    """PER-457: `compose run`, `up` and `create` with every image of the project there build
    nothing, so they never wait behind builds (`hill run`'s `run --rm ... cat` waited 20 minutes
    behind them); with `--build` they still wait."""
    (tmp_path / "there").touch()
    with locked(tmp_path / "lock", fcntl.LOCK_EX):
        ran = [
            docker(tmp_path, *args)
            for args in [
                (
                    "compose",
                    "--project-name",
                    "p",
                    "run",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "cat",
                    "grader",
                ),
                ("compose", "up", "--abort-on-container-exit"),
                ("compose", "create"),
            ]
        ]
        assert [process.wait(10) for process in ran] == [0] * 3
        assert [process.communicate()[0].split()[0] for process in ran] == ["compose"] * 3, "no turn"
        rebuilt = docker(tmp_path, "compose", "up", "--build")
        assert waiting(rebuilt)
    assert rebuilt.wait(10) == 0


def test_every_build_gets_the_mirrors_build_args_before_its_own(tmp_path: Path) -> None:
    """PER-457: COMMONHILL_TEST_BUILD_ARGS's KEY=VALUE lines are build args of every build, right after its
    `build` (a compose option's value `build` isn't it), so the build's own come after and win;
    nothing else gets them."""
    args = {"COMMONHILL_TEST_BUILD_ARGS": "PIP_INDEX_URL=http://mirror/simple/\nA=b c"}
    mirrors = "--build-arg PIP_INDEX_URL=http://mirror/simple/ --build-arg A=b c"
    runs = {
        ("build", "-t", "x", "."): f"held build {mirrors} -t x .",
        ("buildx", "build", "."): f"held buildx build {mirrors} .",
        ("compose", "--profile", "build", "build", "w"): f"held compose --profile build build {mirrors} w",
        ("compose", "up", "--no-build"): " compose up --no-build",
    }
    for argv, printed in runs.items():
        process = docker(tmp_path, *argv, **args)
        assert process.wait(10) == 0
        assert process.communicate()[0].rstrip("\n") == printed


def test_builds_share_their_turn_and_one_alone_waits_for_them_and_holds_off_the_next(tmp_path: Path) -> None:
    """PER-437: builds share the daemon; the one alone (COMMONHILL_BUILD_TURN=alone: the attempt after a
    stall) waits for those running to end, and no build starts while it waits."""
    with locked(tmp_path / "lock", fcntl.LOCK_SH):
        assert docker(tmp_path, "build", ".").wait(10) == 0, "shared with the build running"
        alone = docker(tmp_path, "build", ".", turn="alone")
        assert waiting(alone)
        later = docker(tmp_path, "build", ".")
        assert waiting(alone, later), "held at the gate by the build waiting to start alone"
    assert (alone.wait(10), later.wait(10)) == (0, 0)


def test_a_build_waiting_past_its_limit_fails_naming_the_builds_it_waits_for(tmp_path: Path) -> None:
    """PER-457: a turn held too long (a build that hangs) fails the builds waiting for it after
    COMMONHILL_BUILD_TURN_WAIT_S, exit 125, saying which builds hold the turn, rather than leaving their
    callers to time out on a wait that only said it waited."""
    holder = docker(tmp_path, "build", "--tag", "hung", ".", turn="alone", SLEEP="60")
    try:
        assert waiting(holder)
        late = docker(tmp_path, "build", ".", COMMONHILL_BUILD_TURN_WAIT_S="5")
        assert late.wait(60) == 125
        error = late.communicate()[1]
        assert "no build turn (PER-437) in " in error, error
        assert re.search(rf"pid {holder.pid}, building alone, since .*: docker build --tag hung \.", error), (
            error
        )
    finally:
        holder.kill()
        holder.wait()


@pytest.fixture
def compose_project(
    tmp_path: Path, busybox_base: str, docker_compose: Callable[..., subprocess.CompletedProcess[str]]
) -> Iterator[tuple[Path, str]]:
    """A compose project of one service whose build runs a step of its own (a nonce in its
    output, passed as a build arg and also written in the step, which names its cache record),
    and its name."""
    root, name = tmp_path / "project", f"chbuilds{uuid.uuid4().hex[:8]}"
    root.mkdir()
    nonce = uuid.uuid4().hex
    (root / "dockerfile.job").write_text(
        f"FROM {busybox_base}\nARG NONCE\nRUN echo ran-$NONCE-{nonce} > /ran\n"
    )
    (root / "compose.yaml").write_text(
        json.dumps(
            {"services": {"job": {"build": {"dockerfile": "dockerfile.job", "args": {"NONCE": nonce}}}}}
        )
    )
    yield root, name
    docker_compose(root, name, "down", "--rmi", "local")


def project_networks(name: str) -> list[str]:
    ls = ["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={name}"]
    return subprocess.run(ls, capture_output=True, text=True, check=True).stdout.split()


def test_down_with_a_one_off_container_still_there_leaves_no_network(
    docker_compose: Callable[..., subprocess.CompletedProcess[str]], compose_project: tuple[Path, str]
) -> None:
    """A one-off container still there at `down` (a `run --rm`'s still being removed, as on the box
    under load, where leaked networks used up Docker's address pools): `down` removes it first, and
    leaves no network."""
    root, name = compose_project
    docker_compose(root, name, "build")
    docker_compose(root, name, "run", "--detach", "job", "sleep", "60")
    docker_compose(root, name, "down")
    assert project_networks(name) == []


def test_compose_builds_a_project_under_the_watchdog_and_up_builds_nothing(
    harness: ModuleType,
    docker_compose: Callable[..., subprocess.CompletedProcess[str]],
    compose_project: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`up --build` has compose build the project with `built` (the watchdog, the retries), once,
    and starts that image; the image is the step's."""
    root, name = compose_project
    builds: list[list[str]] = []
    build = harness.built

    def counted(argv: list[str], cwd: Path | None) -> str:
        builds.append(argv)
        return build(argv, cwd)

    monkeypatch.setattr(harness, "built", counted)
    docker_compose(root, name, "up", "--build")
    ran = docker_compose(root, name, "run", "--rm", "job", "cat", "/ran").stdout
    assert [argv[-1] for argv in builds] == ["build"]
    assert ran.startswith("ran-")


def test_up_after_the_tests_build_builds_nothing_even_with_the_cache_collected(
    harness: ModuleType,
    docker_compose: Callable[..., subprocess.CompletedProcess[str]],
    compose_project: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On CI's runner the build's cache went while the suite built (run 42331871230: `up --build`s
    ran up to 23 steps again just after the tests' build). Here the step's cache goes between the
    two, and `up --build` still builds nothing."""
    root, name = compose_project
    nonce = json.loads((root / "compose.yaml").read_text())["services"]["job"]["build"]["args"]["NONCE"]
    prebuilt = harness.compose_prebuilt
    pruned: list[str] = []

    def collected(tmp_path_factory: pytest.TempPathFactory, root: Path, project: str) -> str:
        output = prebuilt(tmp_path_factory, root, project)
        prune = ["docker", "builder", "prune", "--all", "--force", "--filter", f"description~={nonce}"]
        pruned.append(subprocess.run(prune, capture_output=True, text=True, check=True).stdout)
        return output

    monkeypatch.setattr(harness, "compose_prebuilt", collected)
    docker_compose(root, name, "up", "--build")
    assert len(pruned) == 1
    records = [line for line in pruned[0].splitlines() if line and not line.startswith(("ID", "Total:"))]
    assert len(records) >= 1, pruned  # the step's own record went


def test_a_compose_command_that_builds_itself_fails_naming_the_build(
    docker_compose: Callable[..., subprocess.CompletedProcess[str]], compose_project: tuple[Path, str]
) -> None:
    """`run` before anything built the project builds it itself: a failure, not a build no watchdog
    saw."""
    root, name = compose_project
    with pytest.raises(
        AssertionError,
        match=r"run --rm job true built 1 images itself[^\n]*\n.*load build definition from dockerfile.job",
    ):
        docker_compose(root, name, "run", "--rm", "job", "true")


def test_a_compose_command_that_fetches_a_pinned_download_itself_fails(
    docker_compose: Callable[..., subprocess.CompletedProcess[str]],
    resets: tuple[Resets, str],
    tmp_path: Path,
    busybox_base: str,
) -> None:
    """A checksum-pinned ADD of a URL never fetched before: `run` builds it, fetching once, and
    fails (PR #279's review, R1: its steps print `DONE` alike whether they fetched or not)."""
    server, url = resets
    server.serving = True
    checksum = hashlib.sha256(server.content).hexdigest()
    root, name = tmp_path / "project", f"chbuilds{uuid.uuid4().hex[:8]}"
    root.mkdir()
    (root / "dockerfile.kit").write_text(
        f"FROM {busybox_base}\nADD --checksum=sha256:{checksum} {url}/kit.tar /kit.tar\n"
    )
    (root / "compose.yaml").write_text(
        json.dumps({"services": {"kit": {"build": {"dockerfile": "dockerfile.kit"}}}})
    )
    try:
        with pytest.raises(AssertionError, match=r"docker compose run --rm kit true built 1 images itself"):
            docker_compose(root, name, "run", "--rm", "kit", "true")
    finally:
        docker_compose(root, name, "down", "--rmi", "local")
    assert server.served == 1


def test_a_project_hill_run_builds_is_built_first_as_it_will_build_it(
    harness: ModuleType,
    compose_built: Callable[[Path, str], None],
    compose_project: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`compose_built` builds each service with `docker_build` (its watchdog and retries) as
    `hill run` then does, Dockerfile, args and context. (Whether `hill run` then finds it in the
    cache is CI's runner's to say: its cache went between two builds in run 42343848889.)"""
    root, name = compose_project
    builds: list[tuple[str, ...]] = []
    build = harness.docker_build

    def counted(*args: str, cwd: Path | None = None) -> str:
        builds.append(args)
        return build(*args, cwd=cwd)

    monkeypatch.setattr(harness, "docker_build", counted)
    compose_built(root, name)
    nonce = json.loads((root / "compose.yaml").read_text())["services"]["job"]["build"]["args"]["NONCE"]
    assert builds == [("--file", str(root / "dockerfile.job"), "--build-arg", f"NONCE={nonce}", str(root))]


def test_a_compose_command_that_builds_several_services_fails_naming_their_build(
    docker_compose: Callable[..., subprocess.CompletedProcess[str]], compose_project: tuple[Path, str]
) -> None:
    """`create` builds a project of two services itself; BuildKit names their build `[SERVICE
    internal]`."""
    root, name = compose_project
    config = json.loads((root / "compose.yaml").read_text())
    config["services"]["job2"] = config["services"]["job"]
    (root / "compose.yaml").write_text(json.dumps(config))
    with pytest.raises(
        AssertionError, match=r"create built 1 images itself[^\n]*\n\[job2? internal\] load build"
    ):
        docker_compose(root, name, "create")


def test_a_compose_build_failing_on_the_network_is_built_again_until_it_fetches(
    harness: ModuleType,
    tmp_path_factory: pytest.TempPathFactory,
    resets: tuple[Resets, str],
    tmp_path: Path,
) -> None:
    """The tests' compose build of a project fetching through the resetting server: an unpinned `ADD`,
    so nothing fetches it first. (Any later build would fetch it again: BuildKit asks for an unpinned
    URL every time, which is why the projects' downloads are pinned.)"""
    server, url = resets
    root, name = tmp_path / "project", f"chbuilds{uuid.uuid4().hex[:8]}"
    root.mkdir()
    (root / "dockerfile.kit").write_text(f"FROM scratch\nADD {url}/kit.tar /kit.tar\n")
    (root / "compose.yaml").write_text(
        json.dumps({"services": {"kit": {"build": {"dockerfile": "dockerfile.kit"}}}})
    )
    try:
        harness.compose_prebuilt(tmp_path_factory, root, name)
    finally:
        subprocess.run(["docker", "image", "rm", f"{name}-kit"], capture_output=True, check=False)
    assert [seconds for seconds, _ in server.waits] == list(harness.BUILD_RETRY_WAITS_S)
    assert server.served == 1


def test_a_compose_command_is_bounded_and_leaves_nothing_running(
    docker_compose: Callable[..., subprocess.CompletedProcess[str]], compose_project: tuple[Path, str]
) -> None:
    """Killed at its bound, and its container with it (left, it held the project's network, and
    leaked networks used up Docker's address pools on the box)."""
    root, name = compose_project
    docker_compose(root, name, "build")
    started = time.monotonic()
    with pytest.raises(AssertionError, match="docker compose run --rm job sleep 120: over 5 s"):
        docker_compose(root, name, "run", "--rm", "job", "sleep", "120", run_s=5)
    assert time.monotonic() - started < 120
    left = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={name}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert left == []


# A registry to stand in for CI's (Docker treats 127.0.0.1's as plain HTTP).
REGISTRY = "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"


@pytest.fixture
def registry(harness: ModuleType) -> Iterator[str]:
    """A registry of the test's own: its address."""
    harness.pulled(REGISTRY)
    name = f"commonhill-test-registry-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "--detach", "--rm", "--name", name, "--publish", "127.0.0.1::5000", REGISTRY],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        address = subprocess.run(
            ["docker", "port", name, "5000"], check=True, capture_output=True, text=True
        ).stdout.split()[0]
        host, _, port = address.rpartition(":")
        deadline = time.monotonic() + 60
        while True:
            try:
                with socket.create_connection((host, int(port)), timeout=2):
                    break
            except OSError:
                assert time.monotonic() < deadline, f"the registry at {address} never answered"
                time.sleep(1)
        yield address
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)


def test_a_keyed_image_comes_from_the_registry_only_for_the_same_inputs(
    build_image: Callable[..., str],
    local_base: str,
    registry: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PER-477: a keyed image (`keyed=True`) is built and pushed to COMMONHILL_TEST_IMAGE_CACHE by the run
    that first needs it, and a later run of the same inputs takes the registry's copy and builds
    nothing, whatever its tag. A change of any input under the same tag is built: a file the
    Dockerfile copies, the Dockerfile, a build arg, the image it builds on, the .dockerignore; a
    file the .dockerignore leaves out of the context is no input. An image built without the key
    never reaches the registry."""
    monkeypatch.setenv("COMMONHILL_TEST_IMAGE_CACHE", f"{registry}/cache")
    monkeypatch.setenv("COMMONHILL_TEST_IMAGE_CACHE_PUSH", "1")
    nonce = uuid.uuid4().hex
    keyed, unkeyed = f"commonhill-test/keyed-{nonce[:12]}:1", f"commonhill-test/unkeyed-{nonce[:12]}:1"
    base = f"commonhill-test/base-{nonce[:12]}:1"
    subprocess.run(["docker", "tag", local_base, base], check=True, capture_output=True)
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = f"FROM {base}\nARG WHO=nobody\nCOPY . /ctx/\nRUN echo {nonce} $WHO > /built\n"
    (context / "Dockerfile").write_text(dockerfile)
    (context / "input").write_text("one\n")
    (context / "left-out").write_text("one\n")
    (context / ".dockerignore").write_text("left-out\n")

    def build(*args: str) -> str:
        """Build `keyed` afresh (none in the daemon): "pulled" or "built", and what it holds."""
        subprocess.run(["docker", "image", "rm", "--force", keyed], capture_output=True, check=False)
        output = build_image("--tag", keyed, *args, str(context), keyed=True)
        held = subprocess.run(
            ["docker", "run", "--rm", keyed, "cat", "/built", "/ctx/input"],
            check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip
        return f"{'pulled' if output == f'image cache: {keyed}' else 'built'}: {held}"

    try:
        assert build() == f"built: {nonce} nobody\none\n"
        assert build() == f"pulled: {nonce} nobody\none\n"
        (context / "left-out").write_text("two\n")
        assert build().startswith("pulled"), "a file the context leaves out is no input"
        (context / "input").write_text("two\n")
        assert build() == f"built: {nonce} nobody\ntwo\n", "a copied file changed"
        (context / "Dockerfile").write_text(dockerfile.replace("echo", "echo  "))
        assert build().startswith("built"), "the Dockerfile changed"
        assert build("--build-arg", "WHO=someone") == f"built: {nonce} someone\ntwo\n", "a build arg"
        (context / ".dockerignore").write_text("left-out\ninput\n")
        with pytest.raises(subprocess.CalledProcessError):  # the image has no /ctx/input now
            build()
        (context / ".dockerignore").write_text("left-out\n")
        subprocess.run(["docker", "tag", keyed, base], check=True, capture_output=True)
        assert build().startswith("built"), "the image it builds on changed"

        build_image("--tag", unkeyed, str(context))
        with urllib.request.urlopen(f"http://{registry}/v2/_catalog") as answer:
            catalog = json.loads(answer.read())["repositories"]
        assert f"cache/commonhill-test/keyed-{nonce[:12]}" in catalog, catalog
        assert not any("unkeyed" in name for name in catalog), "an unkeyed image is pushed nowhere"
    finally:
        listed = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True
        ).stdout.split()
        mine = [name for name in listed if nonce[:12] in name]
        subprocess.run(["docker", "image", "rm", "--force", *mine], capture_output=True, check=False)
