#!/usr/bin/env python3
"""speedrun - a console download-throughput probe.

Performs N sequential requests against a URL, waits for each response body to be
read in full, then reports average request time, transferred volume and
throughput.

Behaviour is specified in SPEC.md. Standard library only; no third-party deps.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import math
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

# --- Units (SPEC section 5) --------------------------------------------------

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_BIT_UNITS = ("bps", "kbps", "Mbps", "Gbps", "Tbps")

_CHUNK = 262_144  # 256 KiB reads: few syscalls, bounded memory


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
    # durations above. The gap between them is overhead (DNS, first handshake,
    # Python startup per request) and is the point of FR-3.4 - deriving it from
    # the sum would make the metric tautological.
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
    """Decide the exit status from a run's outcome (FR-5.6).

    Kept separate from ``Summary``: aggregation reports what was measured, this
    decides how the process should end.
    """
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
    infinities are rejected because they corrupt the deadline comparison silently:
    `now() > nan` is always False, so the deadline would never fire.
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
        help="per-request timeout in seconds (default: 30)",
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
        help="show exception tracebacks instead of one-line errors (FR-5.5)",
    )
    return parser.parse_args(argv)


# --- Transport seam (NFR-6) ---------------------------------------------------


class Transport(Protocol):
    """One fetch of a URL, body read to completion.

    Networking lives behind this interface so every numeric rule in the spec can
    be tested deterministically against a fake (section 7).
    """

    def fetch(self, url: str, timeout: float) -> FetchResult: ...

    def close(self) -> None: ...


@dataclass
class HttpTransport:
    """Real transport on ``http.client`` with one persistent connection per host.

    Why not ``urllib.request``: its handler sets ``Connection: close`` and closes
    the socket after every response, so it cannot reuse connections at all. The
    measurement we advertise is single-stream throughput *after* setup (FR-2.3),
    which requires keep-alive; a local-server test counts accepted TCP sockets to
    prove it, since comparing request durations proves nothing.

    Redirects are followed by hand rather than via urllib's opener, so the status
    that decides success or failure is the final response's (FR-5.4a).

    ``now`` is injectable so the deadline logic is tested against scripted time
    instead of sleeping.
    """

    now: Callable[[], float] = time.monotonic
    conn_factory: Callable[..., Any] | None = None

    _connections: dict[tuple[str, int], Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.conn_factory is None:
            self.conn_factory = _default_connection

    # -- public API -----------------------------------------------------------

    def fetch(self, url: str, timeout: float) -> FetchResult:
        start = self.now()
        deadline = start + timeout
        target = url
        for hop in range(_MAX_REDIRECTS + 1):
            result, redirect = self._fetch_one(target, deadline, start)
            if redirect is not None:
                target = redirect
                continue
            if hop == _MAX_REDIRECTS and result.error == "too many redirects":
                return result
            return result
        return FetchResult(ok=False, seconds=self.now() - start, bytes=0, error="unreachable")

    def close(self) -> None:
        # Best effort: a socket already broken by the peer still has to be released
        # locally, and shutdown must not raise on account of it.
        for conn in self._connections.values():
            with contextlib.suppress(OSError):
                conn.close()
        self._connections.clear()

    # -- internals ------------------------------------------------------------

    def _fetch_one(self, url: str, deadline: float, start: float) -> tuple[FetchResult, str | None]:
        try:
            scheme, host, port, path = _split_url(url)
        except ValueError as exc:
            # A malformed URL is this attempt's failure, not a fatal parse crash.
            return self._failure(exc, start), None
        if deadline - self.now() <= 0:
            return (
                FetchResult(
                    ok=False,
                    seconds=self.now() - start,
                    bytes=0,
                    error=f"exceeded {deadline - start:.0f} s deadline before sending",
                ),
                None,
            )
        try:
            response = self._exchange_resilient(scheme, host, port, path, deadline)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # Socket errors, protocol errors and malformed status lines all mean
            # "this attempt failed", never "stop the run" (FR-5.1).
            return self._failure(exc, start), None

        status = int(response.status)
        location = response.getheader("Location")
        if 300 <= status < 400 and location:
            # Drain before reuse, otherwise the next request on this connection
            # would read the redirect's body as its own.
            self._drain(response)
            return FetchResult(ok=True, seconds=0.0, bytes=0, error=None), _absolute(url, location)

        size, timed_out = self._read_body(response, deadline)
        seconds = self.now() - start
        if timed_out:
            self._drop((host, port))
            return (
                FetchResult(
                    ok=False,
                    seconds=seconds,
                    bytes=0,
                    error=f"exceeded deadline after {format_bytes(size)}",
                ),
                None,
            )
        if not 200 <= status < 300:
            return (
                FetchResult(ok=False, seconds=seconds, bytes=0, error=f"HTTP {status}"),
                None,
            )
        return FetchResult(ok=True, seconds=seconds, bytes=size, error=None), None

    def _read_body(self, response: Any, deadline: float) -> tuple[int, bool]:
        """Read to EOF, returning (payload bytes, deadline_exceeded).

        The deadline is checked after *every* read including the one that returns
        EOF: an empty final read that lands late is still a missed deadline, and
        checking only after non-empty chunks would report such a response as a
        success (FR-3.5). Each blocking read is bounded by the time left, so a
        stall cannot outlive the deadline either.
        """
        size = 0
        while True:
            remaining = deadline - self.now()
            if remaining <= 0:
                return size, True
            # Bound the blocking read itself by what is left of the deadline, so a
            # stalled transfer cannot outlive it between chunks (FR-3.5).
            _set_socket_timeout(response, remaining)
            try:
                chunk = response.read(_CHUNK)
            except TimeoutError:
                return size, True
            if not chunk:
                # EOF reached, but if that final read arrived late the deadline was
                # still missed - checking only after non-empty chunks would call
                # this a success.
                return size, self.now() > deadline
            size += len(chunk)
            if self.now() > deadline:
                return size, True

    @staticmethod
    def _drain(response: Any) -> None:
        """Consume a redirect's body so the pooled connection stays usable.

        Load-bearing, and pinned by TestRedirectWithBody: a 302 that also carries a
        payload leaves unread bytes on the socket, and http.client then raises
        ResponseNotReady on the next request through this connection.

        Failures while draining are ignored deliberately - the redirect decision is
        already made, and if the socket is genuinely broken the next request finds
        out for itself and gets a fresh connection. Only I/O-layer errors qualify;
        anything else is a bug and must surface.
        """
        # The body is read purely to advance the stream; its content is irrelevant
        # and deliberately discarded, which is what makes this loop's body empty.
        # Volume accounting for the attempt uses the final response only (FR-4.1),
        # asserted by TestRedirectWithBody.
        with contextlib.suppress(OSError, http.client.HTTPException):
            while response.read(65_536):
                continue

    def _exchange_resilient(
        self, scheme: str, host: str, port: int, path: str, deadline: float
    ) -> Any:
        """GET with one transparent retry on a stale pooled connection.

        Keep-alive has a known race: the server may close an idle connection at any
        moment, and the client only finds out when it writes to or reads from it.
        That surfaces as RemoteDisconnected on the first request after an idle
        period - not a real failure, so it is retried once on a fresh socket.
        Anything else propagates and becomes this request's outcome.
        """
        try:
            return self._exchange(scheme, host, port, path, deadline)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            dropped = self._drop((host, port))
            if not (dropped and self._is_reusable_error(exc)):
                raise
            return self._exchange(scheme, host, port, path, deadline)

    def _exchange(self, scheme: str, host: str, port: int, path: str, deadline: float) -> Any:
        """Send one GET on a pooled connection and return the response object."""
        conn = self._connection(scheme, host, port, deadline)
        conn.request("GET", path, headers={"Accept-Encoding": "identity"})
        return conn.getresponse()

    def _connection(self, scheme: str, host: str, port: int, deadline: float) -> Any:
        key = (host, port)
        conn = self._connections.get(key)
        if conn is None:
            assert self.conn_factory is not None
            conn = self.conn_factory(scheme, host, port, max(0.1, deadline - self.now()))
            self._connections[key] = conn
        return conn

    def _drop(self, key: tuple[str, int]) -> bool:
        conn = self._connections.pop(key, None)
        if conn is None:
            return False
        with contextlib.suppress(OSError):
            conn.close()
        return True

    @staticmethod
    def _is_reusable_error(exc: BaseException) -> bool:
        return isinstance(
            exc, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)
        )

    def _failure(self, exc: BaseException, start: float) -> FetchResult:
        """A failed attempt: real elapsed time, no bytes credited."""
        return FetchResult(ok=False, seconds=self.now() - start, bytes=0, error=_reason(exc))


_MAX_REDIRECTS = 5


def _default_connection(scheme: str, host: str, port: int, timeout: float) -> Any:
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, port, timeout=timeout)


def _set_socket_timeout(response: Any, seconds: float) -> None:
    """Re-arm the underlying socket for one read of at most ``seconds``.

    ``http.client`` exposes no public API for this; reaching for the socket is the
    only way to bound a blocking read by a *remaining* deadline rather than by the
    original full timeout. Guarded so an unexpected response object (a test fake)
    degrades to leaving the timeout as-is instead of crashing.
    """
    sock = getattr(getattr(response, "fp", None), "_sock", None) or getattr(response, "sock", None)
    if sock is not None:
        with contextlib.suppress(OSError, ValueError):
            sock.settimeout(max(0.001, seconds))


def _split_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, str(parsed.hostname), port, path


def _absolute(base: str, location: str) -> str:
    return urljoin(base, location)


# --- Run loop -----------------------------------------------------------------

Emit = Callable[[str], None]


def _reason(exc: BaseException) -> str:
    """Short single-line failure reason, never a traceback (FR-5.5)."""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def _progress_line(index: int, total: int, result: FetchResult) -> str:
    label = f"[{index}/{total}]"
    if result.ok:
        return f"{label} ok       {result.seconds:7.3f} s   {format_bytes(result.bytes)}"
    return f"{label} FAILED    {result.seconds:7.3f} s   {result.error or 'error'}"


def _attempt(
    transport: Transport, url: str, timeout: float, now: Callable[[], float]
) -> FetchResult:
    """Run one request, converting any transport exception into a failed result.

    The duration is measured here, around the call, so it is real on both paths:
    a failure that took 30 s must not be recorded as 0.0 s (FR-3.1). Exceptions
    escaping ``fetch`` are unexpected ones - the transport's own contract is to
    report ordinary network failures as a failed result.
    """
    start = now()
    try:
        return transport.fetch(url, timeout)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        # Deliberately broad, and tested to be so: FR-5.1 says one bad request must
        # never abort a run, which includes failures of our own making (a bug in the
        # transport raises whatever it likes). Only KeyboardInterrupt is re-raised,
        # because Ctrl-C is an instruction rather than an outcome.
        # See TestNoErrorEscapesTheTransport - narrowing this line breaks it.
        if isinstance(exc, Exception):
            return FetchResult(ok=False, seconds=now() - start, bytes=0, error=_reason(exc))
        raise


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

    Each attempt is timed with the monotonic clock (FR-3.1); exceptions raised by
    the transport become failed requests rather than aborting the loop (FR-5.1).
    """
    writer: Emit = emit if emit is not None else print
    results: list[FetchResult] = []
    for index in range(1, requests + 1):
        result = _attempt(transport, url, timeout, now)
        results.append(result)
        if not quiet:
            writer(_progress_line(index, requests, result))
    return summarize(results)


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


def _collect(
    transport: Transport, url: str, args: argparse.Namespace
) -> tuple[list[FetchResult], bool]:
    """Run requests until done or interrupted, returning (results, was_interrupted).

    Kept separate from main() so the interrupt handling - the only reason this
    loop cannot just call run() - is one small readable function.
    """
    results: list[FetchResult] = []
    for index in range(1, args.requests + 1):
        try:
            result = _attempt(transport, url, args.timeout, time.monotonic)
        except KeyboardInterrupt:
            return results, True
        results.append(result)
        if not args.quiet:
            print(_progress_line(index, args.requests, result))
        if args.debug and not result.ok:
            print(f"speedrun: request {index} failed: {result.error}", file=sys.stderr)
    return results, False


def main(argv: Sequence[str] | None = None, transport: Transport | None = None) -> int:
    args = parse_args(argv)
    try:
        url = validate_url(args.url)
    except UsageError as exc:
        print(f"speedrun: error: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)

    # Injectable for tests; production uses the real keep-alive transport.
    active: Transport = transport if transport is not None else HttpTransport()
    started = time.monotonic()
    try:
        results, interrupted = _collect(active, url, args)
    finally:
        active.close()
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


if __name__ == "__main__":
    sys.exit(main())
