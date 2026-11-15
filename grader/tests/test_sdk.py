"""The grader SDK's template subset: wait_ready, seed, write_result."""

import json
import math
import os
import re
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

import arh_grader.ready as ready_module
from arh_grader import (
    ITEMS_FILE,
    PUBLIC_SEED,
    STARTUP_ENV,
    FetchError,
    Value,
    WorkerNotReady,
    begin_timed,
    check_items,
    check_result,
    fetch,
    invalid_result,
    remaining,
    run_deadline,
    seed,
    startup,
    startup_timeout,
    wait_ready,
    worker_started,
    write_result,
)
from arh_grader.ready import DEFAULT_STARTUP_TIMEOUT_S
from arh_grader.result import MAX_COUNT, MAX_ITEMS_BYTES


class Health(BaseHTTPRequestHandler):
    """/healthz answers 503 until `ready_at` (monotonic), then 200; `hang` never answers."""

    ready_at = 0.0
    hang = False

    def do_GET(self) -> None:
        if self.hang:
            time.sleep(30)
            return
        status = 200 if self.path == "/healthz" and time.monotonic() >= self.ready_at else 503
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve(**handler: object) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Health,), handler))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_wait_ready_returns_once_healthz_answers_200() -> None:
    for url in serve(ready_at=time.monotonic() + 0.5):
        took = wait_ready(url + "/", timeout_s=60)
        assert 0.4 <= took < 30, "once it answers, not at the deadline"  # loaded machines: PER-283


def test_wait_ready_gives_up_at_its_deadline_with_the_last_answer(capsys: pytest.CaptureFixture[str]) -> None:
    """Release 3: how long it waited (its own clock, at least the limit) is in the exception and on
    one stderr line, which `arh run` reads (PER-336)."""
    for url in serve(ready_at=float("inf")):
        started = time.monotonic()
        with pytest.raises(
            WorkerNotReady, match=r"/healthz not ready after \d+\.\d{3} s \(limit 1 s\): HTTP 503"
        ) as refused:
            wait_ready(url, timeout_s=1.0)
        took = time.monotonic() - started
        assert took < 10, "at the deadline, give or take a loaded machine's delays"
        assert refused.value.limit_s == 1.0
        assert 1.0 <= refused.value.waited_s <= took
        assert capsys.readouterr().err == (
            f"arh_grader: worker not ready after {refused.value.waited_s:.3f} s (limit 1 s)\n"
        )


def test_wait_ready_isnt_held_by_a_worker_that_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe gets at most what is left of the deadline: with probes of a minute (instead of
    2 s), a probe that outlived the 1 s deadline would show by tens of seconds, whatever the load."""
    monkeypatch.setattr(ready_module, "PROBE_TIMEOUT_S", 60.0)
    for url in serve(hang=True):
        started = time.monotonic()
        with pytest.raises(WorkerNotReady, match="timed out"):
            wait_ready(url, timeout_s=1.0)
        assert time.monotonic() - started < 10


def test_a_last_probe_its_deadline_cut_short_keeps_the_answer_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release 6: a last probe with less than PROBE_TIMEOUT_S left that times out doesn't replace
    the last answer: under load, its "timed out" hid the worker's 503 (PER-344). Probes of a minute
    make every probe one the deadline cuts short; the worker answers once, then never in time."""
    monkeypatch.setattr(ready_module, "PROBE_TIMEOUT_S", 60.0)
    answered = threading.Event()

    def do_get(self: Health) -> None:
        if answered.is_set():
            time.sleep(30)
            return
        answered.set()
        Health.do_GET(self)

    for url in serve(ready_at=float("inf"), do_GET=do_get):
        with pytest.raises(WorkerNotReady, match=r": HTTP 503$"):
            wait_ready(url, timeout_s=3.0)


def test_wait_ready_retries_a_refused_connection() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(WorkerNotReady, match="ConnectionRefusedError"):
        wait_ready(f"http://127.0.0.1:{port}", timeout_s=0.5)


