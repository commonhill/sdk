"""The oracle (release 5, PER-363): the grader's counts of the worker's calls are exact, whatever
the worker sends and however many calls run at once; only a well-formed call to an open session
under its cap counts; nothing the worker sends or the instance holds is printed or put in the
result; and the Client closes each batch's sessions as soon as the batch is answered."""

import base64
import http.client
import json
import random
import socket
import string
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from arh_grader import Client, Oracle, Session, Value, write_result
from arh_grader.oracle import canonical

CANARY = "oracle-heldout-canary-7c2e"
CANARY_NUMBER = 918273645


def compare(hidden: int, query: object) -> dict[str, int]:
    probe = cast(dict[str, object], query).get("probe") if isinstance(query, dict) else None
    if not isinstance(probe, int) or isinstance(probe, bool):
        raise ValueError("a probe is an integer")
    return {"cmp": (hidden > probe) - (hidden < probe)}


@pytest.fixture
def serve() -> Iterator[Callable[..., Oracle]]:
    """Enter Oracles on free local ports; exit them after the test."""
    entered: list[Oracle] = []

    def start(answer: Callable[[Any, object], object] = compare, **options: Any) -> Oracle:
        oracle = Oracle(answer, host="127.0.0.1", port=0, **({"max_calls": 100} | options))
        entered.append(oracle.__enter__())
        return oracle

    yield start
    for oracle in entered:
        oracle.__exit__(None, None, None)


def post(
    oracle: Oracle,
    body: bytes,
    headers: dict[str, str] | None = None,
    path: str = "/query",
    connection: http.client.HTTPConnection | None = None,
) -> tuple[int, object]:
    conn = connection or http.client.HTTPConnection("127.0.0.1", oracle.port, timeout=10)
    conn.request("POST", path, body, {"Content-Type": "application/json"} | (headers or {}))
    response = conn.getresponse()
    data = response.read()
    if connection is None:
        conn.close()
    return response.status, json.loads(data)


def ask(oracle: Oracle, session: Session | str, query: object, **kwargs: Any) -> tuple[int, object]:
    token = session if isinstance(session, str) else session.token
    return post(oracle, json.dumps({"session": token, "query": query}).encode(), **kwargs)


def raw(oracle: Oracle, request: bytes) -> bytes:
    """`request` as sent, and everything the oracle answers until it closes."""
    with socket.create_connection(("127.0.0.1", oracle.port), timeout=10) as sock:
        sock.sendall(request)
        chunks: list[bytes] = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def test_each_call_is_counted_on_its_own_session_and_answered_about_its_instance(
    serve: Callable[..., Oracle],
) -> None:
    oracle = serve()
    low, high = oracle.session(10, key="low"), oracle.session(1000, key="high")
    assert [ask(oracle, low, {"probe": p}) for p in (5, 10, 15)] == [
        (200, {"answer": {"cmp": 1}}),
        (200, {"answer": {"cmp": 0}}),
        (200, {"answer": {"cmp": -1}}),
    ]
    assert ask(oracle, high, {"probe": 10}) == (200, {"answer": {"cmp": 1}})
    assert (low.calls, high.calls) == (3, 1)


