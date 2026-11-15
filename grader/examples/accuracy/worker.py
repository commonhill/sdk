"""Worker for the accuracy example: labels a text positive or negative by counting words from two
short lists. Deliberately naive, so there is room to improve.

    python worker.py [port]        (default 8080)
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

POSITIVE = {"good", "great", "love", "excellent", "happy", "fine", "nice"}
NEGATIVE = {"bad", "awful", "hate", "terrible", "sad", "poor", "broken"}


def label(text: str) -> str:
    words = text.lower().split()
    return (
        "positive" if sum(w in POSITIVE for w in words) >= sum(w in NEGATIVE for w in words) else "negative"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._reply(200 if self.path == "/healthz" else 404, {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        items = cast(list[str], json.loads(self.rfile.read(length))["items"])
        self._reply(200, {"outputs": [label(item) for item in items]})

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
