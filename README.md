# speedrun

Console throughput probe. Point it at a URL, it performs sequential requests,
waits for each response to be fully received, and reports average request time,
total volume and download speed.

Built for measuring how fast a host serves a heavy static asset — an image, an
archive — where response time is dominated by link bandwidth rather than server
latency.

```console
$ python3 speedrun.py https://proof.ovh.net/files/10Mb.dat -n 5
[1/5] ok         0.628 s   10.00 MiB
[2/5] ok         0.617 s   10.00 MiB
[3/5] ok         0.621 s   10.00 MiB
[4/5] ok         0.619 s   10.00 MiB
[5/5] ok         0.622 s   10.00 MiB

Requests       5 ok / 0 failed
Total size     50.00 MiB
Avg time       0.621 s   (median 0.620 s)
Run wall time  3.108 s   (3.105 s in requests)
Throughput     16.10 MiB/s   (135.06 Mbps)
```

## Requirements

Python 3.11 or newer, plus [HTTPX](https://www.python-httpx.org) — the only runtime
dependency, and it owns the HTTP semantics (connections, redirects, decoding) while
the script owns measurement:

```console
pip install httpx
# or: pip install -e .
```

Everything else is standard library.

## Usage

```console
python3 speedrun.py <url> [-n N] [--timeout SEC] [-q] [--debug]
```

| Flag | Default | Meaning |
|---|---|---|
| `url` | — | Absolute `http(s)://` address to fetch repeatedly |
| `-n`, `--requests` | `10` | Number of sequential requests |
| `--timeout` | `30` | Deadline for a whole request, seconds; must be > 0 and finite |
| `-q`, `--quiet` | off | Print the summary block only |
| `--debug` | off | Show tracebacks instead of one-line error reasons |

Make it executable if you prefer `./speedrun.py`:

```console
chmod +x speedrun.py && ./speedrun.py https://example.com/big.iso
```

### Examples

```console
# 20 requests against a large file
python3 speedrun.py https://proof.ovh.net/files/100Mb.dat -n 20

# Machine-readable-ish: summary only, no progress lines
python3 speedrun.py https://proof.ovh.net/files/10Mb.dat -q

# Fail fast on a slow link
python3 speedrun.py https://proof.ovh.net/files/10Mb.dat --timeout 2

# Capture results without warnings mixed in
python3 speedrun.py https://proof.ovh.net/files/10Mb.dat > out.txt 2> err.txt
```

Good test targets: `https://proof.ovh.net/files/{1,10,100}Mb.dat` (fixed size, so
you can verify the reported volume against the real file). Random-image endpoints
such as `picsum.photos` work but return a different payload per request, which
makes the numbers harder to interpret.

## Units

Both families are printed because they answer different questions, and mixing
them up is an error by a factor, not a percentage.

| Quantity | Unit | Definition |
|---|---|---|
| Volume | `KiB`, `MiB`, `GiB` | Binary prefixes: KiB = 1024 bytes, MiB = 1024², GiB = 1024³ |
| Speed | `MiB/s` | Same binary bytes, per second — "how fast the file arrived" |
| Speed | `Mbps` | Megabits per second, decimal: 1 Mbps = 10⁶ bit/s — networking convention |
| Time | `s` | Seconds, three decimals |

Conversion between the two speeds:

```
Mbps = MiB/s × 8 × 1024² / 10⁶ ≈ MiB/s × 8.389
```

So `16.10 MiB/s` above is `135.06 Mbps`, not `128.8` — the difference is exactly
the binary-vs-decimal prefix mismatch.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | At least one request succeeded; the measurement is valid |
| `1` | No request succeeded — nothing could be measured |
| `2` | Bad invocation: missing URL, unsupported scheme, invalid flag value |
| `130` | Interrupted with Ctrl-C; the summary shows what was already measured |

`1` and `2` are deliberately distinct: "could not measure" and "invoked wrong"
need different reactions from whatever wraps this script.

## How the measurement works

The choices below change the numbers, so they are documented rather than hidden:

- **Sequential, never concurrent.** Request *k+1* starts after *k* has been read to
  completion. This measures single-stream throughput, not server capacity.
- **One persistent connection per host (HTTP/1.1 keep-alive), provided by HTTPX.**
  Without it every sample includes a fresh TCP handshake — and over HTTPS a TLS one —
  so the result describes connection setup rather than transfer. Measured effect on a
  10 MiB file: first request 0.62 s, subsequent ones 0.16 s. Consequence to keep in
  mind — these numbers are better than cold-start downloads would be.
- **Responses are requested uncompressed** (`Accept-Encoding: identity`), so the
  counted volume is the response body exactly as delivered, with no decoding step
  in between. Note what that volume is *not*: it counts HTTP payload bytes only, so
  request and response headers, chunk framing, and TLS/TCP/IP overhead are excluded.
  The figure is application-payload throughput, not physical wire utilization.
- **Volume counts bytes received**, not `Content-Length`. A chunked response has no
  declared length, and a declared length can disagree with what arrives.
- **Body is streamed in 64 KiB chunks and discarded**, so peak memory stays constant
  regardless of resource size. Verified: downloading a 100 MiB file uses the same
  identical peak RSS (~33 MB, mostly the interpreter) as a 10 MiB one. Measured by
  `tests/test_memory.py`, which also asserts both downloads really transferred their
  full payload — without that check the test passes while downloading nothing.
- **`--timeout` is an absolute deadline on the whole request.** HTTPX's own timeouts
  are per-phase and its read timeout resets whenever any byte arrives, so a trickling
  server could run arbitrarily long past them (measured: 4.4 s of trickle under a
  0.6 s read timeout). The deadline is therefore enforced with `asyncio.timeout`
  around the entire exchange, including body consumption. Not covered: DNS resolution
  (resolver timeouts are not ours); the deadline fires on time, but a resolver thread
  may linger.
- **Environment proxies are ignored** (`trust_env=False`): a developer's
  `http_proxy` must not redirect what the measurement actually connects to.
- **Redirects are followed** (up to 5 hops) and the *final* response status decides
  success. A redirect ending in 404 fails; a CDN answering with a 302 in front of its
  asset measures normally. The hop limit stops a self-referential `Location`.
- **Failed requests are excluded** from the average and from the volume, and counted
  separately. A truncated response has provably incomplete time *and* bytes;
  averaging them in would silently corrupt the metric.
- **Median is shown next to the mean** because a single outlier drags the mean and
  leaves no trace of having done so.
- **Throughput is `total_bytes / total_time`**, not the average of per-request
  speeds. Averaging rates weights a short request the same as a long one and
  understates the link.

Interpret results accordingly: one run is one run, not a benchmark, and keep-alive
makes these numbers better than cold-start downloads would be.

## Tests

```console
pip install pytest            # test-only dependency; the script needs none
pytest                        # unit tests plus live-network integration tests
pytest -m "not network"       # offline-safe subset only
```

Unit tests cover the numeric core without touching the network: unit formatting
and prefix boundaries, mean/median/throughput formulas, argument parsing, exit
codes, failure handling, and the request deadline (against a stubbed transport and
an injected clock). Integration tests hit a real fixed-size file to verify that the
measured volume matches the known file size and that connection reuse behaves as
claimed; they skip automatically when there is no network access.

Behaviour is specified in [SPEC.md](SPEC.md); acceptance criteria AC-1 … AC-14 map
onto those tests.

## Layout

```
speedrun.py        the script (HTTP semantics delegated to HTTPX)
SPEC.md            requirements, acceptance criteria, design decisions
tests/             pytest suite
pyproject.toml     tool configuration (ruff, mypy, pytest markers)
```
