"""The Client against hostile workers (scripted byte for byte): whatever a worker does, a grader
built on the Client finishes in time, counts the failures and writes a valid result."""

import json
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from commonhill_grader import Client, Value, write_result
from commonhill_grader.client import MAX_RESPONSE_BYTES, json_values

type Script = list[tuple[float, bytes]]
type Start = Callable[[Callable[[bytes], Script]], Any]
LEAK = b"SECRET-WORKER-DATA"
STRINGS = {"type": "string"}


def test_a_correct_batch(scripted_http: Start, worker_scripts: Any) -> None:
    server = scripted_http(worker_scripts.echo)
    batch = Client(server.url).run(["a", 1, None, {"k": [1]}])
    assert batch.outputs == ["a", 1, None, {"k": [1]}]  # no schema: anything, but null fails
    assert (batch.failures, batch.error) == (1, None)
    assert 0 < batch.seconds < 5
    assert server.requests[0].startswith(b"POST /run HTTP/1.1\r\n")


def test_outputs_that_dont_fit_the_schema_fail_one_by_one(scripted_http: Start, worker_scripts: Any) -> None:
    body = json.dumps({"outputs": ["a", 1, "c", None, True, "d" * 11]}).encode()
    server = scripted_http(lambda _: [(0, worker_scripts.answer(b"200 OK", body))])
    batch = Client(server.url, output_schema={"type": "string", "maxLength": 10}).run(list("abcdef"))
    assert batch.outputs == ["a", None, "c", None, None, None]
    assert (batch.failures, batch.error) == (4, None)


def answer_with(worker_scripts: Any, body: bytes, status: bytes = b"200 OK") -> Callable[[bytes], Script]:
    return lambda _: [(0, worker_scripts.answer(status, body))]


