"""`/result/result.json`, schema 1: the only thing the platform reads from a run (never stdout).

    {
      "schema": 1,
      "primary": {"name": "wall_ms", "value": 812.4},     value null: the run is invalid
      "secondary": {"p90_batch_ms": 9.1},                  other metrics, reported not scored
      "public": {"name": "wall_ms", "value": 798.0},       the public sample's score, or null
      "n_items": 1000,
      "failures": 0,                                       items the worker got wrong or lost
      "failure_kinds": {},                                 optional: the failures by kind
      "timings": {"total_ms": 812.4},
      "grader_version": "example-1"
    }

`failure_kinds` (since the SDK's release 4) counts the failed items by kind: at most
`MAX_FAILURE_KINDS` kinds, each named in `FAILURE_KIND` (lowercase letters, digits, `_`, `.`,
`-`; at most 40), each count an integer >= 0, the counts summing to `failures`. A kind with no
items (0) says why a run with none evaluated is invalid. Absent: the grader doesn't say (an older
release). Only names and counts, never anything the worker sent or the evaluation data: on the
platform they are the held-out set's failures, which only the project's author console shows.
The SDK's kinds: a failed batch's items by its cause and a single item's problem (`commonhill_grader.client`),
a run that couldn't be evaluated (`invalid_result`: "worker_not_ready", "grader_error"); a grader
adds its own ("wrong" for an answer that isn't the expected one, say).

`check_result` is the one definition of a valid result: `write_result` checks what it is about to
write with it, so a grader bug fails the grader, not the platform's parser, and the platform
checks what it reads with it (any other top-level keys are kept but carry no meaning).
`write_result` it writes atomically (a temporary file renamed over the result), so a grader
killed halfway leaves no half-written result. `n_items` and `failures` (so each failure kind's
count too) are integers from 0 to `MAX_COUNT` (2^31 - 1, since release 4).

Per-item scores (since the SDK's release 2) go in a file of their own beside it,
`/result/items.json` (`{"schema": 1, "item_scores": [1, 0, 1, ...]}`, one per item, up to
`MAX_ITEMS_BYTES`): each held-out item's contribution to the primary value, which must be their
mean (`check_items`). A deterministic metric's noise profile bootstraps over them for its MMI floor
(the platform's `arh_core.noise`). A grader writes them on every run, the same whatever the run is
for: only a noise profile's runs read them (the platform's runner, `items_cap_bytes`), so a large
held-out set's scores never weigh on result.json, which a verification caps at 64 KiB. They are
held-out data: the platform keeps only their standard error, never the scores; they are never
printed, and `check_items` says what is wrong with them without any score, count or value.

In a local run (no platform seed) it also prints the result as omp autoresearch's `METRIC
name=value` and `ASI key=value` lines (`metric_lines`). The platform ignores stdout for scoring.
"""

import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from commonhill_grader.ready import WorkerNotReady
from commonhill_grader.seed import is_local
from commonhill_grader.startup import began

SCHEMA_VERSION = 1
FAILURE_KINDS = "failure_kinds"
FAILURE_KIND = re.compile(r"[a-z0-9_.-]{1,40}")
MAX_FAILURE_KINDS = 32
RESULT_PATH = Path("/result/result.json")
# The runner reads at most this much of result.json (arh_runner.protocol.MAX_RESULT_BYTES).
MAX_RESULT_BYTES = 1024 * 1024
ITEMS_FILE = "items.json"  # beside result.json
# The most a noise profile's runs read of items.json (arh_runner.protocol.MAX_ITEMS_BYTES).
MAX_ITEMS_BYTES = 1024 * 1024
# The most any count in a result may be: the platform keeps them as 32-bit integers
# (verification_runs.failures), so a larger one is an invalid result, not a failed ingest.
MAX_COUNT = 2**31 - 1


@dataclass(frozen=True)
class Value:
    """A named metric value; `value` None means it couldn't be measured validly."""

    name: str
    value: float | None


def _name(what: str, name: object) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be a non-empty string, got {name!r}")
    try:
        name.encode("utf-8")  # lone surrogates can't be: the runner couldn't encode the result
    except UnicodeEncodeError:
        raise ValueError(f"{what} must be valid Unicode text (no lone surrogates)") from None
    return name


def _short(value: object) -> str:
    text = repr(value) if not isinstance(value, int) or value.bit_length() < 64 else "a huge integer"
    return text if len(text) <= 80 else text[:77] + "..."


