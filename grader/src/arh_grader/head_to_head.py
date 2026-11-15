"""A head-to-head run's referee side (since release 8; PER-448, ADR-0016).

In a head-to-head version the grader is the referee of a two-player game. The run has two bot
services serving the same worker contract, `worker` and `opponent`; the runner puts each on a
network of its own and gives the grader their URLs (WORKER_URL, OPPONENT_URL). One is the
candidate's bot, the other the opponent's (the incumbent, or an earlier incumbent): the runner
draws which, per run, and tells only the grader, in AR_HEAD_TO_HEAD (`settings()`):

    {"schema": 1, "candidate": "worker" | "opponent", "turn_ms": 100,
     "pairs": 1, "references": ["mcts-200"], "reference_pairs": 1}

The grader plays **game pairs**: two games from one opening, the candidate on each side once, and
scores each pair from the candidate's side (its points over the two games, halved: 0, 1/4, 1/2,
3/4 or 1). A run plays exactly `pairs` pairs against the opponent seat, then `reference_pairs`
against each of the grader's own reference bots in `references`; the platform starts every run
from fresh containers and decides on many runs, each a unit of its sequential test, so a run is
short (one pair, by default). `play(settings, play_pair)` runs that schedule around the project's
own `play_pair(against, index)`; `write_match(...)` writes the result and items.json:

    {"schema": 2, "pairs": [{"against": "opponent" | "<reference>", "score": 0.75}, ...],
     "invalid": null | "candidate" | "opponent"}

in play order. `invalid` names the seat that made the run invalid (its service stopped answering:
raise `SeatFailed` from `play_pair`); the platform rejects the candidate for its own, and an
opponent's is the platform's error. The result's primary value is the Elo the run's mean pair
score implies.

Every bot gets `turn_ms` per move: `Bot` asks a seat for one move with a deadline of `turn_ms`
plus TRANSPORT_ALLOWANCE_MS for the request and the response on the run's network, the same for
both seats. A move that misses it, or isn't a valid output, is None: the referee scores it as a
lost game (or move), never as a failed run.

Without AR_HEAD_TO_HEAD (`hill run` on a laptop) the settings are a local run's: the candidate in
the worker seat, LOCAL_PAIRS fixed pairs against the opponent seat (its own image, under plain
`hill run`), the turn time the caller gives.
"""

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from arh_grader.client import Client
from arh_grader.deadline import remaining
from arh_grader.result import (
    ITEMS_FILE,
    RESULT_PATH,
    Value,
    _write_atomically,  # pyright: ignore[reportPrivateUsage]
    write_result,
)

# A game pair's score: the candidate's points over the pair's two games, halved (a win 1, a draw
# 1/2): one of these. A mean score this close to 0 or 1 counts as that far from it, for its Elo.
PAIR_SCORES = (0.0, 0.25, 0.5, 0.75, 1.0)
SCORE_CLAMP = 1e-3

HEAD_TO_HEAD_ENV = "AR_HEAD_TO_HEAD"
WORKER_ENV = "WORKER_URL"
OPPONENT_ENV = "OPPONENT_URL"
WORKER_SEAT, OPPONENT_SEAT = "worker", "opponent"
OPPONENT = "opponent"  # the `against` of the pairs played against the opponent seat
CANDIDATE = "candidate"
# A move's deadline beyond the turn's time: the request and its response over the run's network.
TRANSPORT_ALLOWANCE_MS = 20
PAIRS_SCHEMA = 2
LOCAL_PAIRS = 8
MAX_PAIRS_BYTES = 1024 * 1024  # items.json, as the platform reads it


class SeatFailed(Exception):
    """A seat's bot stopped answering, so the run can't be scored: `seat` is CANDIDATE or
    OPPONENT (whose bot it was, from the referee's side)."""

    def __init__(self, seat: str, why: str = "") -> None:
        if seat not in (CANDIDATE, OPPONENT):
            raise ValueError(f"seat must be {CANDIDATE!r} or {OPPONENT!r}, got {seat!r}")
        super().__init__(f"the {seat}'s bot failed{': ' + why if why else ''}")
        self.seat = seat


@dataclass(frozen=True)
class Settings:
    """A head-to-head run's settings (see the module's docstring)."""

    candidate: str  # WORKER_SEAT or OPPONENT_SEAT
    turn_ms: int | None
    pairs: int
    references: tuple[str, ...]
    reference_pairs: int

    def candidate_url(self, env: Mapping[str, str] = os.environ) -> str:
        """The URL of the candidate's bot."""
        return env[WORKER_ENV if self.candidate == WORKER_SEAT else OPPONENT_ENV]

    def opponent_url(self, env: Mapping[str, str] = os.environ) -> str:
        """The URL of the opponent's bot."""
        return env[OPPONENT_ENV if self.candidate == WORKER_SEAT else WORKER_ENV]

    def move_timeout_s(self) -> float:
        """A move's deadline, in seconds: turn_ms plus TRANSPORT_ALLOWANCE_MS."""
        if self.turn_ms is None:
            raise ValueError("a local run's settings need the turn time: settings(turn_ms=...)")
        return (self.turn_ms + TRANSPORT_ALLOWANCE_MS) / 1000


