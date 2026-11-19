"""The worker's `POST /run`, from the grader's side (CONTRACT.md).

`Client.run(items)` sends one batch and returns a `Batch`: one output per item, `None` for each
item that failed, and the time the request took. A batch never raises for anything the worker
does. Everything that can go wrong is counted as item failures instead:

- the whole batch fails (every output None) when the request fails (`fetch`: timeout against an
  absolute deadline, connection refused or dropped, malformed HTTP, a body over the cap), the
  status isn't exactly 200, the body holds more JSON values than the batch may (`max_values`, at
  most `MAX_VALUES_PER_OUTPUT` per item: counted before decoding, so a body of `[]`s can't take
  gigabytes to decode), the body isn't strict UTF-8 JSON (`NaN`, `Infinity` and numbers too large
  for a float, like `1e999`, are refused anywhere in it; so is nesting too deep to decode), or it
  isn't `{"outputs": [...]}` with exactly one output per item;
- a single item fails when its output is `null` or doesn't match the output schema
  (`commonhill_grader.schema`), and also if checking it raises for any reason.

`Client.evaluate(items, batch_size, warmup)` runs a whole evaluation: warmup batches first (their
results and times are thrown away), then every item once, timed per batch. Nothing the worker
sends is ever logged; `Batch.error` names only the kind of failure.

Each failed item has a failure kind (since the SDK's release 4; `commonhill_grader.result`'s
`failure_kinds`): its whole batch's (`Batch.kind`: "timeout", "too_large" (bytes or values),
"connection", "http_<status>", "not_json", "bad_shape" for a body that isn't `{"outputs": [...]}`,
"wrong_count" for a check that raised). `Evaluation.failure_kinds()` counts them, summing to its
failures.

With an oracle (since release 5, `commonhill_grader.oracle`), `run` and `evaluate` take the items'
sessions, one per item: a batch's sessions are closed as soon as its response is read, or its
request failed, so their counts are final before anything is checked; an item whose session went
over its cap fails as "oracle_cap", whatever its output (a whole batch's failure keeps its kind).
"""

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from commonhill_grader.fetch import FetchError, fetch
from commonhill_grader.schema import Checker
from commonhill_grader.startup import begin_timed
from commonhill_grader.timing import Timings, clock

if TYPE_CHECKING:
    from commonhill_grader.oracle import Session

# CONTRACT.md's defaults.
REQUEST_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# The most JSON values (containers, keys' values, items, scalars) a response may hold: at most this
# many per item, and this many in all. Decoding takes up to ~90 bytes of memory per value.
MAX_VALUES_PER_OUTPUT = 1_000_000
MAX_VALUES = 4_000_000
_QUOTE, _BACKSLASH = ord('"'), ord("\\")
_MARKS = frozenset(b"[{,:")


@dataclass(frozen=True)
class Batch:
    outputs: list[object | None]  # None: this item failed
    seconds: float  # request sent to whole response read, by the grader's clock
    error: str | None = None  # why the whole batch failed
    item_errors: dict[str, int] = field(default_factory=dict[str, int])  # failed items by kind
    kind: str | None = None  # the whole batch's failure kind, with `error`

    @property
    def failures(self) -> int:
        return sum(output is None for output in self.outputs)


@dataclass
class Evaluation:
    outputs: list[object | None] = field(default_factory=list[object | None])
    timings: Timings = field(default_factory=Timings)
    errors: dict[str, int] = field(default_factory=dict[str, int])  # failed batches by kind
    item_errors: dict[str, int] = field(default_factory=dict[str, int])  # failed items by kind
    startup_s: float = 0.0  # `begin_timed()` before the first timed batch: report it as timings.startup_s
    batch_items: dict[str, int] = field(default_factory=dict[str, int])  # items of failed batches, by kind

    @property
    def failures(self) -> int:
        return sum(output is None for output in self.outputs)

    def failure_kinds(self) -> dict[str, int]:
        """The failed items by failure kind, their whole batch's or their own: they sum to
        `failures` (write them as `write_result(failure_kinds=...)`, with the grader's own)."""
        kinds = dict(self.batch_items)
        for kind, count in self.item_errors.items():
            kinds[kind] = kinds.get(kind, 0) + count
        return kinds


