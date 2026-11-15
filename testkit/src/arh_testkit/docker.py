"""The tests' Docker builds, pulls and compose runs: the package mirrors every build goes through,
the build turn (bin/docker, first on the tests' PATH), the pinned downloads fetched before a
build, the watchdog and the network retries of every build and pull, the tests' `docker compose`,
the registry's copies of keyed images, and what a session builds once."""

import fcntl
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import urlsplit

import pytest

# --- the mirrors every pull and build goes through (PER-304, PER-457) ---------------------------
# In CI, the package proxy's PyPI and crates.io mirrors (.github/workflows/ci.yml), as a build VM's
# (its cache stands between a build and the WAN: PyPI's read timeouts, cargo's "transfer too slow"
# from static.crates.io): ARH_TEST_PIP_INDEX, a simple index, for pip (PIP_INDEX_URL and
# PIP_TRUSTED_HOST); ARH_TEST_PYPI_FILES, PyPI's files, for the URLs in uv.lock (PYPI_FILES_MIRROR:
# the services' images, whose `uv sync --frozen` takes each file from its locked URL); and
# ARH_TEST_CARGO_MIRROR, a sparse index, for cargo (CARGO_MIRROR_URL). Each is a build arg of every
# build a test runs (the testkit's bin/docker adds them), which a Dockerfile that declares it reads.
# Unset: PyPI and crates.io.
PIP_MIRROR_ENV = "ARH_TEST_PIP_INDEX"
PYPI_FILES_ENV = "ARH_TEST_PYPI_FILES"
CARGO_MIRROR_ENV = "ARH_TEST_CARGO_MIRROR"
# Docker Hub through the job's own daemon's registry mirror (CI's runner pods: arc-runners'
# docker-hub-mirror), for the Docker daemons the tests start in containers, which pull from Docker
# Hub directly otherwise: an empty daemon's pulls met its anonymous rate limit (429, PER-457). The
# local server's job containers get it as ARH_LOCAL_REGISTRY_MIRROR (deploy/local/compose.yaml),
# test_dockerjob_docker's as ARH_DOCKER_REGISTRY_MIRROR (arh_controller.dockerjob).
REGISTRY_MIRROR_ENV = "ARH_DOCKER_REGISTRY_MIRROR"
LOCAL_REGISTRY_MIRROR_ENV = "ARH_LOCAL_REGISTRY_MIRROR"


def mirror_build_args(env: Mapping[str, str]) -> dict[str, str]:
    """The build args that send a build's pip, uv and cargo downloads to the mirrors `env` names
    (see above); none for an unset one."""
    args: dict[str, str] = {}
    if url := env.get(PIP_MIRROR_ENV, ""):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise pytest.UsageError(
                f"{PIP_MIRROR_ENV} must be a simple index URL like http://host/pypi/simple/, got {url!r}"
            )
        args |= {"PIP_INDEX_URL": url, "PIP_TRUSTED_HOST": parts.netloc}
    if url := env.get(PYPI_FILES_ENV, ""):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or not url.endswith("/"):
            raise pytest.UsageError(
                f"{PYPI_FILES_ENV} must be a URL like http://host/pypi/files/ (standing for "
                f"https://files.pythonhosted.org/), got {url!r}"
            )
        args["PYPI_FILES_MIRROR"] = url
    if url := env.get(CARGO_MIRROR_ENV, ""):
        parts = urlsplit(url)
        if parts.scheme not in ("sparse+http", "sparse+https") or not parts.netloc:
            raise pytest.UsageError(
                f"{CARGO_MIRROR_ENV} must be a sparse index URL like sparse+http://host/crates/index/, "
                f"got {url!r}"
            )
        args["CARGO_MIRROR_URL"] = url
    return args


