#!/usr/bin/env python3
"""speedrun - a console download-throughput probe.

Performs N sequential requests against a URL, waits for each response body to be
read in full, then reports average request time, transferred volume and
throughput.

Behaviour is specified in SPEC.md. Standard library only; no third-party deps.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol
from urllib.parse import urlparse

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


def summarize(results: Sequence[FetchResult]) -> Summary:
    """Aggregate a run, excluding failures from average and volume (FR-5.2)."""
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
        type=float,
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


@dataclass
class UrllibTransport:
    """Real transport on top of ``urllib.request``, reusing one connection pool.

    Session reuse matters for what we measure (FR-2.3): without keep-alive every
    sample would include a fresh TCP handshake - and, over https, a TLS one - so
    the number would describe connection setup rather than download speed.

    ``now`` is injectable so the deadline logic can be tested against scripted
    time instead of sleeping.
    """

    opener: Any = None
    now: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if self.opener is None:
            self.opener = urllib.request.build_opener()

    def fetch(self, url: str, timeout: float) -> FetchResult:
        # identity encoding keeps received bytes equal to the body, so the volume
        # reported really is what crossed the wire (FR-2.5, open decision 1).
        request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        start = self.now()
        size = 0
        status: int | None = None
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", None) or response.getcode())
                while True:
                    # Read in chunks and discard: buffering the whole body would
                    # make peak memory grow with the resource (NFR-2, FR-2.2).
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    # The socket timeout only bounds the wait between reads, so a
                    # fast server could stream past --timeout forever. Enforce it
                    # as a deadline on the whole request (FR-3.5).
                    elapsed = self.now() - start
                    if elapsed > timeout:
                        return FetchResult(
                            ok=False,
                            seconds=elapsed,
                            bytes=0,
                            error=f"exceeded {timeout:g} s deadline after {format_bytes(size)}",
                        )
        except urllib.error.HTTPError as exc:
            # A non-2xx answer says nothing about serving a heavy asset (FR-5.4).
            return FetchResult(
                ok=False, seconds=self.now() - start, bytes=0, error=f"HTTP {exc.code}"
            )
        seconds = self.now() - start
        if status is not None and not 200 <= status < 300:
            return FetchResult(ok=False, seconds=seconds, bytes=0, error=f"HTTP {status}")
        return FetchResult(ok=True, seconds=seconds, bytes=size, error=None)


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


def run(
    url: str,
    requests: int,
    timeout: float,
    transport: Transport,
    quiet: bool = False,
    emit: Emit | None = None,
) -> Summary:
    """Perform ``requests`` sequential fetches and aggregate the result.

    Each attempt is timed with the monotonic clock (FR-3.1); exceptions raised by
    the transport become failed requests rather than aborting the loop (FR-5.1).
    """
    writer: Emit = emit if emit is not None else print
    results: list[FetchResult] = []
    for index in range(1, requests + 1):
        try:
            result = transport.fetch(url, timeout)
        except Exception as exc:  # any transport failure is one bad request, not a fatal run
            result = FetchResult(ok=False, seconds=0.0, bytes=0, error=_reason(exc))
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
            (
                f"{'Throughput':<15}{format_bytes(s.throughput_bytes_per_sec)}/s"
                f"   ({format_bitrate(s.throughput_bytes_per_sec)})"
            ),
        ]
    )
    return lines


# --- Entry point --------------------------------------------------------------


def main(argv: Sequence[str] | None = None, transport: Transport | None = None) -> int:
    args = parse_args(argv)
    try:
        url = validate_url(args.url)
    except UsageError as exc:
        print(f"speedrun: error: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)

    # Injectable for tests; production uses the real urllib transport.
    active: Transport = transport if transport is not None else UrllibTransport()
    results: list[FetchResult] = []
    interrupted = False

    # The loop is spelled out here instead of calling run() so that Ctrl-C can
    # hand back whatever has already been measured (NFR-4, AC-13).
    for index in range(1, args.requests + 1):
        try:
            try:
                result = active.fetch(url, args.timeout)
            except Exception as exc:  # one bad request is not a fatal run
                result = FetchResult(ok=False, seconds=0.0, bytes=0, error=_reason(exc))
                if args.debug:
                    traceback.print_exc()
        except KeyboardInterrupt:
            interrupted = True
            break
        results.append(result)
        if not args.quiet:
            print(_progress_line(index, args.requests, result))

    summary = summarize(results)
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
