"""Waiting for the worker to be ready (CONTRACT.md: `GET /healthz` answers 200 when ready).

How long to wait is the project's `runner.startup_timeout_s` (autoresearch.toml), which the
platform and `arh run` (plain or `--pairs`) give the grader as `AR_STARTUP_TIMEOUT_S`; graders
don't keep a copy of it. Without it (the grader run by hand, `docker compose up`) the wait is
DEFAULT_STARTUP_TIMEOUT_S, sized for a loaded machine.

When the worker isn't ready in time (release 3), `wait_ready` prints how long it waited and the
limit on stderr, one line (`NOT_READY_LINE`), before it raises: `arh run` reads it to say why the
run is invalid, whatever the grader then prints.
"""

import math
import os
import sys
import time

from arh_grader.fetch import FetchError, fetch

STARTUP_ENV = "AR_STARTUP_TIMEOUT_S"
DEFAULT_STARTUP_TIMEOUT_S = 60.0
# One probe gets at most this long (and never past the overall deadline).
PROBE_TIMEOUT_S = 2.0
# A /healthz body larger than this makes the probe fail; the body itself doesn't matter.
MAX_HEALTH_BYTES = 4096
# What `wait_ready` prints when it gives up: the seconds it waited, and the limit.
NOT_READY_LINE = "arh_grader: worker not ready after {waited:.3f} s (limit {limit:g} s)"


class WorkerNotReady(TimeoutError):
    """The worker wasn't ready within `limit_s`: `waited_s` is how long the grader waited."""

    def __init__(self, message: str, waited_s: float, limit_s: float) -> None:
        super().__init__(message)
        self.waited_s = waited_s
        self.limit_s = limit_s


def startup_timeout() -> float:
    """How long the worker may take to get ready: `AR_STARTUP_TIMEOUT_S`, or
    DEFAULT_STARTUP_TIMEOUT_S when it is unset or empty. A value that isn't a positive finite
    number is the runner's bug: ValueError."""
    raw = os.environ.get(STARTUP_ENV)
    if not raw:
        return DEFAULT_STARTUP_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{STARTUP_ENV} is not a positive number of seconds: {raw!r}")
    return value


def wait_ready(url: str, timeout_s: float | None = None, interval_s: float = 0.2) -> float:
    """Poll `url`/healthz until it answers exactly 200, within `timeout_s` in total (headers and
    body included; by default the project's, `startup_timeout()`); returns the seconds it took.
    Failed probes (refused, garbage, timeouts, other statuses, redirects) are retried until the
    deadline; then it prints `NOT_READY_LINE` on stderr, and WorkerNotReady says how long it
    waited, the limit and what the last probe got (never the worker's content). A last probe the
    deadline cut short that timed out (release 6) doesn't replace the answer one before it got: it
    had less than PROBE_TIMEOUT_S to answer in. `url` is the worker's base URL, e.g.
    `http://worker:8080` (the `WORKER_URL` the template passes the grader)."""
    if timeout_s is None:
        timeout_s = startup_timeout()
    started = time.monotonic()
    deadline = started + timeout_s
    health = url.rstrip("/") + "/healthz"
    last = "no answer yet"
    while True:
        now = time.monotonic()
        if now >= deadline:
            waited = now - started
            print(NOT_READY_LINE.format(waited=waited, limit=timeout_s), file=sys.stderr, flush=True)
            raise WorkerNotReady(
                f"{health} not ready after {waited:.3f} s (limit {timeout_s:g} s): {last}", waited, timeout_s
            )
        probe_deadline = min(now + PROBE_TIMEOUT_S, deadline)
        try:
            response = fetch(health, deadline=probe_deadline, max_bytes=MAX_HEALTH_BYTES)
        except FetchError as exc:
            cut_short = exc.kind == "timeout" and probe_deadline < now + PROBE_TIMEOUT_S
            if not cut_short or last == "no answer yet":
                last = str(exc)
        else:
            if response.status == 200:
                return time.monotonic() - started
            last = f"HTTP {response.status}"
        time.sleep(max(0.0, min(interval_s, deadline - time.monotonic())))