def test_a_worker_ready_within_the_projects_startup_budget_is_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PER-310: without a timeout of its own, wait_ready waits the project's startup_timeout_s,
    which the runner gives the grader (AR_STARTUP_TIMEOUT_S): a worker that gets ready at 15 s
    under a 30 s budget is measured, not failed (as it was against a grader's fixed 10 s)."""
    monkeypatch.setenv(STARTUP_ENV, "30")
    ready_at = time.monotonic() + 15
    seen: list[float] = []  # when each probe reached the worker

    def do_get(self: Health) -> None:
        seen.append(time.monotonic())
        Health.do_GET(self)

    for url in serve(ready_at=ready_at, do_GET=do_get):
        before = time.monotonic()
        took = wait_ready(url)
        after = time.monotonic()
        # Its clock started before its first probe, and stopped after the worker's 200 (at
        # ready_at or later): whatever the load, no wall-clock slack (PER-344).
        assert ready_at - seen[0] <= took <= after - before


def test_a_worker_never_ready_fails_after_the_projects_startup_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PER-310: a worker that never gets ready fails as before, WorkerNotReady (the run invalid:
    a reject for the candidate, a platform error for the incumbent), once the budget is spent."""
    monkeypatch.setenv(STARTUP_ENV, "2")
    for url in serve(ready_at=float("inf")):
        started = time.monotonic()
        with pytest.raises(
            WorkerNotReady, match=r"/healthz not ready after \d+\.\d{3} s \(limit 2 s\): HTTP 503"
        ):
            wait_ready(url)
        assert 2 <= time.monotonic() - started < 30


def test_the_startup_budget_is_the_runners_or_a_default_for_a_loaded_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without AR_STARTUP_TIMEOUT_S (the grader run by hand, `docker compose up`) the budget is
    DEFAULT_STARTUP_TIMEOUT_S, at least a minute; a value that isn't a positive number of seconds
    is the runner's bug."""
    monkeypatch.delenv(STARTUP_ENV, raising=False)
    assert startup_timeout() == DEFAULT_STARTUP_TIMEOUT_S >= 60
    monkeypatch.setenv(STARTUP_ENV, "")
    assert startup_timeout() == DEFAULT_STARTUP_TIMEOUT_S
    monkeypatch.setenv(STARTUP_ENV, "45")
    assert startup_timeout() == 45.0
    for bad in ("0", "-5", "inf", "nan", "soon"):
        monkeypatch.setenv(STARTUP_ENV, bad)
        with pytest.raises(ValueError, match=STARTUP_ENV):
            startup_timeout()


def test_seed_is_the_secret_when_set_else_the_public_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AR_SEED", raising=False)
    assert seed() == PUBLIC_SEED
    monkeypatch.setenv("AR_SEED", "")
    assert seed() == PUBLIC_SEED
    monkeypatch.setenv("AR_SEED", "s3cr3t-seed")
    assert seed() == "s3cr3t-seed"


def test_the_run_deadline_and_what_is_left_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AR_RUN_DEADLINE", raising=False)
    assert (run_deadline(), remaining()) == (None, None)
    monkeypatch.setenv("AR_RUN_DEADLINE", "")
    assert (run_deadline(), remaining()) == (None, None)
    now = time.time()
    monkeypatch.setenv("AR_RUN_DEADLINE", f"{now + 30:.3f}")
    deadline = run_deadline()
    left = remaining()
    assert deadline is not None
    assert abs(deadline - (now + 30)) < 0.001
    assert left is not None
    assert 20 < left <= 30.0005, "30 s, but for the value's rounding to the millisecond (and the load)"
    monkeypatch.setenv("AR_RUN_DEADLINE", f"{now - 5:.3f}")
    assert remaining() == 0.0, "never negative"


@pytest.mark.parametrize("value", ["soon", "nan", "inf", "-inf"])
def test_a_deadline_that_isnt_a_number_is_an_error(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AR_RUN_DEADLINE", value)
    with pytest.raises(ValueError, match=r"AR_RUN_DEADLINE|could not convert"):
        run_deadline()


def test_startup_counts_from_the_workers_start_or_else_the_sdks_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = time.time
    now = real()
    monkeypatch.setattr(time, "time", lambda: now)  # the clock stands still: no rounding at the edges
    monkeypatch.setenv("AR_WORKER_STARTED_AT", f"{now - 12.25:.3f}")
    assert begin_timed() == pytest.approx(12.25, abs=0.001)
    monkeypatch.setenv("AR_WORKER_STARTED_AT", f"{now + 5:.3f}")
    assert begin_timed() == 0.0, "never negative"
    monkeypatch.setattr(time, "time", real)
    for unset in ("", None):
        if unset is None:
            monkeypatch.delenv("AR_WORKER_STARTED_AT")
        else:
            monkeypatch.setenv("AR_WORKER_STARTED_AT", unset)
        assert worker_started() is None
        since_import = begin_timed()
        assert 0 < since_import < time.time() - 1e9, "since the SDK's import, not since 1970"


@pytest.mark.parametrize("called", [False, True])
def test_a_startup_is_written_only_if_the_grader_measured_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, called: bool
) -> None:
    """Sol's R6: a grader that never called begin_timed() reports no startup (a reward version
    that folds it fails the run), not a startup of 0."""
    monkeypatch.setattr(startup, "_began", False)
    if called:
        begin_timed()
    written = write_result(
        primary=Value("wall_ms", 812.5),
        n_items=1,
        failures=0,
        grader_version="g",
        timings={"total_ms": 812.5, "startup_s": 0.0},
        path=tmp_path / "result.json",
    )
    assert written["timings"] == ({"total_ms": 812.5, "startup_s": 0.0} if called else {"total_ms": 812.5})


@pytest.mark.parametrize("value", ["soon", "nan", "inf"])
def test_a_worker_start_that_isnt_a_number_is_an_error(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AR_WORKER_STARTED_AT", value)
    with pytest.raises(ValueError, match=r"AR_WORKER_STARTED_AT|could not convert"):
        begin_timed()


def test_write_result_writes_schema_1(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    written = write_result(
        primary=Value("wall_ms", 812.5),
        n_items=1000,
        failures=0,
        grader_version="example-1",
        secondary={"p90_batch_ms": 9},
        public=Value("wall_ms", 798.0),
        timings={"total_ms": 812.5},
        path=path,
    )
    expected = {
        "schema": 1,
        "primary": {"name": "wall_ms", "value": 812.5},
        "secondary": {"p90_batch_ms": 9.0},
        "public": {"name": "wall_ms", "value": 798.0},
        "n_items": 1000,
        "failures": 0,
        "timings": {"total_ms": 812.5},
        "grader_version": "example-1",
    }
    assert json.loads(path.read_text()) == written == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]


def test_an_invalid_run_has_a_null_primary_value(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    write_result(primary=Value("wall_ms", None), n_items=10, failures=3, grader_version="g", path=path)
    assert json.loads(path.read_text()) == {
        "schema": 1,
        "primary": {"name": "wall_ms", "value": None},
        "secondary": {},
        "public": None,
        "n_items": 10,
        "failures": 3,
        "timings": {},
        "grader_version": "g",
    }


def test_per_item_scores_go_to_items_json_beside_the_result_checked_but_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Release 2: `item_scores`, one per item, averaging to the primary value, in items.json, not
    in result.json; held-out data, so a local run's METRIC/ASI lines leave them out."""
    path = tmp_path / "result.json"
    scores = [1, 0, 1, 1, 0.25]
    written = write_result(
        primary=Value("accuracy", 0.65),
        n_items=5,
        failures=0,
        grader_version="g",
        item_scores=scores,
        path=path,
        print_metrics=True,
    )
    assert "item_scores" not in json.loads(path.read_text())
    assert "item_scores" not in written
    items = json.loads((tmp_path / ITEMS_FILE).read_text())
    assert items == {"schema": 1, "item_scores": [1.0, 0.0, 1.0, 1.0, 0.25]}
    assert check_items(items, check_result(json.loads(path.read_text()))) == [1.0, 0.0, 1.0, 1.0, 0.25]
    printed = capsys.readouterr().out
    assert "METRIC accuracy=0.65" in printed
    assert "0.25" not in printed
    assert "item_scores" not in printed


def test_twenty_thousand_items_keep_the_result_small(tmp_path: Path) -> None:
    """Sol's R3: covertype's 20,000 held-out rows. Their scores are ~100 KB, over a verification's
    64 KiB result cap, so they are in items.json, and result.json stays as small as without them."""
    path = tmp_path / "result.json"
    scores = [1.0] * 16_742 + [0.0] * 3_258
    write_result(
        primary=Value("accuracy", math.fsum(scores) / len(scores)),
        n_items=len(scores),
        failures=0,
        grader_version="g",
        item_scores=scores,
        path=path,
    )
    assert path.stat().st_size < 1024
    assert 64 * 1024 < (tmp_path / ITEMS_FILE).stat().st_size <= MAX_ITEMS_BYTES


VALUES = re.compile(r"\d")


@pytest.mark.parametrize(
    ("scores", "message"),
    [
        ([1.0, 0.0], "item_scores don't have one score per item"),
        ([1.0, 0.0, float("nan")], "an item score isn't a finite number"),
        ([1.0, 0.0, True], "an item score isn't a number"),
        ([1.0, 1.0, 1.0], "item_scores don't average to the primary value"),
    ],
)
def test_per_item_scores_must_be_one_finite_number_per_item_averaging_to_the_primary(
    scores: object, message: str, tmp_path: Path
) -> None:
    """And what is wrong is said in words only: no score, count or value (Sol's R2). Nothing is
    written."""
    with pytest.raises(ValueError, match=message) as raised:
        write_result(
            primary=Value("accuracy", 2 / 3),
            n_items=3,
            failures=0,
            grader_version="g",
            item_scores=scores,  # pyright: ignore[reportArgumentType]
            path=tmp_path / "result.json",
        )
    assert not VALUES.search(str(raised.value))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ({"item_scores": [1.0]}, "items.json must be an object"),
        ({"schema": 2, "item_scores": [1.0]}, "items.json's schema must be 1"),
        ({"schema": 1, "item_scores": "101"}, "item_scores must be a list of numbers"),
        ({"schema": 1, "item_scores": [1.0], "debug": 1}, "items.json must be an object"),
    ],
)
def test_an_items_file_must_be_schema_1_with_only_its_scores(items: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_items(items, check_result({**VALID_RESULT, "n_items": 1, "failures": 0}))
    assert (
        check_items(
            {"schema": 1, "item_scores": []}, check_result({**VALID_RESULT, "n_items": 0, "failures": 0})
        )
        == []
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"primary": Value("", 1.0)}, "primary.name must be a non-empty string"),
        ({"primary": Value("x", float("nan"))}, "primary.value must be a finite number"),
        ({"primary": Value("x", float("inf"))}, "primary.value must be a finite number"),
        ({"primary": Value("x", True)}, "primary.value must be a finite number"),  # pyright: ignore[reportArgumentType]
        ({"public": Value("x", "1")}, "public.value must be a finite number"),  # pyright: ignore[reportArgumentType]
        ({"n_items": -1}, "n_items must be an integer >= 0"),
        ({"failures": 1.5}, "failures must be an integer >= 0"),
        ({"failures": 11}, r"failures \(11\) can't exceed n_items \(10\)"),
        ({"grader_version": ""}, "grader_version must be a non-empty string"),
        ({"secondary": {"x": None}}, r"secondary\['x'\] must be a finite number"),
        ({"timings": {"total_ms": -1}}, r"timings\['total_ms'\] must be at least 0"),
        ({"timings": {"": 1}}, "timings key must be a non-empty string"),
        ({"primary": Value("wall\ud800", 1.0)}, "primary.name must be valid Unicode"),
        ({"grader_version": "v\udfff"}, "grader_version must be valid Unicode"),
        ({"secondary": {"\ud83d": 1.0}}, "secondary key must be valid Unicode"),
        ({"secondary": {f"k{i}": 1.0 for i in range(100_000)}}, "over the 1048576 byte limit"),
    ],
)
def test_a_result_that_doesnt_fit_the_schema_is_not_written(
    change: dict[str, object], message: str, tmp_path: Path
) -> None:
    path = tmp_path / "result.json"
    path.write_text("previous")
    fields: dict[str, object] = {
        "primary": Value("x", 1.0),
        "n_items": 10,
        "failures": 0,
        "grader_version": "g",
    }
    with pytest.raises(ValueError, match=message):
        write_result(**(fields | change), path=path)  # pyright: ignore[reportArgumentType]
    assert path.read_text() == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]


