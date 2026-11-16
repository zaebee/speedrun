"""Unit tests for aggregation (SPEC FR-3.2, FR-3.3, FR-5.2, FR-6.5).

The interesting case is FR-6.5: throughput must be total_bytes / total_time, not
the mean of per-request speeds. A test with two requests of very different sizes
separates the two formulas by a factor, so a wrong implementation cannot pass.
"""

from __future__ import annotations

import pytest

from speedrun import FetchResult, Summary, median, summarize, throughput


def ok(seconds: float, nbytes: int) -> FetchResult:
    return FetchResult(ok=True, seconds=seconds, bytes=nbytes, error=None)


def fail(seconds: float = 0.5) -> FetchResult:
    return FetchResult(ok=False, seconds=seconds, bytes=0, error="HTTP 404")


class TestMedianAndMean:
    def test_empty_is_zero(self) -> None:
        # No samples must not raise; the caller checks counts before printing.
        assert median([]) == 0.0

    def test_single_element(self) -> None:
        assert median([2.5]) == 2.5

    def test_even_count_averages_middle_pair(self) -> None:
        assert median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_unsorted_input(self) -> None:
        assert median([5.0, 1.0, 3.0]) == 3.0


class TestSummarizeExcludesFailures:
    """FR-5.2: failed requests are out of both the average and the volume."""

    def test_all_failed_yields_no_samples(self) -> None:
        s = summarize([fail(), fail()])
        assert s.ok_count == 0
        assert s.fail_count == 2
        assert s.total_bytes == 0
        assert s.avg_seconds == 0.0
        assert s.median_seconds == 0.0
        assert s.throughput_bytes_per_sec == 0.0

    def test_failures_do_not_drag_the_average(self) -> None:
        # A fast failure must not pull avg down, nor add to size.
        s = summarize([ok(2.0, 200), ok(4.0, 400), fail(0.01)])
        assert s.ok_count == 2
        assert s.fail_count == 1
        assert s.total_bytes == 600
        assert s.avg_seconds == pytest.approx(3.0)
        assert s.total_seconds == pytest.approx(6.0)

    def test_partial_run_counts_only_successes(self) -> None:
        results = [ok(1.0, 100), fail(), ok(3.0, 300), fail(), fail()]
        s = summarize(results)
        assert s.ok_count == 2
        assert s.fail_count == 3
        assert s.total_bytes == 400
        assert s.avg_seconds == pytest.approx(2.0)


class TestThroughputFormula:
    """FR-6.5: aggregate rate, not mean of rates."""

    def test_two_requests_of_different_size(self) -> None:
        # 100 B in 1 s and 900 B in 3 s.
        #   correct: 1000 B / 4 s          = 250 B/s
        #   wrong:   mean(100 B/s, 300 B/s) = 200 B/s
        s = summarize([ok(1.0, 100), ok(3.0, 900)])
        assert s.throughput_bytes_per_sec == pytest.approx(250.0)
        assert s.throughput_bytes_per_sec != pytest.approx(200.0)

    def test_matches_explicit_helper(self) -> None:
        assert throughput(total_bytes=1000, total_seconds=4) == pytest.approx(250.0)

    def test_zero_time_does_not_raise(self) -> None:
        # Defensive: a sub-microsecond local resource could round to 0.0.
        assert throughput(total_bytes=100, total_seconds=0.0) == 0.0

    def test_zero_bytes_is_zero(self) -> None:
        assert throughput(total_bytes=0, total_seconds=5) == 0.0


class TestSummaryShape:
    def test_median_reported_separately_from_mean(self) -> None:
        # One outlier: mean moves, median stays. Both must be visible (FR-3.3).
        s = summarize([ok(1.0, 100), ok(1.0, 100), ok(11.0, 100)])
        assert s.avg_seconds == pytest.approx(4.3333333, abs=1e-6)
        assert s.median_seconds == pytest.approx(1.0)

    def test_summary_is_a_value_object(self) -> None:
        s = summarize([ok(1.0, 10)])
        assert isinstance(s, Summary)
        assert s.total_bytes == 10
