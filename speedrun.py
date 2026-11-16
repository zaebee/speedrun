#!/usr/bin/env python3
"""speedrun - a console download-throughput probe.

Performs N sequential requests against a URL, waits for each response body to be
read in full, then reports average request time, transferred volume and
throughput.

Behaviour is specified in SPEC.md. HTTP semantics are delegated to HTTPX; this
module owns only measurement: timing, byte accounting, aggregation and exit
codes.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol
from urllib.parse import urlparse

import httpx

# --- Units (SPEC section 5) --------------------------------------------------

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_BIT_UNITS = ("bps", "kbps", "Mbps", "Gbps", "Tbps")

_CHUNK = 65_536
_MAX_REDIRECTS = 5


# --- Exit codes ---------------------------------------------------------------


class ExitCode(IntEnum):
    """Process exit status. 2 and 1 are distinct on purpose (FR-1 vs FR-5.6)."""

    OK = 0
    CANNOT_MEASURE = 1
    USAGE = 2
    INTERRUPTED = 130


# --- Data model ---------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a single request.

    ``seconds`` is measured for every attempt, successful or not, so the caller
    can report where time went; only successes feed the summary (FR-5.2).
    """

    ok: bool
    seconds: float
    bytes: int
    error: str | None = None


@dataclass(frozen=True)
class Summary:
    """Aggregated figures over the successful requests of one run."""

    ok_count: int
    fail_count: int
    total_bytes: int
    total_seconds: float
    avg_seconds: float
    median_seconds: float
    throughput_bytes_per_sec: float
    # Elapsed time of the whole run, measured independently of the request
    # durations above. The gap between them is overhead (DNS, first handshake)
    # and is the point of FR-3.4 - deriving it from the sum would make the
    # metric tautological.
    wall_seconds: float = 0.0


# --- Pure numeric logic (unit-tested without a network) -----------------------


