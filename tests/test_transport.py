"""Unit tests for the per-request deadline (SPEC FR-3.5).

--timeout is a deadline on the whole request, not merely socket idle time.
urllib's timeout only bounds the wait between two reads, so a fast server can
stream far longer than the declared limit and still "succeed". These tests pin
the intended semantics with a fake response and an injected clock - no network.
"""

from __future__ import annotations

import pytest

from speedrun import UrllibTransport


class FakeResponse:
    """A response whose body arrives one chunk per read() call."""

    def __init__(self, chunks: list[bytes], status: int = 200) -> None:
        self.chunks = list(chunks)
        self.status = status
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, _size: int) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True


class StubOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.timeouts_seen: list[float] = []

    def open(self, _request: object, timeout: float = 0) -> FakeResponse:
        self.timeouts_seen.append(timeout)
        return self.response


def scripted_clock(values: list[float]) -> object:
    """Return a monotonic-style clock yielding the given readings in order."""
    it = iter(values)
    return lambda: next(it)


def transport_for(chunks: list[bytes], clock: list[float], status: int = 200) -> UrllibTransport:
    opener = StubOpener(FakeResponse(chunks, status))
    return UrllibTransport(opener=opener, now=scripted_clock(clock))


class TestDeadline:
    def test_slow_body_is_aborted_at_deadline(self) -> None:
        # Body trickles in; by the third chunk the 2 s deadline has passed.
        result = transport_for([b"x" * 10] * 4, [0.0, 0.5, 1.0, 2.5]).fetch(
            "https://x/y", timeout=2.0
        )
        assert result.ok is False
        assert "deadline" in (result.error or "").lower()

    def test_partial_bytes_are_not_reported_as_success(self) -> None:
        # FR-5.2: an aborted download must not contribute volume.
        result = transport_for([b"x" * 100] * 3, [0.0, 1.0, 4.0]).fetch("https://x/y", timeout=3.0)
        assert result.bytes == 0

    def test_fast_completion_within_deadline_succeeds(self) -> None:
        result = transport_for([b"a" * 8, b"b" * 8], [0.0, 0.1, 0.2, 0.3]).fetch(
            "https://x/y", timeout=5.0
        )
        assert result.ok is True
        assert result.bytes == 16

    def test_empty_body_counts_zero_bytes_but_succeeds(self) -> None:
        result = transport_for([], [0.0, 0.1, 0.1]).fetch("https://x/y", timeout=5.0)
        assert result.ok is True
        assert result.bytes == 0

    def test_socket_timeout_still_passed_through(self) -> None:
        # The deadline guards total time; the socket timeout still catches a hard
        # stall between reads. Both are wanted, they cover different failures.
        opener = StubOpener(FakeResponse([b"a"]))
        transport = UrllibTransport(opener=opener, now=lambda: 0.0)
        transport.fetch("https://x/y", timeout=7.5)
        assert opener.timeouts_seen == [7.5]


class TestNonSuccessStatus:
    def test_204_is_ok(self) -> None:
        assert (
            transport_for([b"abc"], [0.0, 0.1, 0.1], status=204).fetch("https://x/y", 5.0).ok
            is True
        )

    def test_500_marks_failure_with_code(self) -> None:
        result = transport_for([b"nope"], [0.0, 0.1, 0.1], status=500).fetch("https://x/y", 5.0)
        assert result.ok is False
        assert "500" in (result.error or "")


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_2xx_band_is_successful(status: int) -> None:
    assert (
        transport_for([b"x"], [0.0, 0.1, 0.1], status=status).fetch("https://x/y", 5.0).ok is True
    )


@pytest.mark.parametrize("status", [301, 400, 404, 500, 503])
def test_outside_2xx_is_failure(status: int) -> None:
    assert (
        transport_for([b"x"], [0.0, 0.1, 0.1], status=status).fetch("https://x/y", 5.0).ok is False
    )
