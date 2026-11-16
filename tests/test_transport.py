"""Unit tests for the transport with a scripted clock and a stub connection.

The companion file test_transport_live.py proves the same requirements against a
real socket; this one proves the deadline arithmetic deterministically, without
timing dependence. Both matter: the live test shows it works, the scripted test
shows it works for the right reason and pins edge cases (late EOF) that are hard
to hit reliably over a real socket.
"""

from __future__ import annotations

import pytest

from speedrun import HttpTransport


class Clock:
    """Monotonic fake clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class StubResponse:
    status = 200

    def __init__(
        self,
        chunks: list[bytes],
        clock: Clock,
        location: str | None = None,
        eof_delay: float = 0.0,
    ) -> None:
        self.chunks = list(chunks)
        self.clock = clock
        self.location = location
        self.eof_delay = eof_delay

    def getheader(self, name: str) -> str | None:
        return self.location if name == "Location" else None

    def read(self, _size: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        # The blocking call that discovers EOF took eof_delay seconds.
        self.clock.advance(self.eof_delay)
        return b""


class StubSocket:
    """Records every timeout the transport arms, so re-arming is observable.

    Without this the deadline logic could call settimeout with the original full
    value every time and no test would notice - which is exactly the bug found in
    the real code, where the timeout was being set on the response instead of the
    connection and therefore did nothing at all.
    """

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, seconds: float) -> None:
        self.timeouts.append(seconds)


class StubConnection:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str]] = []
        self.closed = 0
        self.sock = StubSocket()

    def request(self, method: str, path: str, headers: object = None) -> None:
        self.requests.append((method, path))

    def getresponse(self) -> StubResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed += 1


def build(
    responses: list[StubResponse], clock: Clock | None = None
) -> tuple[HttpTransport, Clock, list[StubConnection]]:
    """Transport whose every new connection replays the given responses.

    The clock is taken from the first response unless passed explicitly: a
    response advancing one Clock while the transport reads another silently
    disables every deadline assertion, and that failure mode looks like a pass.
    """
    clk = clock or (responses[0].clock if responses else Clock())
    made: list[StubConnection] = []
    pool = list(responses)

    def factory(*args: object, **kwargs: object) -> StubConnection:
        conn = StubConnection(pool)
        made.append(conn)
        return conn

    return HttpTransport(now=clk, conn_factory=factory), clk, made


def resp(
    chunks: list[bytes],
    clock: Clock,
    *,
    status: int = 200,
    **kw: object,
) -> StubResponse:
    r = StubResponse(chunks, clock, **kw)  # type: ignore[arg-type]
    r.status = status
    return r


class TestBodyAccounting:
    def test_counts_every_chunk(self) -> None:
        clk = Clock()
        transport, _, _ = build([resp([b"a" * 8, b"b" * 8], clk)])
        result = transport.fetch("https://x/y", timeout=5.0)
        assert result.ok is True
        assert result.bytes == 16

    def test_empty_body_succeeds_with_zero_bytes(self) -> None:
        clk = Clock()
        transport, _, _ = build([resp([], clk)])
        assert transport.fetch("https://x/y", 5.0).ok is True

    def test_get_uses_path_and_query(self) -> None:
        # FR-2.5 relies on our own header set; verify the request line too.
        clk = Clock()
        transport, _, made = build([resp([b"x"], clk)])
        transport.fetch("https://x/a/b?x=1&y=2", timeout=5.0)
        assert made[0].requests == [("GET", "/a/b?x=1&y=2")]


class TestStatusHandling:
    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    def test_2xx_is_success(self, status: int) -> None:
        clk = Clock()
        transport, _, _ = build([resp([b"x"], clk, status=status)])
        assert transport.fetch("https://x/y", 5.0).ok is True

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    def test_non_2xx_fails_and_reports_code(self, status: int) -> None:
        clk = Clock()
        transport, _, _ = build([resp([b"nope"], clk, status=status)])
        result = transport.fetch("https://x/y", 5.0)
        assert result.ok is False
        assert str(status) in (result.error or "")
        assert result.bytes == 0


class TestLateEofRegression:
    """P1-2: EOF discovered after the deadline must not count as success.

    The original loop checked the deadline only after a non-empty chunk, so this
    exact shape - fast body, late EOF - returned ok=True.
    """

    def test_late_eof_is_a_missed_deadline(self) -> None:
        clk = Clock()
        transport, _, _ = build([resp([b"x" * 10], clk, eof_delay=3.0)])
        result = transport.fetch("https://h/p", timeout=2.0)
        assert result.ok is False
        assert "deadline" in (result.error or "")

    def test_partial_bytes_excluded_on_late_eof(self) -> None:
        # FR-5.2: an aborted transfer contributes no volume.
        clk = Clock()
        transport, _, _ = build([resp([b"x" * 10], clk, eof_delay=3.0)])
        assert transport.fetch("https://h/p", timeout=2.0).bytes == 0

    def test_duration_reflects_the_wait(self) -> None:
        # P2-4 adjacent: timing must be real, not fabricated.
        clk = Clock()
        transport, _, _ = build([resp([b"x" * 10], clk, eof_delay=3.0)])
        assert transport.fetch("https://h/p", timeout=2.0).seconds == pytest.approx(3.0, abs=0.01)

    def test_ontime_completion_still_succeeds(self) -> None:
        clk = Clock()
        transport, _, _ = build([resp([b"x" * 10], clk, eof_delay=0.0)])
        assert transport.fetch("https://h/p", timeout=2.0).ok is True


class TestRedirects:
    def test_location_header_is_followed_to_absolute_url(self) -> None:
        clk = Clock()
        transport, _, made = build(
            [resp([], clk, status=302, location="/final"), resp([b"body"], clk)]
        )
        result = transport.fetch("http://h/start", timeout=5.0)
        assert result.ok is True
        assert [p for _, p in made[0].requests] == ["/start", "/final"]

    def test_final_status_decides_success(self) -> None:
        # FR-5.4a: a redirect ending in 404 is a failure, not a 3xx.
        clk = Clock()
        transport, _, _ = build(
            [resp([], clk, status=301, location="/gone"), resp([b"x"], clk, status=404)]
        )
        result = transport.fetch("http://h/a", timeout=5.0)
        assert result.ok is False
        assert "404" in (result.error or "")


class TestStaleConnectionRetry:
    """Keep-alive race: a server-closed idle connection must not fail the run."""

    def test_remote_disconnected_retries_once_and_succeeds(self) -> None:
        import http.client

        clk = Clock()
        attempts = {"n": 0}

        class FlakyConn:
            def request(self, method: str, path: str, headers: object = None) -> None:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise http.client.RemoteDisconnected("closed by server")

            def getresponse(self) -> StubResponse:
                return StubResponse([b"payload"], clk)

            def close(self) -> None:
                return None

        made: list[FlakyConn] = []

        def factory(*a: object, **k: object) -> FlakyConn:
            c = FlakyConn()
            made.append(c)
            return c

        transport = HttpTransport(now=clk, conn_factory=factory)
        result = transport.fetch("https://h/p", timeout=5.0)
        assert result.ok is True
        assert result.bytes == 7
        # The stale connection was dropped and a fresh one opened.
        assert len(made) == 2

    def test_non_staleness_error_is_not_retried(self) -> None:
        # A DNS failure must surface as this request's outcome, not be retried.
        clk = Clock()
        calls = {"n": 0}

        class BadConn:
            def request(self, method: str, path: str, headers: object = None) -> None:
                calls["n"] += 1
                raise OSError("name or service not known")

            def getresponse(self) -> StubResponse:  # pragma: no cover - unreachable
                raise AssertionError

            def close(self) -> None:
                return None

        transport = HttpTransport(
            now=clk,
            conn_factory=lambda *a, **k: BadConn(),  # type: ignore[arg-type]
        )
        result = transport.fetch("https://h/p", timeout=5.0)
        assert result.ok is False
        assert calls["n"] == 1
