"""The grader SDK: what a project's grader needs to measure a worker safely.

- `Client(url, output_schema=...)`: `run(items)` sends one batch, `evaluate(items, batch_size,
  warmup)` a whole evaluation; whatever the worker does becomes item failures, never an exception.
- `Timings`, `summary(values)`: per-batch and per-item times, median, p90 and CV.
- `seed()`, `rng(label)`: the run's seed (the platform's secret `AR_SEED`, else the public seed)
  and independent random streams derived from it.
- `run_deadline()`, `remaining()`: when the runner kills this run (`AR_RUN_DEADLINE`), and the
  seconds left until then, for bounding optional work after the primary result.
- `wait_ready(url)`: wait for the worker's `GET /healthz`, at most the project's
  `runner.startup_timeout_s` (`AR_STARTUP_TIMEOUT_S`; `startup_timeout()`).
- `begin_timed()`: the worker's startup as the platform charges it, from when the runner started
  it (`AR_WORKER_STARTED_AT`) to the first timed request; report it as `timings.startup_s`.
- `write_result(...)`: write `/result/result.json`, schema 1, atomically (and, locally, print
  METRIC/ASI lines), and the per-item scores, if given, to `items.json` beside it; its
  `failure_kinds` count the failures by kind (`Evaluation.failure_kinds()`, and the grader's own);
  `check_result(result)` and `check_items(items, result)` are the checks it and the platform share.
- `invalid_result(exc, ...)`: the result of a run that couldn't be evaluated, in a grader's
  `except Exception` around the whole run.
- `Oracle(answer, max_calls=...)`, `Session`: an endpoint the worker calls during the run
  (`AR_ORACLE_URL`, `http://grader:8081`) about a hidden instance, one session per item, whose
  calls the grader counts exactly; `Client.run`/`evaluate(..., sessions=...)` close them as each
  batch is answered (since release 5).
- `write_view(panels)`: in the platform's view run (`is_view_run()`: the public sample, `AR_VIEW`),
  write `/result/view.json`, what the project's pages show of a check: SVG, PNG/WebP and board
  replay panels (`svg_panel`, `image_panel`, `games_panel`); `check_view(view)` is the check it and
  the platform share (since release 7).
- `fetch(...)`, `Checker(schema)`: the deadline-bound request and the output schema check
  underneath.
- `arh_grader.head_to_head` (since release 8): a head-to-head version's referee side, the run's
  settings (`settings()`, AR_HEAD_TO_HEAD), a seat's bot with its move deadline (`Bot`), the run's
  game pairs (`play`), and the result with items.json schema 2 (`write_match`, `check_pairs`).

Standard library only, so it installs anywhere (`template/dockerfile.grader` vendors it).
`RELEASE` is this copy's release (sdk/grader/README.md: a project vendors one release, exactly).
"""

from arh_grader.client import Batch, Client, Evaluation
from arh_grader.deadline import DEADLINE_ENV, remaining, run_deadline
from arh_grader.fetch import FetchError, Response, fetch
from arh_grader.oracle import ORACLE_ENV, ORACLE_PORT, ORACLE_URL, Oracle, Session
from arh_grader.ready import STARTUP_ENV, WorkerNotReady, startup_timeout, wait_ready
from arh_grader.result import (
    ITEMS_FILE,
    RESULT_PATH,
    SCHEMA_VERSION,
    Value,
    check_items,
    check_result,
    invalid_result,
    metric_lines,
    write_result,
)
from arh_grader.schema import Checker
from arh_grader.seed import PUBLIC_SEED, SEED_ENV, is_local, rng, seed
from arh_grader.startup import STARTED_ENV, begin_timed, worker_started
from arh_grader.timing import Timings, cv, percentile, summary
from arh_grader.view import (
    MAX_VIEW_BYTES,
    VIEW_ENV,
    VIEW_FILE,
    VIEW_SCHEMA,
    check_view,
    encode_view,
    games_panel,
    image_panel,
    is_view_run,
    svg_panel,
    write_view,
)

RELEASE = 8

__all__ = [
    "DEADLINE_ENV",
    "ITEMS_FILE",
    "MAX_VIEW_BYTES",
    "ORACLE_ENV",
    "ORACLE_PORT",
    "ORACLE_URL",
    "PUBLIC_SEED",
    "RELEASE",
    "RESULT_PATH",
    "SCHEMA_VERSION",
    "SEED_ENV",
    "STARTED_ENV",
    "STARTUP_ENV",
    "VIEW_ENV",
    "VIEW_FILE",
    "VIEW_SCHEMA",
    "Batch",
    "Checker",
    "Client",
    "Evaluation",
    "FetchError",
    "Oracle",
    "Response",
    "Session",
    "Timings",
    "Value",
    "WorkerNotReady",
    "begin_timed",
    "check_items",
    "check_result",
    "check_view",
    "cv",
    "encode_view",
    "fetch",
    "games_panel",
    "image_panel",
    "invalid_result",
    "is_local",
    "is_view_run",
    "metric_lines",
    "percentile",
    "remaining",
    "rng",
    "run_deadline",
    "seed",
    "startup_timeout",
    "summary",
    "svg_panel",
    "wait_ready",
    "worker_started",
    "write_result",
    "write_view",
]