def format_bytes(n: float) -> str:
    """Format a byte count with a binary prefix (KiB = 1024 bytes).

    Values below 1 KiB stay in bytes but keep two decimals so the summary block
    aligns in a column (FR-6.3).
    """
    if n < 0:
        raise ValueError(f"size cannot be negative: {n}")
    value = float(n)
    for unit in _BYTE_UNITS:
        if value < 1024 or unit == _BYTE_UNITS[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def format_bitrate(bytes_per_sec: float) -> str:
    """Format throughput as bits per second with decimal prefixes (Mbps = 1e6).

    Networking counts megabits as 10**6 while our byte units are binary, so
    1 MiB/s is about 8.389 Mbps rather than 8 (FR-6.1).
    """
    if bytes_per_sec < 0:
        raise ValueError(f"rate cannot be negative: {bytes_per_sec}")
    bits = float(bytes_per_sec) * 8
    for unit in _BIT_UNITS:
        if bits < 1000 or unit == _BIT_UNITS[-1]:
            return f"{bits:.2f} {unit}"
        bits /= 1000
    raise AssertionError("unreachable")


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean of samples; 0.0 for an empty sample."""
    return statistics.fmean(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    """Median of samples; 0.0 for an empty sample (FR-3.3)."""
    return statistics.median(values) if values else 0.0


def throughput(total_bytes: int, total_seconds: float) -> float:
    """Aggregate rate in bytes/s: volume over time, never a mean of rates.

    Averaging per-request speeds weights a short request the same as a long one
    and understates the link (FR-6.5). A zero duration returns 0.0 rather than
    raising: a sub-microsecond response on a local server can round to zero.
    """
    if total_seconds <= 0:
        return 0.0
    return total_bytes / total_seconds


def summarize(results: Sequence[FetchResult], wall_seconds: float = 0.0) -> Summary:
    """Aggregate a run, excluding failures from average and volume (FR-5.2).

    ``wall_seconds`` is the elapsed time of the whole run, passed in because only
    the caller can measure it (FR-3.4): deriving it from the request durations
    would make the overhead metric identically zero by construction.
    """
    good = [r for r in results if r.ok]
    durations = [r.seconds for r in good]
    total_bytes = sum(r.bytes for r in good)
    total_seconds = sum(durations)
    return Summary(
        ok_count=len(good),
        fail_count=len(results) - len(good),
        total_bytes=total_bytes,
        total_seconds=total_seconds,
        avg_seconds=mean(durations),
        median_seconds=median(durations),
        throughput_bytes_per_sec=throughput(total_bytes, total_seconds),
        wall_seconds=wall_seconds,
    )


def exit_code_for(summary: Summary) -> int:
    """Decide the exit status from a run's outcome (FR-5.6)."""
    return ExitCode.OK if summary.ok_count else ExitCode.CANNOT_MEASURE


# --- Input validation (FR-1) --------------------------------------------------


class UsageError(ValueError):
    """Bad invocation: wrong argument or unsupported URL. Maps to exit code 2."""


def validate_url(url: str) -> str:
    """Accept absolute http(s) URLs only, before any network access (FR-1.2)."""
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise UsageError(f"invalid URL {url!r}: expected an absolute http:// or https:// address")
    if not parsed.netloc:
        raise UsageError(f"invalid URL {url!r}: host is missing")
    return cleaned


def positive_int(value: str) -> int:
    """argparse type for --requests: integer >= 1 (FR-1.3)."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"--requests must be at least 1, got {n}")
    return n


def positive_finite(value: str) -> float:
    """argparse type for --timeout: a finite number > 0 (FR-1.5).

    Zero and negatives are rejected because the whole-request deadline would then
    fail every attempt - a confusing run rather than a clear usage error. NaN and
    infinities are rejected because they corrupt the deadline comparison silently.
    """
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {value!r}") from None
    if not math.isfinite(n):
        raise argparse.ArgumentTypeError(f"--timeout must be finite, got {value!r}")
    if n <= 0:
        raise argparse.ArgumentTypeError(f"--timeout must be greater than 0, got {n:g}")
    return n


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="speedrun",
        description="Measure download throughput against a URL with sequential requests.",
    )
    parser.add_argument("url", help="absolute http(s) URL to fetch repeatedly")
    parser.add_argument(
        "-n",
        "--requests",
        type=positive_int,
        default=10,
        metavar="N",
        help="number of sequential requests (default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_finite,
        default=30.0,
        metavar="SEC",
        help="deadline for a whole request in seconds (default: 30)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print the summary block only, no per-request progress",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show per-request failure details on stderr (FR-5.5)",
    )
    return parser.parse_args(argv)


# --- Transport seam (NFR-6) ---------------------------------------------------


class Transport(Protocol):
    """One fetch of a URL, body read to completion.

    The seam keeps every numeric rule in the spec testable without a network:
    orchestration depends on this protocol, not on HTTPX. HTTPX's MockTransport
    could inject responses too, but scripting our own FetchResult model keeps
    orchestration tests free of HTTP semantics entirely.
    """

    async def fetch(self, url: str) -> FetchResult: ...

    def set_deadline(self, seconds: float) -> None:
        """Set the absolute bound for subsequent fetches (FR-3.5).

        Sync, and outside the coroutine on purpose: the deadline is configuration,
        not a parameter threaded through async code. Enforced by the implementation
        with a timeout context manager around the whole exchange.
        """
        ...


def _reason(exc: BaseException) -> str:
    """Short single-line failure reason, never a traceback (FR-5.5)."""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


@dataclass
class HttpTransport:
    """Real transport: an HTTPX async client reused across requests.

    What this class does NOT contain is the point - connection pooling, keep-alive,
    redirect following, chunked decoding and header parsing are HTTPX's job
    (FR-2.3, FR-5.4a). This class owns only measurement:

    - an absolute deadline per request. HTTPX's connect/read/write/pool timeouts
      are per-phase, and the read timeout resets whenever any byte arrives, so a
      trickling response can run arbitrarily long past it (measured: 4.4 s of
      trickle under a 0.6 s read timeout). asyncio.timeout around the entire
      exchange, including body consumption, is what makes --timeout an absolute
      bound (FR-3.5);
    - payload accounting: ``aiter_raw`` with ``Accept-Encoding: identity`` counts
      bytes as delivered on the wire, before any decoding (FR-2.5, FR-4.3);
    - monotonic timing around the whole exchange (FR-3.1).

    One known non-interruptible phase: DNS resolution runs in a worker thread
    inside connect. The deadline fires on time regardless, but the resolver thread
    may linger past it - the same limitation as the previous implementation, stated
    in FR-3.5 rather than hidden.
    """

    now: Callable[[], float] = time.monotonic
    timeout: float = 30.0
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    def set_deadline(self, seconds: float) -> None:
        self.timeout = seconds

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False: a developer's http_proxy env var must not silently
            # redirect the measurement. follow_redirects with a hop limit and
            # final-status semantics implements FR-5.4a.
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> FetchResult:
        start = self.now()
        try:
            # asyncio.timeout is the whole-request deadline (FR-3.5): it cancels the
            # exchange at the budget's end wherever it happens to be blocked - connect,
            # headers, or mid-body. HTTPX's per-phase timeouts are only a stall guard.
            async with asyncio.timeout(self.timeout):
                size, status = await self._exchange(url)
        except TimeoutError:
            # The deliberate outcome for any exchange slower than --timeout.
            elapsed = self.now() - start
            return FetchResult(
                ok=False,
                seconds=elapsed,
                bytes=0,
                error=f"exceeded {self.timeout:g} s deadline",
            )
        except httpx.TooManyRedirects:
            return FetchResult(
                ok=False,
                seconds=self.now() - start,
                bytes=0,
                error=f"more than {_MAX_REDIRECTS} redirects",
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return self._failure(exc, start)

        seconds = self.now() - start
        if not 200 <= status < 300:
            # The final response's status decides (FR-5.4a): a redirect ending in a
            # 404 is a failure, a CDN 302 in front of the asset measures normally.
            return FetchResult(ok=False, seconds=seconds, bytes=0, error=f"HTTP {status}")
        return FetchResult(ok=True, seconds=seconds, bytes=size, error=None)

    async def _exchange(self, url: str) -> tuple[int, int]:
        """Stream one response to completion; return (payload bytes, final status).

        A stale pooled connection can surface as a protocol error on the first
        request that touches it - a property of every keep-alive client, not a
        defect of the resource. It is retried once: counting that failure would
        report a connection event as the resource's speed (FR-2.3 measurement
        integrity). Any second failure is this request's outcome.
        """
        try:
            return await self._stream_once(url)
        except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
            if not self._looks_like_stale_connection(exc):
                raise
            return await self._stream_once(url)

    async def _stream_once(self, url: str) -> tuple[int, int]:
        client = self._get_client()
        # Per-phase timeouts cap each blocking step at the budget; the absolute
        # bound is asyncio.timeout in fetch(). Without them a stalled step could
        # hang on OS-level defaults well past the deadline.
        async with client.stream(
            "GET",
            url,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx.Timeout(self.timeout),
        ) as response:
            size = 0
            async for chunk in response.aiter_raw(chunk_size=_CHUNK):
                size += len(chunk)
            return size, response.status_code

    @staticmethod
    def _looks_like_stale_connection(exc: Exception) -> bool:
        message = str(exc).lower()
        markers = (
            "server disconnected",
            "connection reset",
            "broken pipe",
            "peer closed",
            "closed connection",
        )
        return any(marker in message for marker in markers)

    def _failure(self, exc: Exception, start: float) -> FetchResult:
        return FetchResult(ok=False, seconds=self.now() - start, bytes=0, error=_reason(exc))


# --- Run loop -----------------------------------------------------------------

Emit = Callable[[str], None]


def _progress_line(index: int, total: int, result: FetchResult) -> str:
    label = f"[{index}/{total}]"
    if result.ok:
        return f"{label} ok       {result.seconds:7.3f} s   {format_bytes(result.bytes)}"
    return f"{label} FAILED    {result.seconds:7.3f} s   {result.error or 'error'}"


async def _attempt(transport: Transport, url: str, now: Callable[[], float]) -> FetchResult:
    """Run one request, timing it on both the success and failure paths (FR-3.1).

    Failures are contained here: FR-5.1 says one bad request never aborts a run,
    including failures of our own making - a bug in the transport may raise
    anything, and TestNoErrorEscapesTheTransport pins that. KeyboardInterrupt is
    deliberately re-raised: it is an instruction from the operator, not an outcome
    of the request. CancelledError belongs to the event loop and is handled by the
    callers, which convert it into a partial summary (NFR-4).
    """
    start = now()
    try:
        return await transport.fetch(url)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as exc:
        return FetchResult(ok=False, seconds=now() - start, bytes=0, error=_reason(exc))


async def _collect(
    transport: Transport, url: str, args: argparse.Namespace
) -> tuple[list[FetchResult], bool]:
    """Run requests until done or interrupted; return (results, was_interrupted)."""
    results: list[FetchResult] = []
    for index in range(1, args.requests + 1):
        try:
            result = await _attempt(transport, url, time.monotonic)
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Ctrl-C cancels the enclosing task; whatever was measured so far is
            # still a measurement and must reach the operator (NFR-4).
            return results, True
        results.append(result)
        if not args.quiet:
            print(_progress_line(index, args.requests, result))
        if args.debug and not result.ok:
            print(f"speedrun: request {index} failed: {result.error}", file=sys.stderr)
    return results, False


async def _arun(
    url: str,
    requests: int,
    transport: Transport,
    quiet: bool,
    emit: Emit | None,
    now: Callable[[], float],
) -> Summary:
    writer: Emit = emit if emit is not None else print
    results: list[FetchResult] = []
    started = now()
    try:
        for index in range(1, requests + 1):
            try:
                result = await _attempt(transport, url, now)
            except (asyncio.CancelledError, KeyboardInterrupt):
                # This loop owns the event loop (see run()), so it also owns
                # releasing the transport's connections - here, not in some
                # caller's loop where the client's sockets are already orphaned.
                await _maybe_aclose(transport)
                return summarize(results, wall_seconds=now() - started)
            results.append(result)
            if not quiet:
                writer(_progress_line(index, requests, result))
    finally:
        await _maybe_aclose(transport)
    return summarize(results, wall_seconds=now() - started)


async def _maybe_aclose(transport: Transport) -> None:
    """Close a transport that has async cleanup; tolerate ones that do not."""
    aclose = getattr(transport, "aclose", None)
    if aclose is not None:
        await aclose()


def run(
    url: str,
    requests: int,
    timeout: float,
    transport: Transport,
    quiet: bool = False,
    emit: Emit | None = None,
    now: Callable[[], float] = time.monotonic,
) -> Summary:
    """Perform ``requests`` sequential fetches and aggregate the result.

    Sync by design: each call owns one event loop. The real transport must
    therefore not be shared across separate run() calls - within one call its
    connections are pooled and reused (FR-2.3); across calls it would not be.
    """
    set_deadline(transport, timeout)
    return asyncio.run(_arun(url, requests, transport, quiet, emit, now))


def set_deadline(transport: Transport, seconds: float) -> None:
    """Hand the deadline to the transport, if it accepts one.

    Sync, and outside the event loop: the deadline is run configuration. Transports
    without a deadline (test doubles that return instantly) skip it silently.
    """
    configure = getattr(transport, "set_deadline", None)
    if configure is not None:
        configure(seconds)


# --- Output rendering ---------------------------------------------------------


def format_summary(s: Summary) -> list[str]:
    """Render the summary block (FR-6)."""
    lines = [f"{'Requests':<15}{s.ok_count} ok / {s.fail_count} failed"]
    if s.ok_count == 0:
        # Nothing was measured. Printing zeros would present an artifact of an
        # empty sample as a result (FR-5.6); say plainly that there is none.
        lines.append(f"{'Result':<15}not measured - no request succeeded")
        return lines
    lines.extend(
        [
            f"{'Total size':<15}{format_bytes(s.total_bytes)}",
            f"{'Avg time':<15}{s.avg_seconds:.3f} s   (median {s.median_seconds:.3f} s)",
            f"{'Run wall time':<15}{s.wall_seconds:.3f} s   ({s.total_seconds:.3f} s in requests)",
            (
                f"{'Throughput':<15}{format_bytes(s.throughput_bytes_per_sec)}/s"
                f"   ({format_bitrate(s.throughput_bytes_per_sec)})"
            ),
        ]
    )
    return lines


# --- Entry point --------------------------------------------------------------


async def _amain(args: argparse.Namespace, transport: Transport | None) -> int:
    try:
        url = validate_url(args.url)
    except UsageError as exc:
        print(f"speedrun: error: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)

    active: Transport = transport if transport is not None else HttpTransport()
    set_deadline(active, args.timeout)
    started = time.monotonic()
    try:
        results, interrupted = await _collect(active, url, args)
    finally:
        # Same loop as the requests: releasing here is what keeps the sockets
        # reachable for a clean shutdown (NFR-4) instead of orphaning them.
        await _maybe_aclose(active)
    wall = time.monotonic() - started

    summary = summarize(results, wall_seconds=wall)
    if not args.quiet:
        print()
    for line in format_summary(summary):
        print(line)

    if interrupted:
        print(
            f"\nspeedrun: interrupted after {len(results)} of {args.requests} requests",
            file=sys.stderr,
        )
        return int(ExitCode.INTERRUPTED)
    if summary.ok_count == 0:
        print("speedrun: error: no request succeeded, nothing to measure", file=sys.stderr)
        return int(ExitCode.CANNOT_MEASURE)
    return int(exit_code_for(summary))


def main(argv: Sequence[str] | None = None, transport: Transport | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_amain(args, transport))
    except KeyboardInterrupt:
        # On 3.10 the first Ctrl-C surfaces here without the task having printed its
        # partial summary; 3.11+ keeps NFR-4's full behaviour. Either way the process
        # must exit as interrupted (AC-13).
        return int(ExitCode.INTERRUPTED)


if __name__ == "__main__":
    sys.exit(main())
