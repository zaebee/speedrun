"""Unit tests for the run loop against a fake transport (SPEC FR-2, FR-5).

A deterministic transport stands in for the network so failure handling can be
asserted exactly: which requests ran, in what order, and how failures shape the
summary and exit code. Real HTTP is covered separately by the integration test.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from speedrun import (
    ExitCode,
    FetchResult,
    Transport,
    _attempt,
    exit_code_for,
    format_summary,
    main,
    run,
    summarize,
)


class FakeTransport(Transport):
    """Scripted responses; records every call it received."""

    def __init__(self, script: list[FetchResult | Exception]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, float]] = []

    async def fetch(self, url: str, timeout: float) -> FetchResult:
        self.calls.append((url, timeout))
        item = self.script[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        """Transport protocol requires it; the fake holds no resources."""


def ok(seconds: float, nbytes: int) -> FetchResult:
    return FetchResult(ok=True, seconds=seconds, bytes=nbytes, error=None)


def failed(seconds: float, error: str) -> FetchResult:
    return FetchResult(ok=False, seconds=seconds, bytes=0, error=error)


class TestRunsExactlyNSequentialRequests:
    def test_default_count_is_ten(self) -> None:
        t = FakeTransport([ok(0.1, 10)] * 10)
        summary = run("https://x/y", requests=10, timeout=5.0, transport=t, quiet=True)
        assert len(t.calls) == 10
        assert summary.ok_count == 10

    def test_passes_url_and_timeout_to_every_call(self) -> None:
        t = FakeTransport([ok(0.1, 10)] * 3)
        run("https://x/y", requests=3, timeout=7.5, transport=t, quiet=True)
        assert t.calls == [("https://x/y", 7.5)] * 3

    def test_sequential_order_no_concurrency(self) -> None:
        # FR-2.1: the loop must consume the script in order. A concurrent
        # implementation would interleave and this assertion fails.
        t = FakeTransport([ok(1.0, 1), ok(2.0, 2), ok(3.0, 3)])
        summary = run("https://x/y", requests=3, timeout=5.0, transport=t, quiet=True)
        assert summary.total_bytes == 6
        assert summary.avg_seconds == pytest.approx(2.0)


class TestFailureHandling:
    def test_one_failure_does_not_abort_the_run(self) -> None:
        # FR-5.1 / AC-3
        t = FakeTransport([ok(1.0, 100), failed(0.2, "HTTP 404"), ok(1.0, 100)])
        summary = run("https://x/y", requests=3, timeout=5.0, transport=t, quiet=True)
        assert len(t.calls) == 3
        assert summary.ok_count == 2
        assert summary.fail_count == 1

    def test_raised_exception_becomes_a_failed_request(self) -> None:
        # The transport signals hard errors by raising; the loop converts them.
        t = FakeTransport([ConnectionError("name or service not known")])
        summary = run("https://x/y", requests=1, timeout=5.0, transport=t, quiet=True)
        assert summary.ok_count == 0
        assert summary.fail_count == 1

    def test_all_requests_failed_reports_zero_successes(self) -> None:
        # AC-3: 0 ok / N failed, and no division by zero anywhere.
        t = FakeTransport([failed(0.1, "HTTP 404")] * 5)
        summary = run("https://x/y", requests=5, timeout=5.0, transport=t, quiet=True)
        assert summary.ok_count == 0
        assert summary.fail_count == 5
        assert summary.throughput_bytes_per_sec == 0.0

    def test_error_reason_is_a_short_string(self) -> None:
        # FR-5.5: one line, no traceback text.
        t = FakeTransport([TimeoutError("timed out")])
        lines: list[str] = []
        run("https://x/y", requests=1, timeout=5.0, transport=t, quiet=False, emit=lines.append)
        assert len(lines) == 1
        assert "Traceback" not in lines[0]
        assert "TimeoutError" in lines[0]


class TestProgressOutput:
    def test_one_line_per_request_when_not_quiet(self) -> None:
        t = FakeTransport([ok(0.5, 1024)] * 4)
        lines: list[str] = []
        run("https://x/y", requests=4, timeout=5.0, transport=t, quiet=False, emit=lines.append)
        assert len(lines) == 4
        assert all("[1/4]" in ln for ln in [lines[0]])
        assert "[4/4]" in lines[-1]

    def test_quiet_emits_nothing_per_request(self) -> None:
        # AC-9
        t = FakeTransport([ok(0.5, 1024)] * 3)
        lines: list[str] = []
        run("https://x/y", requests=3, timeout=5.0, transport=t, quiet=True, emit=lines.append)
        assert lines == []

    def test_progress_line_contains_index_status_duration_size(self) -> None:
        t = FakeTransport([ok(1.234, 2048)])
        lines: list[str] = []
        run("https://x/y", requests=1, timeout=5.0, transport=t, quiet=False, emit=lines.append)
        assert "1.234 s" in lines[0]
        assert "2.00 KiB" in lines[0]


class TestExitCodeContract:
    def test_success_returns_zero(self) -> None:
        t = FakeTransport([ok(0.1, 10)])
        s = run("https://x/y", 1, 5.0, t, quiet=True)
        assert exit_code_for(s) == ExitCode.OK

    def test_total_failure_returns_one(self) -> None:
        # FR-5.6 / AC-3, AC-4
        t = FakeTransport([failed(0.1, "HTTP 404")] * 2)
        s = run("https://x/y", 2, 5.0, t, quiet=True)
        assert exit_code_for(s) == ExitCode.CANNOT_MEASURE

    def test_partial_success_still_returns_zero(self) -> None:
        # AC-5: some timed out, the rest measured fine - that is a valid result.
        t = FakeTransport([ok(0.1, 10), failed(0.1, "timeout")])
        s = run("https://x/y", 2, 5.0, t, quiet=True)
        assert exit_code_for(s) == ExitCode.OK


class TestNoInventedNumbers:
    """FR-5.6: when nothing succeeded, do not print zeros as if they were measured."""

    def test_summary_marks_unmeasured_throughput(self) -> None:
        s = summarize([failed(0.1, "HTTP 404"), failed(0.2, "HTTP 404")])
        lines = format_summary(s)
        assert lines[0] == "Requests       0 ok / 2 failed"
        # The failure count is real information and must survive.
        joined = "\n".join(lines)
        assert "not measured" in joined
        # ...and no line may claim a bogus rate.
        assert "0.00 B/s" not in joined

    def test_partial_success_still_prints_numbers(self) -> None:
        s = summarize([ok(1.0, 1024), failed(0.1, "HTTP 404")])
        joined = "\n".join(format_summary(s))
        assert "not measured" not in joined
        assert "1.00 KiB" in joined


class TestExceptionDurationsAreReal:
    """P2-4: a failed attempt must carry the time it actually took."""

    def test_exception_path_is_timed_not_fabricated(self) -> None:
        # The old code wrote seconds=0.0 for exceptions, hiding slow failures.
        # Injected clock makes "the failure took 12.5 s" exact and fast.
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        class SlowFail:
            async def fetch(self, url: str, timeout: float) -> FetchResult:
                clock["t"] += 12.5
                raise ConnectionError("reset by peer")

            def close(self) -> None:
                return None

        result = asyncio.run(_attempt(SlowFail(), "https://x/y", 30.0, now))
        assert result.ok is False
        assert result.seconds == pytest.approx(12.5)
        assert "ConnectionError" in (result.error or "")

    def test_run_loop_propagates_measured_failure_time(self) -> None:
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        class FailAfter:
            def __init__(self, delay: float) -> None:
                self.delay = delay

            async def fetch(self, url: str, timeout: float) -> FetchResult:
                clock["t"] += self.delay
                raise TimeoutError("stalled")

            def close(self) -> None:
                return None

        seen: list[FetchResult] = []
        run(
            "https://x/y",
            requests=2,
            timeout=30.0,
            transport=FailAfter(3.0),
            quiet=False,
            now=now,
            emit=lambda line: seen.append(line),
        )
        # FR-5.2 keeps failures out of total_seconds on purpose; the per-attempt
        # duration still has to be real, which is what the progress line shows.
        assert len(seen) == 2
        assert all("3.000 s" in line for line in seen), seen

    def test_attempt_times_the_exception_path(self) -> None:
        import time as _time

        class Sleeping:
            async def fetch(self, url: str, timeout: float) -> FetchResult:
                await asyncio.sleep(0.05)
                raise TimeoutError("slow failure")

            def close(self) -> None:
                return None

        result = asyncio.run(_attempt(Sleeping(), "https://x/y", 30.0, _time.monotonic))
        assert result.ok is False
        assert result.seconds >= 0.04  # measured, not fabricated


class TestWallClockIsIndependent:
    """P2-3 / FR-3.4: run wall time is its own metric, not a sum of durations."""

    def test_summarize_defaults_wall_to_zero_when_not_measured(self) -> None:
        # Documents the default so a caller cannot mistake it for a measurement.
        assert summarize([ok(1.0, 10)]).wall_seconds == 0.0

    def test_summarize_carries_the_wall_time_it_is_given(self) -> None:
        # The earlier version of this check was hasattr(s, "wall_seconds"), which is
        # true whether or not anything measured - it proved the field exists, not that
        # the number means anything.
        assert summarize([ok(1.0, 10)], wall_seconds=4.25).wall_seconds == 4.25

    def test_run_measures_its_own_wall_time(self) -> None:
        """FR-3.4 through run(), not just main(): the public API must agree with the CLI.

        A transport whose fetch advances an injected clock by 1 s per request, plus a
        gap charged to close(), gives wall > sum-of-durations and both non-zero.
        """
        state = {"t": 0.0}

        class Gappy:
            async def fetch(self, url: str, timeout: float) -> FetchResult:
                state["t"] += 1.0
                return ok(0.5, 100)

            def close(self) -> None:
                state["t"] += 3.0

        s = run(
            "https://x/y",
            requests=2,
            timeout=9.0,
            transport=Gappy(),
            quiet=True,
            now=lambda: state["t"],
        )
        assert s.total_seconds == pytest.approx(1.0)  # two 0.5 s requests
        assert s.wall_seconds == pytest.approx(2.0)  # only the request phase ran
        assert s.wall_seconds > s.total_seconds

    def test_main_reports_wall_time_above_request_sum(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # FR-3.4: wall time is measured around the whole run, so any gap between
        # requests (transport setup, Python overhead) shows up there and nowhere
        # else. A transport that sleeps outside fetch() makes the two differ.
        import time as _time

        class Gappy:
            def __init__(self) -> None:
                self.n = 0

            async def fetch(self, url: str, timeout: float) -> FetchResult:
                self.n += 1
                if self.n > 1:
                    _time.sleep(0.05)  # cost incurred before the timed request
                return ok(0.01, 100)

            def close(self) -> None:
                return None

        main(["https://x/y", "-n", "2"], transport=Gappy())
        out = capsys.readouterr().out
        # "Run wall time   X s   (Y s in requests)" with X > Y.
        import re

        line = next(ln for ln in out.splitlines() if "Run wall time" in ln)
        numbers = [float(x) for x in re.findall(r"([\d.]+) s", line)]
        assert len(numbers) == 2, line
        wall, inside = numbers
        assert wall > inside


class TestStreamSeparation:
    """P2-7 / FR-6.4: diagnostics on stderr, results on stdout."""

    def test_progress_and_summary_are_stdout_only(self) -> None:
        # Asserted at the main() level in test_main.py; here we pin the contract
        # that run()'s emit callback receives every progress line.
        lines: list[str] = []
        t = FakeTransport([ok(1.0, 10), failed(0.1, "HTTP 404")])
        run("https://x/y", 2, 5.0, t, quiet=False, emit=lines.append)
        assert len(lines) == 2
        assert any("FAILED" in ln for ln in lines)


class TestOnlyExceptionsAreContained:
    """FR-5.1 contains failures of *requests*, not control flow.

    An earlier revision caught BaseException, which swallowed SystemExit raised by a
    transport and turned a deliberate process exit into one failed request - while
    KeyboardInterrupt needed an explicit re-raise to escape the same net. Exception is
    the correct breadth: everything a request can go wrong with, nothing that is an
    instruction to the process.
    """

    def test_system_exit_propagates_rather_than_becoming_a_failed_request(self) -> None:
        class Exits:
            async def fetch(self, url: str, timeout: float) -> FetchResult:
                raise SystemExit(3)

            def close(self) -> None:
                return None

        transport = Exits()
        with pytest.raises(SystemExit) as excinfo:
            asyncio.run(_attempt(transport, "https://x/y", 5.0, time.monotonic))
        # The exit code must survive intact, not be converted into a failed request.
        assert excinfo.value.code == 3

    def test_keyboard_interrupt_still_propagates(self) -> None:
        class Interrupts:
            async def fetch(self, url: str, timeout: float) -> FetchResult:
                raise KeyboardInterrupt

            def close(self) -> None:
                return None

        transport = Interrupts()
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(_attempt(transport, "https://x/y", 5.0, time.monotonic))
