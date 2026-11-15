"""The oracle (since release 5, PER-363): an endpoint the grader serves and the worker calls
during a run, about a hidden instance, counted by the grader.

The grader opens a `Session` per item (`Oracle.session(instance, key=...)`) and sends the item with the
session's token; the worker asks `POST {AR_ORACLE_URL}/query` with
`{"session": token, "query": ...}` and gets `{"answer": ...}`, the author's
`answer(instance, query)`. The grader serves it from its own process (on the compose network it
already shares with the worker, `http://grader:8081`), so its counts are exact and the worker can't
lower them without making the calls: the count is the metric of a query-complexity project (fewest
probes, tests, backend hits).

What counts (`Session.calls`): a request with a well-formed body that names an open session under
its cap, counted under the session's lock before `answer` runs, whatever it answers (an invalid
query, `ValueError`, is answered 400 and counted). Nothing else does: a malformed or oversized
body, an unknown session (404), a session already closed (410: its item's output was read), a
call over the session's cap (429: the session is `over_cap`, and the Client fails its item as
`oracle_cap`). `query_bytes` counts each counted query's canonical JSON (compact, keys sorted), so
neither the worker's formatting nor its HTTP library changes it; `answer_bytes` the answers sent,
the grader's own encoding. One session's calls are answered one at a time; `answer` should be a
pure function of the instance and the query, so that the order of calls made at once doesn't
matter. Closing a session never waits for an answer: a call counted before the close stays
counted, and an answer still being computed then is never sent (410).

A session's token is HMAC-SHA256 of its key (the item's index or id, unique in the run, which the
grader gives) under the run's secret seed (`arh_grader.seed`), URL-safe: the same in every run of
the seed whatever order the sessions are opened in, so a deterministic worker that sees it stays
deterministic, and unpredictable without the seed, which the worker never has, so no token tells
anything of another. Tokens appear in no result and no log.

Nothing a worker sends, nothing an answer holds and no exception's text is ever printed or sent:
error bodies are fixed, an `answer` that raises anything but `ValueError` (or returns something
that isn't JSON or is over `max_answer_bytes`) is the author's bug, answered as an invalid query
and counted in `Oracle.handler_errors`. Requests run at once on at most `max_connections`
connections (more wait to be accepted); a connection idle for `idle_timeout_s` is closed.
"""

import base64
import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, cast

from arh_grader.client import loads
from arh_grader.seed import seed

ORACLE_ENV = "AR_ORACLE_URL"  # the worker's: compose.yaml sets it to ORACLE_URL
ORACLE_PORT = 8081
ORACLE_URL = f"http://grader:{ORACLE_PORT}"
QUERY_PATH = "/query"
MAX_QUERY_BYTES = 64 * 1024  # a request's body
MAX_ANSWER_BYTES = 64 * 1024  # an answer's encoding
MAX_CONNECTIONS = 32
IDLE_TIMEOUT_S = 30.0
TOKEN_LABEL = "arh-oracle-session"
MAX_LENGTH_DIGITS = 12  # a Content-Length longer than this is over any cap

Answer = Callable[[Any, object], object]

_INVALID = (400, b'{"error": "invalid query"}')
_MALFORMED = (400, b'{"error": "malformed request"}')
_UNKNOWN = (404, b'{"error": "unknown session"}')
_CLOSED = (410, b'{"error": "session closed"}')
_NO_LENGTH = (411, b'{"error": "Content-Length required"}')
_TOO_LARGE = (413, b'{"error": "too large"}')
_CAP = (429, b'{"error": "cap"}')
_NO_ANSWER = object()  # `answer` raised


def canonical(value: object) -> bytes:
    """`value`'s canonical JSON: compact, keys sorted, UTF-8 (ValueError/TypeError if it isn't
    JSON, NaN and the infinities included)."""
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode()


