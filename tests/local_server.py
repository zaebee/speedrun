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
from collections.abc import Iterator


class _Handler(http.server.BaseHTTPRequestHandler):
    # Response bodies are scripted per path so one server can serve every case.

    # HTTP/1.1 is required for the keep-alive assertions: BaseHTTPRequestHandler
    # defaults to 1.0, on which a response carries no length framing and the
    # connection closes after each reply - so a reuse test against a 1.0 server
    # would be measuring the fixture rather than the client.
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # silence per-request logging
        return None

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, LocalTestServer)
        server.request_paths.append(self.path)

        if self.path == "/slow-eof":
            # Body arrives quickly, EOF is delayed past any reasonable deadline:
            # the case where checking the deadline only after a non-empty read
            # would wrongly report success.
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"abcd")
            self.wfile.flush()
            server.delay_before_close()
            return

        if self.path == "/stalled":
            # Headers then nothing: exercises the socket timeout, distinct from
            # the whole-request deadline.
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b"x")
            self.wfile.flush()
            server.sleep_long()
            return

        if self.path == "/loop":
            # Self-redirect: proves the hop limit stops a cycle instead of hanging.
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/moved-gone":
            # A redirect to a dead resource; final status must drive the result.
            self.send_response(301)
            self.send_header("Location", "/missing")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/gzip":
            payload = gzip.compress(b"z" * 5000)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/chunked":
            # No Content-Length at all: proves volume comes from bytes received.
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(4):
                body = b"y" * 700
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return

        if self.path.startswith("/error"):
            code = int(self.path.split("/error")[1] or 500)
            self.send_response(code)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"nope!")
            return

        if self.path.startswith("/payload") or self.path == "/final":
            # Known paths serve a predictable number of bytes.
            pass
        else:
            # Anything else is genuinely absent. Returning a body for unknown paths
            # would hide redirect bugs: a 301 to a missing resource must surface as
            # 404, not as a successful download.
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not found")
            return

        size = int(self.server.query_size(self.path))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        written = 0
        while written < size:
            block = min(65_536, size - written)
            self.wfile.write(b"a" * block)
            written += block


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


def serve(paths: Iterator[str] | None = None) -> LocalTestServer:
    return LocalTestServer()
