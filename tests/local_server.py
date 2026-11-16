"""Deterministic local HTTP server used by transport tests.

Everything that cannot be proven against a fake response object - connection
reuse, redirect handling, real socket timeouts, chunked bodies - is proven here
instead, against a server we control. No internet required, and the assertions do
not depend on remote latency.

The server counts accepted TCP connections, which is what makes FR-2.3 (session
reuse) checkable: comparing request durations proves nothing, counting sockets
proves exactly the claim.
"""

from __future__ import annotations

import gzip
import http.server
import socketserver
import threading
from collections.abc import Callable


class _Handler(http.server.BaseHTTPRequestHandler):
    # Response bodies are scripted per path so one server can serve every case.

    # HTTP/1.1 is required for the keep-alive assertions: BaseHTTPRequestHandler
    # defaults to 1.0, on which a response carries no length framing and the
    # connection closes after each reply - so a reuse test against a 1.0 server
    # would be measuring the fixture rather than the client.
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # silence per-request logging
        return None

    def do_GET(self) -> None:  # name fixed by BaseHTTPRequestHandler
        server = self.server
        assert isinstance(server, LocalTestServer)
        server.request_paths.append(self.path)
        handler = self._route()
        handler(server)

    def _route(self) -> Callable[[_Handler, LocalTestServer], None]:
        """Pick the responder for this path.

        A table rather than a chain of ifs: each response shape is independent, and
        an if-ladder here reads as control flow when it is really a lookup.
        """
        path = self.path
        for prefix, name in _ROUTES:
            if path.startswith(prefix):
                return getattr(self, name)
        return self._serve_unknown

    # -- responders -----------------------------------------------------------

    def _serve_slow_eof(self, server: LocalTestServer) -> None:
        # Body arrives quickly, EOF is delayed past any reasonable deadline: the
        # case where checking the deadline only after a non-empty read would
        # wrongly report success.
        self._respond(200, b"abcd")
        self.wfile.flush()
        server.delay_before_close()

    def _serve_stalled(self, server: LocalTestServer) -> None:
        # Headers then silence: exercises the socket timeout, which is a different
        # failure from the whole-request deadline.
        self.send_response(200)
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        self.wfile.write(b"x")
        self.wfile.flush()
        server.sleep_long()

    def _serve_loop(self, _server: LocalTestServer) -> None:
        # Self-redirect: proves the hop limit stops a cycle instead of hanging.
        self._redirect("/loop")

    def _serve_redirect(self, _server: LocalTestServer) -> None:
        self._redirect("/final")

    def _serve_moved_gone(self, _server: LocalTestServer) -> None:
        # A redirect to a dead resource; the final status must drive the result.
        self._redirect("/missing", permanent=True)

    def _serve_gzip(self, _server: LocalTestServer) -> None:
        payload = gzip.compress(b"z" * 5000)
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_chunked(self, _server: LocalTestServer) -> None:
        # No Content-Length at all: proves volume comes from bytes received (FR-4.2).
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(4):
            body = b"y" * 700
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _serve_error(self, _server: LocalTestServer) -> None:
        code = int(self.path.removeprefix("/error") or 500)
        self._respond(code, b"nope!")

    def _serve_payload(self, server: LocalTestServer) -> None:
        size = server.query_size(self.path)
        self._respond(200, b"a" * size)

    def _serve_unknown(self, _server: LocalTestServer) -> None:
        # Serving a body for unknown paths would hide redirect bugs: a 301 to a
        # missing resource must surface as 404, not as a successful download.
        self._respond(404, b"not found")

    # -- helpers --------------------------------------------------------------

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _redirect(self, location: str, *, permanent: bool = False) -> None:
        self.send_response(301 if permanent else 302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


# Longest prefixes first so "/error404" is not caught by a shorter match.
_ROUTES: tuple[tuple[str, str], ...] = (
    ("/slow-eof", "_serve_slow_eof"),
    ("/stalled", "_serve_stalled"),
    ("/loop", "_serve_loop"),
    ("/redirect", "_serve_redirect"),
    ("/moved-gone", "_serve_moved_gone"),
    ("/gzip", "_serve_gzip"),
    ("/chunked", "_serve_chunked"),
    ("/error", "_serve_error"),
    ("/payload", "_serve_payload"),
    ("/final", "_serve_payload"),
)


class LocalTestServer(socketserver.ThreadingTCPServer):
    """Loopback server exposing connection and request counts."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.accepted_connections = 0
        self.request_paths: list[str] = []
        self._thread: threading.Thread | None = None

    # -- introspection used by assertions ------------------------------------

    def query_size(self, path: str) -> int:
        try:
            return int(path.rsplit("=", 1)[1]) if "=" in path else 4096
        except ValueError:
            return 4096

    def delay_before_close(self) -> None:
        import time

        time.sleep(1.5)

    def sleep_long(self) -> None:
        import time

        time.sleep(30)

    def server_bind(self) -> None:
        super().server_bind()
        self.port = self.server_address[1]  # type: ignore[attr-defined]

    def get_request(self) -> tuple[object, object]:  # type: ignore[override]
        conn, addr = super().get_request()
        self.accepted_connections += 1
        return conn, addr

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> LocalTestServer:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