class Bot:
    """A seat's bot service as a player: `move(item)` sends one item (`POST /run`, the worker
    contract) and returns its output, or None when the answer missed the move's deadline
    (`Settings.move_timeout_s`) or isn't a valid output (null, or not `output_schema`): a lost
    move. SeatFailed when the service failed otherwise (refused or dropped the connection, an HTTP
    error, a malformed body): the run can't be scored. `seat` is CANDIDATE or OPPONENT."""

    def __init__(
        self, url: str, seat: str, given: Settings, output_schema: Mapping[str, object] | None = None
    ) -> None:
        if seat not in (CANDIDATE, OPPONENT):
            raise ValueError(f"seat must be {CANDIDATE!r} or {OPPONENT!r}, got {seat!r}")
        self.seat = seat
        self.client = Client(url, timeout_s=given.move_timeout_s(), output_schema=output_schema)

    def move(self, item: object) -> object | None:
        batch = self.client.run([item])
        if batch.kind == "timeout":
            return None
        if batch.error is not None:
            raise SeatFailed(self.seat, batch.kind or "request failed")
        return batch.outputs[0]


def _int(raw: Mapping[str, object], key: str, low: int) -> int:
    value = raw.get(key)
    if type(value) is not int or value < low:
        raise ValueError(f"{HEAD_TO_HEAD_ENV}: {key} must be an integer >= {low}, got {value!r}")
    return value


def elo_of(score: float) -> float:
    """The Elo difference an expected `score` implies, the score kept SCORE_CLAMP from 0 and 1."""
    s = min(max(score, SCORE_CLAMP), 1 - SCORE_CLAMP)
    return 400 * math.log10(s / (1 - s))


def settings(turn_ms: int | None = None, env: Mapping[str, str] = os.environ) -> Settings:
    """The run's settings from AR_HEAD_TO_HEAD, or a local run's (`turn_ms` the caller's) without
    it. ValueError for settings that aren't the platform's shape."""
    text = env.get(HEAD_TO_HEAD_ENV)
    if not text:
        return Settings(WORKER_SEAT, turn_ms, LOCAL_PAIRS, (), 0)
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{HEAD_TO_HEAD_ENV} is not a JSON object")
    raw = cast(dict[str, object], loaded)
    if raw.get("schema") != 1:
        raise ValueError(f"{HEAD_TO_HEAD_ENV}: unknown schema {raw.get('schema')!r}")
    candidate, references = raw.get("candidate"), raw.get("references")
    if candidate not in (WORKER_SEAT, OPPONENT_SEAT):
        raise ValueError(f"{HEAD_TO_HEAD_ENV}: candidate {candidate!r} isn't a seat the platform sends")
    if not isinstance(references, list) or not all(
        isinstance(r, str) for r in cast(list[object], references)
    ):
        raise ValueError(f"{HEAD_TO_HEAD_ENV}: references must be a list of names")
    return Settings(
        candidate=str(candidate),
        turn_ms=_int(raw, "turn_ms", 1),
        pairs=_int(raw, "pairs", 0),
        references=tuple(cast(list[str], references)),
        reference_pairs=_int(raw, "reference_pairs", 0),
    )


@dataclass
class Match:
    """What a run played: its pairs as (against, score) in play order, and the seat that made it
    invalid (CANDIDATE or OPPONENT), if one did."""

    pairs: list[tuple[str, float]]
    invalid: str | None = None
    stopped: str | None = None  # why it stopped before its schedule's end (its time), if it did

    def scores(self, against: str = OPPONENT) -> list[float]:
        return [score for name, score in self.pairs if name == against]

    def elo(self) -> float | None:
        """The Elo the run's mean pair score implies; None without a pair."""
        if not self.pairs:
            return None
        return elo_of(math.fsum(score for _, score in self.pairs) / len(self.pairs))