def test_a_failed_write_leaves_the_previous_result_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "result.json"
    path.write_text("previous")

    def no_rename(source: str, destination: Path) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", no_rename)
    with pytest.raises(OSError, match="disk gone"):
        write_result(primary=Value("x", 1.0), n_items=1, failures=0, grader_version="g", path=path)
    assert path.read_text() == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]


# --- hostile workers (scripted byte for byte) ------------------------------------------------------

type Start = Callable[[Callable[[bytes], list[tuple[float, bytes]]]], Any]
OK_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"


def trickle(data: bytes, every_s: float) -> list[tuple[float, bytes]]:
    return [(every_s, data[i : i + 1]) for i in range(len(data))]


def elapsed(call: Callable[[], object]) -> tuple[object, float]:
    started = time.monotonic()
    try:
        result: object = call()
    except Exception as exc:
        result = exc
    return result, time.monotonic() - started


def test_fetch_returns_the_status_and_body(scripted_http: Start) -> None:
    server = scripted_http(lambda _: [(0, OK_HEAD + b"Content-Length: 2\r\n\r\n{}")])
    response = fetch(server.url + "/run?x=1", method="POST", body=b"[]", content_type="application/json",
                     deadline=time.monotonic() + 5, max_bytes=10)  # fmt: skip
    assert (response.status, response.body) == (200, b"{}")
    assert server.requests[0].startswith(b"POST /run?x=1 HTTP/1.1\r\n")
    assert server.requests[0].endswith(b"\r\n\r\n[]")


