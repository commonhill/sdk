"""Worker for the oracle example: finds each item's hidden number by binary search over the
grader's oracle, BITS probes an item (the fewest a comparison can need in the worst case; the
average has a little room).

The oracle client is the worker's own (nothing to vendor: the grader counts the calls, so the
worker's side carries no trust): `POST {AR_ORACLE_URL}/query` with `{"session": token, "query":
...}`, answered `{"answer": ...}`, over one keep-alive connection per thread.

    AR_ORACLE_URL=http://127.0.0.1:8081 python worker.py [port]        (default 8080)
"""

import http.client
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit


class OracleClient:
    """The oracle at `url` (AR_ORACLE_URL): `ask(session, query)` returns its answer."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self.host, self.port = parts.hostname or "grader", parts.port or 80
        self.local = threading.local()

    def ask(self, session: str, query: object) -> object:
        body = json.dumps({"session": session, "query": query})
        for attempt in (1, 2):  # once more on a fresh connection if the kept one was closed
            connection = cast(http.client.HTTPConnection | None, getattr(self.local, "connection", None))
            if connection is None:
                connection = self.local.connection = http.client.HTTPConnection(
                    self.host, self.port, timeout=10
                )
            try:
                connection.request("POST", "/query", body, {"Content-Type": "application/json"})
                response = connection.getresponse()
                data = response.read()
            except (http.client.HTTPException, OSError):
                connection.close()
                self.local.connection = None
                if attempt == 2:
                    raise
                continue
            if response.status != 200:
                raise RuntimeError(f"oracle answered {response.status}")
            return cast(dict[str, object], json.loads(data))["answer"]
        raise AssertionError("unreachable")


ORACLE = OracleClient(os.environ.get("AR_ORACLE_URL", "http://grader:8081"))


def find(item: dict[str, object]) -> int:
    session, low, high = cast(str, item["oracle"]), 0, 2 ** cast(int, item["bits"]) - 1
    while low < high:
        probe = (low + high) // 2
        answer = cast(dict[str, int], ORACLE.ask(session, {"probe": probe}))
        if answer["cmp"] == 0:
            return probe
        low, high = (probe + 1, high) if answer["cmp"] > 0 else (low, probe - 1)
    return low


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._reply(200 if self.path == "/healthz" else 404, {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        items = cast(list[dict[str, object]], json.loads(self.rfile.read(length))["items"])
        self._reply(200, {"outputs": [find(item) for item in items]})

    def _reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8080).serve_forever()
