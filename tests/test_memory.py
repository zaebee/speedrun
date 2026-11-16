"""AC-14: peak memory must not grow with the size of the downloaded resource.

The claim rests on reading the body in chunks and discarding it (FR-2.2, NFR-2). It
is verified by measuring a child process, because the pytest process already holds
interpreter and library state; ru_maxrss of a subprocess running only this script is
the number that means something.

Two constraints shape the implementation:

* Platform. ``resource.getrusage`` is Unix-only. On Windows the test skips rather
  than fails, since there is no portable stdlib equivalent worth adding for a probe.
* Network. The measurement downloads real bytes from a fixed-size public file, so the
  test is marked ``network`` and skips when that host is unreachable - matching the
  documented workflow (``pytest -m "not network"`` must pass offline).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import speedrun

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SMALL = "https://proof.ovh.net/files/10Mb.dat"
LARGE = "https://proof.ovh.net/files/100Mb.dat"

_IS_UNIX = sys.platform.startswith(("linux", "darwin"))


def _download_and_measure(url: str) -> tuple[int, int, int]:
    """Return (peak RSS in KiB, payload bytes actually received) from a child process.

    The byte count is reported by the script itself rather than assumed: an earlier
    version of this test asserted only that the child exited 0, but the child called
    main() directly instead of sys.exit(main()), so it exited 0 even when every
    request failed. The test then passed while downloading nothing at all, which made
    the constant-memory result vacuous. Both numbers are checked by the caller now.
    """
    # Capture the summary line to get the reported volume, and let the child exit
    # with main()'s own status so a failed download cannot look like a success.
    wrapper = (
        "import contextlib, io, re, resource, sys\n"
        "sys.argv = ['speedrun', sys.argv[1], '-n', '1', '-q']\n"
        "import speedrun\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    rc = speedrun.main()\n"
        "text = buf.getvalue()\n"
        "sys.stderr.write(text)\n"
        "size = 0\n"
        "m = re.search(r'Total size\\s+([\\d.]+) (B|KiB|MiB|GiB)', text)\n"
        "if m:\n"
        "    mult = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}[m.group(2)]\n"
        "    size = int(float(m.group(1)) * mult)\n"
        "print(int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), size, rc)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", wrapper, url],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
        timeout=300,
    )
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    parts = tail.split()
    if len(parts) != 3:
        pytest.fail(
            f"measurement child produced no result: rc={proc.returncode} {proc.stderr[:200]}"
        )
    rss_kib, payload_bytes, rc = (int(value) for value in parts)
    return rss_kib, payload_bytes, rc


def _reachable() -> bool:
    import socket

    try:
        socket.create_connection(("proof.ovh.net", 443), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.mark.network
@pytest.mark.skipif(not _IS_UNIX, reason="resource.getrusage is Unix-only")
@pytest.mark.skipif(not _reachable(), reason="no network access to the measurement host")
class TestConstantMemory:
    def test_downloads_actually_happened(self) -> None:
        """Guard against the vacuous-pass failure mode described in _download_and_measure."""
        _, small_bytes, small_rc = _download_and_measure(SMALL)
        assert small_rc == 0, "10 MiB download did not succeed"
        assert small_bytes > 9_000_000, f"only {small_bytes} bytes were transferred"

    def test_peak_memory_independent_of_resource_size(self) -> None:
        """A 10x larger download must not show up as proportional RSS growth.

        Both runs are asserted to have transferred their full payload first; without
        that, comparing two no-op runs would trivially satisfy the memory bound.
        Threshold is deliberately loose because interpreter noise and allocator arenas
        dominate at this scale. The failure mode being guarded against is a whole-body
        buffer, which would add ~100 MiB (102_400 KiB).
        """
        small_rss, small_bytes, small_rc = _download_and_measure(SMALL)
        large_rss, large_bytes, large_rc = _download_and_measure(LARGE)
        assert small_rc == 0 and large_rc == 0, "a download failed; the comparison is meaningless"
        assert small_bytes > 9_000_000, small_bytes
        assert large_bytes > 99_000_000, large_bytes

        print(
            f"\n  peak RSS: 10 MiB ({small_bytes} B) -> {small_rss} KiB, "
            f"100 MiB ({large_bytes} B) -> {large_rss} KiB"
        )
        assert large_rss < small_rss + 20_480, (
            f"memory grew with payload: {small_rss} -> {large_rss} KiB"
        )

    def test_chunk_size_is_small_relative_to_payload(self) -> None:
        # Structural guard: if someone raises the read chunk to absurd sizes, the
        # constant-memory property degrades even while the RSS test above still
        # passes. Imported at module level so no sys.path mutation is needed.
        assert speedrun._CHUNK <= 1024 * 1024


def test_child_reporter_parses_the_summary_line() -> None:
    """The regex the memory test relies on, checked without any network.

    If the summary format changes, the parser silently returns 0 bytes and the
    download assertions start failing for an unrelated-looking reason. This pins the
    coupling in one place.
    """
    sample = (
        "Requests       1 ok / 0 failed\n"
        "Total size     100.00 MiB\n"
        "Avg time       6.200 s   (median 6.200 s)\n"
    )
    match = re.search(r"Total size\s+([\d.]+) (B|KiB|MiB|GiB)", sample)
    assert match
    mult = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match.group(2)]
    assert int(float(match.group(1)) * mult) == 104_857_600
