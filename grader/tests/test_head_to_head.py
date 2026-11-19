"""The grader SDK's head-to-head side (release 8, PER-448): the run's settings, a seat's bot and
its move deadline, a run's game pairs, and items.json schema 2."""

import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from commonhill_grader.head_to_head import (
    CANDIDATE,
    HEAD_TO_HEAD_ENV,
    LOCAL_PAIRS,
    OPPONENT,
    TRANSPORT_ALLOWANCE_MS,
    Bot,
    Match,
    SeatFailed,
    check_pairs,
    play,
    settings,
    write_match,
)

URLS = {"WORKER_URL": "http://worker:8080", "OPPONENT_URL": "http://opponent:8080"}


def env(**given: object) -> dict[str, str]:
    fields: dict[str, object] = {
        "schema": 1, "candidate": "opponent", "turn_ms": 100, "pairs": 1, "references": [],
        "reference_pairs": 0,
    }  # fmt: skip
    return {**URLS, HEAD_TO_HEAD_ENV: json.dumps(fields | given)}


def test_the_settings_say_which_seat_is_the_candidates() -> None:
    given = settings(env=env())
    assert (given.candidate_url(env()), given.opponent_url(env())) == (
        URLS["OPPONENT_URL"],
        URLS["WORKER_URL"],
    )
    assert given.pairs == 1
    assert given.move_timeout_s() == (100 + TRANSPORT_ALLOWANCE_MS) / 1000
    local = settings(turn_ms=20, env=URLS)
    assert (local.candidate, local.pairs, local.turn_ms) == ("worker", LOCAL_PAIRS, 20)
    with pytest.raises(ValueError, match="candidate"):
        settings(env=env(candidate="both"))
    with pytest.raises(ValueError, match="pairs"):
        settings(env=env(pairs=-1))
    with pytest.raises(ValueError, match="turn time"):
        settings(env=URLS).move_timeout_s()


def test_a_run_plays_its_pairs_then_each_reference_and_nothing_more() -> None:
    asked: list[tuple[str, int]] = []
    given = settings(env=env(pairs=3, references=["a", "b"], reference_pairs=2))
    match = play(given, lambda against, index: asked.append((against, index)) or 0.5)
    assert asked == [(OPPONENT, 0), (OPPONENT, 1), (OPPONENT, 2), ("a", 0), ("a", 1), ("b", 0), ("b", 1)]
    assert match.pairs[-1] == ("b", 0.5)
    won = play(settings(env=env()), lambda against, index: 1.0)
    assert won.pairs == [(OPPONENT, 1.0)], "a unit: one pair, however it went"
    elo = won.elo()
    assert elo is not None
    assert elo > 1000


def test_a_seat_that_fails_ends_the_run_and_is_named(tmp_path: Path) -> None:
    def pair(against: str, index: int) -> float:
        if index == 2:
            raise SeatFailed(OPPONENT, "stopped answering")
        return 0.75

    match = play(settings(env=env(pairs=4)), pair)
    assert (len(match.pairs), match.invalid) == (2, OPPONENT)
    result = write_match(match, grader_version="t", path=tmp_path / "result.json", print_metrics=False)
    assert (result["primary"], result["failures"], result["n_items"]) == (
        {"name": "elo", "value": None},
        1,
        3,
    )
    items = json.loads((tmp_path / "items.json").read_text())
    assert check_pairs(items) == ([(OPPONENT, 0.75), (OPPONENT, 0.75)], OPPONENT)
    with pytest.raises(ValueError, match="seat"):
        SeatFailed("referee")


@pytest.mark.parametrize(
    "items",
    [
        {"schema": 1, "pairs": [], "invalid": None},
        {"schema": 2, "pairs": [{"against": "opponent", "score": 0.3}], "invalid": None},
        {"schema": 2, "pairs": [{"against": "", "score": 0.5}], "invalid": None},
        {"schema": 2, "pairs": [], "invalid": "grader"},
        {"schema": 2, "pairs": []},
    ],
)
def test_items_that_arent_a_head_to_head_runs_are_refused(items: object) -> None:
    with pytest.raises(ValueError, match=r"^items\.json"):
        check_pairs(items)


@contextmanager
def bot_server(delay_s: float, answer: object) -> Generator[str]:
    """A bot service on localhost that answers every move with `answer` after `delay_s`."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(delay_s)
            body = json.dumps({"outputs": [answer]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_bot_that_misses_its_move_deadline_loses_the_move_and_a_dead_one_fails_its_seat() -> None:
    """R6: a move's deadline is turn_ms plus TRANSPORT_ALLOWANCE_MS, the same for both seats; an
    answer after it is a lost move (None), never a failed run; an invalid answer too. A service
    that refuses the connection fails its seat."""
    given = settings(turn_ms=50, env=URLS)
    schema = {"type": "integer"}
    with bot_server(0.0, 2) as url:
        assert Bot(url, CANDIDATE, given, output_schema=schema).move({"pile": 5}) == 2
    with bot_server(0.0, "two") as url:
        assert Bot(url, CANDIDATE, given, output_schema=schema).move({"pile": 5}) is None
    with bot_server(0.25, 2) as url:  # over 50 + 20 ms
        started = time.monotonic()
        assert Bot(url, OPPONENT, given, output_schema=schema).move({"pile": 5}) is None
        assert time.monotonic() - started < 0.2, "the deadline cut it off"
    with bot_server(0.0, 2) as url:
        dead = url
    with pytest.raises(SeatFailed) as failed:
        Bot(dead, OPPONENT, given).move({"pile": 5})
    assert failed.value.seat == OPPONENT
    with pytest.raises(ValueError, match="seat"):
        Bot(dead, "referee", given)


def test_a_match_of_the_candidates_seat_failing_blames_it() -> None:
    assert Match([], invalid=CANDIDATE).elo() is None
