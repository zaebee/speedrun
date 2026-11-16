"""End-to-end test against a real URL (SPEC section 7, AC-1/AC-11/AC-12).

Skipped when offline. This is the only test that exercises what a local fixture
cannot: TLS, a real remote host, and byte counts against a file whose size is
published. It uses a fixed-size file so AC-11 is meaningful - a random-image
endpoint returns a different payload per request and makes it uncheckable.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest

from speedrun import FetchResult, HttpTransport, run

URL = "https://proof.ovh.net/files/1Mb.dat"
KNOWN_BYTES = 1_048_576


def _online() -> bool:
    try:
        socket.create_connection(("proof.ovh.net", 443), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.mark.network
@pytest.mark.skipif(not _online(), reason="no network access")
class TestAgainstRealHost:
    def test_two_requests_download_the_whole_file_each(self) -> None:
        summary = run(URL, requests=2, timeout=30.0, transport=HttpTransport(), quiet=True)
        assert summary.ok_count == 2
        # AC-11: measured volume matches the file's known size within 1%.
        assert summary.total_bytes == pytest.approx(2 * KNOWN_BYTES, rel=0.01)

    def test_summary_numbers_reconcile(self) -> None:
        # AC-12: throughput must equal total_bytes / sum of successful durations.
        summary = run(URL, requests=2, timeout=30.0, transport=HttpTransport(), quiet=True)
        expected = summary.total_bytes / summary.total_seconds
        assert summary.throughput_bytes_per_sec == pytest.approx(expected, rel=1e-9)

    def test_keep_alive_does_not_degrade_later_requests(self) -> None:
        # FR-2.3 observed over the public internet. The strict version of this
        # claim - counting accepted TCP sockets - lives in test_transport_live.py;
        # asserting an ordering on remote timings would only test the network.
        results: list[FetchResult] = []

        class Recording:
            def __init__(self, inner: HttpTransport) -> None:
                self.inner = inner

            async def fetch(self, url: str) -> FetchResult:
                r = await self.inner.fetch(url)
                results.append(r)
                return r

        run(URL, requests=4, timeout=30.0, transport=Recording(HttpTransport()), quiet=True)
        assert len(results) == 4
        assert all(r.ok for r in results)
        # First request pays DNS+TCP+TLS, so it cannot be quicker than the best
        # reused one. A reconnecting client would show up here as no such gap.
        assert results[0].seconds >= min(r.seconds for r in results[1:])

    def test_http_error_status_is_reported_not_raised(self) -> None:
        # AC-3 on a live host.
        summary = run(
            "https://proof.ovh.net/definitely-not-here",
            requests=1,
            timeout=30.0,
            transport=HttpTransport(),
            quiet=True,
        )
        assert summary.fail_count == 1
        assert summary.total_bytes == 0


@pytest.mark.network
@pytest.mark.skipif(not _online(), reason="no network access")
def test_identity_encoding_means_no_decoding_gap() -> None:
    """FR-2.5: our count equals the body as delivered, verified independently."""
    request = urllib.request.Request(URL, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=30) as response:
        direct = len(response.read())
    summary = run(URL, requests=1, timeout=30.0, transport=HttpTransport(), quiet=True)
    assert summary.total_bytes == direct