def _number(what: str, value: object, minimum: float | None = None) -> float:
    """`value` as a float: a JSON number (not a boolean) that a float holds finitely."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{what} must be a finite number, got {_short(value)}")
    try:
        number = float(value)  # an int too large for a float raises OverflowError
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number, got {_short(value)}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{what} must be at least {minimum}, got {_short(value)}")
    return number


def _count(what: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be an integer >= 0, got {_short(value)}")
    if value > MAX_COUNT:
        raise ValueError(f"{what} is over {MAX_COUNT}")
    return value


def _metric(what: str, metric: object) -> dict[str, object]:
    if not isinstance(metric, dict):
        raise ValueError(f"{what} must be an object with a name and a value, got {_short(metric)}")
    fields = cast(dict[object, object], metric)
    if set(fields) != {"name", "value"}:
        raise ValueError(f"{what} must have exactly the keys name and value, got {sorted(map(str, fields))}")
    value = fields["value"]
    return {
        "name": _name(f"{what}.name", fields["name"]),
        "value": None if value is None else _number(f"{what}.value", value),
    }


def _numbers(what: str, numbers: object, minimum: float | None = None) -> dict[str, float]:
    if not isinstance(numbers, dict):
        raise ValueError(f"{what} must be an object of numbers, got {_short(numbers)}")
    items = cast(dict[object, object], numbers).items()
    return {_name(f"{what} key", k): _number(f"{what}[{k!r}]", v, minimum) for k, v in items}


RESULT_KEYS = ("schema", "primary", "secondary", "public", "n_items", "failures", "timings", "grader_version")
ITEM_SCORES = "item_scores"


def check_items(items: object, result: Mapping[str, object]) -> list[float]:
    """The per-item scores of `items` (a parsed items.json) for `result` (as `check_result`
    returns it): one finite number per item, averaging to the primary value. ValueError, and only
    ValueError, saying what is wrong in words only: never a score, a count or a value (they are
    held-out data)."""
    if not isinstance(items, dict) or set(cast(dict[object, object], items)) != {"schema", ITEM_SCORES}:
        raise ValueError(f'{ITEMS_FILE} must be an object {{"schema": 1, "item_scores": [...]}}')
    fields = cast(dict[str, object], items)
    if type(fields["schema"]) is not int or fields["schema"] != SCHEMA_VERSION:
        raise ValueError(f"{ITEMS_FILE}'s schema must be {SCHEMA_VERSION}")
    scores = fields[ITEM_SCORES]
    if not isinstance(scores, list):
        raise ValueError("item_scores must be a list of numbers")
    checked: list[float] = []
    for score in cast(list[object], scores):
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ValueError("an item score isn't a number")
        try:
            number = float(score)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number):
            raise ValueError("an item score isn't a finite number")
        checked.append(number)
    if len(checked) != result["n_items"]:
        raise ValueError("item_scores don't have one score per item (n_items)")
    value = cast(dict[str, object], result["primary"])["value"]
    if value is None:
        raise ValueError("the primary value is null: a run without a value has no item scores")
    if checked:
        mean = math.fsum(checked) / len(checked)
        if not math.isclose(mean, cast(float, value), rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("item_scores don't average to the primary value")
    return checked


def _failure_kinds(kinds: object, failures: int) -> dict[str, int]:
    """`kinds` as `failure_kinds` must be (see the module's docstring). Its errors name no kind
    or count: on the platform they are the held-out set's."""
    if not isinstance(kinds, dict):
        raise ValueError("failure_kinds must be an object of kinds to counts")
    fields = cast(dict[object, object], kinds)
    if len(fields) > MAX_FAILURE_KINDS:
        raise ValueError(f"failure_kinds has more than {MAX_FAILURE_KINDS} kinds")
    checked: dict[str, int] = {}
    for kind, count in fields.items():
        if not isinstance(kind, str) or FAILURE_KIND.fullmatch(kind) is None:
            raise ValueError("a failure kind isn't 1 to 40 of a-z, 0-9, _, . and -")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("a failure kind's count isn't an integer >= 0")
        checked[kind] = count
    if sum(checked.values()) != failures:
        raise ValueError("failure_kinds' counts don't sum to failures")
    return checked


def check_result(result: object) -> dict[str, object]:
    """The schema-1 fields of `result` (a parsed result.json), checked and normalized (numbers as
    floats); ValueError, and only ValueError, naming the first field that doesn't fit. The
    platform reads results with this, so whatever a grader writes either passes or is an
    invalid result, never a crash. `failure_kinds` is there only if the result has it."""
    if not isinstance(result, dict):
        raise ValueError(f"the result must be a JSON object, got {_short(result)}")
    fields = cast(dict[object, object], result)
    missing = [key for key in RESULT_KEYS if key not in fields]
    if missing:
        raise ValueError(f"the result has no {', '.join(missing)}")
    if type(fields["schema"]) is not int or fields["schema"] != SCHEMA_VERSION:
        raise ValueError(f"schema must be {SCHEMA_VERSION}, got {_short(fields['schema'])}")
    n_items, failures = _count("n_items", fields["n_items"]), _count("failures", fields["failures"])
    if failures > n_items:
        raise ValueError(f"failures ({failures}) can't exceed n_items ({n_items})")
    public = fields["public"]
    kinds: dict[str, object] = {}
    if FAILURE_KINDS in fields:
        kinds[FAILURE_KINDS] = _failure_kinds(fields[FAILURE_KINDS], failures)
    return {
        **kinds,
        "schema": SCHEMA_VERSION,
        "primary": _metric("primary", fields["primary"]),
        "secondary": _numbers("secondary", fields["secondary"]),
        "public": None if public is None else _metric("public", public),
        "n_items": n_items,
        "failures": failures,
        "timings": _numbers("timings", fields["timings"], minimum=0),
        "grader_version": _name("grader_version", fields["grader_version"]),
    }


def _value(value: Value) -> dict[str, object]:
    return {"name": value.name, "value": value.value}


def write_result(
    *,
    primary: Value,
    n_items: int,
    failures: int,
    grader_version: str,
    secondary: Mapping[str, float] | None = None,
    public: Value | None = None,
    timings: Mapping[str, float] | None = None,
    path: Path = RESULT_PATH,
    print_metrics: bool | None = None,
    item_scores: Sequence[float] | None = None,
    failure_kinds: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Write result.json (schema 1) to `path` atomically; returns what was written. ValueError,
    before anything is written, if a field doesn't fit the schema. Then prints `metric_lines`
    if `print_metrics` (by default: in a local run). `timings.startup_s` is left out unless the
    grader called `begin_timed()` (`commonhill_grader.startup`): a startup it never measured isn't 0.
    `item_scores`, if given, are the per-item scores whose mean is the primary value, written
    first, to items.json beside `path` (held-out data: see the module's docstring).
    `failure_kinds`, if given, counts the failures by kind (they must sum to `failures`)."""
    reported = {k: v for k, v in (timings or {}).items() if k != "startup_s" or began()}
    result = check_result(
        {
            "schema": SCHEMA_VERSION,
            "primary": _value(primary),
            "secondary": dict(secondary or {}),
            "public": None if public is None else _value(public),
            "n_items": n_items,
            "failures": failures,
            "timings": reported,
            "grader_version": grader_version,
            **({} if failure_kinds is None else {FAILURE_KINDS: dict(failure_kinds)}),
        }
    )
    data = (json.dumps(result, allow_nan=False, sort_keys=True) + "\n").encode()
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError(f"result.json would be {len(data)} bytes, over the {MAX_RESULT_BYTES} byte limit")
    items: bytes | None = None
    if item_scores is not None:
        scores = check_items({"schema": SCHEMA_VERSION, ITEM_SCORES: list(item_scores)}, result)
        items = (json.dumps({"schema": SCHEMA_VERSION, ITEM_SCORES: scores}, allow_nan=False) + "\n").encode()
        if len(items) > MAX_ITEMS_BYTES:
            raise ValueError(f"{ITEMS_FILE} would be over the {MAX_ITEMS_BYTES} byte limit")
    if items is not None:
        _write_atomically(path.parent / ITEMS_FILE, items)
    _write_atomically(path, data)
    if is_local() if print_metrics is None else print_metrics:
        print("\n".join(metric_lines(result)), flush=True)
    return result


def _write_atomically(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _key(name: str) -> str:
    """`name` as omp's METRIC/ASI keys allow it: word characters, `.` and `-`."""
    return re.sub(r"[^\w.-]", "_", name, flags=re.ASCII)


def metric_lines(result: Mapping[str, object]) -> list[str]:
    """`METRIC` lines for the primary value (when valid), the secondary metrics and the public
    score; `ASI` lines for validity, counts, the failures by kind, the grader version and the
    timings."""
    lines: list[str] = []
    primary = _mapping(result["primary"])
    if primary["value"] is not None:
        lines.append(f"METRIC {_key(str(primary['name']))}={primary['value']!r}")
    for name, value in _mapping(result["secondary"]).items():
        lines.append(f"METRIC {_key(name)}={value!r}")
    public = _mapping(result["public"]) if result["public"] is not None else None
    if public is not None and public["value"] is not None:
        lines.append(f"METRIC public.{_key(str(public['name']))}={public['value']!r}")
    lines.append(f"ASI valid={'true' if primary['value'] is not None else 'false'}")
    lines += [f"ASI {key}={result[key]}" for key in ("n_items", "failures", "grader_version")]
    kinds = _mapping(result.get(FAILURE_KINDS, {}))
    lines += [f"ASI failures.{kind}={count}" for kind, count in sorted(kinds.items())]
    lines += [f"ASI timings.{_key(name)}={value!r}" for name, value in _mapping(result["timings"]).items()]
    return lines


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def invalid_result(
    exc: BaseException,
    *,
    metric: str,
    n_items: int,
    grader_version: str,
    kind: str | None = None,
    path: Path = RESULT_PATH,
) -> dict[str, object]:
    """The result of a run that couldn't be evaluated because `exc` was raised: its primary value
    null, all of its `n_items` items failed, of one failure kind: `kind`, else "worker_not_ready"
    for `WorkerNotReady` (the worker never got ready), else "grader_error". Prints `run invalid:
    <the exception's type>` on stderr first: never the exception's own text, which may quote what
    the worker sent or the evaluation data. A grader calls it from its `except Exception` around
    the whole run, so it always ends with a result."""
    print(f"run invalid: {type(exc).__name__}", file=sys.stderr, flush=True)
    if kind is None:
        kind = "worker_not_ready" if isinstance(exc, WorkerNotReady) else "grader_error"
    return write_result(
        primary=Value(metric, None),
        n_items=n_items,
        failures=n_items,
        failure_kinds={kind: n_items},
        grader_version=grader_version,
        path=path,
    )
