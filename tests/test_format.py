"""Unit tests for unit formatting (SPEC section 5, FR-6.1, FR-6.3).

Written before the implementation. The prefix boundaries are exercised on
purpose: a naive implementation (one constant for everything, or decimal byte
units) is wrong by a factor, not by a percentage.
"""

from __future__ import annotations

import pytest

from speedrun import format_bitrate, format_bytes


class TestFormatBytes:
    """Binary prefixes: KiB = 1024, MiB = 1024**2, GiB = 1024**3."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (0, "0.00 B"),
            (1, "1.00 B"),
            (999, "999.00 B"),
            (1000, "1000.00 B"),  # below 1 KiB stays in bytes
            (1023, "1023.00 B"),  # last value expressed in bytes
            (1024, "1.00 KiB"),  # exact transition
            (1025, "1.00 KiB"),
            (1536, "1.50 KiB"),
            (1024**2 - 1, "1024.00 KiB"),  # rounds up at the threshold
            (1024**2, "1.00 MiB"),
            (1024**2 + 1024 // 2, "1.00 MiB"),
            (48 * 1024**2 + 220 * 1024, "48.21 MiB"),
            (1024**3, "1.00 GiB"),
            (1024**4, "1.00 TiB"),
            (1024**5, "1.00 PiB"),
        ],
    )
    def test_binary_prefixes(self, n: int, expected: str) -> None:
        assert format_bytes(n) == expected

    def test_negative_raises(self) -> None:
        """A negative size is impossible; printing it silently would be a bug."""
        with pytest.raises(ValueError):
            format_bytes(-1)

    def test_always_two_decimals(self) -> None:
        # Fixed precision keeps the summary column aligned (FR-6.3).
        for n in (1, 1024, 1024**2):
            value, _unit = format_bytes(n).split()
            assert len(value.split(".")[1]) == 2


class TestFormatBitrate:
    """Decimal prefixes as used in networking: kbps = 10**3 bit/s."""

    @pytest.mark.parametrize(
        ("bytes_per_sec", "expected"),
        [
            (0, "0.00 bps"),
            (1, "8.00 bps"),
            (124, "992.00 bps"),  # 999 bit/s is still bps
            (125, "1.00 kbps"),  # exactly 1000 bit/s
            (12_500, "100.00 kbps"),
            (125_000, "1.00 Mbps"),
            # 1 MiB/s == 8.389 Mbps: byte units binary, bit units decimal.
            (1024**2, "8.39 Mbps"),
            # Exactly 25 MiB/s -> 209.7152 Mbps -> 209.72.
            (25 * 1024**2, "209.72 Mbps"),
        ],
    )
    def test_decimal_prefixes(self, bytes_per_sec: float, expected: str) -> None:
        assert format_bitrate(bytes_per_sec) == expected

    def test_mib_per_sec_is_not_megabits(self) -> None:
        """Classic mistake: 1 MiB/s != 1 MB/s. Pin the conversion factor."""
        mbps_value = float(format_bitrate(1024**2).split()[0])
        assert mbps_value == pytest.approx(8.389, abs=0.01)

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            format_bitrate(-1)