@pytest.mark.parametrize(
    ("script", "error"),
    [
        # Headers a byte every 500 ms: each read is quick, the whole is far past the deadline.
        (trickle(OK_HEAD + b"Content-Length: 2\r\n\r\n{}", 0.5), "timed out"),
        # Headers at once, then the body trickled.
        ([(0, OK_HEAD + b"Content-Length: 40\r\n\r\n"), *trickle(b"x" * 40, 0.5)], "timed out"),
        # A chunked body cut off in the middle of a chunk.
        ([(0, OK_HEAD + b'Transfer-Encoding: chunked\r\n\r\n10\r\n{"outp')], "IncompleteRead"),
        # Fewer bytes than Content-Length.
        ([(0, OK_HEAD + b"Content-Length: 100\r\n\r\n{}")], "IncompleteRead"),
        # A declared body and then nothing at all.
        ([(0, OK_HEAD + b"Content-Length: 1\r\n\r\n")], "IncompleteRead"),
        # A Content-Length that isn't a number.
        ([(0, OK_HEAD + b"Content-Length: two\r\n\r\n{}")], "IncompleteRead"),
        # Part of the head just before the deadline, then silence: the read waits only for the
        # time left, not for the time the request started with.
        ([(0.4, b"HTTP/1.1 200 OK\r\n"), (5, b"")], "timed out"),
        # Not HTTP.
        ([(0, b"SECRET-WORKER-DATA garbage\r\n\r\n")], "BadStatusLine"),
        ([(0, b"")], "RemoteDisconnected"),
        # Too large, with or without saying so.
        ([(0, OK_HEAD + b"Content-Length: 11\r\n\r\n" + b"x" * 11)], "response body over 10 bytes"),
        ([(0, OK_HEAD + b"\r\n" + b"x" * 100)], "response body over 10 bytes"),
        # A header line too long for http.client.
        ([(0, b"HTTP/1.1 200 OK\r\nX: " + b"y" * 70000 + b"\r\n\r\n")], "LineTooLong"),
    ],
)
def test_fetch_turns_hostile_responses_into_fetch_errors_within_the_deadline(
    scripted_http: Start, script: list[tuple[float, bytes]], error: str
) -> None:
    server = scripted_http(lambda _: script)
    outcome, took = elapsed(lambda: fetch(server.url, deadline=time.monotonic() + 0.5, max_bytes=10))
    assert isinstance(outcome, FetchError), outcome
    assert str(outcome) == error
    assert "SECRET-WORKER-DATA" not in str(outcome)
    # The deadline is 0.5 s; reading on past it would take the whole trickled response (10 s or more).
    assert took < 5


