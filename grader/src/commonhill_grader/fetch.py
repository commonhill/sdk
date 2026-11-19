"""One HTTP/1.1 request to the worker, under an absolute deadline.

A worker is hostile: it may trickle its headers or body a byte at a time, send garbage instead of
HTTP, cut a chunked body short or answer with far too much. `fetch` gives every such case the
same outcome, FetchError, within the deadline: each read waits only for the time left, and a
response finished after the deadline is refused. It never follows redirects and never decodes
the body. Error messages name what went wrong, never the response's content (nor the URL).

The deadline covers connecting, sending the request and reading the response. Resolving the
worker's name is left to the container's local resolver (Docker's embedded DNS on the internal
network), which the worker can't influence.
"""

import http.client
import io
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import cast


class FetchError(Exception):
    """The request failed; `kind` says how, as a failure kind (`commonhill_grader.result`): "timeout"
    (the deadline passed), "too_large" (a body over the cap) or "connection" (anything else:
    refused, dropped, malformed HTTP, cut short)."""

    def __init__(self, message: str, kind: str = "connection") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class _DeadlineReader(io.RawIOBase):
    """Reads from `sock`, each read waiting only until `deadline` (monotonic)."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview | bytearray) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline passed")
        self._sock.settimeout(remaining)
        return self._sock.recv_into(buffer)


class _DeadlineSocket:
    """What http.client.HTTPResponse needs of a socket: `makefile`."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def makefile(self, mode: str) -> io.BufferedReader:
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline))


def _describe(exc: BaseException) -> str:
    # Only the kind of failure: some exceptions (BadStatusLine, ...) quote what the worker sent.
    return "timed out" if isinstance(exc, TimeoutError) else type(exc).__name__


def _check_length(response: http.client.HTTPResponse, received: int) -> None:
    """The body is as long as its Content-Length says. http.client stops quietly at a connection
    closed early (even before the first byte), so this compares the count itself."""
    declared = response.getheader("Content-Length")
    if declared is None or response.chunked or response.status in (204, 304):
        return
    if not declared.strip().isdigit() or int(declared) != received:
        raise http.client.IncompleteRead(b"")


def fetch(
    url: str,
    *,
    deadline: float,
    max_bytes: int,
    method: str = "GET",
    body: bytes = b"",
    content_type: str | None = None,
) -> Response:
    """`method` `url` (http only), the whole response read by `deadline` (time.monotonic()), the
    body at most `max_bytes`. Any status is returned as it is; anything else that goes wrong is
    FetchError."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port or 80
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        head = [
            f"{method} {target} HTTP/1.1",
            f"Host: {parts.netloc}",
            "Connection: close",
            f"Content-Length: {len(body)}",
            *([f"Content-Type: {content_type}"] if content_type else []),
        ]
        request = ("\r\n".join(head) + "\r\n\r\n").encode() + body
    except ValueError:  # urlsplit's and the encoder's messages quote the URL
        raise FetchError("invalid URL") from None
    if parts.scheme != "http" or not host:
        raise FetchError("only http:// URLs with a host")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline passed")
        with socket.create_connection((host, port), timeout=remaining) as sock:
            sock.settimeout(max(deadline - time.monotonic(), 1e-3))  # all of sendall
            sock.sendall(request)
            response = http.client.HTTPResponse(
                cast(socket.socket, _DeadlineSocket(sock, deadline)), method=method
            )
            try:
                response.begin()
                data = response.read(max_bytes + 1)
                if len(data) <= max_bytes:
                    # read(amt) returns a short body without complaint; read() checks that the
                    # chunks were complete (IncompleteRead otherwise).
                    data += response.read()
                    _check_length(response, len(data))
            finally:
                response.close()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        kind = "timeout" if isinstance(exc, TimeoutError) else "connection"
        raise FetchError(_describe(exc), kind) from None
    if len(data) > max_bytes:
        raise FetchError(f"response body over {max_bytes} bytes", "too_large")
    if time.monotonic() > deadline:
        raise FetchError("timed out", "timeout")
    return Response(response.status, data)