def registry_mirror(docker: str) -> str:
    """The first registry mirror of the daemon `docker` reaches, or "" (none, or no daemon)."""
    try:
        done = subprocess.run(
            [docker, "info", "--format", "{{json .RegistryConfig.Mirrors}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        mirrors = cast(object, json.loads(done.stdout)) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    return str(cast(list[object], mirrors)[0]) if isinstance(mirrors, list) and mirrors else ""


def session_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory every pytest-xdist worker of this session shares (each worker's own basetemp
    is popen-gwN in it); without xdist, the basetemp."""
    basetemp = tmp_path_factory.getbasetemp()
    return basetemp.parent if os.environ.get("PYTEST_XDIST_WORKER") else basetemp


@pytest.fixture(scope="session")
def shared_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`session_dir`: for what the session's pytest-xdist workers share, such as locks."""
    return session_dir(tmp_path_factory)


def docker_turns() -> None:
    """bin/docker (DOCKER_TURNS_BIN) first on PATH, the `docker` found before it as ARH_REAL_DOCKER,
    and the turn's lock and gate, for this process and those it starts (pytest-xdist's workers find
    them set): every build a test runs takes turns (PER-437, at BUILD_TURN_LOCK) and gets the
    package mirrors' build args (ARH_TEST_BUILD_ARGS, `mirror_build_args`). The Docker daemons the
    tests start in containers pull through this daemon's registry mirror, if it has one
    (`registry_mirror`)."""
    real = shutil.which("docker")
    if os.environ.get("ARH_REAL_DOCKER") or real is None:
        return
    if shutil.which("flock") is None:
        raise pytest.UsageError("arh_testkit's docker (its bin/docker) needs flock (util-linux) on PATH")
    os.environ.update(
        ARH_REAL_DOCKER=real,
        ARH_BUILD_TURN_LOCK=str(BUILD_TURN_LOCK),
        ARH_BUILD_TURN_GATE=str(BUILD_TURN_GATE),
        ARH_TEST_BUILD_ARGS="\n".join(
            f"{key}={value}" for key, value in mirror_build_args(os.environ).items()
        ),
        PATH=f"{DOCKER_TURNS_BIN}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    mirror = os.environ.get(REGISTRY_MIRROR_ENV) or registry_mirror(real)
    if mirror:
        os.environ.setdefault(REGISTRY_MIRROR_ENV, mirror)
        os.environ.setdefault(LOCAL_REGISTRY_MIRROR_ENV, mirror)


def built_once(
    tmp_path_factory: pytest.TempPathFactory, name: str, inputs: object, build: Callable[[Path], None]
) -> Path:
    """The file `name`, built by `build(path)` once per pytest session (PER-345): the first
    pytest-xdist worker to ask builds it under a lock, in the directory every worker of the session
    shares, and the others wait for it and use it. Its name carries the digest of `inputs` (what
    the build is made of: scripts, base images), so a build of other inputs is never taken for it.
    A build that fails fails every later ask of the session with its error (PER-344), rather than
    each waiting worker building it again."""
    shared = session_dir(tmp_path_factory)
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    path = shared / f"{name}-{digest}"
    failed = shared / f"{path.name}.failed"
    with (shared / f"{path.name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if failed.exists():
            raise AssertionError(f"{name}: its build failed earlier in this session:\n{failed.read_text()}")
        if not path.exists():
            partial = shared / f"{path.name}.partial"
            try:
                build(partial)
            except Exception as error:
                failed.write_text(f"{type(error).__name__}: {error}"[-BUILD_OUTPUT_TAIL:])
                raise
            partial.rename(path)
    return path


@pytest.fixture(scope="session")
def build_once(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[str, object, Callable[[Path], None]], Path]:
    """`built_once`, for the tests' own session fixtures."""
    return lambda name, inputs, build: built_once(tmp_path_factory, name, inputs, build)


# --- builds that can't hang (PER-344) or fall to a flaky mirror (PER-388) -----------------------
# A `docker build` that prints nothing for BUILD_STALL_S is stuck, not slow: under load a build's
# slowest step (QEMU's compile) still prints a line every few seconds. One went silent on main
# (run 41938581849: the 20-minute dump, then nothing until a cancel at 68 minutes), most likely in
# an `ADD https://...`: BuildKit retries a request that fails, but nothing bounds reading the body
# of one that stalls. So `docker_build` kills a silent build and fails with its last lines, which
# name the step; and the projects' pinned downloads are fetched first, by `warmed`, with bounds and
# retries of the tests' own.
#
# A frozen Dockerfile's `apt-get install` can't be given more retries than apt's own (three, 30 s
# apart): nothing reaches its RUN but the Dockerfile. deb.debian.org's Fastly addresses dropped
# connections for longer than that (PER-388, run 42289113597: `E: Failed to fetch ... Connection
# timed out` after 195 s). So a build that fails on a network error, or stalls on one, is built
# again after BUILD_RETRY_WAITS_S, up to BUILD_ATTEMPTS times: its finished steps come back CACHED,
# and only the failed fetch runs again. Any other failure fails at once. The evidence is read per
# BuildKit step (`steps`), since steps run side by side and their lines interleave: a failed step
# whose network error is its own (a RUN's only if the RUN starts by fetching, so a command's echo
# or exit status isn't taken for one), or a step still running at a stall that is waiting on the
# network (`network_failure`).
#
# A build after a stall has the daemon to itself (PER-437). BuildKit runs a step once for every
# build that asks for it at the same time (the same base, commands and args: the AlgoTune
# projects' graders, or any two images with the same `apt-get` step), and kills it only once every
# one of those builds is gone. So a stalled build killed while another build still waited on the
# same step left the step hung, and its next attempt joined it again, replaying its lines (run
# 42849431922: all three attempts of three tests end on the same `30.95 Get:3`); `arh run`'s
# builds, unwatched, joined it too (run 42818995685: over 2100 s). So every build takes turns:
# `docker` is the testkit's bin/docker on the tests' PATH (`docker_turns`), whatever runs it (the
# tests, `arh run`, the runner, the template's stack). Builds share the daemon, but the attempt
# after a stall (ARH_BUILD_TURN=alone) waits until every build running has ended (one may still
# wait on the step that stalled; its own watchdog ends it at its stall) and starts alone, so it
# runs the step afresh. A failed step fails every build waiting on it, so the attempt after a
# failure shares the daemon. Only a command that builds takes a turn: a compose `run` or `up`
# whose images are all there runs at once (PER-457: `arh run`'s paired runs and `run --rm ... cat`
# waited 20 minutes behind builds and a retry waiting to go alone), and a wait for a turn past
# ARH_BUILD_TURN_WAIT_S fails naming the builds that hold it (bin/docker).
BUILD_STALL_S = 600
BUILD_DEADLINE_S = 3600
BUILD_OUTPUT_TAIL = 8000  # characters of a failed build's output in its error
BUILD_ATTEMPTS = 3
BUILD_RETRY_WAITS_S = (60, 120)  # before the 2nd attempt, before the 3rd
BUILD_TURN_LOCK = Path(tempfile.gettempdir()) / "arh-test-docker-builds.lock"
BUILD_TURN_GATE = Path(tempfile.gettempdir()) / "arh-test-docker-builds.gate"
# The testkit's `docker` (bin/docker), beside src/ in its source tree: the package is installed
# editable from it (a uv workspace member), not as a wheel, which has no bin/.
DOCKER_TURNS_BIN = Path(__file__).resolve().parents[2] / "bin"
# A transport failure, as a line names it. (apt's `Failed to fetch` alone isn't one: it ends with
# the reason, which is, or a `404  Not Found` that isn't.) A registry's 429 and 5xx answers (Docker
# Hub's rate limit; CI's Docker Hub mirror's 502s when its upstream drops a connection), BuildKit's
# content store losing a pull to a concurrent one (`lease does not exist`, `failed commit on ref`)
# and uv's network errors are too (PER-457).
NETWORK_FAILURE = re.compile(
    r"Temporary failure resolving|Could not resolve|Could not connect|Connection timed out"
    r"|Connection failed|Connection reset|connection reset by peer|Connection refused"
    r"|Network is unreachable|i/o timeout|TLS handshake timeout|ReadTimeoutError|Max retries exceeded"
    r'|transfer too slow|Unable to connect|Get "[^"]+": (?:EOF|dial tcp)'
    r"|429 Too Many Requests|toomanyrequests|50[234] (?:Bad Gateway|Service Unavailable|Gateway Time)"
    r"|lease does not exist|failed commit on ref|operation timed out|connection-timeout"
    r"|a temporary issue with the remote server or the network connection"
    r"|is under load\. Please try again later",  # a Forgejo registry's refusal (Codeberg's 403)
    re.IGNORECASE,
)
# apt's last word after failed fetches (run 42289113597: `E: Unable to fetch some archives`): a
# network failure only if the step also printed a transport failure and no failed fetch for an
# HTTP 4xx (a package gone from the mirror is `404  Not Found`, and is so again next time; the
# 404s apt meets trying an index's other compressions, as `Err:`, don't count).
APT_SUMMARY = re.compile(r"E: Unable to fetch some archives|E: Some index files failed to download")
APT_GONE = re.compile(r"E: Failed to fetch \S+\s+4\d\d\s")
APT_FETCH_LINE = re.compile(r"^[\d.]+ (?:Get|Ign|Err):\d+ ")  # a step's line, its `#N` taken off
# A step that is the network from its start: a RUN whose first command fetches (apt prints nothing
# while it connects: probe.dockerfile's `RUN apt-get update` sat 600 s so), a base image's pull or
# its metadata's. (A stalled `ADD` of a URL is not retried: the projects' downloads are fetched
# first, by `warmed`, with retries of its own, so the build's ADD is the cache.) Its first command
# may be bounded and tried again (the services' `for try in 1 2 3; do timeout 120 apt-get update`).
FETCHING_RUN = re.compile(
    r"^\[[^\]]+\] RUN (?:for +\w+ +in [^;]*; *do +)?(?:timeout +\d+ +)?(?:[A-Za-z_]\w*=\S* +)*"
    r"(?:apt-get +(?:-o +\S+ +|-\S+ +)*(?:update|install)|apk +add"
    r"|(?:python3? +-m +)?pip3? +(?:install|download)|uv +(?:sync|pip +install)|pnpm +install"
    r"|npm +(?:ci|install)|curl|wget|cargo +(?:fetch|install|build))\b"
)
FETCHING_STEP = re.compile(r"^\[[^\]]+\] FROM |^\[(?:[^\]]+ )?internal\] load metadata for ")
STEP_LINE = re.compile(r"^#(\d+) (.*)$", re.MULTILINE)
STEP_END = re.compile(r"^(?:DONE [\d.]+s|CACHED|CANCELED|ERROR\b.*)$")
NETWORK_TAIL = 3000  # characters at a failed build's end BuildKit's own error is looked for in


@dataclass
class Step:
    """A BuildKit step of a `--progress plain` build: its name, its own lines, how it ended ("" if
    it hadn't)."""

    name: str
    lines: list[str]
    ended: str = ""


def steps(output: str) -> dict[str, Step]:
    """`output`'s steps, by number."""
    found: dict[str, Step] = {}
    for n, text in STEP_LINE.findall(output):
        step = found.get(n)
        if step is None:
            found[n] = Step(text, [])
        elif text == step.name:  # its name again: its lines resume after another's, or, after it
            if step.ended:  # ended (CANCELED), BuildKit runs it again: what it printed was the
                step.lines = []  # run before's (a stalled build's retry that joined the step, PER-457)
            step.ended = ""
        elif STEP_END.match(text):
            step.ended = text
        elif text != "...":  # `...`: its lines go on later, after another step's
            step.lines.append(text)
    return found


def watched(
    argv: list[str], cwd: Path | None, stall_s: float, deadline_s: float, env: dict[str, str] | None = None
) -> tuple[str, str, int]:
    """Run `argv` (in `env`, or this process's), its output and error merged: (output, why it was
    killed or "", exit code). It is killed (its whole process group) when it prints nothing for
    `stall_s` or runs past `deadline_s`."""
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    assert process.stdout is not None
    lines: list[bytes] = []
    last = [time.monotonic()]

    def read(stream: IO[bytes]) -> None:
        for line in stream:
            lines.append(line)
            last[0] = time.monotonic()

    reader = threading.Thread(target=read, args=(process.stdout,), daemon=True)
    reader.start()
    started, why = time.monotonic(), ""
    while process.poll() is None:
        now = time.monotonic()
        if now - last[0] > stall_s:
            why = f"printed nothing for {stall_s:g} s"
        elif now - started > deadline_s:
            why = f"ran past {deadline_s:g} s"
        if why:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            break
        time.sleep(1)
    reader.join(10)
    return b"".join(lines).decode(errors="replace"), why, process.returncode


def network_failure(output: str, why: str) -> str:
    """Why a failed build is a network failure (the step, and its line that shows it), or "".
    Killed silent: a step still running that waits on the network (a base image's pull, or a RUN
    that starts by fetching and printed nothing since, or apt's fetching or a network error last).
    Failed: a failed step's own network error (a RUN's only if it starts by fetching); with
    no step failed, BuildKit's own error. Killed at the deadline: never."""
    if why and not why.startswith("printed nothing"):
        return ""
    found = steps(output)
    if why:
        for n, step in found.items():
            last = step.lines[-1] if step.lines else ""
            waiting = FETCHING_RUN.match(step.name) and (
                not last or APT_FETCH_LINE.match(last) or NETWORK_FAILURE.search(last)
            )
            if not step.ended and (FETCHING_STEP.match(step.name) or waiting):
                return f"#{n} {step.name}" + (f": {last}" if last else "")
        return ""
    failed = {n: step for n, step in found.items() if step.ended.startswith("ERROR")}
    for n, step in failed.items():
        run = re.match(r"\[[^\]]+\] RUN ", step.name)
        if run and not FETCHING_RUN.match(step.name):
            continue
        # Only what the step printed last, before it failed, counts: a later line (a compile
        # error after a fetch that failed and was let pass) means it failed on something else. A
        # RUN's error line holds its command, not what it printed, so it doesn't count.
        last = step.lines[-1] if step.lines else ""
        for line in [last] if run else [step.ended, last]:
            if NETWORK_FAILURE.search(line):
                return f"#{n} {line}"
        if run and APT_FETCH_LINE.match(last):  # its fetch cut short (its own `timeout`, PER-457)
            return f"#{n} {last}"
        if (
            APT_SUMMARY.search(last)
            and any(NETWORK_FAILURE.search(line) for line in step.lines)
            and not any(APT_GONE.search(line) for line in step.lines)
        ):
            return f"#{n} {last}"
    if failed:
        return ""
    for line in reversed(output[-NETWORK_TAIL:].splitlines()):
        if not line.startswith("#") and 'process "' not in line and NETWORK_FAILURE.search(line):
            return line.strip()
    return ""


def retry_wait(seconds: float, reason: str) -> None:
    """Wait before building again after a network failure, saying so (CI's log counts them)."""
    print(f"build: network failure ({reason[:300]}); building again in {seconds:g} s", flush=True)
    time.sleep(seconds)


def built(argv: list[str], cwd: Path | None) -> str:
    """`argv`, a build (`docker build` or `docker compose build`, plain progress), its output; an
    AssertionError with the output's end if it fails, prints nothing for BUILD_STALL_S, or runs
    past BUILD_DEADLINE_S (it is killed then). A failure on a network error (`network_failure`) is
    built again, up to BUILD_ATTEMPTS times: alone after a stall (ARH_BUILD_TURN, above)."""
    attempt, env = 1, None
    while True:
        output, why, code = watched(argv, cwd, BUILD_STALL_S, BUILD_DEADLINE_S, env)
        if not why and code == 0:
            return output
        reason = network_failure(output, why)
        if not reason or attempt == BUILD_ATTEMPTS:
            what = why or f"exit {code}"
            if reason:
                what += f", on the network in all {attempt} attempts"
            tail = output[-BUILD_OUTPUT_TAIL:]
            raise AssertionError(f"{' '.join(argv[:6])}: {what}; its output's end:\n{tail}")
        retry_wait(BUILD_RETRY_WAITS_S[attempt - 1], reason)
        attempt, env = attempt + 1, {**os.environ, "ARH_BUILD_TURN": "alone"} if why else None


def docker_build(*args: str, cwd: Path | None = None) -> str:
    """`docker build --progress=plain *args` (`built`), its base images pulled first (`pulled`):
    its output."""
    dockerfile = dockerfile_of(args, cwd)
    if dockerfile.is_file():
        for image in base_images(dockerfile.read_text()):
            pulled(image)
    return built(["docker", "build", "--progress=plain", *args], cwd)


# --- base images: pulled first, with retries (PER-457) ------------------------------------------
# BuildKit takes a FROM's image (or a COPY --from's) from the daemon's own store when it is there,
# and asks its registry otherwise, at every build, cached or not: CI's builds met its Docker Hub
# mirror's 502s, Docker Hub's 429s, and concurrent builds' pulls of one image racing in BuildKit's
# content store (`lease does not exist`). So a build's images are pulled first (`pulled`), with
# retries and backoff of the tests' own, once per daemon (the pulls of an image take turns across
# the session's workers); the build then never asks a registry. A pull that fails other than on
# the network (no such image) is left to the build to report.
PULL_ATTEMPTS = 4
PULL_RETRY_WAITS_S = (15, 60, 180)  # before the 2nd attempt, the 3rd, the 4th
PULL_TIMEOUT_S = 900
PULLS_DIR = Path(tempfile.gettempdir()) / "arh-test-pulls"  # the pulls' locks
FROM_LINE = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
COPY_FROM = re.compile(r"^COPY\s+(?:--\S+\s+)*?--from=(\S+)", re.IGNORECASE)


def base_images(dockerfile: str) -> list[str]:
    """The images a Dockerfile's text builds on: its FROMs' and its COPY --from's that name no
    stage, each once (none of `scratch`, nor one spelled with a build arg)."""
    stages: set[str] = set()
    found: dict[str, None] = {}
    for line in re.sub(r"\\\n[ \t]*", " ", dockerfile).splitlines():
        line = line.strip()
        if match := FROM_LINE.match(line):
            image, stage = match.group(1), match.group(2)
        elif match := COPY_FROM.match(line):
            image, stage = match.group(1), None
        else:
            continue
        if image.lower() != "scratch" and image not in stages and not image.isdigit() and "$" not in image:
            found[image] = None
        if stage:
            stages.add(stage)
    return list(found)


def pulled(image: str) -> None:
    """`image` in the daemon's store: there already, or pulled (see above)."""

    def there() -> bool:
        inspect = ["docker", "image", "inspect", "--format", ".", image]
        return subprocess.run(inspect, capture_output=True, check=False).returncode == 0

    if there():
        return
    PULLS_DIR.mkdir(exist_ok=True)
    errors: list[str] = []
    with (PULLS_DIR / f"{hashlib.sha256(image.encode()).hexdigest()[:16]}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for attempt in range(1, PULL_ATTEMPTS + 1):
            if there():
                return
            try:
                done = subprocess.run(
                    ["docker", "pull", "--quiet", image],
                    capture_output=True,
                    text=True,
                    timeout=PULL_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                error = f"over {PULL_TIMEOUT_S} s"
            else:
                if done.returncode == 0:
                    return
                error = (done.stderr or done.stdout).strip()[-500:]
                if not NETWORK_FAILURE.search(error):
                    return
            errors.append(f"attempt {attempt}: {error}")
            if attempt < PULL_ATTEMPTS:
                wait_s = PULL_RETRY_WAITS_S[attempt - 1]
                print(f"pull: {image}: {error[:300]}; pulling again in {wait_s} s", flush=True)
                time.sleep(wait_s)
    raise AssertionError(
        f"pulling {image} failed on the network {PULL_ATTEMPTS} times:\n" + "\n".join(errors)
    )


@pytest.fixture(scope="session")
def pull_image() -> Callable[[str], None]:
    """`pulled`, for a test that runs an image it doesn't build (the test Forgejo)."""
    return pulled


@dataclass(frozen=True)
class PinnedDownload:
    """An `ADD [--flags] --checksum=sha256:... URL DEST` of a Dockerfile: its flags, as written."""

    url: str
    sha256: str
    flags: str

    @property
    def filename(self) -> str:
        return urlsplit(self.url).path.rsplit("/", 1)[-1]


URL_ADD = re.compile(r"^ADD\s+((?:--\S+\s+)*)(https?://\S+)\s+\S+$")
FETCH_READ_TIMEOUT_S = 60  # a read (or connect) waiting this long fails the attempt
FETCH_DEADLINE_S = 900  # an attempt's whole download (QEMU's 135 MB at 1.2 Mbit/s)
FETCH_ATTEMPTS = 3
FETCH_READ_BYTES = 1 << 16


def url_adds(dockerfile: Path) -> list[tuple[str, str]]:
    """The `ADD`s of a URL in `dockerfile`, as (flags, URL), an instruction's continuation lines
    joined (Hutter's Zig `ADD` spans two)."""
    text = re.sub(r"\\\n[ \t]*", " ", dockerfile.read_text())
    return [
        (" ".join(match.group(1).split()), match.group(2))
        for line in text.splitlines()
        if (match := URL_ADD.match(line.strip()))
    ]


def pinned_downloads(*dockerfiles: Path) -> list[PinnedDownload]:
    """The checksum-pinned `ADD`s of `dockerfiles`, each once."""
    found: dict[str, PinnedDownload] = {}
    for dockerfile in dockerfiles:
        for flags, url in url_adds(dockerfile):
            checksum = re.search(r"--checksum=sha256:([0-9a-f]{64})\b", flags)
            if checksum is not None:
                found.setdefault(url, PinnedDownload(url, checksum.group(1), flags))
    return list(found.values())


def fetch(download: PinnedDownload, path: Path) -> None:
    """`download`'s file, at `path`: up to FETCH_ATTEMPTS attempts, each failing on a read that
    waits FETCH_READ_TIMEOUT_S or past FETCH_DEADLINE_S, and checked by its sha256. A read returns
    what has arrived (`read1`), so a server trickling bytes can't hold an attempt past its
    deadline: it ends at the first read after it."""
    errors: list[str] = []
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            digest = hashlib.sha256()
            with (
                urllib.request.urlopen(download.url, timeout=FETCH_READ_TIMEOUT_S) as response,
                path.open("wb") as out,
            ):
                while chunk := response.read1(FETCH_READ_BYTES):
                    if time.monotonic() - started > FETCH_DEADLINE_S:
                        raise TimeoutError(f"not done after {FETCH_DEADLINE_S} s")
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != download.sha256:
                raise ValueError(f"sha256 {digest.hexdigest()}, not the pinned {download.sha256}")
            return
        except (OSError, ValueError) as error:  # URLError, timeouts and resets are OSErrors
            errors.append(f"attempt {attempt}, after {time.monotonic() - started:.0f} s: {error!r}")
    raise AssertionError(f"fetching {download.url} failed {FETCH_ATTEMPTS} times:\n" + "\n".join(errors))


def warm_download(download: PinnedDownload, directory: Path) -> None:
    """BuildKit's cache holds `download` after this, so a build's `ADD` of it never fetches: the
    cache key of a pinned `ADD` is its file's name, flags and checksum, not its URL. The file is
    fetched here (`fetch`) and added by a build from a server on 127.0.0.1, which the daemon shares
    (the runner's Docker daemon runs in its pod)."""
    directory.mkdir(parents=True, exist_ok=True)
    fetch(download, directory / download.filename)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/{download.filename}"
        context = directory / "context"
        context.mkdir(exist_ok=True)
        (context / "Dockerfile").write_text(
            f"FROM scratch\nADD {download.flags} {url} /{download.filename}\n"
        )
        docker_build(str(context))
    finally:
        server.shutdown()


def warmed(tmp_path_factory: pytest.TempPathFactory, *dockerfiles: Path) -> None:
    """`dockerfiles`' pinned downloads in BuildKit's cache (`warm_download`), each once per session
    (`built_once`)."""
    for download in pinned_downloads(*dockerfiles):

        def build(path: Path, download: PinnedDownload = download) -> None:
            warm_download(download, tmp_path_factory.mktemp("download"))
            path.write_text(download.url)

        built_once(tmp_path_factory, "download", [download.url, download.sha256, download.flags], build)


# --- the tests' `docker compose` (PER-388) -------------------------------------------------------
# A project's README runs `docker compose up --build`, and compose's build ran outside
# `docker_build`: no silence watchdog, no network retries, an hour per call (run 42289113597's
# attempt 3 hung in four of them). So `docker_compose` has compose build the project first under
# the same watchdog and retries (`compose_prebuilt`: `built`), and `up` then starts the images that
# built (`--no-build`): a cached rebuild isn't safe, since on CI's runner the build's cache went
# while the suite built (run 42331871230's `up --build`s ran up to 23 steps again). Any other
# command that builds at all fails (`COMPOSE_BUILT`): its build is one nothing watched, cached or
# not (a checksum-pinned ADD prints `DONE` alike whether it fetched or not). Every command is
# bounded by COMPOSE_RUN_S. (Compose builds for compose: under the whole suite on the box, a
# compose build found none of a `docker build`'s steps a moment after it.)
COMPOSE_RUN_S = 1200
COMPOSE_BUILDS = {"up", "build"}
COMPOSE_BUILT = re.compile(r"^#(\d+) (\[(?:\S+ )?internal\] load build definition from .*)$", re.MULTILINE)


@dataclass(frozen=True)
class ComposeBuild:
    """A compose service's build, resolved (`docker compose config`)."""

    dockerfile: Path
    context: Path
    args: tuple[tuple[str, str], ...]
    target: str

    def flags(self) -> list[str]:
        flags = ["--file", str(self.dockerfile)]
        flags += [flag for key, value in self.args for flag in ("--build-arg", f"{key}={value}")]
        return [*flags, *(["--target", self.target] if self.target else [])]


def compose_services(root: Path | None, *args: str) -> dict[str, dict[str, Any]]:
    """The services of `docker compose *args` in `root`, as compose resolves them (its overrides
    merged)."""
    done = subprocess.run(
        ["docker", "compose", *args, "config", "--format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(f"docker compose config in {root}: {done.returncode}\n{done.stderr}")
    return cast(dict[str, dict[str, Any]], json.loads(done.stdout)["services"])


def compose_builds(root: Path, project: str) -> list[ComposeBuild]:
    """The builds of `root`'s compose project (`compose_services`), each once."""
    builds: dict[ComposeBuild, None] = {}
    for service in compose_services(root, "--project-name", project).values():
        build = cast(dict[str, Any] | None, service.get("build"))
        if not build:
            continue
        context = Path(build["context"])
        declared = cast(dict[str, object], build.get("args") or {})
        args = tuple(sorted((key, str(value)) for key, value in declared.items()))
        dockerfile = context / str(build.get("dockerfile", "Dockerfile"))
        builds[ComposeBuild(dockerfile, context, args, str(build.get("target", "")))] = None
    return list(builds)


def compose_pulled(services: dict[str, dict[str, Any]]) -> None:
    """Every image the services build on, or run unbuilt, in the daemon's store (`pulled`)."""
    images: dict[str, None] = {}
    for service in services.values():
        build = cast(dict[str, Any] | None, service.get("build"))
        if not build:
            if image := service.get("image"):
                images[str(image)] = None
            continue
        dockerfile = Path(build["context"]) / str(build.get("dockerfile", "Dockerfile"))
        inline = build.get("dockerfile_inline")
        text = str(inline) if inline else dockerfile.read_text() if dockerfile.is_file() else ""
        images |= dict.fromkeys(base_images(text))
    for image in images:
        pulled(image)


def compose_prebuilt(tmp_path_factory: pytest.TempPathFactory, root: Path, project: str) -> str:
    """`root`'s compose project built by compose (`built`: under the watchdog, its network
    failures built again), its images pulled (`compose_pulled`) and its Dockerfiles' pinned
    downloads warmed first; the build's output."""
    compose_pulled(compose_services(root, "--project-name", project))
    warmed(tmp_path_factory, *(build.dockerfile for build in compose_builds(root, project)))
    return built(["docker", "compose", "--progress", "plain", "--project-name", project, "build"], root)


def containers_gone(project: str, wait_s: float = 30) -> None:
    """`project`'s containers removed, and waited out: a `run --rm`'s removal goes on after compose
    returns, a killed compose's containers keep running, and either holds the project's network,
    which `down` then leaves behind (leaked, they used up Docker's address pools on the box)."""
    ps = ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
    deadline = time.monotonic() + wait_s
    while left := subprocess.run(ps, capture_output=True, text=True, check=False).stdout.split():
        if time.monotonic() > deadline:
            raise AssertionError(f"{project}: containers {left} still there after {wait_s:g} s")
        subprocess.run(["docker", "rm", "--force", *left], capture_output=True, check=False)
        time.sleep(0.5)


def run_compose(
    tmp_path_factory: pytest.TempPathFactory,
    root: Path,
    project: str,
    *args: str,
    run_s: float = COMPOSE_RUN_S,
) -> subprocess.CompletedProcess[str]:
    """`docker compose --project-name PROJECT *args` in `root`: `up` and `build` (unless
    `--no-build`) have the project built first (`compose_prebuilt`), which is all `build` does and
    whose images `up` then starts without building; any command that still builds fails; and every
    command is bounded by `run_s` (a project whose grader waits longer for its worker gives its
    own). Raises with its output when it fails."""
    if args and args[0] in COMPOSE_BUILDS and "--no-build" not in args:
        output = compose_prebuilt(tmp_path_factory, root, project)
        if args[0] == "build":
            return subprocess.CompletedProcess(["docker", "compose", *args], 0, output, "")
        args = (args[0], "--no-build", *(arg for arg in args[1:] if arg != "--build"))
    if args and args[0] == "down":
        containers_gone(project)
    argv = ["docker", "compose", "--progress", "plain", "--project-name", project, *args]
    command, tail = f"docker compose {' '.join(args)}", BUILD_OUTPUT_TAIL
    try:
        done = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=run_s, check=False)
    except subprocess.TimeoutExpired as late:
        out, err = (
            stream.decode(errors="replace") if isinstance(stream, bytes) else (stream or "")
            for stream in (late.stdout, late.stderr)
        )
        containers_gone(project)  # killing compose leaves them running
        raise AssertionError(f"{command}: over {run_s:g} s\n{out[-tail:]}\n{err[-tail:]}") from None
    output = done.stdout + "\n" + done.stderr  # its build prints on stdout
    builds = list(dict(COMPOSE_BUILT.findall(output)).values())  # each step once
    if builds:
        raise AssertionError(
            f"{command} built {len(builds)} images itself, unwatched (the tests' build of the project "
            "didn't come first):\n" + "\n".join(builds) + f"\n{done.stdout[-tail:]}\n{done.stderr[-tail:]}"
        )
    if done.returncode != 0:
        raise AssertionError(f"{command}: {done.returncode}\n{done.stdout[-tail:]}\n{done.stderr[-tail:]}")
    return done


@pytest.fixture(scope="session")
def docker_compose(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`docker_compose(root, project, *args, run_s=...)` (`run_compose`), for the projects' tests."""

    def run(
        root: Path, project: str, *args: str, run_s: float = COMPOSE_RUN_S
    ) -> subprocess.CompletedProcess[str]:
        return run_compose(tmp_path_factory, root, project, *args, run_s=run_s)

    return run


@pytest.fixture(scope="session")
def compose(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., str]:
    """`compose(root, project, *args, run_s=...)`: `docker_compose`'s output."""

    def run(root: Path, project: str, *args: str, run_s: float = COMPOSE_RUN_S) -> str:
        return run_compose(tmp_path_factory, root, project, *args, run_s=run_s).stdout

    return run


@pytest.fixture(scope="session")
def compose_built(tmp_path_factory: pytest.TempPathFactory) -> Callable[[Path, str], None]:
    """`compose_built(root, project)`, for a test whose project `arh run` builds from its compose
    config with `docker build`: each of its builds done first by `docker_build` (its watchdog and
    retries), so `arh run` finds them in the cache."""

    def prebuild(root: Path, project: str) -> None:
        builds = compose_builds(root, project)
        warmed(tmp_path_factory, *(build.dockerfile for build in builds))
        for build in builds:
            docker_build(*build.flags(), str(build.context))

    return prebuild


def dockerfile_of(args: tuple[str, ...], cwd: Path | None) -> Path:
    """The Dockerfile `docker build *args` (run in `cwd`) builds: its `-f`/`--file`, or the context's
    own; the context is the last argument."""
    base = cwd or Path.cwd()
    for index, arg in enumerate(args[:-1]):
        if arg in ("-f", "--file"):
            return base / args[index + 1]
        if arg.startswith("--file="):
            return base / arg.removeprefix("--file=")
    return base / args[-1] / "Dockerfile"


# CI's registry copies of the tests' images (PER-477): `build_image(..., keyed=True)` names an image
# by a digest of everything its build reads (`build_key`), which is the same in every run that has
# those inputs, so a run takes it from ARH_TEST_IMAGE_CACHE (a repository prefix) when it is there,
# instead of building it on its job's empty Docker daemon (a kit's took 3 to 12 minutes); a run
# with ARH_TEST_IMAGE_CACHE_PUSH (CI's) pushes what it built. Without ARH_TEST_IMAGE_CACHE, every
# image is built. The key is what BuildKit's own cache keys on: a RUN's network fetches aren't in
# it, as a warm daemon would reuse that layer too.
IMAGE_CACHE_PULL_TIMEOUT_S = 300
CONTEXT_DOCKERFILE = "FROM scratch\nCOPY . /\n"  # the context as Docker sends a build, as files


def tag_of(args: tuple[str, ...]) -> str:
    """The tag `docker build *args` gives its image (`-t`/`--tag`)."""
    for index, arg in enumerate(args[:-1]):
        if arg in ("-t", "--tag"):
            return args[index + 1]
        if arg.startswith("--tag="):
            return arg.removeprefix("--tag=")
    raise ValueError(f"no tag in docker build {' '.join(args)}")


def context_digest(context: Path, dockerfile: Path) -> bytes:
    """A digest of the build context `context` as Docker sends it for `dockerfile`: its
    .dockerignore (or the Dockerfile's own, `<dockerfile>.dockerignore`) applied by Docker itself,
    exported as files; each file's path, type, mode, link and bytes, not its times."""
    with tempfile.TemporaryDirectory() as scratch:
        exporter = Path(scratch) / "Dockerfile"
        exporter.write_text(CONTEXT_DOCKERFILE)
        own = dockerfile.with_name(dockerfile.name + ".dockerignore")
        if own.is_file():
            shutil.copyfile(own, exporter.with_name("Dockerfile.dockerignore"))
        exported = Path(scratch) / "context.tar"
        docker_build("--output", f"type=tar,dest={exported}", "--file", str(exporter), str(context))
        digest = hashlib.sha256()
        with tarfile.open(exported) as tar:
            for member in sorted(tar.getmembers(), key=lambda m: m.name):
                extracted = tar.extractfile(member) if member.isfile() else None
                data = extracted.read() if extracted else b""
                fields = [member.name, str(member.type), oct(member.mode), member.linkname]
                digest.update("\0".join(fields).encode() + b"\0" + hashlib.sha256(data).digest())
        return digest.digest()


def build_key(args: tuple[str, ...], cwd: Path | None) -> str | None:
    """A digest of everything `docker build *args` (in `cwd`) reads: its Dockerfile's text, the ID
    of every image it builds on (pulled first, as `docker_build` would), its context as Docker
    sends it (`context_digest`), every other argument but the tag (build args, target) and the
    tests' own build args (ARH_TEST_BUILD_ARGS). None if a base image is named by a build arg,
    which the key can't follow."""
    dockerfile = dockerfile_of(args, cwd)
    text = dockerfile.read_text()
    lines = [line.strip() for line in re.sub(r"\\\n[ \t]*", " ", text).splitlines()]
    if any("$" in line for line in lines if FROM_LINE.match(line) or COPY_FROM.match(line)):
        return None
    digest = hashlib.sha256(text.encode())
    for image in base_images(text):
        pulled(image)
        inspect = ["docker", "image", "inspect", "--format", "{{.Id}}", image]
        found = subprocess.run(inspect, check=True, capture_output=True).stdout
        digest.update(f"\0FROM {image} ".encode() + found)
    digest.update(context_digest((cwd or Path.cwd()) / args[-1], dockerfile))
    rest: list[str] = []
    skip = False
    for arg in args[:-1]:
        if skip:
            skip = False
        elif arg in ("-t", "--tag", "-f", "--file"):
            skip = True
        elif not arg.startswith(("--tag=", "--file=")):
            rest.append(arg)
    rest.append(os.environ.get("ARH_TEST_BUILD_ARGS", ""))
    digest.update(json.dumps(rest).encode())
    return digest.hexdigest()[:32]


def cached_name(tag: str, key: str) -> str:
    """The registry's copy of the image tagged `tag` built from inputs of digest `key`."""
    repository = tag.rpartition(":")[0] if ":" in tag.rpartition("/")[2] else tag
    return f"{os.environ['ARH_TEST_IMAGE_CACHE']}/{repository}:{key}"


def from_image_cache(tag: str, key: str) -> bool:
    """Is `tag` in the daemon now, taken from the registry's copy of `key`'s build?"""
    remote = cached_name(tag, key)
    try:
        pulled = subprocess.run(
            ["docker", "pull", "--quiet", remote],
            capture_output=True, text=True, timeout=IMAGE_CACHE_PULL_TIMEOUT_S, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        print(f"image cache: {remote}: no answer in {IMAGE_CACHE_PULL_TIMEOUT_S} s; building", flush=True)
        return False
    if pulled.returncode != 0:
        print(f"image cache: {remote}: {pulled.stderr.strip()[-300:]}; building", flush=True)
        return False
    subprocess.run(["docker", "tag", remote, tag], check=True, capture_output=True)
    print(f"image cache: {tag} from {remote}", flush=True)
    return True


def to_image_cache(tag: str, key: str) -> None:
    """Push the built `tag` as the registry's copy of `key`'s build, in a run that pushes
    (`ARH_TEST_IMAGE_CACHE_PUSH`); a failed push only leaves the next run to build it."""
    if not os.environ.get("ARH_TEST_IMAGE_CACHE_PUSH"):
        return
    remote = cached_name(tag, key)
    subprocess.run(["docker", "tag", tag, remote], check=True, capture_output=True)
    pushed = subprocess.run(
        ["docker", "push", "--quiet", remote], capture_output=True, text=True, check=False
    )
    print(
        f"image cache: {remote}: {'pushed' if pushed.returncode == 0 else pushed.stderr.strip()[-300:]}",
        flush=True,
    )


@pytest.fixture(scope="session")
def build_image(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., str]:
    """`docker_build(*args, cwd=...)`, for the tests' own image fixtures, the Dockerfile's pinned
    downloads warmed first (`warmed`). `keyed=True`, with ARH_TEST_IMAGE_CACHE: the registry's copy
    of a build of the same inputs (`build_key`) stands in for it (`from_image_cache`), and its
    output is then the copy's name."""

    def build(*args: str, cwd: Path | None = None, keyed: bool = False) -> str:
        tag = tag_of(args) if keyed else ""
        key = build_key(args, cwd) if keyed and os.environ.get("ARH_TEST_IMAGE_CACHE") else None
        if key and from_image_cache(tag, key):
            return f"image cache: {tag}"
        warmed(tmp_path_factory, dockerfile_of(args, cwd))
        output = docker_build(*args, cwd=cwd)
        if key:
            to_image_cache(tag, key)
        return output

    return build


@pytest.fixture(scope="session")
def compose_build() -> Callable[..., str]:
    """`docker compose --progress plain *args` (a `build`, its last `build` the subcommand), as
    `built` runs a build: its images pulled first (`compose_pulled`), and one stalled or failed on
    the network built again (tests/local's server, whose images Compose builds)."""

    def build(*args: str, cwd: Path | None = None) -> str:
        options = args[: len(args) - 1 - args[::-1].index("build")]
        compose_pulled(compose_services(cwd, *options))
        return built(["docker", "compose", "--progress", "plain", *args], cwd)

    return build
