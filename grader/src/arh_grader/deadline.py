"""The run's deadline. The runner kills a run `runner.run_timeout_s` after it starts (or sooner,
when its job's own time runs out first), and tells the grader when: `AR_RUN_DEADLINE`, in unix
seconds, is in the grader's environment on the platform and under `arh run --pairs`, not plain
`arh run`. A grader that does optional work after its primary result (a best-effort pass on the
public sample, say) bounds it with `remaining()` instead of a copy of the project's
`run_timeout_s`.

The deadline is the wall clock's, the one the grader's container shares with the runner, so
`remaining()` compares it with `time.time()`; measure durations inside the run with
`time.monotonic()` as usual. Without `AR_RUN_DEADLINE` (plain `arh run`, or the grader run by
hand) both are None.
"""

import math
import os
import time

DEADLINE_ENV = "AR_RUN_DEADLINE"


def run_deadline() -> float | None:
    """The run's deadline in unix seconds, or None when `AR_RUN_DEADLINE` is unset or empty.
    A value that isn't a finite number is the runner's bug: ValueError."""
    raw = os.environ.get(DEADLINE_ENV)
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{DEADLINE_ENV} is not a finite number of seconds: {raw!r}")
    return value


def remaining() -> float | None:
    """Seconds left until the run's deadline (0 once it has passed), or None without one."""
    deadline = run_deadline()
    return None if deadline is None else max(0.0, deadline - time.time())