def test_calls_at_once_are_all_counted_exactly(serve: Callable[..., Oracle]) -> None:
    """16 threads, each 40 calls on its own keep-alive connection, half to each session."""
    oracle = serve(max_calls=10_000)
    sessions = [oracle.session(7, key=0), oracle.session(8, key=1)]
    statuses: list[int] = []
    lock = threading.Lock()

    def calls(index: int) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", oracle.port, timeout=10)
        for probe in range(40):
            status, _ = ask(oracle, sessions[index % 2], {"probe": probe}, connection=connection)
            with lock:
                statuses.append(status)
        connection.close()

    threads = [threading.Thread(target=calls, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses == [200] * 640
    assert [s.calls for s in sessions] == [320, 320]


@pytest.mark.parametrize(
    ("body", "headers", "path", "status"),
    [
        (b"{not json", None, "/query", 400),
        (b'{"session": "x", "query": NaN}', None, "/query", 400),
        (b'["session", "query"]', None, "/query", 400),
        (b'{"query": {"probe": 1}}', None, "/query", 400),
        (b'{"session": 1, "query": {"probe": 1}}', None, "/query", 400),
        (b"\xff\xfe", None, "/query", 400),
        (b'{"session": "%s", "query": {"probe": 1}}', None, "/other", 404),
    ],
    ids=["not-json", "nan", "not-object", "no-session", "token-not-string", "not-utf8", "other-path"],
)
def test_a_malformed_request_is_refused_and_not_counted(
    serve: Callable[..., Oracle], body: bytes, headers: dict[str, str] | None, path: str, status: int
) -> None:
    oracle = serve()
    session = oracle.session(5, key=0)
    got, _ = post(oracle, body.replace(b"%s", session.token.encode()), headers, path)
    assert (got, session.calls, session.query_bytes) == (status, 0, 0)
    assert ask(oracle, session, {"probe": 1})[0] == 200  # the session still works
    assert session.calls == 1


def test_an_unknown_session_is_404_and_counts_nowhere(serve: Callable[..., Oracle]) -> None:
    oracle = serve()
    session = oracle.session(5, key=0)
    other = session.token[:-1] + ("A" if session.token[-1] != "A" else "B")  # one character off
    assert ask(oracle, other, {"probe": 1}) == (404, {"error": "unknown session"})
    assert session.calls == 0


def test_bodies_without_a_length_or_over_the_cap_are_refused_unread(serve: Callable[..., Oracle]) -> None:
    oracle = serve(max_query_bytes=100)
    session = oracle.session(5, key=0)
    query = json.dumps({"session": session.token, "query": {"probe": 1, "pad": "x" * 200}}).encode()
    over = raw(oracle, b"POST /query HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(query) + query)
    chunked = raw(  # a Content-Length beside it is never trusted
        oracle,
        b"POST /query HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n",
    )
    missing = raw(oracle, b"POST /query HTTP/1.1\r\n\r\n")
    twice = raw(oracle, b"POST /query HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
    signed = raw(oracle, b"POST /query HTTP/1.1\r\nContent-Length: +2\r\n\r\n{}")
    long = raw(oracle, b"POST /query HTTP/1.1\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n{}")
    assert [r.split(b" ", 2)[1] for r in (over, chunked, missing, twice, signed, long)] == [
        b"413",
        b"411",
        b"411",
        b"400",
        b"400",
        b"413",  # 5,000 digits: refused before int() (SolReviewer363's R5)
    ]
    assert session.calls == 0
    assert ask(oracle, session, {"probe": 1})[0] == 200  # the server serves on


def test_an_invalid_query_is_counted_and_the_authors_bugs_are_counted_apart(
    serve: Callable[..., Oracle],
) -> None:
    def answer(instance: str, query: object) -> object:
        match query:
            case "invalid":
                raise ValueError(instance)
            case "bug":
                return cast(dict[str, object], {})[instance]
            case "not-json":
                return {instance}
            case "huge":
                return instance * 100
            case _:
                return instance

    oracle = serve(answer, max_answer_bytes=200)
    session = oracle.session(CANARY, key=0)
    replies = [ask(oracle, session, query) for query in ("invalid", "bug", "not-json", "huge", "ok")]
    assert replies == [(400, {"error": "invalid query"})] * 4 + [(200, {"answer": CANARY})]
    assert (session.calls, oracle.handler_errors) == (5, 3)
    assert session.answer_bytes == len(canonical(CANARY))  # only answers sent are counted


def test_a_session_over_its_cap_is_refused_from_then_on_and_counts_only_its_answered_calls(
    serve: Callable[..., Oracle],
) -> None:
    oracle = serve(max_calls=3)
    capped, wider = oracle.session(5, key="capped"), oracle.session(5, key="wider", max_calls=4)
    assert [ask(oracle, capped, {"probe": p})[0] for p in range(5)] == [200, 200, 200, 429, 429]
    assert [ask(oracle, wider, {"probe": p})[0] for p in range(5)] == [200, 200, 200, 200, 429]
    assert (capped.calls, capped.over_cap, wider.calls, wider.over_cap) == (3, True, 4, True)


def test_closing_never_waits_for_an_answer_and_drops_it(serve: Callable[..., Oracle]) -> None:
    """A call counts when it comes: closing its session while its answer is being computed
    returns at once, the count stays, and the answer is never sent (SolReviewer363's R3)."""
    answering, release = threading.Event(), threading.Event()

    def slow(instance: int, query: object) -> object:
        answering.set()
        release.wait(10)
        return instance

    oracle = serve(slow)
    session = oracle.session(3, key=0)
    replies: list[tuple[int, object]] = []
    call = threading.Thread(target=lambda: replies.append(ask(oracle, session, "q")))
    call.start()
    assert answering.wait(10)
    started = time.monotonic()
    session.close()
    assert time.monotonic() - started < 0.5
    assert (session.closed, session.calls) == (True, 1)
    release.set()
    call.join()
    assert replies == [(410, {"error": "session closed"})]
    assert (session.calls, session.answer_bytes) == (1, 0)
    assert ask(oracle, session, "q") == (410, {"error": "session closed"})
    assert session.calls == 1


def test_a_slow_answer_never_holds_the_client_past_its_timeout(
    serve: Callable[..., Oracle], worker: Callable[..., "Worker"]
) -> None:
    """The worker answers its batch while a call it made without waiting is still being answered:
    the Client returns at its own pace, the answer still blocked, the call counted, its answer
    dropped. (Before SolReviewer363's R3 fix, closing waited for the answer: 30 s here.)"""
    answering, release = threading.Event(), threading.Event()

    def slow(instance: int, query: object) -> object:
        answering.set()
        release.wait(30)
        return instance

    oracle = serve(slow)
    replies: list[tuple[int, object]] = []

    def behave(item: dict[str, Any], ask_oracle: Callable[..., object]) -> object:
        threading.Thread(target=lambda: replies.append(ask(oracle, item["oracle"], "q")), daemon=True).start()
        answering.wait(10)  # the call is in the answer
        return 1

    session = oracle.session(1, key=0)
    client = Client(worker(oracle, behave).url, timeout_s=15.0)
    started = time.monotonic()
    batch = client.run([{"oracle": session.token}], sessions=[session])
    assert time.monotonic() - started < 15.0
    assert answering.is_set()
    assert not release.is_set()  # the answer is still being computed
    assert (batch.outputs, session.closed, session.calls) == ([1], True, 1)
    release.set()
    for _ in range(100):
        if replies:
            break
        time.sleep(0.05)
    assert replies == [(410, {"error": "session closed"})]


def test_bytes_are_counted_canonically_whatever_the_worker_sends(serve: Callable[..., Oracle]) -> None:
    def answer(instance: int, query: object) -> object:
        return {"z": 1, "a": [instance, "é"]}

    oracle = serve(answer)
    session = oracle.session(2, key=0)
    tight = json.dumps({"session": session.token, "query": {"b": 1, "a": "é"}}, separators=(",", ":"))
    loose = '{ "query" : {\n "a" : "\\u00e9" , "b" : 1.0e0 } ,\n "session" : "TOKEN" }'
    loose = loose.replace("TOKEN", session.token)
    assert post(oracle, tight.encode())[0] == post(oracle, loose.encode())[0] == 200
    assert session.query_bytes == len(b'{"a":"\xc3\xa9","b":1}') + len(b'{"a":"\xc3\xa9","b":1.0}')
    assert session.answer_bytes == 2 * len('{"a":[2,"é"],"z":1}'.encode())


def tokens_of(seed: str, count: int, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("AR_SEED", seed)
    oracle = Oracle(compare, max_calls=1)
    return [oracle.session(CANARY_NUMBER, key=i).token for i in range(count)]


def untemper(value: int) -> int:
    """MT19937's output tempering, undone (SolReviewer363's R1 probe)."""

    def right(value: int, shift: int) -> int:
        estimate = value
        for _ in range(6):
            estimate = value ^ (estimate >> shift)
        return estimate

    def left(value: int, shift: int, mask: int) -> int:
        estimate = value
        for _ in range(6):
            estimate = value ^ ((estimate << shift) & mask)
        return estimate & 0xFFFFFFFF

    return right(left(left(right(value, 18), 15, 0xEFC60000), 7, 0x9D2C5680), 11)


def test_tokens_follow_the_seed_so_runs_of_it_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker sees its sessions' tokens, so a run's must be its seed's alone (SolReviewer363's
    round 2: random tokens moved a token-dependent worker's count between same-seed runs)."""
    first = tokens_of("seed-a", 3, monkeypatch)
    assert tokens_of("seed-a", 3, monkeypatch) == first
    assert set(tokens_of("seed-b", 3, monkeypatch)).isdisjoint(first)
    assert len(set(first)) == 3
    assert all(len(t) == 43 and set(t) <= set(string.ascii_letters + string.digits + "-_") for t in first)
    assert str(CANARY_NUMBER) not in repr(Oracle(compare, max_calls=1).session(CANARY_NUMBER, key=0))


def test_tokens_follow_the_key_not_the_order_sessions_are_opened_in(
    monkeypatch: pytest.MonkeyPatch, worker: Callable[..., "Worker"]
) -> None:
    """A grader may open its sessions in any order, at once even: an item's token is its key's
    (SolReviewer363's round 3: indices taken in lock order gave 0 versus 1). Opened in opposite
    orders, the items get the same tokens and the token-reading worker the same counts; a key
    used twice is refused."""
    monkeypatch.setenv("AR_SEED", "one-seed")
    hidden = {"a": 3, "b": 700, "c": 512, "d": 1}

    def behave(item: dict[str, Any], ask_oracle: Callable[..., object]) -> object:
        for _ in range(sum(map(ord, item["oracle"])) % 5):  # a call or more, as the token says
            ask_oracle(item["oracle"], {"probe": 0})
        return search(item, ask_oracle)

    runs: list[tuple[dict[str, str], dict[str, int]]] = []
    for order in (list(hidden), list(reversed(hidden))):
        with Oracle(compare, max_calls=100, host="127.0.0.1", port=0) as oracle:
            opened = {key: oracle.session(hidden[key], key=key) for key in order}
            with pytest.raises(ValueError, match="already has a session"):
                oracle.session(0, key=order[0])
            sessions = [opened[key] for key in hidden]
            Client(worker(oracle, behave).url).evaluate(
                [{"oracle": s.token} for s in sessions], 2, sessions=sessions
            )
        runs.append(({k: s.token for k, s in opened.items()}, {k: s.calls for k, s in opened.items()}))
    assert runs[0] == runs[1]


def test_tokens_shown_dont_give_away_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """SolReviewer363's R1 probe: 624 words of shown tokens rebuild a Mersenne Twister's state
    and predict its next outputs. HMAC tokens give it nothing to rebuild: the prediction misses."""
    tokens = tokens_of("reviewer-secret-seed", 82, monkeypatch)
    words = [
        int.from_bytes(raw[i : i + 4], "little")
        for raw in (base64.urlsafe_b64decode(t + "=") for t in tokens[:78])
        for i in range(0, 32, 4)
    ]
    clone = random.Random()
    clone.setstate((3, (*map(untemper, words), 624), None))
    predicted = [clone.getrandbits(256).to_bytes(32, "little") for _ in range(4)]
    assert [base64.urlsafe_b64decode(t + "=") for t in tokens[78:]] != predicted


def test_connections_past_the_limit_wait_for_one_to_end(serve: Callable[..., Oracle]) -> None:
    oracle = serve(max_connections=2)
    session = oracle.session(5, key=0)
    held = [http.client.HTTPConnection("127.0.0.1", oracle.port, timeout=10) for _ in range(2)]
    for connection in held:
        assert ask(oracle, session, {"probe": 1}, connection=connection)[0] == 200
    replies: list[tuple[int, object]] = []
    third = threading.Thread(target=lambda: replies.append(ask(oracle, session, {"probe": 2})))
    third.start()
    third.join(0.5)
    assert third.is_alive()  # accepted by the kernel, not yet served
    assert replies == []
    held[0].close()
    third.join(10)
    assert replies == [(200, {"answer": {"cmp": 1}})]
    held[1].close()
    assert session.calls == 3


def test_an_idle_connection_is_closed(serve: Callable[..., Oracle]) -> None:
    oracle = serve(idle_timeout_s=0.2)
    with socket.create_connection(("127.0.0.1", oracle.port), timeout=10) as sock:
        started = time.monotonic()
        assert sock.recv(1) == b""
        assert time.monotonic() - started < 5


def test_leaving_closes_every_session_and_stops_listening() -> None:
    oracle = Oracle(compare, max_calls=5, host="127.0.0.1", port=0)
    with oracle:
        port = oracle.port
        session = oracle.session(1, key=0)
        assert ask(oracle, session, {"probe": 1})[0] == 200
    assert session.closed
    assert session.calls == 1
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
    with pytest.raises(RuntimeError):
        _ = oracle.port


@pytest.mark.parametrize(
    "options",
    [{"max_calls": 0}, {"max_calls": 1, "max_query_bytes": 0}, {"max_calls": 1, "idle_timeout_s": 0}],
)
def test_caps_must_be_positive(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be"):
        Oracle(compare, **options)


class Worker:
    """A worker on a free port whose /run calls `behave(item, ask)` for each item, `ask(token,
    query)` asking the oracle at `oracle_port`."""

    def __init__(
        self, oracle: Oracle, behave: Callable[[dict[str, Any], Callable[..., object]], object]
    ) -> None:
        def ask_oracle(token: str, query: object) -> object:
            status, body = ask(oracle, token, query)
            return cast(dict[str, object], body).get("answer") if status == 200 else None

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                items = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["items"]
                data = json.dumps({"outputs": [behave(item, ask_oracle) for item in items]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def worker() -> Iterator[Callable[..., Worker]]:
    started: list[Worker] = []

    def start(oracle: Oracle, behave: Callable[[dict[str, Any], Callable[..., object]], object]) -> Worker:
        started.append(Worker(oracle, behave))
        return started[-1]

    yield start
    for one in started:
        one.close()


def search(item: dict[str, Any], ask_oracle: Callable[..., object]) -> object:
    low, high = 0, 1023
    while low < high:
        probe = (low + high) // 2
        answer = ask_oracle(item["oracle"], {"probe": probe})
        if answer is None:  # refused (over the cap): give up with a guess
            return low
        cmp = cast(dict[str, int], answer)["cmp"]
        if cmp == 0:
            return probe
        low, high = (probe + 1, high) if cmp > 0 else (low, probe - 1)
    return low


def test_the_client_closes_each_batch_and_an_item_over_its_cap_fails_as_oracle_cap(
    serve: Callable[..., Oracle], worker: Callable[..., Worker]
) -> None:
    oracle = serve(max_calls=10)
    late: list[int] = []

    def behave(item: dict[str, Any], ask_oracle: Callable[..., object]) -> object:
        if item["early"]:  # asks the session of an item of an earlier batch: closed by now
            late.append(ask(oracle, item["early"], {"probe": 0})[0])
        return search(item, ask_oracle)

    hidden = [0, 1023, 500, 7]
    sessions = [oracle.session(h, key=i, max_calls=9 if h == 1023 else None) for i, h in enumerate(hidden)]
    items = [
        {"oracle": s.token, "early": sessions[0].token if i >= 2 else None} for i, s in enumerate(sessions)
    ]
    evaluation = Client(worker(oracle, behave).url).evaluate(items, 2, sessions=sessions)
    assert evaluation.outputs == [0, None, 500, 7]  # 1023 needs 10 probes: over its 9
    assert evaluation.failure_kinds() == {"oracle_cap": 1}
    assert all(s.closed for s in sessions)
    assert late == [410, 410]
    assert [s.calls for s in sessions] == [9, 9, 9, 7]
    assert sessions[1].over_cap


def test_a_failed_batch_closes_its_sessions(
    serve: Callable[..., Oracle], worker: Callable[..., Worker]
) -> None:
    oracle = serve()
    after: list[int] = []
    done = threading.Event()

    def behave(item: dict[str, Any], ask_oracle: Callable[..., object]) -> object:
        ask_oracle(item["oracle"], {"probe": 1})
        time.sleep(1.0)  # past the batch's timeout
        after.append(ask(oracle, item["oracle"], {"probe": 2})[0])
        done.set()
        return 1

    session = oracle.session(1, key=0)
    batch = Client(worker(oracle, behave).url, timeout_s=0.3).run(
        [{"oracle": session.token}], sessions=[session]
    )
    assert (batch.outputs, batch.kind, session.closed) == ([None], "timeout", True)
    assert done.wait(10)
    assert (after, session.calls) == ([410], 1)


def test_sessions_must_line_up_with_the_items_and_exclude_warmup(serve: Callable[..., Oracle]) -> None:
    oracle = serve()
    sessions = [oracle.session(1, key=0)]
    client = Client("http://127.0.0.1:9")
    with pytest.raises(ValueError, match="one per item"):
        client.run([1, 2], sessions=sessions)
    with pytest.raises(ValueError, match="one per item"):
        client.evaluate([1, 2], 1, sessions=sessions)
    with pytest.raises(ValueError, match="without warmup"):
        client.evaluate([1], 1, warmup=1, sessions=sessions)


def test_no_canary_of_the_instance_or_its_answers_leaves_the_grader(
    serve: Callable[..., Oracle],
    worker: Callable[..., Worker],
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The instances hold canaries; the worker echoes every answer and error it gets into its
    outputs, sends hostile requests, and makes the handler fail. The result, items.json and the
    grader's output hold none of it: only counts."""

    def answer(instance: dict[str, object], query: object) -> object:
        if query == "bug":
            raise KeyError(instance["secret"])
        if query == "invalid":
            raise ValueError(instance["secret"])
        return instance

    oracle = serve(answer, max_calls=7, max_query_bytes=200)

    def behave(item: dict[str, Any], ask_oracle: Callable[..., object]) -> object:
        echoes = [ask(oracle, item["oracle"], query) for query in ("ok", "bug", "invalid")]
        echoes.append(
            post(oracle, b'{"session": "' + item["oracle"].encode() + b'", "query": "' + b"x" * 300 + b'"}')
        )
        echoes += [ask(oracle, item["oracle"], "ok") for _ in range(4)]
        return json.dumps(echoes)

    instances = [{"secret": CANARY, "number": CANARY_NUMBER + i} for i in range(3)]
    sessions = [oracle.session(instance, key=i) for i, instance in enumerate(instances)]
    evaluation = Client(worker(oracle, behave).url, output_schema={"type": "string"}).evaluate(
        [{"oracle": s.token} for s in sessions], 3, sessions=sessions
    )
    assert evaluation.failures == 0
    calls = [float(s.calls) for s in sessions]
    assert calls == [7.0, 7.0, 7.0]  # the oversized body isn't a call
    result = tmp_path / "result.json"
    write_result(
        primary=Value("calls", sum(calls) / 3),
        n_items=3,
        failures=0,
        secondary={"query_bytes": float(sum(s.query_bytes for s in sessions))},
        item_scores=calls,
        grader_version="canary-1",
        path=result,
    )
    written = result.read_text() + (tmp_path / "items.json").read_text()
    out, err = capfd.readouterr()
    for text in (written, out, err):
        assert CANARY not in text
        assert not any(session.token in text for session in sessions), "a token in a result or a log"
        assert all(str(CANARY_NUMBER + i) not in text for i in range(3))