def test_fetch_doesnt_follow_redirects(scripted_http: Start) -> None:
    server = scripted_http(
        lambda _: [(0, b"HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\nContent-Length: 0\r\n\r\n")]
    )
    assert fetch(server.url, deadline=time.monotonic() + 60, max_bytes=10).status == 302
    assert len(server.requests) == 1


def test_wait_ready_has_a_total_deadline_against_trickled_headers(scripted_http: Start) -> None:
    """Each byte arrives well within any per-read timeout; the 1 s deadline still holds (a byte
    every 0.5 s: the whole answer would take 19 s)."""
    server = scripted_http(lambda _: trickle(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", 0.5))
    outcome, took = elapsed(lambda: wait_ready(server.url, timeout_s=1.0))
    assert isinstance(outcome, WorkerNotReady)
    assert str(outcome).endswith(": timed out")
    assert took < 5


@pytest.mark.parametrize(
    ("answer", "last"),
    [
        (b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n", "HTTP 201"),
        (b"HTTP/1.1 302 Found\r\nLocation: /ok\r\nContent-Length: 0\r\n\r\n", "HTTP 302"),
        (b"SECRET-WORKER-DATA\r\n\r\n", "BadStatusLine"),
        (OK_HEAD + b"Transfer-Encoding: chunked\r\n\r\n5\r\nab", "IncompleteRead"),
    ],
)
def test_wait_ready_needs_exactly_a_complete_200(scripted_http: Start, answer: bytes, last: str) -> None:
    server = scripted_http(lambda _: [(0, answer)])
    with pytest.raises(WorkerNotReady) as refused:
        wait_ready(server.url, timeout_s=3)  # time for more than one try, even on a busy host
    assert str(refused.value).endswith(f": {last}")
    assert "SECRET-WORKER-DATA" not in str(refused.value)
    assert len(server.requests) > 1  # retried until the deadline


def test_wait_ready_retries_until_the_worker_answers_properly(scripted_http: Start) -> None:
    answers = iter([b"garbage\r\n\r\n", b"HTTP/1.1 503 No\r\nContent-Length: 0\r\n\r\n"])

    def script(_: bytes) -> list[tuple[float, bytes]]:
        return [(0, next(answers, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))]

    server = scripted_http(script)
    wait_ready(server.url, timeout_s=5)
    assert len(server.requests) == 3


@pytest.mark.parametrize(
    "url", ["http://[bad", "http://[SECRET-host]/run", "http://host:SECRET/run", "ftp://x/"]
)
def test_fetch_refuses_a_bad_url_without_quoting_it(url: str) -> None:
    with pytest.raises(FetchError) as refused:
        fetch(url, deadline=time.monotonic() + 1, max_bytes=10)
    assert str(refused.value) in ("invalid URL", "only http:// URLs with a host")
    assert "SECRET" not in str(refused.value)
    assert "[" not in str(refused.value)


VALID_RESULT: dict[str, object] = {
    "schema": 1,
    "primary": {"name": "wall_ms", "value": 812},
    "secondary": {"p90": 9.5},
    "public": None,
    "n_items": 10,
    "failures": 2,
    "timings": {"total_ms": 812.4},
    "grader_version": "g",
}


def test_check_result_normalizes_a_valid_result_and_ignores_other_keys() -> None:
    checked = check_result(VALID_RESULT | {"debug": [1, 2]})
    assert checked == VALID_RESULT | {"primary": {"name": "wall_ms", "value": 812.0}}
    assert isinstance(cast(dict[str, object], checked["primary"])["value"], float)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"schema": True}, "schema must be 1, got True"),
        ({"schema": 1.0}, "schema must be 1, got 1.0"),
        ({"schema": 2}, "schema must be 1, got 2"),
        (
            {"primary": {"name": "x", "value": 10**309}},
            "primary.value must be a finite number, got a huge integer",
        ),
        ({"primary": {"name": "x", "value": -(10**400)}}, "primary.value must be a finite number"),
        ({"primary": {"name": "x", "value": "1"}}, "primary.value must be a finite number"),
        ({"primary": {"name": "x", "value": True}}, "primary.value must be a finite number"),
        ({"primary": {"name": "x"}}, "primary must have exactly the keys name and value"),
        ({"primary": 3}, "primary must be an object"),
        ({"public": {"name": "", "value": 1}}, "public.name must be a non-empty string"),
        ({"failures": 11}, r"failures \(11\) can't exceed n_items \(10\)"),
        ({"n_items": -1}, "n_items must be an integer >= 0"),
        ({"n_items": 10**400}, "n_items is over 2147483647"),  # the platform's counts are 32-bit
        ({"timings": {"total_ms": -0.5}}, "timings\\['total_ms'\\] must be at least 0"),
        ({"timings": {"total_ms": 10**309}}, "must be a finite number"),
        ({"timings": [1]}, "timings must be an object of numbers"),
        ({"secondary": {"x": None}}, r"secondary\['x'\] must be a finite number"),
        ({"grader_version": 3}, "grader_version must be a non-empty string"),
    ],
)
def test_check_result_refuses_what_write_result_would_not_write(
    change: dict[str, object], error: str | None
) -> None:
    if error is None:
        check_result(VALID_RESULT | change)
        return
    with pytest.raises(ValueError, match=error):
        check_result(VALID_RESULT | change)