def play(given: Settings, play_pair: Callable[[str, int], float], reserve_s: float = 30.0) -> Match:
    """The run's games (see the module's docstring): `play_pair(against, index)` plays pair
    `index` (0, 1, ... per opponent) against OPPONENT (the opponent seat) or a reference bot's
    name and returns the candidate's pair score, or raises SeatFailed. Play stops early, with the
    pairs so far, once less than `reserve_s` of the run's time is left (`remaining()`), to leave
    time to write the result; on the platform a run cut short doesn't count (the grader's error)."""
    match = Match([])

    def one(against: str, index: int) -> bool:
        left = remaining()
        if left is not None and left < reserve_s:
            match.stopped = f"{left:.0f} s of the run's time left"
            return False
        try:
            score = play_pair(against, index)
        except SeatFailed as failed:
            match.invalid = failed.seat
            return False
        if score not in PAIR_SCORES:
            raise ValueError(f"a pair score must be one of {PAIR_SCORES}, got {score!r}")
        match.pairs.append((against, score))
        return True

    for index in range(given.pairs):
        if not one(OPPONENT, index):
            return match
    for name in given.references:
        for index in range(given.reference_pairs):
            if not one(name, index):
                return match
    return match


def check_pairs(items: object) -> tuple[list[tuple[str, float]], str | None]:
    """A head-to-head run's items.json (schema 2), checked: its pairs as (against, score) and the
    invalid seat. ValueError naming the field for anything else (never a score)."""
    if not isinstance(items, dict) or set(cast(dict[object, object], items)) != {
        "schema",
        "pairs",
        "invalid",
    }:
        raise ValueError(f'{ITEMS_FILE} must be an object {{"schema": 2, "pairs": [...], "invalid": ...}}')
    fields = cast(dict[str, object], items)
    if fields["schema"] != PAIRS_SCHEMA or type(fields["schema"]) is not int:
        raise ValueError(f"{ITEMS_FILE}'s schema must be {PAIRS_SCHEMA}")
    if fields["invalid"] not in (None, CANDIDATE, OPPONENT):
        raise ValueError(f"{ITEMS_FILE}'s invalid must be null, {CANDIDATE!r} or {OPPONENT!r}")
    raw = fields["pairs"]
    if not isinstance(raw, list):
        raise ValueError(f"{ITEMS_FILE}'s pairs must be a list")
    pairs: list[tuple[str, float]] = []
    for entry in cast(list[object], raw):
        if not isinstance(entry, dict) or set(cast(dict[object, object], entry)) != {"against", "score"}:
            raise ValueError(f"{ITEMS_FILE}'s pairs must be objects of against and score")
        pair = cast(dict[str, object], entry)
        against, score = pair["against"], pair["score"]
        if not isinstance(against, str) or not against or len(against) > 40:
            raise ValueError(f"{ITEMS_FILE}: a pair's against must be a name")
        if type(score) not in (int, float) or score not in PAIR_SCORES:
            raise ValueError(f"{ITEMS_FILE}: a pair's score must be one of {PAIR_SCORES}")
        pairs.append((against, float(cast(float, score))))
    return pairs, cast(str | None, fields["invalid"])


def write_match(
    match: Match,
    *,
    grader_version: str,
    metric: str = "elo",
    secondary: Mapping[str, float] | None = None,
    timings: Mapping[str, float] | None = None,
    path: Path = RESULT_PATH,
    print_metrics: bool | None = None,
) -> dict[str, object]:
    """Write items.json (schema 2: `match`'s pairs and invalid seat), then result.json (the
    primary the run's Elo, null for an invalid run or one without a pair; failures 1 for an
    invalid run; `print_metrics` as `write_result`'s); returns the result."""
    items = {
        "schema": PAIRS_SCHEMA,
        "pairs": [{"against": against, "score": score} for against, score in match.pairs],
        "invalid": match.invalid,
    }
    check_pairs(items)
    data = (json.dumps(items, allow_nan=False, sort_keys=True) + "\n").encode()
    if len(data) > MAX_PAIRS_BYTES:
        raise ValueError(f"{ITEMS_FILE} would be over the {MAX_PAIRS_BYTES} byte limit")
    _write_atomically(path.parent / ITEMS_FILE, data)
    elo = None if match.invalid is not None else match.elo()
    failed = 0 if match.invalid is None else 1  # the pair the failed seat left unplayed
    return write_result(
        primary=Value(metric, elo),
        n_items=len(match.pairs) + failed,
        failures=failed,
        grader_version=grader_version,
        secondary=secondary,
        timings=timings,
        path=path,
        print_metrics=print_metrics,
        failure_kinds=None if match.invalid is None else {f"{match.invalid}_failed": 1},
    )


__all__ = [
    "CANDIDATE",
    "HEAD_TO_HEAD_ENV",
    "LOCAL_PAIRS",
    "OPPONENT",
    "OPPONENT_ENV",
    "OPPONENT_SEAT",
    "PAIRS_SCHEMA",
    "PAIR_SCORES",
    "TRANSPORT_ALLOWANCE_MS",
    "WORKER_ENV",
    "WORKER_SEAT",
    "Bot",
    "Match",
    "SeatFailed",
    "Settings",
    "check_pairs",
    "elo_of",
    "play",
    "settings",
    "write_match",
]
