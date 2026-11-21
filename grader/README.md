# commonhill-grader: the grader SDK

The Python package project authors use to write a grader. A grader measures the worker over the
evaluation items, treats everything the worker sends as hostile, and writes `/result/result.json`,
the only thing the platform reads. Standard library only (Python 3.12+).

```sh
uv add commonhill-grader          # in a uv project, from this repository: uv add ./grader
```

Each project template (commonhill/templates) vendors a copy in its `grader/commonhill_grader`, and so
does each project, so a grader image builds without network. Authors get the current release with
a template: `hill new` writes out a template the served CLI wheel carries. The guide
(`/api/guide.md` on the platform) documents the SDK, `items.json` included, so the guide changes
with the SDK.

### Releases

The SDK is released in numbered releases (`commonhill_grader.RELEASE`); a release is the repository's
tag `v<RELEASE>.0.0`. `grader/src` is the current one. An earlier one lives on in the vendored
copies that pin it, and in its digest, which `tools/vendor.py` keeps (`RELEASES`). A project's
vendored copy is a frozen file of its reward versions: changing it changes the project's reward
version (and, for a project without an upstream, its seeded base commit). The platform reads a
reward version's copy from its frozen files. So:

- **A change to the SDK is a new release.** First add the current release's digest to `RELEASES`
  in `tools/vendor.py`; then change `src` and add 1 to `RELEASE`. A release, once out, never
  changes. A release's digest is the sha256 of its files' `sha256sum` listing:
  `(cd grader/src/commonhill_grader && find . -type f -not -path '*/__pycache__/*' -printf '%P\n' | LC_ALL=C sort | xargs sha256sum) | sha256sum`.
- **A template or project moves to a newer release** with `python3 tools/vendor.py copy DIR`
  (the templates at once, a project in its next reward version).
- **`python3 tools/vendor.py check --release N [--kit KIT]... DIR`** says whether DIR's copy is
  exactly release N (the current one byte for byte, an earlier one by its digest) and DIR has
  exactly those kits, each the sdk's. What DIR should have is given, not read from DIR, so a
  deleted copy or kit fails too. Each template's and project's CI runs it with its own release
  and kits.

What each release added:

- **Release 2:** per-item scores, in `items.json` beside the result ("The result").
- **Release 3:** `wait_ready` says how long it waited when it gives up ("Talking to the worker").
- **Release 4:** the result counts the failures by kind (`failure_kinds`, "Failure kinds"), and
  `invalid_result` writes an invalid run's result in one call.
- **Release 5:** the oracle: an endpoint the worker calls and the grader counts (`Oracle`,
  `Session`, `sessions=` on `Client.run` and `Client.evaluate`, the failure kind `oracle_cap`;
  "The oracle").
- **Release 6:** `wait_ready`, giving up, names the last answer the worker gave, not the time-out
  of a last probe its deadline cut short ("Talking to the worker").
- **Release 7:** views: `write_view` and `check_view`, what a project's pages show of a check
  besides its numbers, written in the platform's view run (`is_view_run`; "Views").
- **Release 8:** head-to-head graders for two-player games (PER-448): `commonhill_grader.head_to_head`
  (the run's settings from `AR_HEAD_TO_HEAD`, `Bot` with its move deadline of `turn_ms` plus
  `TRANSPORT_ALLOWANCE_MS`, the run's game pairs, `write_match` with `items.json` schema 2,
  `SeatFailed`). The authors' guide, section 4, "Two-player games".
- **Release 9:** the grader SDK is release 8's; the cycles kit prices x87 (PER-492): x87
  arithmetic and conversions as their SSE2 twins (FDIV as DIVSD, FSQRT as SQRTSD, FILD as
  CVTSI2SD), the microcoded and BCD instructions at their cost measured on Zen 3, where the cost
  table's default had charged one cycle each (`kits/cycles/README.md`, "x87").
- **Release 10:** the package is `commonhill_grader` (distribution `commonhill-grader`), vendored
  as `grader/commonhill_grader`; releases before it are `arh_grader`, in `grader/arh_grader`
  (PER-500). `tools/vendor.py copy` moves a project over and deletes the old copy. A worker's
  response may be up to 64 MiB (`MAX_RESPONSE_BYTES`, was 1 MiB; the per-request time-out stays
  10 s), and must be UTF-8 JSON of at most 1 million values per item and 4 million in all
  (`MAX_VALUES_PER_OUTPUT`, `MAX_VALUES`), counted before it is decoded: 64 MiB of `[]` would
  decode to about 1.4 GB.