@pytest.mark.parametrize(
    ("kinds", "error"),
    [
        ({"null": 1, "wrong": 1}, None),
        ({"timeout": 2, "grader_error": 0}, None),  # a kind with no items is allowed
        ({"null": 1}, "failure_kinds' counts don't sum to failures"),
        ({"null": 3}, "failure_kinds' counts don't sum to failures"),
        ({}, "failure_kinds' counts don't sum to failures"),
        ({"Null": 2}, "a failure kind isn't 1 to 40"),
        ({"a" * 41: 2}, "a failure kind isn't 1 to 40"),
        ({"": 2}, "a failure kind isn't 1 to 40"),
        ({"bad kind": 2}, "a failure kind isn't 1 to 40"),
        ({"null": 2.0}, "a failure kind's count isn't an integer >= 0"),
        ({"null": True, "x": 1}, "a failure kind's count isn't an integer >= 0"),
        ({"null": 3, "x": -1}, "a failure kind's count isn't an integer >= 0"),
        ({f"k{i}": 0 for i in range(32)} | {"k0": 2}, None),
        ({f"k{i}": 0 for i in range(33)} | {"k0": 2}, "more than 32 kinds"),
        ([["null", 2]], "failure_kinds must be an object"),
    ],
)
def test_failure_kinds_must_be_named_counts_summing_to_failures(kinds: object, error: str | None) -> None:
    """Release 4's optional `failure_kinds`; the errors name no kind or count (the held-out set's
    on the platform). A result without it stays without it."""
    if error is None:
        assert check_result(VALID_RESULT | {"failure_kinds": kinds})["failure_kinds"] == kinds
    else:
        with pytest.raises(ValueError, match=re.escape(error)) as refused:
            check_result(VALID_RESULT | {"failure_kinds": kinds})
        assert "Null" not in str(refused.value)
        assert "bad kind" not in str(refused.value)
    assert "failure_kinds" not in check_result(VALID_RESULT)


