"""AC-14: peak memory must not grow with the size of the downloaded resource.

The claim rests on reading the body in chunks and discarding it (FR-2.2, NFR-2).
Verifying it requires measuring a child process, since the pytest process already
holds interpreter and library state; ru_maxrss of a subprocess running only the
script is the number that actually means something.

Marked `network` because it downloads real bytes from a fixed-size public file.
Run manually with:  pytest -m network tests/test_memory.py -s
which prints both measurements.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import speedrun

SCRIPT = Path(__file__).resolve().parent.parent / "speedrun.py"
SMALL = "https://proof.ovh.net/files/10Mb.dat"
LARGE = "https://proof.ovh.net/files/100Mb.dat"


def _peak_rss_kib(url: str) -> int:
    """Run the script in a child process and report its peak RSS in KiB."""
    code = (
        "import resource, sys;"
        "sys.argv=['speedrun', sys.argv[1], '-n', '1', '-q'];"
        "import speedrun;"
        "speedrun.main();"
        "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, url],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT.parent),
        timeout=180,
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip().splitlines()[-1])


@pytest.mark.network
class TestConstantMemory:
    def test_peak_memory_independent_of_resource_size(self) -> None:
        """A 10x larger download must not show up as proportional RSS growth.

        Threshold is deliberately loose: interpreter noise and allocator arenas
        dominate at this scale. The failure mode being guarded against is a
        whole-body buffer, which would add ~100 MiB (102_400 KiB) here.
        """
        small = _peak_rss_kib(SMALL)
        large = _peak_rss_kib(LARGE)
        print(f"\n  peak RSS: 10 MiB file -> {small} KiB, 100 MiB file -> {large} KiB")
        # Allow generous slack, but catch buffering: 100 MiB would blow past this.
        assert large < small + 20_480, f"memory grew with payload: {small} -> {large} KiB"

    def test_chunk_size_is_small_relative_to_payload(self) -> None:
        # Structural guard: if someone raises the read chunk to absurd sizes, the
        # constant-memory property degrades even while the RSS test above still
        # passes. Imported at module level so no sys.path mutation is needed.
        assert speedrun._CHUNK <= 1024 * 1024