- **Release 11:** the platform's old name is gone from what a project vendors. Local runs use the
  public seed `commonhill-public-seed-v1`, and so do the platform's view runs (they draw a
  project's pages, not its scores); a measured run's `AR_SEED` replaces it, so scores don't move.
  The oracle's session tokens are keyed `commonhill-oracle-session` (the grader makes and checks
  them; the worker only echoes them). The kits' sources say Commonhill, and the instruction-count
  kit's guest sees the host name `commonhill` in `uname`. The tests' harness is
  `commonhill_testkit` (distribution `commonhill-testkit`), its variables `COMMONHILL_*`.

## A grader in brief

```python
import os
from commonhill_grader import Client, Value, rng, wait_ready, write_result

worker_url = os.environ["WORKER_URL"]  # http://worker:8080 in compose.yaml
# The public sample locally; on the platform the held-out set, or an open project's checked file.
items = load_my_items("/eval/items.jsonl")
rng("order").shuffle(items)  # order from the run's seed
wait_ready(worker_url)  # runner.startup_timeout_s, from the platform (AR_STARTUP_TIMEOUT_S)
client = Client(worker_url, output_schema={"type": "string"})
evaluation = client.evaluate([i["input"] for i in items], batch_size=100, warmup=1)
wrong = sum(o is not None and o != i["expected"] for o, i in zip(evaluation.outputs, items))
failures = evaluation.failures + wrong
timings = evaluation.timings.as_result()
write_result(
    primary=Value("wall_ms", timings["total_ms"] if failures == 0 else None),  # null: invalid run
    n_items=len(items),
    failures=failures,
    timings={**timings, "startup_s": evaluation.startup_s},  # see "Startup" below
    grader_version="my-grader-1",
)
```

Wrap the whole run in `try/except Exception`, and write an invalid result in the handler, printing
only the exception's type: a grader must end with a result, and must never print what the worker
sent or the evaluation data. The examples below do exactly that.

