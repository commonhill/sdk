"""Scripted HTTP workers: servers that answer each request byte for byte as a test scripts them
(a worker that trickles, cuts off or sends garbage)."""

import json
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

# A scripted HTTP peer: for each request, a list of (delay in seconds, bytes to send) pairs.
type Script = list[tuple[float, bytes]]


@dataclass
class ScriptedServer:
    url: str
    requests: list[bytes]


def _read_request(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = next(
        (
            int(line.split(b":", 1)[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        ),
        0,
    )
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


@pytest.fixture
def scripted_http() -> Iterator[Callable[[Callable[[bytes], Script]], ScriptedServer]]:
    """Start servers that answer each request (given its raw bytes) with a script, then close the
    connection: a worker that trickles, cuts off or sends garbage, byte for byte."""
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(script: Callable[[bytes], Script]) -> ScriptedServer:
        requests: list[bytes] = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                request = _read_request(self.request)
                requests.append(request)
                try:
                    for delay, data in script(request):
                        time.sleep(delay)
                        self.request.sendall(data)
                except OSError:
                    pass  # the client gave up

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return ScriptedServer(f"http://127.0.0.1:{server.server_address[1]}", requests)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def http_answer(status: bytes, body: bytes, extra: bytes = b"") -> bytes:
    """A complete HTTP/1.1 response with a JSON content type."""
    head = b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n" + extra
    return head + b"Content-Length: %d\r\n\r\n" % len(body) + body


def _echo(request: bytes) -> Script:
    items = json.loads(request.partition(b"\r\n\r\n")[2])["items"]
    return [(0, http_answer(b"200 OK", json.dumps({"outputs": items}).encode()))]


def _healthy_then(run: Callable[[bytes], Script]) -> Callable[[bytes], Script]:
    def script(request: bytes) -> Script:
        if request.startswith(b"GET /healthz "):
            return [(0, http_answer(b"200 OK", b"{}"))]
        return run(request)

    return script


@dataclass(frozen=True)
class WorkerScripts:
    """Building blocks for scripted workers: `answer(status, body)`, `echo` (a correct echo
    worker's /run) and `healthy_then(run)` (/healthz is 200, /run answers with `run`)."""

    answer: Callable[..., bytes] = http_answer
    echo: Callable[[bytes], Script] = _echo
    healthy_then: Callable[[Callable[[bytes], Script]], Callable[[bytes], Script]] = _healthy_then


@pytest.fixture(scope="session")
def worker_scripts() -> WorkerScripts:
    return WorkerScripts()