def token(key: str | int) -> str:
    """The token of the session keyed `key`: HMAC-SHA256(seed, TOKEN_LABEL and the key), URL-safe
    base64."""
    digest = hmac.new(seed().encode(), f"{TOKEN_LABEL}:{key}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class Session:
    """One item's calls: its `token` (sent to the worker with the item), and once it is closed its
    final counts. The instance is the grader's alone: it is in no repr."""

    def __init__(self, token: str, instance: object, max_calls: int) -> None:
        self.token = token
        self.max_calls = max_calls
        self.calls = 0
        self.query_bytes = 0
        self.answer_bytes = 0
        self.over_cap = False  # a call came over max_calls: the item fails (`oracle_cap`)
        self.closed = False
        self._instance = instance
        self._lock = threading.Lock()  # the counts and `closed`, never held while answering
        self._answering = threading.Lock()  # one answer at a time

    def close(self) -> None:
        """No call counts after this, and no answer is sent (410). It never waits for one being
        computed: that call was counted when it came."""
        with self._lock:
            self.closed = True

    def call(self, query: object, answer: Answer, max_answer_bytes: int) -> tuple[tuple[int, bytes], bool]:
        """One call with `query`, answered by `answer` about the instance: the reply, and whether
        `answer` failed (the author's bug: it raised, but not ValueError, or its answer isn't JSON
        or is over `max_answer_bytes`). The call counts before `answer` runs; if the session is
        closed by the time it has answered, the answer is dropped and the reply is 410."""
        with self._lock:
            if self.closed:
                return _CLOSED, False
            if self.over_cap or self.calls >= self.max_calls:
                self.over_cap = True
                return _CAP, False
            self.calls += 1
            self.query_bytes += len(canonical(query))
        reply, failed, encoded = _INVALID, False, b""
        with self._answering:
            try:
                answered = answer(self._instance, query)
            except ValueError:  # an invalid query
                answered = _NO_ANSWER
            except Exception:
                answered, failed = _NO_ANSWER, True
            if answered is not _NO_ANSWER:
                try:
                    encoded = canonical(answered)
                except (ValueError, TypeError, RecursionError):
                    encoded = b""  # not JSON (canonical JSON is never empty)
                failed = not encoded or len(encoded) > max_answer_bytes
                if not failed:
                    reply = (200, b'{"answer":' + encoded + b"}")
        with self._lock:
            if self.closed:
                return _CLOSED, failed
            if reply[0] == 200:
                self.answer_bytes += len(encoded)
        return reply, failed

    def __repr__(self) -> str:
        return f"Session(calls={self.calls}, over_cap={self.over_cap}, closed={self.closed})"


class Oracle:
    """`answer(instance, query)`, served on `port` (ORACLE_PORT; 0 picks a free one) while the
    Oracle is entered (`with Oracle(...) as oracle:`), and every session closed on exit.
    `max_calls` is each session's cap unless `session()` gives its own."""

    def __init__(
        self,
        answer: Answer,
        *,
        max_calls: int,
        max_query_bytes: int = MAX_QUERY_BYTES,
        max_answer_bytes: int = MAX_ANSWER_BYTES,
        host: str = "0.0.0.0",
        port: int = ORACLE_PORT,
        max_connections: int = MAX_CONNECTIONS,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
    ) -> None:
        if max_calls < 1 or max_query_bytes < 1 or max_answer_bytes < 1 or max_connections < 1:
            raise ValueError("max_calls, max_query_bytes, max_answer_bytes and max_connections must be >= 1")
        if idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s must be > 0")
        self.answer = answer
        self.max_calls = max_calls
        self.max_query_bytes = max_query_bytes
        self.max_answer_bytes = max_answer_bytes
        self.address = (host, port)
        self.max_connections = max_connections
        self.idle_timeout_s = idle_timeout_s
        self.handler_errors = 0  # answers that raised (not ValueError), weren't JSON, or were too large
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The port it listens on (the one picked, with port 0); only while entered."""
        if self._server is None:
            raise RuntimeError("the oracle isn't serving")
        return self._server.server_address[1]

    def session(self, instance: object, *, key: str | int, max_calls: int | None = None) -> Session:
        """A new open session about `instance`, named by `key`'s token (`token`): give each item's
        index or id, the same in every run whatever the order, unique in it (ValueError if it
        isn't)."""
        cap = self.max_calls if max_calls is None else max_calls
        if cap < 1:
            raise ValueError("max_calls must be >= 1")
        name = token(key)
        with self._lock:
            if name in self._sessions:
                raise ValueError("this key already has a session in this run")
            session = Session(name, instance, cap)
            self._sessions[name] = session
        return session

    def __enter__(self) -> "Oracle":
        if self._server is not None:
            raise RuntimeError("the oracle is already serving")
        server = _Server(self.address, _Handler, self)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="arh-oracle", daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close()
        server, self._server = self._server, None
        if server is not None:
            server.closing.set()
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def reply(self, body: bytes) -> tuple[int, bytes]:
        """The status and body the oracle answers a request's `body` with (already within
        max_query_bytes)."""
        try:
            request = loads(body)
        except (ValueError, RecursionError, UnicodeDecodeError):
            return _MALFORMED
        if not isinstance(request, dict) or set(cast(dict[str, object], request)) != {"session", "query"}:
            return _MALFORMED
        token, query = cast(dict[str, object], request)["session"], cast(dict[str, object], request)["query"]
        if not isinstance(token, str):
            return _MALFORMED
        with self._lock:
            session = self._sessions.get(token)
        if session is None:
            return _UNKNOWN
        reply, failed = session.call(query, self.answer, self.max_answer_bytes)
        if failed:
            with self._lock:
                self.handler_errors += 1
        return reply


class _Server(ThreadingHTTPServer):
    """A thread per connection, at most the oracle's max_connections at once: past that, accepted
    connections wait here for one to end (the shutdown flag ends the wait)."""

    daemon_threads = True
    block_on_close = False

    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], oracle: Oracle
    ) -> None:
        self.oracle = oracle
        self.slots = threading.BoundedSemaphore(oracle.max_connections)
        self.closing = threading.Event()
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        while not self.slots.acquire(timeout=0.1):
            if self.closing.is_set():
                self.shutdown_request(request)
                return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # never print what a worker's connection did


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    # The head and the body are two writes: without TCP_NODELAY the body waits for the client's
    # delayed ACK of the head (40 ms or more a call).
    disable_nagle_algorithm = True
    error_message_format = '{"error": "bad request"}'
    error_content_type = "application/json"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._oracle().idle_timeout_s)

    def _oracle(self) -> Oracle:
        return cast(_Server, self.server).oracle

    def do_POST(self) -> None:
        oracle = self._oracle()
        if self.path != QUERY_PATH:
            self._reply((404, b'{"error": "not found"}'), close=True)
            return
        if "Transfer-Encoding" in self.headers:
            self._reply(_NO_LENGTH, close=True)
            return
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            self._reply(_NO_LENGTH if not lengths else _MALFORMED, close=True)
            return
        text = lengths[0].strip()
        if not text.isascii() or not text.isdigit():
            self._reply(_MALFORMED, close=True)
            return
        if len(text) > MAX_LENGTH_DIGITS:  # before int(), which refuses very long digit strings
            self._reply(_TOO_LARGE, close=True)
            return
        length = int(text)
        if length > oracle.max_query_bytes:
            self._reply(_TOO_LARGE, close=True)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            return
        self._reply(oracle.reply(body))

    def _reply(self, reply: tuple[int, bytes], close: bool = False) -> None:
        status, body = reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # nothing about the worker's requests is printed