This example times the worker to show the SDK's timings; a project's metric is never a time on the
platform: count the work instead (the authors' guide, section 3).

## Talking to the worker

`Client(url, timeout_s=10, max_bytes=64 MiB, max_values=4 million, output_schema=None)` speaks the
worker contract (commonhill/templates, `count/CONTRACT.md`).

- **`client.run(items)`** sends one batch (`POST /run`) and returns a `Batch`: `outputs` (one per
  item, `None` where the item failed), `seconds` (measured by the grader) and `error` (why the
  whole batch failed, or `None`).
- **`client.evaluate(items, batch_size, warmup=0)`** sends `warmup` unrecorded batches, then every
  item once, and returns an `Evaluation`: `outputs` (in the items' order), `failures`, `timings`
  and `errors` (failed batches by kind).

Nothing the worker does raises. **A whole batch fails** when the response is:

- late (an absolute deadline over connecting, headers and body);
- larger than `max_bytes`, or holding more JSON values than 1 million per item or `max_values`;
- malformed HTTP, or cut off;
- not exactly `200` (redirects are never followed);
- not strict UTF-8 JSON (`NaN`, `Infinity` and numbers that overflow a float, like `1e999`, are refused
  anywhere in the body), or nested too deeply;
- not `{"outputs": [...]}` with one output per item.

**A single item fails** when its output is `null`, doesn't match `output_schema`, or checking it
raises for any reason. `Batch.item_errors` and `Evaluation.item_errors` count failed items by kind
(`null`, `schema`, or the exception's type).

**`output_schema`** is a JSON Schema subset: `type`, `enum`, `const`, `minimum`, `maximum`,
`exclusiveMinimum`, `exclusiveMaximum`, `minLength`, `maxLength`, `items`, `minItems`,
`maxItems`, `properties`, `required`, `additionalProperties`.

- The schema is checked when the Client is made: other keywords, or a keyword with a value of the
  wrong type (`"minimum": "0"`, `"enum": "cat"`), are a `ValueError` naming the path.
- Numbers are equal when mathematically equal (`1` and `1.0`, never `true`), and integers stay
  exact at any size, also against bounds.
- Checking costs time linear in the output's size. `enum` and `const` are compiled once (options
  that fail the rest of their schema are dropped) into a set of canonical encodings, and a node
  with one is decided by a single lookup without descending into it. So a large enum, or enums
  nested at every level, cost one encoding of the output, and the response itself is capped at
  `max_bytes`.

Describe outputs tightly: a schema is also what keeps a hostile output from being something your
comparison can't handle.

**`wait_ready(url)`** polls `GET /healthz` until it answers exactly `200`, within the project's
`runner.startup_timeout_s` in total, else raises `WorkerNotReady`.

- The runner (and `hill run`, plain or `--pairs`) gives the grader that budget as
  `AR_STARTUP_TIMEOUT_S` (`startup_timeout()`); without it the SDK waits
  `DEFAULT_STARTUP_TIMEOUT_S`, 60 s. Don't keep a copy of the budget in the grader: a `timeout_s`
  argument is for tests.
- Since release 3, when it gives up it prints one line on stderr,
  `commonhill_grader: worker not ready after 301.204 s (limit 300 s)`: the seconds it waited by the
  grader's own clock, and the limit. `hill run` quotes it for an invalid run, and the exception has
  both too (`waited_s`, `limit_s`).

## Timing

The Client times each request with a monotonic clock, from sending to reading the whole response.

- `Timings` holds the measured batches; `timings.as_result()` gives `total_ms`, `batches`,
  `batch_median_ms`, `batch_p90_ms`, `batch_cv`, `item_median_ms` and `item_p90_ms` for the result.
- `summary(values)` gives n, total, mean, median, p90, min, max and the coefficient of variation
  (`cv`: sample standard deviation / mean) of any list.

Per-item times are batch time divided by its items: a batched request can't resolve finer. HTTP and
JSON overheads are in every measurement, and cancel out when the platform compares the incumbent
and a candidate in pairs on the same items. Use warmup batches, so that caches and lazy
initialisation don't land in the first measured batch.

### Startup

`begin_timed()` returns the seconds from the run's start until now. The run's start is
`AR_WORKER_STARTED_AT`: unix seconds on the eval VM's clock, in the grader's environment on the
platform and under `hill run --pairs`. Without it, it counts from when the grader imported the SDK.

The platform charges the worker's startup from when its process started: it takes off the seconds
before that, which it reads from Docker (Compose creating the containers is its time, not the
worker's).

- Call it right before the first timed request, after `wait_ready` and any warmups, and report it
  as `timings.startup_s`. `Client.evaluate` calls it after its warmup batches
  (`Evaluation.startup_s`).
- The startup the platform measures (`[runner.startup]`'s cap warns when a candidate's grows) then
  includes work a worker puts off to its first, untimed, requests, not only what it does before
  `/healthz` answers.
- Call it exactly there, and keep the scored phase continuous: unscored work after
  `begin_timed()` goes uncharged, and that is the grader author's error, which the platform
  doesn't defend against.
- A grader that never calls it has no startup to report: `write_result` leaves `timings.startup_s`
  out (`commonhill_grader.startup.began()`), and the startup cap's warning has nothing to compare.

## Seeds

`seed()` is the platform's secret `AR_SEED` on the platform, and a fixed public seed locally
(`AR_SEED` unset or empty). Don't use it directly: `rng(label)` gives a `random.Random` for one
purpose (`rng("order")`, `rng("inputs")`), derived with HMAC-SHA256 from the seed, so streams are
independent, and knowing one reveals neither the seed nor the others. Never print the seed or put
it in the result. The SDK doesn't, and an `rng`'s repr and state don't contain it.

## The run's deadline

The runner kills a run `runner.run_timeout_s` after it starts, or sooner when the evaluation's own
time runs out first. It tells the grader when, in `AR_RUN_DEADLINE`: unix seconds on the eval VM's
clock, which the grader's container shares. `run_deadline()` returns it, and `remaining()` gives
the seconds left (never negative). Both are `None` when the variable is unset.

Work that is optional after the primary result, like a best-effort pass on the public sample,
should stop well before the deadline. Leave a margin for writing the result: a run that is killed
has no result at all.

```python
left = remaining()
budget = min(PUBLIC_CAP_S, left - MARGIN_S) if left is not None else PUBLIC_CAP_S
```

Where it is set: on the platform, for every run, warmups included, from the run's start. Locally,
`hill run --pairs` sets it to the limit it gives each run. Plain `hill run` and the README's compose
commands leave it unset, so a grader needs a fallback for `None`.

## The result

`write_result(primary=Value(name, value), n_items, failures, grader_version, secondary=None,
public=None, timings=None, item_scores=None, failure_kinds=None)` writes `result.json` schema 1
atomically, after checking every field: finite numbers, non-negative counts,
`failures <= n_items`, valid Unicode, at most 1 MiB. A `primary` value of `None` marks the run
invalid. Which runs are invalid is the author's call: the accuracy example makes a run with more than
5% failed items invalid, and the oracle example one with any wrong number.

`secondary` takes side metrics (time, CPU, memory beside a count, say): a local run prints them,
and the platform neither scores nor keeps them.

**`item_scores`** (release 2, optional) are each item's score, one per item, whose mean must be the
primary value: an accuracy's 1 or 0 per item, a Brier score's per-item squared error. They are
written first, to `items.json` beside the result (`{"schema": 1, "item_scores": [...]}`, at most
1 MiB).

- A held-out project's deterministic metric needs them: its MMI floor is twice the held-out set's
  sampling error, a bootstrap over them (the platform's noise profile). Without
  them its profile fails. An open project's needs none: its items are the target, not a sample.
- Write them on every run: only a noise profile's runs read the file, so result.json stays small
  however many items there are (a verification reads at most 64 KiB of it).
- On a held-out project they are held-out data: the platform reduces them to their standard error
  and keeps nothing else, they are never printed, and `check_items(items, result)`, the check the
  platform reads them with, says what is wrong without a score, a count or a value.

### Failure kinds

`failure_kinds` (release 4, optional) counts the failed items by kind:
`{"timeout": 100, "wrong": 3}`. The counts sum to `failures`; there are at most 32 kinds, each 1
to 40 of `a-z`, `0-9`, `_`, `.`, `-`; and a count of 0 is allowed (a run-level reason when no item
was evaluated). Without it, the platform and `hill run` know only the count. The SDK's kinds:

- a failed batch's items, by its cause (`Batch.kind`): `timeout`, `too_large` (bytes or JSON
  values), `connection` (refused, dropped, malformed HTTP, cut short), `http_<status>`, `not_json`, `bad_shape` (not
  `{"outputs": [...]}`), `wrong_count` (another number of outputs than items);
- an item's own problem: `null`, `schema`, `check_error` (the check raised);
- a run that couldn't be evaluated (`invalid_result`): `worker_not_ready`, `grader_error`, or the
  kind the grader gives (`invalid_set` for an evaluation set that isn't what it should be, say).

`Evaluation.failure_kinds()` counts the first two, summing to `evaluation.failures`. A grader adds
its own, like `wrong` for an answer that isn't the expected one, and passes them as
`write_result(..., failure_kinds=kinds)`:

```python
kinds = {**evaluation.failure_kinds(), **({"wrong": wrong} if wrong else {})}
```

They are names and counts only, never anything the worker sent or the evaluation data. `hill run`
lists them for an invalid local run. On the platform, a held-out project's are the held-out set's
failures, which only the project's author console shows, like the failure count itself. An open
project's readiness report shows its check runs' failures as it shows the sample's.

**`invalid_result(exc, metric=..., n_items=..., grader_version=..., kind=None)`** is the
`except Exception` block every grader needs: it prints `run invalid: <the exception's type>`,
never the exception's text, and writes the result with the primary value null and all `n_items`
failed under one kind.

**`check_result(result)`** is the result's check on its own, for a parsed `result.json`: it returns
the normalized schema-1 fields, or raises `ValueError`. `n_items` and `failures` go up to
`MAX_COUNT` (2^31 - 1). The platform reads every run's result with it, so a result written any
other way must pass it too (extra top-level keys are kept but ignored).

**Local runs print omp autoresearch's lines.** In a local run (`AR_SEED` unset or empty, the same
condition under which `seed()` gives the public seed), `write_result` also prints
`METRIC <primary>=<v>` (when valid), `METRIC <secondary>=<v>`, `METRIC public.<name>=<v>`, and
`ASI valid=…`, `ASI n_items=…`, `ASI failures=…`, `ASI failures.<kind>=…`,
`ASI grader_version=…`, `ASI timings.<name>=…`. On the platform nothing is printed; stdout never
counts for scoring.

## Views

Since release 7, a grader can give its project's pages something to look at besides the numbers:
a drawing of the packing, the games a candidate played. A project with views (a `[view]` table in
its spec) gets one more run after each check's measured runs, the **view run**:

- the candidate as the worker (for head-to-head, the incumbent as its opponent, as in the check);
- the **public sample** at `/eval`, never the held-out set, and `AR_SEED` empty, so `seed()` is the
  public seed: the same run as an agent's local one;
- `AR_VIEW=1` (`is_view_run()`); only `/result/view.json` is read. Its result and logs are ignored:
  it runs after the decision and can't change it. A view run that fails or writes an invalid view
  is recorded, in words, and changes nothing else.

**A view shows the public sample only.** Anyone who can see an attempt can see its view, and a
measured run's instances are the held-out set: drawn, they would leak it. So write a view only in
the view run (or locally), from what the run evaluated there, never from a measured run. Since it
is the sample and the public seed, a deterministic project's view on the platform is the one its
agent sees locally, and two attempts' views are comparable: the attempt page shows the candidate
beside the incumbent it faced.

`write_view(panels)` writes `{"schema": 1, "panels": [...]}` to `/result/view.json` atomically,
after `check_view`, the check the platform reads it with (`ValueError`, saying what is wrong, and
nothing written). The web draws the panels itself; no code of the project's runs in a browser.

- **`svg_panel(svg, caption=None)`**: an SVG document. It is checked by refusal, never rewritten:
  well-formed XML with no DOCTYPE, entity or processing instruction, `<svg xmlns="http://www.w3.org/2000/svg">`
  at the root, at most 20,000 elements nested at most 64 deep, and only drawing: `svg g defs title desc
  path circle ellipse rect line polyline polygon text tspan linearGradient radialGradient stop
  clipPath use` with geometry and presentation attributes (`fill`, `stroke`, `font-size`, ...;
  `commonhill_grader.view.SVG_ATTRIBUTES`). No `style` (attribute or element), scripts, event handlers,
  `<image>`, `<a>`, `<foreignObject>` or animation; `href` and `url(...)` only to an `#id` inside it.
- **`image_panel(data, media, caption=None)`**: a PNG (`"image/png"`) or WebP (`"image/webp"`)
  image's bytes, at most 4096 pixels a side.
- **`games_panel(games, caption=None)`**: board replays (up to 64), for games like ultimate
  tic-tac-toe and Battlesnake. A game is a grid of up to 32 x 32 cells (x right, y down), 1 to 4
  players (the platform picks their colours), up to 32 kinds of piece (`cross`, `ring`, `dot`,
  `square`, `shade`, or `text` with a 1 or 2 character label, each optionally a player's),
  optional regions (UTTT's nine small boards), and up to 2,000 frames. A frame's `clear`, `set`
  and `own` (a region won) change the board from the frame before, at most 50,000 changes a game
  (`MAX_GAME_CHANGES`); its `paths` (snakes, head first, at most one a player), `focus`
  (highlighted regions, each once) and `note` are its own. `commonhill_grader.view`'s docstring has
  the format; `grader/tests/data/` has a UTTT game and a Battlesnake game.

A caption is 1 to 200 characters. There are 1 to 64 panels, and the whole view, as `encode_view`
writes it (compact UTF-8 JSON), is at most 512 KiB (`MAX_VIEW_BYTES`).

```python
from commonhill_grader import is_local, is_view_run, svg_panel, write_view


def packing_svg(width: float, height: float, circles: list[tuple[float, float, float]]) -> str:
    shapes = "".join(f'<circle cx="{x:.4f}" cy="{y:.4f}" r="{r:.4f}"/>' for x, y, r in circles)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="none" stroke="black" stroke-width="0.01"/>'
        f'<g fill="steelblue" fill-opacity="0.6">{shapes}</g></svg>'
    )


# after write_result, on the public sample only
if is_view_run() or is_local():
    write_view(
        svg_panel(packing_svg(w, h, circles), caption=f"n = {len(circles)}, {w} x {h}")
        for w, h, circles in packings  # what the worker answered for each sample instance
    )
```

## The oracle

Since release 5, a grader can serve the worker an oracle: an endpoint that answers questions
about a hidden instance during the run and counts every call. The calls become a metric the
worker can't report away (query complexity: fewest probes, tests, backend hits). The protocol the
worker speaks is in commonhill/templates' `count/CONTRACT.md` ("The oracle");
commonhill/commonhill's `docs/adr/0014-oracle.md` has why.

```python
with Oracle(answer, max_calls=64) as oracle:                   # serves 0.0.0.0:8081 in a thread
    sessions = [oracle.session(instance, key=i) for i, instance in enumerate(instances)]
    items = [{"oracle": s.token, ...} for s in sessions]
    evaluation = client.evaluate(items, batch_size, sessions=sessions)
calls = [s.calls for s in sessions]
```

- **`Oracle(answer, *, max_calls, max_query_bytes=64 KiB, max_answer_bytes=64 KiB, port=8081,
  max_connections=32, idle_timeout_s=30)`** is a context manager: it listens while entered and
  closes every session on exit.
  - `answer(instance, query)` is yours. Make it a pure function. Raise `ValueError` for an
    invalid query: that is answered 400, and counted.
  - Any other exception, an answer that isn't JSON, or one over `max_answer_bytes` is a bug in
    the grader. It is answered the same way, counted as a call, and also counted in
    `oracle.handler_errors`. Its text is never printed.
- **`oracle.session(instance, *, key, max_calls=None)`** opens a session named by `key`'s token:
  HMAC-SHA256 of the key under the run's secret seed. Give each item's index or id, unique in the
  run (a key used twice is a `ValueError`): every run of a seed then gives each item the same
  token, whatever order the sessions are opened in, so a deterministic worker that reads them
  stays deterministic, and no token tells a worker anything about another without the seed. Send
  the token with the item. The session holds `calls`, `query_bytes` and `answer_bytes`
  (canonical JSON: compact, keys sorted), `over_cap` and `closed`. Its repr never shows the
  instance.
- **`Client.run(items, sessions=...)` and `Client.evaluate(items, batch_size, sessions=...)`**
  take one session per item, and no warmup.
  - A batch's sessions are closed as soon as its response is read, or as soon as the request
    failed. Closing never waits for an answer still being computed: that call counted when it
    came, and its answer is dropped. Read the counts after that.
  - A call that arrives later gets 410 and doesn't count.
  - An item whose session went over its cap fails as `oracle_cap`, whatever its output.
- **What counts:** a well-formed request naming an open session under its cap, counted under the
  session's lock before `answer` runs. Malformed or oversized bodies, unknown sessions (404),
  closed ones (410) and calls over the cap (429) don't count.
- **Determinism:** the count is exact whatever runs at once. A worker that waits for its answers
  gets the same count on every run, given the same instances.
- **Held-out safety:** the oracle prints nothing, and its error bodies are fixed. Report counts
  only.
- **Intake:** `compose.yaml` must give the worker exactly `AR_ORACLE_URL=http://grader:8081`
  (the platform checks it when it takes the project in).

## Examples

Both run against a worker at `WORKER_URL`, and come with one (`python worker.py 8080`):

- **`examples/accuracy`:** the worker labels texts positive or negative. The grader keeps the
  labels (`labels.jsonl`, a 20-line sample), sends only the texts, and reports `accuracy` (invalid
  if more than 5% of items failed outright).
- **`examples/oracle`:** the worker finds 50 hidden numbers by binary search over the grader's
  oracle, which it reaches at `AR_ORACLE_URL`. The grader reports `probes`, the mean calls an
  item, as its oracle counted them. A wrong number, or an item over its 64 calls, makes the run
  invalid. Run it with `AR_ORACLE_URL=http://127.0.0.1:8081 python worker.py 8080 &`.

```sh
cd grader/examples/accuracy
python worker.py 8080 &
WORKER_URL=http://127.0.0.1:8080 uv run python grade.py labels.jsonl /tmp/result.json
```
