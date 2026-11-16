"""HttpTransport behaviour via HTTPX's supported test transport.

The old transport needed 270 lines of scripted-clock, stub-socket tests because it
implemented HTTP by hand. On HTTPX those details are the library's responsibility
and are exercised by HTTPX's own test suite; what remains here is OUR contract:
identity-encoding requests, wire-byte accounting, final-status semantics, the
absolute deadline, and the single stale-connection retry.

MockTransport is HTTPX's supported extension point - no private internals.
"""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from speedrun import _MAX_REDIRECTS, HttpTransport


def transport_for(handler):
    """A real HttpTransport whose HTTP layer is HTTPX's MockTransport.

    The client is configured the way HttpTransport._get_client configures it
    (follow_redirects, hop limit) so these tests exercise the production contract.
    """
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,
        trust_env=False,
    )
    return HttpTransport(_client=client)


def fetch(transport: HttpTransport, url: str, timeout: float = 5.0):
    transport.set_deadline(timeout)
    return asyncio.run(transport.fetch(url))


def close(transport: HttpTransport) -> None:
    asyncio.run(transport.aclose())


def resp(status: int, body: bytes = b"", headers: dict | None = None) -> httpx.Response:
    """A streaming-capable response for MockTransport.

    Bytes content breaks client.stream() on MockTransport with StreamConsumed - the
    pre-built byte stream is marked consumed before aiter_raw runs (httpx quirk,
    verified in isolation). An async-generator body is a real stream and streams.
    """

    async def body_stream():
        if body:
            yield body

    return httpx.Response(status, content=body_stream(), headers=headers)


def ok_handler(status: int = 200, body: bytes = b"payload"):
    def handler(request: httpx.Request) -> httpx.Response:
        return resp(status, body)

    return handler


class TestPayloadAccounting:
    """FR-2.5 / FR-4.1: counted bytes are the delivered body, decoded never."""

    def test_counts_delivered_bytes(self) -> None:
        transport = transport_for(ok_handler(body=b"a" * 12345))
        try:
            result = fetch(transport, "https://x/file")
        finally:
            close(transport)
        assert result.ok is True
        assert result.bytes == 12345

    def test_requests_identity_encoding(self) -> None:
        """Without identity, httpx would negotiate compression and the count would
        depend on the server's choice. Identity pins the exchange (FR-2.5)."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return resp(200, b"x")

        transport = transport_for(handler)
        try:
            fetch(transport, "https://x/y")
        finally:
            close(transport)
        assert seen[0].headers.get("Accept-Encoding") == "identity"

    def test_wire_bytes_are_counted_not_decoded_payload(self) -> None:
        """FR-4.3: a compressed body counts its compressed size.

        The transport never asks for compression (identity), but if a server sends
        it anyway, aiter_raw must report what crossed the connection - a decoded
        count would inflate a link measurement by the compression ratio.
        """
        payload = gzip.compress(b"z" * 5000)

        def handler(request: httpx.Request) -> httpx.Response:
            return resp(200, payload, headers={"Content-Encoding": "gzip"})

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/gzipped")
        finally:
            close(transport)
        assert result.ok is True
        assert result.bytes == len(payload)
        assert result.bytes < 5000


class TestStatusSemantics:
    """FR-5.4: non-2xx fails; FR-5.4a: the FINAL response's status decides."""

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    def test_non_2xx_is_a_failed_request(self, status: int) -> None:
        transport = transport_for(ok_handler(status=status, body=b"nope"))
        try:
            result = fetch(transport, "https://x/y")
        finally:
            close(transport)
        assert result.ok is False
        assert str(status) in (result.error or "")
        assert result.bytes == 0

    def test_redirect_chain_reaches_the_final_response(self) -> None:
        hops: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hops.append(request.url.path)
            if request.url.path == "/start":
                return resp(302, headers={"Location": "/middle"})
            if request.url.path == "/middle":
                return resp(301, headers={"Location": "/end"})
            return resp(200, b"arrived")

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/start")
        finally:
            close(transport)
        assert result.ok is True
        assert result.bytes == len(b"arrived")
        assert hops == ["/start", "/middle", "/end"]

    def test_redirect_ending_in_404_is_a_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/gone":
                return resp(301, headers={"Location": "/missing"})
            return resp(404, b"absent")

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/gone")
        finally:
            close(transport)
        assert result.ok is False
        assert "404" in (result.error or "")

    def test_redirect_loop_is_bounded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return resp(302, headers={"Location": "/loop"})

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/loop")
        finally:
            close(transport)
        assert result.ok is False
        assert "redirect" in (result.error or "")


class TestAbsoluteDeadline:
    """FR-3.5: wait_for bounds the entire exchange, not per-phase waits."""

    def test_slow_streaming_body_hits_the_deadline(self) -> None:
        """A trickling sender is the case per-phase timeouts cannot bound."""

        def handler(request: httpx.Request) -> httpx.Response:
            async def trickle():
                for _ in range(100):
                    yield b"x"
                    await asyncio.sleep(0.05)

            return (
                resp(
                    200,
                    b"",
                )
                if False
                else httpx.Response(200, content=trickle())
            )

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/slow", timeout=0.3)
        finally:
            close(transport)
        assert result.ok is False
        assert "deadline" in (result.error or "")
        assert result.seconds < 0.9  # the uninterrupted trickle would take ~5 s

    def test_tiny_deadline_fails_a_slower_exchange(self) -> None:
        """The deadline is absolute: an exchange slower than it fails.

        set_deadline only accepts a positive budget (validated at the CLI); an
        already-expired deadline is expressed by giving the exchange less time
        than it needs and asserting it never completes.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            async def slow():
                yield b"first"
                await asyncio.sleep(0.4)
                yield b"late"

            return httpx.Response(200, content=slow())

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/slow", timeout=0.1)
        finally:
            close(transport)
        assert result.ok is False
        assert "deadline" in (result.error or "")
        assert result.bytes == 0

    def test_dns_failure_is_a_failed_request_not_a_crash(self) -> None:
        # Real socket layer (no mock): a name that cannot resolve.
        transport = HttpTransport()
        try:
            result = fetch(transport, "https://no-such-host-zzz.invalid/x", timeout=2.0)
        finally:
            close(transport)
        assert result.ok is False
        assert result.error


class TestStaleConnectionRetry:
    """A keep-alive race is not a measurement of the resource (FR-2.3)."""

    def test_one_disconnection_that_looks_stale_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("server disconnected")
            return resp(200, b"second try")

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/y")
        finally:
            close(transport)
        assert result.ok is True
        assert calls["n"] == 2

    def test_genuine_server_errors_are_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return resp(503, b"overloaded")

        transport = transport_for(handler)
        try:
            result = fetch(transport, "https://x/y")
        finally:
            close(transport)
        assert calls["n"] == 1
        assert result.ok is False
        assert "503" in (result.error or "")