def test_write_result_writes_the_failure_kinds_and_prints_them_locally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "result.json"
    kinds = {"timeout": 2, "wrong": 1}
    write_result(primary=Value("wall_ms", None), n_items=10, failures=3, grader_version="g", path=path,
                 failure_kinds=kinds, print_metrics=True)  # fmt: skip
    assert json.loads(path.read_text())["failure_kinds"] == kinds
    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if line.startswith("ASI failures")] == [
        "ASI failures=3", "ASI failures.timeout=2", "ASI failures.wrong=1",
    ]  # fmt: skip
    with pytest.raises(ValueError, match="don't sum to failures"):
        write_result(primary=Value("wall_ms", None), n_items=10, failures=3, grader_version="g",
                     path=tmp_path / "other.json", failure_kinds={"timeout": 2})  # fmt: skip
    assert not (tmp_path / "other.json").exists()


@pytest.mark.parametrize(
    ("exc", "kind", "expected"),
    [
        (WorkerNotReady("never", 301.0, 300.0), None, "worker_not_ready"),
        (RuntimeError("SECRET-EVAL-DATA"), None, "grader_error"),
        (ValueError("SECRET-EVAL-DATA"), "invalid_set", "invalid_set"),
    ],
)
def test_invalid_result_fails_every_item_under_one_kind_quoting_only_the_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], exc: Exception, kind: str | None, expected: str
) -> None:
    path = tmp_path / "result.json"
    invalid_result(exc, metric="wall_ms", n_items=7, grader_version="g", kind=kind, path=path)
    result = json.loads(path.read_text())
    assert (result["primary"], result["failures"], result["failure_kinds"]) == (
        {"name": "wall_ms", "value": None}, 7, {expected: 7}
    )  # fmt: skip
    err = capsys.readouterr().err
    assert f"run invalid: {type(exc).__name__}\n" in err
    assert "SECRET" not in err
    invalid_result(exc, metric="wall_ms", n_items=0, grader_version="g", path=path)
    again = "worker_not_ready" if expected == "worker_not_ready" else "grader_error"
    assert json.loads(path.read_text())["failure_kinds"] == {again: 0}


