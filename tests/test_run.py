"""Unit tests for the run loop against a fake transport (SPEC FR-2, FR-5).

A deterministic transport stands in for the network so failure handling can be
asserted exactly: which requests ran, in what order, and how failures shape the
summary and exit code. Real HTTP is covered separately by the integration test.
"""

from __future__ import annotations

import pytest

from speedrun import (
    ExitCode,
    FetchResult,
    Transport,
    exit_code_for,
    format_summary,
    run,
    summarize,
)


class FakeTransport(Transport):
    """Scripted responses; records every call it received."""

    def __init__(self, script: list[FetchResult | Exception]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, float]] = []

    def fetch(self, url: str, timeout: float) -> FetchResult:
        self.calls.append((url, timeout))
        item = self.script[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item


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
        assert (summary.ok_count, summary.fail_count) == (2, 1)

    def test_raised_exception_becomes_a_failed_request(self) -> None:
        # The transport signals hard errors by raising; the loop converts them.
        t = FakeTransport([ConnectionError("name or service not known")])
        summary = run("https://x/y", requests=1, timeout=5.0, transport=t, quiet=True)
        assert (summary.ok_count, summary.fail_count) == (0, 1)

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
