"""Transport behaviour against a local HTTP server we control.

Only what needs real sockets lives here: connection-reuse counting, deadline
enforcement on each blocking phase over actual TCP, and pool recovery after an
abandoned transfer. Status codes, redirects and byte accounting are covered by
test_transport.py through HTTPX's MockTransport.

Everything here runs offline and deterministically.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest
from local_server import LocalTestServer

from speedrun import FetchResult, HttpTransport, run


@dataclass
class Measured:
    """A fetch result plus the wall time the fetch took, from the same loop."""

    result: FetchResult
    elapsed: float


def fetch_measured(url: str, timeout: float) -> Measured:
    """One fetch in its own loop, timing it there; the loop owns the transport."""

    async def run_one() -> Measured:
        transport = HttpTransport()
        started = time.monotonic()
        try:
            result = await transport.fetch(url, timeout)
            elapsed = time.monotonic() - started
            return Measured(result=result, elapsed=elapsed)
        finally:
            await transport.aclose()

    return asyncio.run(run_one())


@pytest.fixture
def server() -> LocalTestServer:
    with LocalTestServer() as s:
        yield s


class TestConnectionReuse:
    """FR-2.3: the claim is about sockets, so it is verified by counting sockets.

    Comparing request durations cannot prove reuse; accepted-connection counts
    can. run() owns the loop and closes the transport itself - closing it from
    outside would be a second loop, where the client's sockets are orphaned.
    """

    def test_four_requests_use_one_connection(self, server: LocalTestServer) -> None:
        transport = HttpTransport()
        summary = run(
            f"{server.base_url}/payload=2000",
            requests=4,
            timeout=10.0,
            transport=transport,
            quiet=True,
        )
        assert summary.ok_count == 4
        assert len(server.request_paths) == 4
        assert server.accepted_connections == 1


class TestDeadlineAgainstRealSocket:
    """FR-3.5 over real sockets: every blocking phase, not just body reads."""

    def test_stalled_body_hits_the_deadline(self, server: LocalTestServer) -> None:
        measured = fetch_measured(f"{server.base_url}/stalled", 0.4)
        assert measured.result.ok is False
        assert measured.result.bytes == 0

    def test_slow_response_headers_are_bounded(self, server: LocalTestServer) -> None:
        measured = fetch_measured(f"{server.base_url}/slow-headers", 0.4)
        # Server delays 2 s before the status line; the deadline must cut the
        # attempt near 0.4 s, not wait out the 2 s.
        assert measured.result.ok is False
        assert measured.elapsed < 1.2, (
            f"header phase outlived the deadline: {measured.elapsed:.2f}s"
        )

    def test_trickling_redirect_body_is_bounded(self, server: LocalTestServer) -> None:
        # The exchange alone would run ~4 s (2000 writes x 2 ms); it must stop on
        # budget.
        measured = fetch_measured(f"{server.base_url}/trickle-redirect", 0.6)
        assert measured.result.ok is False
        assert measured.elapsed < 1.5, f"exchange outlived the deadline: {measured.elapsed:.2f}s"

    def test_pool_reusable_after_a_deadline_miss(self, server: LocalTestServer) -> None:
        """After abandoning a stalled transfer the next request must still succeed."""

        async def fetch_both() -> tuple[FetchResult, FetchResult]:
            transport = HttpTransport()
            try:
                bad = await transport.fetch(f"{server.base_url}/stalled", 0.4)
                good = await transport.fetch(f"{server.base_url}/payload=1000", 10.0)
                return bad, good
            finally:
                await transport.aclose()

        bad, good = asyncio.run(fetch_both())
        assert bad.ok is False
        assert good.ok is True
        assert good.bytes == 1000


class TestFailureIsolation:
    """FR-5.1: one bad request never aborts a run, whatever the failure."""

    def test_dns_failure_is_a_result_not_an_exception(self) -> None:
        measured = fetch_measured("http://no-such-host-zzz.invalid/x", 2.0)
        assert measured.result.ok is False
        assert measured.result.seconds >= 0  # measured, not fabricated (FR-3.1)
        assert measured.result.error

    def test_refused_connection_is_a_result_not_an_exception(self) -> None:
        # Port 1 on loopback: nothing listens there.
        measured = fetch_measured("http://127.0.0.1:1/x", 2.0)
        assert measured.result.ok is False
        assert measured.result.error

    def test_run_survives_a_transport_that_raises_arbitrarily(self) -> None:
        class Buggy:
            async def fetch(self, url: str, timeout: float) -> FetchResult:
                raise RuntimeError("bug in transport")

        summary = run("https://x/y", requests=2, timeout=5.0, transport=Buggy(), quiet=True)
        assert summary.fail_count == 2
        assert summary.ok_count == 0
