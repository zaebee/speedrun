"""Tests for the entry point's control flow (SPEC NFR-4, FR-1, AC-6..AC-13).

A real SIGINT cannot be delivered to a background process in this sandbox, so the
injected transport raises KeyboardInterrupt instead - exercising exactly the
branch main() must handle. That is a stronger test than a signal anyway: it is
deterministic and asserts on the partial summary, not just the exit code.
"""

from __future__ import annotations

import pytest

from speedrun import ExitCode, FetchResult, UsageError, main, validate_url


class ScriptedTransport:
    """Returns queued results; an Exception in the queue is raised instead."""

    def __init__(self, script: list[FetchResult | BaseException]) -> None:
        self.script = list(script)
        self.calls = 0

    def fetch(self, url: str, timeout: float) -> FetchResult:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def ok(seconds: float, nbytes: int) -> FetchResult:
    return FetchResult(ok=True, seconds=seconds, bytes=nbytes, error=None)


def bad(seconds: float, error: str) -> FetchResult:
    return FetchResult(ok=False, seconds=seconds, bytes=0, error=error)


class TestInterrupt:
    def test_returns_130(self) -> None:
        t = ScriptedTransport([ok(1.0, 1000), KeyboardInterrupt()])
        assert main(["https://x/y", "-n", "10"], transport=t) == int(ExitCode.INTERRUPTED)

    def test_partial_measurement_is_still_reported(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # NFR-4: a Ctrl-C run must not throw away what it already measured.
        t = ScriptedTransport([ok(1.0, 1024), ok(2.0, 1024), KeyboardInterrupt()])
        main(["https://x/y", "-n", "10"], transport=t)
        out = capsys.readouterr().out
        assert "2 ok / 0 failed" in out
        assert "2.00 KiB" in out

    def test_stops_requesting_after_interrupt(self) -> None:
        t = ScriptedTransport([ok(1.0, 10), KeyboardInterrupt()])
        main(["https://x/y", "-n", "50"], transport=t)
        assert t.calls == 2

    def test_interrupt_before_any_success_says_not_measured(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        t = ScriptedTransport([KeyboardInterrupt()])
        code = main(["https://x/y", "-n", "5"], transport=t)
        assert code == int(ExitCode.INTERRUPTED)
        assert "not measured" in capsys.readouterr().out


class TestUsagePaths:
    def test_bad_scheme_exits_two_without_network(self) -> None:
        # AC-7: nothing may be attempted when the URL is rejected.
        t = ScriptedTransport([ok(0.1, 1)])
        assert main(["ftp://x/y"], transport=t) == int(ExitCode.USAGE)
        assert t.calls == 0

    @pytest.mark.parametrize("args", [[], ["u", "-n", "0"], ["u", "--bogus"]])
    def test_usage_problems_exit_two(self, args: list[str]) -> None:
        # AC-6, AC-8, FR-1.4: argparse terminates with 2 itself.
        with pytest.raises(SystemExit) as excinfo:
            main(args)
        assert excinfo.value.code == 2


class TestExitCodesFromOutcomes:
    def test_all_failed_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        # AC-3, AC-4, FR-5.6
        t = ScriptedTransport([bad(0.1, "HTTP 404")])
        assert main(["https://x/y", "-n", "3"], transport=t) == int(ExitCode.CANNOT_MEASURE)
        assert "no request succeeded" in capsys.readouterr().err

    def test_partial_success_exits_zero(self) -> None:
        # AC-5: timeouts on some requests still yield a valid measurement.
        t = ScriptedTransport([ok(1.0, 1024), bad(0.2, "TimeoutError: timed out")])
        assert main(["https://x/y", "-n", "2"], transport=t) == int(ExitCode.OK)

    def test_no_traceback_on_failures(self, capsys: pytest.CaptureFixture[str]) -> None:
        # FR-5.5 / AC-4
        t = ScriptedTransport([bad(0.1, "URLError: name or service not known"), ok(1.0, 10)])
        main(["https://x/y", "-n", "2"], transport=t)
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err


class TestQuietMode:
    def test_quiet_prints_only_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        # AC-9
        t = ScriptedTransport([ok(1.0, 1024)] * 3)
        main(["https://x/y", "-n", "3", "-q"], transport=t)
        out = capsys.readouterr().out
        assert "[1/3]" not in out
        assert "Requests" in out


class TestValidateUrlUnit:
    def test_whitespace_is_trimmed(self) -> None:
        assert validate_url("  https://x/y  ") == "https://x/y"

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(UsageError):
            validate_url("")
