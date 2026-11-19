"""The worker's startup as the platform charges it: from when the runner started the run, right
before it started the worker's container (`AR_WORKER_STARTED_AT`, unix seconds on the eval VM's
clock, which the grader's container shares), to the grader's first timed request.

A grader calls `begin_timed()` right before that request, after waiting for the worker and after
any warmups, and reports what it returns as `timings.startup_s`; the scored phase then runs
without a break (unscored work after it would go uncharged: the grader author's error, not
something the platform defends against). Readiness and warmups both count: work a worker puts
off until its first, untimed, requests is charged like work at startup. `AR_WORKER_STARTED_AT` is
in the grader's environment on the platform and under `commonhill run --pairs`; without it (plain `commonhill
run`, or the grader run by hand) the time counts from when the grader imported this SDK.

A grader that never calls `begin_timed()` has no startup to report: `write_result` leaves
`timings.startup_s` out (`began()`), so a reward version that folds startup in fails the run
rather than charging it nothing.
"""

import math
import os
import time

STARTED_ENV = "AR_WORKER_STARTED_AT"
_IMPORTED = time.time()
_began = False


def worker_started() -> float | None:
    """When the runner started the worker, in unix seconds, or None when `AR_WORKER_STARTED_AT`
    is unset or empty. A value that isn't a finite number is the runner's bug: ValueError."""
    raw = os.environ.get(STARTED_ENV)
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{STARTED_ENV} is not a finite number of seconds: {raw!r}")
    return value


def begin_timed() -> float:
    """The seconds from the worker's start (`worker_started()`, else the SDK's import) until now,
    never negative: call it right before the first timed request and report it as
    `timings.startup_s`."""
    global _began
    started = worker_started()
    _began = True
    return max(0.0, time.time() - (_IMPORTED if started is None else started))


def began() -> bool:
    """Whether this grader called `begin_timed()`: only then does it have a startup to report."""
    return _began