@pytest.mark.parametrize("key", ["n_items", "failures"])
def test_a_count_past_32_bits_is_an_invalid_result_and_not_echoed(key: str) -> None:
    """The platform keeps counts as 32-bit integers: a larger one must be refused here, where an
    invalid result is a verdict, not stored, where it would fail the run's ingest."""
    fields = {"n_items": MAX_COUNT, "failures": MAX_COUNT, "failure_kinds": {"null": MAX_COUNT}}
    ok = VALID_RESULT | {"primary": {"name": "wall_ms", "value": None}} | fields
    assert check_result(ok)["failure_kinds"] == {"null": MAX_COUNT}
    huge = MAX_COUNT + 1
    bad = {**ok, key: huge, "failures": huge, "failure_kinds": {"null": huge}}
    with pytest.raises(ValueError, match=rf"^{key} is over {MAX_COUNT}$") as refused:
        check_result(bad)
    assert str(huge) not in str(refused.value)


@pytest.mark.parametrize("result", [[], None, "x", {"schema": 1}])
def test_check_result_refuses_what_isnt_a_whole_result(result: object) -> None:
    with pytest.raises(ValueError, match="the result"):
        check_result(result)


def test_write_result_refuses_a_huge_integer_with_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="finite number"):
        write_result(
            primary=Value("x", 10**400), n_items=1, failures=0, grader_version="g", path=tmp_path / "r.json"
        )
    assert list(tmp_path.iterdir()) == []