class Client:
    """The worker at `url` (e.g. `os.environ["WORKER_URL"]`). `output_schema` describes one
    output; `None` accepts any JSON value. Since `None` marks a failed item, a `null` output
    always counts as a failure."""

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = REQUEST_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        max_values: int = MAX_VALUES,
        output_schema: Mapping[str, object] | None = None,
    ) -> None:
        self.run_url = url.rstrip("/") + "/run"
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_values = max_values
        self.check: Callable[[object], str | None] = Checker(output_schema) if output_schema else _any

    def run(
        self,
        items: Sequence[object],
        timeout_s: float | None = None,
        max_bytes: int | None = None,
        sessions: Sequence["Session"] | None = None,
    ) -> Batch:
        """One batch: `items`' outputs (None where an item failed) and the request's time. With
        `sessions` (one per item), closes them once the response is read or the request failed."""
        if sessions is not None and len(sessions) != len(items):
            raise ValueError("sessions must be one per item")
        body = json.dumps({"items": list(items)}).encode()
        started = clock()
        try:
            response = fetch(
                self.run_url,
                method="POST",
                body=body,
                content_type="application/json",
                deadline=time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s),
                max_bytes=self.max_bytes if max_bytes is None else max_bytes,
            )
            seconds = clock() - started
        except FetchError as exc:
            return Batch([None] * len(items), clock() - started, str(exc), kind=exc.kind)
        finally:
            for session in sessions or ():
                session.close()  # the counts are final from here
        if response.status != 200:
            status = response.status
            return Batch([None] * len(items), seconds, f"HTTP {status}", kind=f"http_{status}")
        cap = min(self.max_values, 2 + MAX_VALUES_PER_OUTPUT * len(items))
        if json.detect_encoding(response.body) not in ("utf-8", "utf-8-sig"):
            return Batch([None] * len(items), seconds, "not JSON (not UTF-8)", kind="not_json")
        counted = json_values(response.body, cap)
        if counted is None:
            return Batch([None] * len(items), seconds, "not JSON (a string never ends)", kind="not_json")
        if counted > cap:
            return Batch([None] * len(items), seconds, f"over {cap} JSON values", kind="too_large")
        try:
            decoded = loads(response.body)
        except (ValueError, RecursionError) as exc:
            return Batch([None] * len(items), seconds, f"not JSON ({type(exc).__name__})", kind="not_json")
        outputs = cast(dict[str, object], decoded).get("outputs") if isinstance(decoded, dict) else None
        if not isinstance(outputs, list):
            return Batch([None] * len(items), seconds, 'not {"outputs": [...]}', kind="bad_shape")
        outputs = cast(list[object], outputs)
        if len(outputs) != len(items):
            counted = f"{len(outputs)} outputs for {len(items)} items"
            return Batch([None] * len(items), seconds, counted, kind="wrong_count")
        kept: list[object | None] = []
        item_errors: dict[str, int] = {}
        for index, output in enumerate(outputs):
            problem = (
                "oracle_cap" if sessions is not None and sessions[index].over_cap else self._problem(output)
            )
            kept.append(output if problem is None else None)
            if problem is not None:
                item_errors[problem] = item_errors.get(problem, 0) + 1
        return Batch(kept, seconds, None, item_errors)

    def _problem(self, output: object) -> str | None:
        """None if `output` is good, else its failure kind: "null", "schema", or "check_error"
        when the check raised (never anything the output contains)."""
        if output is None:
            return "null"
        try:
            return None if self.check(output) is None else "schema"
        except Exception:  # nested too deeply, a number too large to print, ...
            return "check_error"

    def evaluate(
        self,
        items: Sequence[object],
        batch_size: int,
        warmup: int = 0,
        sessions: Sequence["Session"] | None = None,
    ) -> Evaluation:
        """Every item once, in order, `batch_size` per request, after `warmup` unrecorded batches
        (the first `warmup` batches, sent once more each). Outputs line up with `items`; the
        evaluation's `startup_s` is `begin_timed()` right before the first timed batch. With
        `sessions` (one per item; no warmup, which would close them), each batch's are closed
        once it is answered (`run`)."""
        if batch_size < 1 or warmup < 0:
            raise ValueError("batch_size must be >= 1 and warmup >= 0")
        if sessions is not None and (len(sessions) != len(items) or warmup):
            raise ValueError("sessions must be one per item, without warmup")
        starts = range(0, len(items), batch_size)
        batches = [items[i : i + batch_size] for i in starts]
        for batch in batches[:warmup]:
            self.run(batch)
        evaluation = Evaluation(startup_s=begin_timed())
        for start, batch in zip(starts, batches, strict=True):
            result = self.run(
                batch, sessions=None if sessions is None else sessions[start : start + batch_size]
            )
            evaluation.outputs += result.outputs
            evaluation.timings.record(result.seconds, len(batch))
            if result.error is not None:
                evaluation.errors[result.error] = evaluation.errors.get(result.error, 0) + 1
                kind = result.kind or "connection"
                evaluation.batch_items[kind] = evaluation.batch_items.get(kind, 0) + len(batch)
            for kind, count in result.item_errors.items():
                evaluation.item_errors[kind] = evaluation.item_errors.get(kind, 0) + count
        return evaluation


def _any(output: object) -> None:
    return None


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("number out of range")
    return number


def _no_constant(name: str) -> object:
    raise ValueError("NaN and Infinity aren't JSON")


def json_values(body: bytes, stop: int) -> int | None:
    """How many values UTF-8 JSON `body` holds, at least as many as decoding it makes up to where
    it fails, if it does: one, and one more for each `[`, `{`, `,` and `:` outside strings (in
    UTF-8, no multibyte sequence holds an ASCII byte). One pass over the bytes, ended as soon as
    the count passes `stop`; None if a string never ends."""
    count = 1
    inside = escaped = False
    for byte in body:
        if inside:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                inside = False
        elif byte == _QUOTE:
            inside = True
        elif byte in _MARKS:
            count += 1
            if count > stop:
                return count
    return None if inside else count


def loads(data: bytes | str) -> object:
    """Strict JSON: like json.loads, but `NaN`, `Infinity`, `-Infinity` and numbers that
    overflow a float are ValueErrors wherever they appear."""
    return json.loads(data, parse_float=_finite_float, parse_constant=_no_constant)
