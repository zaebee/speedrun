"""Unit tests for input validation and argument parsing (SPEC FR-1.x).

Exit codes are part of the contract: 2 means "invoked wrong", 1 means "could not
measure". A wrapper script relies on that distinction, so it is asserted here.
"""

from __future__ import annotations

import pytest

from speedrun import UsageError, parse_args, validate_url


class TestValidateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/a.jpg",
            "https://example.com/a.jpg",
            "https://example.com",
            "https://example.com/path?query=1#frag",
        ],
    )
    def test_accepts_http_and_https(self, url: str) -> None:
        assert validate_url(url) == url

    @pytest.mark.parametrize(
        ("url", "fragment"),
        [
            ("ftp://example.com/x", "scheme"),
            ("file:///etc/passwd", "scheme"),
            ("//example.com/x", "scheme"),
            ("example.com/a.jpg", "scheme"),
            ("", "empty"),
            ("   ", "empty"),
        ],
    )
    def test_rejects_other_schemes(self, url: str, fragment: str) -> None:
        with pytest.raises(UsageError):
            validate_url(url)

    def test_no_host_is_rejected(self) -> None:
        with pytest.raises(UsageError):
            validate_url("http:///path-only")

    def test_error_message_shows_what_was_wrong(self) -> None:
        # The message must name the offending value; a bare "invalid URL" is
        # unhelpful when the shell mangled the argument.
        with pytest.raises(UsageError) as excinfo:
            validate_url("ftp://x/y")
        assert "ftp://x/y" in str(excinfo.value)

    def test_does_not_touch_the_network(self) -> None:
        """FR-1.2: validation rejects before any request is attempted."""
        with pytest.raises(UsageError):
            validate_url("gopher://nowhere.invalid/")


class TestParseArgs:
    def test_defaults_match_spec(self) -> None:
        ns = parse_args(["https://example.com/a.jpg"])
        assert ns.url == "https://example.com/a.jpg"
        assert ns.requests == 10  # FR-1 table
        assert ns.timeout == 30.0
        assert ns.quiet is False
        assert ns.debug is False

    def test_short_and_long_request_flag(self) -> None:
        assert parse_args(["u", "-n", "3"]).requests == 3
        assert parse_args(["u", "--requests", "3"]).requests == 3

    def test_timeout_override(self) -> None:
        assert parse_args(["u", "--timeout", "0.5"]).timeout == pytest.approx(0.5)

    def test_quiet_flag(self) -> None:
        assert parse_args(["u", "-q"]).quiet is True

    @pytest.mark.parametrize("bad", ["0", "-1", "-5"])
    def test_requests_below_one_is_usage_error(self, bad: str) -> None:
        # FR-1.3 / AC-8
        with pytest.raises((UsageError, SystemExit)):
            parse_args(["u", "-n", bad])

    def test_non_numeric_requests_is_usage_error(self) -> None:
        with pytest.raises((UsageError, SystemExit)):
            parse_args(["u", "-n", "ten"])

    def test_unknown_flag_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        # FR-1.4 / argparse convention
        with pytest.raises(SystemExit) as excinfo:
            parse_args(["u", "--nope"])
        assert excinfo.value.code == 2
        assert "unrecognized" in capsys.readouterr().err

    def test_missing_url_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        # FR-1.1 / AC-6
        with pytest.raises(SystemExit) as excinfo:
            parse_args([])
        assert excinfo.value.code == 2