def hostile(worker_scripts: Any) -> dict[str, tuple[Callable[[bytes], Script], str]]:
    """Workers the ticket lists (and a few more), and the error each batch should record."""
    big = 80 * 1024 * 1024  # past the 64 MiB a response may be (release 10)
    return {
        "hangs": (lambda _: [(30, b"")], "timed out"),
        "answers slowly": (lambda r: [(1.0, b""), *worker_scripts.echo(r)], "timed out"),
        "sends 80 MB": (
            lambda _: [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % big + b"[" * big)],
            f"response body over {MAX_RESPONSE_BYTES} bytes",
        ),
        "sends 2 million empty lists": (
            answer_with(worker_scripts, b'{"outputs": [' + b"[]," * 2_000_000 + b'"c"]}'),
            "over 3000002 JSON values",  # one million per item
        ),
        "sends UTF-16": (
            answer_with(worker_scripts, '{"outputs": ["a", "b", "c"]}'.encode("utf-16-le")),
            "not JSON (not UTF-8)",
        ),
        "sends malformed JSON": (
            answer_with(worker_scripts, b'{"outputs": [' + LEAK),
            "not JSON (JSONDecodeError)",
        ),
        "nests JSON 100000 deep": (
            answer_with(worker_scripts, b'{"outputs": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"),
            "not JSON (RecursionError)",
        ),
        "sends outputs of the wrong type": (
            answer_with(worker_scripts, b'{"outputs": "abc"}'),
            'not {"outputs": [...]}',
        ),
        "sends a list": (answer_with(worker_scripts, b'["a", "b", "c"]'), 'not {"outputs": [...]}'),
        "drops an output": (answer_with(worker_scripts, b'{"outputs": ["a", "b"]}'), "2 outputs for 3 items"),
        "adds an output": (
            answer_with(worker_scripts, b'{"outputs": ["a", "b", "c", "d"]}'),
            "4 outputs for 3 items",
        ),
        "drops the connection": (lambda _: [], "RemoteDisconnected"),
        "cuts the body short": (
            lambda _: [(0, b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"outputs"')],
            "IncompleteRead",
        ),
        "answers 201": (
            answer_with(worker_scripts, b'{"outputs": ["a", "b", "c"]}', b"201 Created"),
            "HTTP 201",
        ),
        "redirects": (answer_with(worker_scripts, b"", b"302 Found"), "HTTP 302"),
        "isn't HTTP": (lambda _: [(0, LEAK + b"\r\n\r\n")], "BadStatusLine"),
        "sends NaN": (answer_with(worker_scripts, b'{"outputs": ["a", NaN, "c"]}'), "not JSON (ValueError)"),
        "sends a nested Infinity": (
            answer_with(worker_scripts, b'{"outputs": ["a", "b", "c"], "x": [[-Infinity]]}'),
            "not JSON (ValueError)",
        ),
        "sends 1e999": (
            answer_with(worker_scripts, b'{"outputs": ["a", "b", 1e999]}'),
            "not JSON (ValueError)",
        ),
    }


def failure_kind(error: str) -> str:
    """The failure kind (release 4) of a batch failed with `error`."""
    if error.startswith("HTTP "):
        return "http_" + error.removeprefix("HTTP ")
    for prefix, found in (("timed out", "timeout"), ("response body over", "too_large"),
                          ("over ", "too_large"), ("not JSON", "not_json"),
                          ("not {", "bad_shape")):  # fmt: skip
        if error.startswith(prefix):
            return found
    return "wrong_count" if error.endswith("for 3 items") else "connection"


@pytest.mark.parametrize(
    "kind",
    [
        "hangs",
        "answers slowly",
        "sends 80 MB",
        "sends 2 million empty lists",
        "sends UTF-16",
        "sends malformed JSON",
        "nests JSON 100000 deep",
        "sends outputs of the wrong type",
        "sends a list",
        "drops an output",
        "adds an output",
        "drops the connection",
        "cuts the body short",
        "answers 201",
        "redirects",
        "isn't HTTP",
        "sends NaN",
        "sends a nested Infinity",
        "sends 1e999",
    ],
)
def test_a_grader_survives_a_hostile_worker_and_writes_a_valid_result(
    kind: str, scripted_http: Start, worker_scripts: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script, error = hostile(worker_scripts)[kind]
    server = scripted_http(script)
    # Short where the timeout is what's tested; generous elsewhere, so a busy host can't turn
    # the expected error into a timeout.
    timeout_s = 0.5 if error == "timed out" else 10
    client = Client(server.url, timeout_s=timeout_s, output_schema=STRINGS)

    started = time.monotonic()
    evaluation = client.evaluate(["a", "b", "c"] * 3, batch_size=3)
    took = time.monotonic() - started
    write_result(
        primary=Value("wall_ms", None if evaluation.failures else evaluation.timings.total_s * 1000),
        n_items=9,
        failures=evaluation.failures,
        timings=evaluation.timings.as_result(),
        grader_version="test",
        path=tmp_path / "result.json",
        failure_kinds=evaluation.failure_kinds(),
    )

    # Three batches, each bounded by its deadline, and a loaded machine's delays.
    assert took < 3 * timeout_s + 30
    assert evaluation.outputs == [None] * 9
    assert evaluation.errors == {error: 3}
    assert len(server.requests) == 3  # one request per batch: no redirect followed
    result = json.loads((tmp_path / "result.json").read_text())
    assert (result["primary"]["value"], result["n_items"], result["failures"]) == (None, 9, 9)
    assert result["failure_kinds"] == {failure_kind(error): 9}, "every item of a failed batch, by its cause"
    assert result["timings"]["batches"] == 3
    out = capsys.readouterr()
    assert "SECRET-WORKER-DATA" not in out.out + out.err + json.dumps(result) + json.dumps(evaluation.errors)


def test_a_body_of_many_small_values_is_refused_before_it_is_decoded(
    scripted_http: Start, worker_scripts: Any
) -> None:
    """Sol's P1 on PER-500: 64 MiB of `[]` decodes to ~1.4 GB of lists. The values are counted
    first, so the batch fails as too_large with memory to spare (decoding these 15 MB of `{}` would
    take ~375 MB)."""
    body = b'{"outputs": [' + b"{}," * 5_000_000 + b'"c"]}'
    server = scripted_http(answer_with(worker_scripts, body))
    client = Client(server.url, timeout_s=60)

    tracemalloc.start()
    try:
        batch = client.run(["a", "b", "c"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (batch.error, batch.kind) == ("over 3000002 JSON values", "too_large")
    assert batch.outputs == [None] * 3
    # The body as read (fetch buffers it): a few times its size, not decoding's 25.
    assert peak < 4 * len(body), f"peak {peak} bytes for a {len(body)}-byte body"


def test_values_are_counted_as_decoding_makes_them() -> None:
    """At least the values decoding makes; strings' contents, escaped quotes and runs of
    backslashes included, aren't."""
    for body, decoded in [
        (b"1", 1),
        (b'{"outputs": ["a", "b", "c"]}', 5),
        (b'{"outputs": [[1, 2], {"k": [3]}]}', 8),
        (json.dumps({"outputs": ["[,{:\\", '\\"[[[[', ",,,"]}).encode(), 5),
        (json.dumps(["\\" * 7 + '"', "\\" * 8, ",:", '"' * 5 + "[{"]).encode(), 5),
        ('["\\u00e9 é {,", 2]'.encode(), 3),
    ]:
        assert json.loads(body) is not None
        counted = json_values(body, 1000)
        assert counted is not None, body
        assert decoded <= counted <= decoded + 3, body
    assert json_values(b'{"outputs": [' + b"[]," * 1000 + b"1]}", 10) == 11, "stops past the cap"


@pytest.mark.parametrize(
    "body",
    [b'"' + b'\\"' * 10, b'["a", "b' + b"\\\\" * 10 + b'\\"]', b'["a"] "', b'"\\'],
    ids=["escaped quotes", "escaped backslashes then an escaped quote", "a lone quote", "a backslash"],
)
def test_a_string_that_never_ends_isnt_json(body: bytes) -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)
    assert json_values(body, 1000) is None


def test_a_megabyte_of_escaped_quotes_is_refused_at_once(scripted_http: Start, worker_scripts: Any) -> None:
    """Sol's re-check: a body of escaped quotes in a string that never ends took a backtracking
    pattern quadratic time (1.6 s for 16 KB). One pass: 1 MB in well under a second."""
    body = b'"' + b'\\"' * 512 * 1024 + b"x"
    server = scripted_http(answer_with(worker_scripts, body))
    started = time.monotonic()
    batch = Client(server.url, timeout_s=60).run(["a", "b", "c"])
    assert (batch.error, batch.kind) == ("not JSON (a string never ends)", "not_json")
    assert time.monotonic() - started < 0.5


def test_a_batch_may_hold_up_to_its_items_values(scripted_http: Start, worker_scripts: Any) -> None:
    body = json.dumps({"outputs": [[0] * 999_990, "b"]}).encode()
    server = scripted_http(answer_with(worker_scripts, body))
    batch = Client(server.url, timeout_s=60).run(["a", "b"])
    assert (batch.error, batch.failures) == (None, 0)
    batch = Client(server.url, timeout_s=60, max_values=100).run(["a", "b"])
    assert (batch.error, batch.kind) == ("over 100 JSON values", "too_large")


def test_only_the_bad_batch_fails(scripted_http: Start, worker_scripts: Any) -> None:
    """Failures are per batch and per item: one batch's dropped connection costs only its items."""
    seen: list[int] = []

    def script(request: bytes) -> Script:
        seen.append(1)
        return [] if len(seen) == 2 else worker_scripts.echo(request)

    server = scripted_http(script)
    evaluation = Client(server.url, output_schema=STRINGS).evaluate([str(i) for i in range(7)], batch_size=3)
    assert evaluation.outputs == ["0", "1", "2", None, None, None, "6"]
    assert (evaluation.failures, evaluation.errors) == (3, {"RemoteDisconnected": 1})
    assert evaluation.failure_kinds() == {"connection": 3}
    assert evaluation.timings.items == [3, 3, 1]


def test_warmup_batches_are_sent_but_not_recorded(scripted_http: Start, worker_scripts: Any) -> None:
    server = scripted_http(worker_scripts.echo)
    items = [str(i) for i in range(10)]
    evaluation = Client(server.url).evaluate(items, batch_size=4, warmup=2)
    sent = [json.loads(r.partition(b"\r\n\r\n")[2])["items"] for r in server.requests]
    assert sent == [items[0:4], items[4:8], items[0:4], items[4:8], items[8:10]]
    assert evaluation.outputs == items
    assert evaluation.timings.items == [4, 4, 2]


def test_an_evaluations_startup_counts_until_its_first_timed_batch(
    scripted_http: Start, worker_scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Evaluation.startup_s` is `begin_timed()` after the warmup batches: a worker that does its
    work on its first, warmup, request has that work in its startup, not in its timings."""
    seen: list[int] = []

    def lazy(request: bytes) -> Script:
        seen.append(1)
        return [(1.0 if len(seen) == 1 else 0.0, b""), *worker_scripts.echo(request)]

    server = scripted_http(lazy)
    monkeypatch.setenv("AR_WORKER_STARTED_AT", f"{time.time():.3f}")
    evaluation = Client(server.url, timeout_s=5).evaluate(["a", "b"], batch_size=1, warmup=1)
    assert evaluation.outputs == ["a", "b"]
    assert 1.0 <= evaluation.startup_s < 5
    assert sum(evaluation.timings.batch_s) < 1.0


def test_evaluate_refuses_nonsense_settings() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        Client("http://127.0.0.1:9").evaluate(["a"], batch_size=0)
    with pytest.raises(ValueError, match="warmup"):
        Client("http://127.0.0.1:9").evaluate(["a"], batch_size=1, warmup=-1)


def test_non_finite_numbers_fail_the_batch_without_a_schema(
    scripted_http: Start, worker_scripts: Any
) -> None:
    """Strict decoding applies whatever the schema: no schema here, and NaN hides in a nested
    output the schema would never look at."""
    server = scripted_http(answer_with(worker_scripts, b'{"outputs": [[1, [NaN]], 2]}'))
    batch = Client(server.url).run([1, 2])
    assert (batch.outputs, batch.error) == ([None, None], "not JSON (ValueError)")


def test_huge_integers_are_checked_exactly_and_never_raise(scripted_http: Start, worker_scripts: Any) -> None:
    huge = b"1" + b"0" * 400  # 10**400: a valid JSON integer, far beyond any float
    body = b'{"outputs": [' + huge + b", -" + huge + b", 5]}"
    server = scripted_http(answer_with(worker_scripts, body))

    integers = Client(server.url, output_schema={"type": "integer"}).run([1, 2, 3])
    assert integers.outputs == [10**400, -(10**400), 5]
    bounded = Client(server.url, output_schema={"type": "integer", "minimum": -10, "maximum": 10}).run(
        [1, 2, 3]
    )
    assert (bounded.outputs, bounded.item_errors) == ([None, None, 5], {"schema": 2})


def test_a_check_that_raises_fails_only_its_item(scripted_http: Start, worker_scripts: Any) -> None:
    server = scripted_http(worker_scripts.echo)
    client = Client(server.url)

    def check(output: object) -> str | None:
        if output == "boom":
            raise RuntimeError("SECRET-WORKER-DATA")
        return None

    client.check = check
    batch = client.run(["a", "boom", "c", None])
    assert batch.outputs == ["a", None, "c", None]
    assert (batch.error, batch.item_errors) == (None, {"check_error": 1, "null": 1})


def count_encoding(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """From now on, the length of every canonical encoding the schema check makes."""
    import commonhill_grader.schema as schema_module

    encoded: list[int] = []
    real = schema_module.canonical

    def counting(value: object) -> str:
        text = real(value)
        encoded.append(len(text))
        return text

    monkeypatch.setattr(schema_module, "canonical", counting)
    return encoded


def test_a_large_enum_against_a_large_response_stays_fast(
    scripted_http: Start, worker_scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """128 arrays of 128 integers that differ only in their last element, and a response of
    near-misses: comparing element by element would take minutes; a set of canonical encodings
    takes one encoding per output."""
    enum = [[*range(127), 1000 + k] for k in range(128)]
    miss = [*range(127), -1]
    outputs = [miss] * 1500
    body = json.dumps({"outputs": outputs}, separators=(",", ":")).encode()
    assert len(body) < 1024 * 1024
    server = scripted_http(answer_with(worker_scripts, body))
    client = Client(server.url, timeout_s=60, output_schema={"enum": enum})
    encoded = count_encoding(monkeypatch)

    started = time.monotonic()
    batch = client.run([0] * 1500)
    assert batch.item_errors == {"schema": 1500}
    assert sum(encoded) <= len(body)  # each output encoded once, whatever the enum's size
    # Generous for a busy host; comparing element by element took minutes.
    assert time.monotonic() - started < 60


def test_deeply_nested_consts_against_a_large_response_stay_fast(
    scripted_http: Start, worker_scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A const at each of 240 levels and 1500 outputs of that depth (~720 KB): one encoding per
    output (re-encoding every level encodes about 120 times the response)."""
    depth = 240
    schema: dict[str, object] = {"const": 0}
    value: object = 0
    for _ in range(depth):
        value = [value]
        schema = {"type": "array", "items": schema, "const": value}
    body = json.dumps({"outputs": [value] * 1500}, separators=(",", ":")).encode()
    assert len(body) < 1024 * 1024
    server = scripted_http(answer_with(worker_scripts, body))
    client = Client(server.url, timeout_s=60, output_schema=schema)
    encoded = count_encoding(monkeypatch)

    batch = client.run([0] * 1500)
    assert (batch.failures, batch.error) == (0, None)
    assert sum(encoded) <= len(body)  # each output encoded once, not once per level


def test_failure_kinds_count_each_failed_item_once_by_its_batch_or_its_own_problem(
    scripted_http: Start, worker_scripts: Any
) -> None:
    """Release 4: a failed batch's items under its cause, a good batch's failed items under their
    own problem; the kinds sum to the evaluation's failures, warmup batches aside."""
    answers = [
        worker_scripts.answer(b"200 OK", b'{"outputs": ["a", "b"]}'),  # warmup: not recorded
        worker_scripts.answer(b"200 OK", b'{"outputs": ["a", null]}'),
        worker_scripts.answer(b"503 Busy", b""),
        worker_scripts.answer(b"200 OK", b'{"outputs": ["e", 5]}'),
        worker_scripts.answer(b"200 OK", b'{"outputs": ["g"]}'),
    ]
    server = scripted_http(lambda _: [(0, answers.pop(0))])
    evaluation = Client(server.url, output_schema=STRINGS).evaluate(list("abcdefgh"), batch_size=2, warmup=1)
    assert evaluation.outputs == ["a", None, None, None, "e", None, None, None]
    assert evaluation.failure_kinds() == {"null": 1, "http_503": 2, "schema": 1, "wrong_count": 2}
    assert sum(evaluation.failure_kinds().values()) == evaluation.failures
