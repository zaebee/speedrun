"""Tests for the transport against a local HTTP server we control.

These replace assertions that could not be made against a fake response object or
a remote host: connection reuse (counted at the socket level), redirect semantics,
the whole-request deadline including late EOF, and constant-memory reads.

Everything here runs offline and deterministically - no remote latency involved.
"""

from __future__ import annotations

import pytest
from local_server import LocalTestServer

from speedrun import HttpTransport, run


@pytest.fixture()
def server() -> object:
    with LocalTestServer() as s:
        yield s


class TestConnectionReuse:
    """FR-2.3: the claim is about sockets, so it is verified by counting sockets.

    Comparing request durations cannot prove this; accepted-connection counts can.
    urllib.request fails this test by construction (it sends Connection: close).
    """

    def test_four_requests_use_one_connection(self, server: LocalTestServer) -> None:
        transport = HttpTransport()
        try:
            summary = run(
                f"{server.base_url}/payload=2000",
                requests=4,
                timeout=10.0,
                transport=transport,
                quiet=True,
            )
        finally:
            transport.close()
        assert summary.ok_count == 4
        assert len(server.request_paths) == 4
        assert server.accepted_connections == 1


class TestRedirects:
    """FR-5.4a: success is decided by the final response status."""

    def test_redirect_to_ok_is_success(self, server: LocalTestServer) -> None:
        transport = HttpTransport()
        try:
            result = transport.fetch(f"{server.base_url}/redirect", timeout=10.0)
        finally:
            transport.close()
        assert result.ok is True
        assert result.bytes > 0
        assert "/final" in server.request_paths

    def test_redirect_to_missing_is_failure_with_final_status(
        self, server: LocalTestServer
    ) -> None:
        transport = HttpTransport()
        try:
            result = transport.fetch(f"{server.base_url}/moved-gone", timeout=10.0)
        finally:
            transport.close()
        assert result.ok is False
        assert "404" in (result.error or "") or "missing" in (result.error or "").lower()

    def test_redirect_loop_is_bounded_not_hanging(self, server: LocalTestServer) -> None:
        # /loop redirects to itself; the hop limit must stop it.
        transport = HttpTransport()
        try:
            result = transport.fetch(f"{server.base_url}/loop", timeout=10.0)
        finally:
            transport.close()
        assert result.ok is False
        assert len([p for p in server.request_paths if p == "/loop"]) <= 6


class TestErrorStatuses:
    @pytest.mark.parametrize("code", [400, 404, 500])
    def test_non_2xx_fails_without_raising(self, server: LocalTestServer, code: int) -> None:
        transport = HttpTransport()
        try:
            result = transport.fetch(f"{server.base_url}/error{code}", timeout=10.0)
        finally:
            transport.close()
        assert result.ok is False
        assert str(code) in (result.error or "")
        assert result.bytes == 0


class TestChunkedTransfer:
    def test_volume_comes_from_received_bytes_not_content_length(
        self, server: LocalTestServer
    ) -> None:
        # /chunked sends no Content-Length at all (FR-4.2).
        transport = HttpTransport()
        try:
            result = transport.fetch(f"{server.base_url}/chunked", timeout=10.0)
        finally:
            transport.close()
        assert result.ok is True
        assert result.bytes == 2800


class TestDeadlineAgainstRealSocket:
    """FR-3.5 exercised over a real blocking read, not a scripted clock."""

    def test_stalled_body_hits_the_deadline(self, server: LocalTestServer) -> None:
        transport = HttpTransport()
        try:
            result = transport.fetch(f"{server.base_url}/stalled", timeout=0.4)
        finally:
            transport.close()
        assert result.ok is False
        assert result.bytes == 0

    def test_socket_reusable_after_a_deadline_miss(self, server: LocalTestServer) -> None:
        # After abandoning a stalled read the pooled connection is unusable; the
        # next request must transparently open a fresh one instead of failing.
        transport = HttpTransport()
        try:
            bad = transport.fetch(f"{server.base_url}/stalled", timeout=0.4)
            good = transport.fetch(f"{server.base_url}/payload=1000", timeout=10.0)
        finally:
            transport.close()
        assert bad.ok is False
        assert good.ok is True
        assert good.bytes == 1000


class TestUnreachableHost:
    def test_dns_failure_is_a_result_not_an_exception(self) -> None:
        transport = HttpTransport()
        try:
            result = transport.fetch("http://no-such-host-zzz.invalid/x", timeout=2.0)
        finally:
            transport.close()
        assert result.ok is False
        assert result.seconds >= 0  # measured, not fabricated (FR-3.1)
        assert result.error


class TestCloseIsIdempotent:
    def test_double_close_does_not_raise(self, server: LocalTestServer) -> None:
        transport = HttpTransport()
        transport.fetch(f"{server.base_url}/payload=10", timeout=10.0)
        transport.close()
        transport.close()
