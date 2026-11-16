# SPEC - console download-throughput probe

Detail level: FRD. This document pins behaviour precisely enough to implement
and verify without further clarification.

## 1. Problem statement

The script measures how fast a host serves a resource: it performs N sequential
requests against a URL, waits for each response body to be read in full, then
prints average request time, transferred volume and throughput.

Typical target is a heavy static asset (image, archive), where response time is
dominated by link bandwidth rather than server latency.

### Out of scope

- concurrent requests and load testing;
- measurement history, export to file/DB, charts;
- authentication against the target host, custom headers, retry with backoff;
- comparing several URLs against each other.

This is a 1-2 hour warmup: scope is limited to measuring. The items above are
separate tasks, not unfinished parts of this one.

## 2. Functional requirements

### FR-1. Input

| Parameter | Required | Default | Description |
|---|---|---|---|
| `url` (positional) | yes | - | Absolute `http://` or `https://` address |
| `-n, --requests` | no | `10` | Number of sequential requests |
| `--timeout` | no | `30` | Per-request timeout, seconds |
| `-q, --quiet` | no | `false` | Summary numbers only, no per-request progress |

Validation:

- **FR-1.1.** Missing `url` -> usage error, exit code `2`.
- **FR-1.2.** Scheme other than `http`/`https` -> error message, exit code `2`,
  no network request is made.
- **FR-1.3.** `--requests < 1` -> usage error, exit code `2`.
- **FR-1.4.** Unknown flag -> standard argparse message, exit code `2`.
- **FR-1.5.** `--timeout` must be finite and > 0; zero, negative, NaN and infinity
  are usage errors with exit code `2`. Zero would fail every attempt for a reason
  the user cannot see, and NaN silently disables the deadline comparison
  (`now() > nan` is always false).

Code `2` is kept distinct from `1` on purpose: "invoked wrong" differs from
"could not measure".

### FR-2. Request sequence

- **FR-2.1.** Exactly `N` requests, strictly one after another. The next starts
  only once the previous has fully completed or been recorded as failed.
- **FR-2.2.** Each response is read to the end. This requirement is substantive:
  a connection with an unread body is not a complete exchange and its volume is
  unknown. The body is read in chunks and discarded, never buffered whole.
- **FR-2.3.** The session is reused across requests (keep-alive). Otherwise a
  TCP+TLS handshake lands in every sample and the result measures something
  other than what was asked. A deliberate measurement-model choice, see NFR-3.
- **FR-2.4.** No delay between requests.
- **FR-2.5.** `Accept-Encoding` is pinned to `identity`, so the counted volume is
  the response body as delivered, with no decoding step in between (see FR-4.3 and
  open decision 1).
- **FR-4.5.** Terminology is fixed by this: the measured quantity is **HTTP response
  payload**. It is not "bytes on the wire" - request/response headers, chunked
  transfer framing, TLS record overhead and TCP/IP retransmissions are all outside
  the measurement. Reporting it as wire throughput would overstate what was observed.

### FR-3. Timing

- **FR-3.1.** Every attempt is timed, successful or not, including those ending in
  an exception: a failure that took 30 s recorded as 0.0 s hides exactly the slow
  pathologies worth seeing. One request's duration runs from just before the request is sent
  until the response body has been read completely. Monotonic clock
  (`time.monotonic`), not wall-clock: an NTP correction or clock change must not
  distort a measured duration.
- **FR-3.2.** Average = arithmetic mean over **successful** requests only.
- **FR-3.3.** Median is printed alongside. Reason: a single outlier (timeout,
  reconnect) drags the mean, and one number alone cannot reveal that it happened.
  Median is the cheapest way to keep the summary honest.
- **FR-3.4.** Wall time of the whole run is measured separately from the sum of
  per-request times; the difference exposes overhead (DNS, first handshake). It is
  populated by `run()` as well as by the CLI: a default of 0.0 is otherwise
  indistinguishable from an instantaneous run, and callers of the library API would
  get a wrong figure while the command line was correct.
- **FR-3.5.** `--timeout` is a deadline on the *whole* request, and it must bound
  every blocking step, not selected ones: connection setup, request write, response
  header read, each body read, and each redirect-body drain. Two implementation facts
  make this non-trivial, and both are asserted by tests:
  - Python's socket timeout bounds only the wait between two reads, so it is re-armed
    to the *remaining* budget before every step. A floor such as `max(0.1, remaining)`
    is forbidden: it lets an operation block past the deadline and makes the contract
    advisory. With no budget left, the attempt fails without opening a socket.
  - `read(n)` on a buffered response waits for a full n bytes, which puts any check
    between reads out of reach for a slow sender. Reads therefore use `read1`, which
    returns whatever has arrived. Measured against a local server streaming 2000
    one-byte writes: `read(65536)` blocked 4.4 s under a 0.6 s deadline; `read1`
    reached the deadline in 0.60 s.

  Explicitly outside the guarantee: DNS resolution (resolver timeouts are not ours)
  and preempting a single read already in progress. Recorded as `DEADLINE_SCOPE` in
  the module so the limitation travels with the code rather than only the docs.

### FR-4. Data volume

- **FR-4.1.** Bytes actually received from the network are counted, summed over
  all successful requests.
- **FR-4.2.** `Content-Length` is not treated as truth: chunked responses have
  none, and a declared value can disagree with what arrives.
- **FR-4.3.** With compression, compressed bytes are counted, not inflated ones.
  Link speed means link speed. `identity` encoding (FR-2.5) makes this moot for
  our own requests but the rule still governs how the number is interpreted.
- **FR-4.4.** Response header bytes are excluded (the unit is payload).

### FR-5. Error handling

- **FR-5.1.** One failed request does not abort the run. The remaining `N-1`
  still execute.
- **FR-5.2.** Failed requests are **excluded** from the average and from the
  volume. A truncated response has provably incomplete time *and* bytes;
  including them silently corrupts the metric.
- **FR-5.3.** The failure counter is always printed, including when it is zero.
  A run where 9 of 10 requests failed must be visible, not hidden.
- **FR-5.4.** HTTP status outside 2xx counts as a failed request, with the code
  reported. Measuring a 404 page says nothing about serving a heavy asset.
- **FR-5.4a.** Redirects (3xx with `Location`) are followed, up to 5 hops, and the
  status that decides success or failure is the **final** response's. A redirect to
  a missing resource is therefore a failure, while a CDN fronting its asset behind a
  302 is measured normally. Bounding the hops keeps a self-referential `Location`
  from hanging the run.
- **FR-5.5.** Failure reason is a short line (exception type, status code), no
  traceback. Stack traces appear only under `--debug`.
- **FR-5.6.** If no request succeeded: the summary reports the counts and states
  `Result  not measured - no request succeeded`, plus a message on stderr and exit
  code `1`. Printing `0.00 B/s` as though it were a measurement is worse than
  saying there is none: an empty sample's zeros are an artifact, not a result.

### FR-6. Output

Progress (unless `--quiet`): one line per request - index, status, duration,
volume.

Summary block, at minimum:

```
Requests        10 ok / 0 failed
Total size      482.10 MiB
Avg time        1.842 s   (median 1.790 s)
Throughput      26.17 MiB/s   (219.55 Mbps)
```

These figures are internally consistent: 10 requests averaging 1.842 s (sum
18.42 s) delivering 482.10 MiB gives 26.17 MiB/s, i.e. 219.55 Mbps. An example
in documentation that does not add up is worse than no example.

- **FR-6.1.** Speed is printed in **two units**: `MiB/s` and `Mbps`. The first
  matches "bytes as in the file", the second matches networking habit. Let the
  reader choose; the unit argument is settled up front.
- **FR-6.2.** Unit definitions are mandatory in the README (section 5).
- **FR-6.3.** Numbers use fixed precision; prefix scale is chosen automatically
  (KiB/MiB/GiB), likewise for the bit version.
- **FR-6.4.** Results go to `stdout`; warnings and errors to `stderr`. That makes
  `speedrun URL > out.txt` yield clean data. Per-request progress lines - including
  ones marked FAILED - are part of the record and stay on stdout; only operator
  diagnostics ("nothing to measure", `--debug` detail) go to stderr.
- **FR-6.5.** Average speed is computed as
  `total_bytes / sum(successful durations)`, **not** as the arithmetic mean of
  per-request speeds. The latter weights a short request the same as a long one.

## 3. Non-functional requirements

- **NFR-1. Python >= 3.11 (asyncio.timeout) with HTTPX as the single runtime dependency
  (revised - owner approval recorded in the PR).** The transport was originally
  stdlib-only on `http.client`; implementing keep-alive, redirect chains and an
  absolute request deadline by hand grew to 38% of the production code and produced
  defects of its own (a socket timeout armed on the wrong object, silently doing
  nothing). HTTPX owns HTTP semantics; this module owns measurement. See section 6,
  open decision 4.
- **NFR-2.** Constant memory: response bodies are not accumulated (FR-2.2), so
  peak RSS is independent of resource size.
- **NFR-3.** The measurement method and its limits are documented in the README:
  keep-alive makes results faster than cold requests would be; one run is one
  run, not a benchmark.
- **NFR-4.** Ctrl-C ends the run gracefully: print the summary for requests
  already completed, exit code `130`. A partial measurement beats a lost one.
- **NFR-5.** Code is typed (`mypy --strict` clean) and linted with `ruff`.
- **NFR-6.** Networking sits behind a transport interface: the module depends on
  `Transport.fetch(url, timeout) -> FetchResult`, not on `urllib` directly.
  This follows from the verification strategy (section 7): all numeric logic is
  pure functions testable without a network, leaving real HTTP as the single
  unit-untestable part covered by one end-to-end run. Side effect: FR-5.x can be
  tested deterministically against a fake transport.

## 4. Acceptance criteria

Verifiable claims. Each row answers yes or no, with no interpretation needed.

| ID | Check | Expected |
|---|---|---|
| AC-1 | `python3 speedrun.py <heavy image>` | 10 progress lines + summary, exit `0` |
| AC-2 | `--requests 3` | exactly 3 requests, average over 3 |
| AC-3 | `http://.../nonexistent-404` with `-n 5` | 5 FAILED lines, `0 ok / 5 failed`, `not measured`, exit `1`, no zeros presented as results |
| AC-4 | URL of a nonexistent host | DNS error per request, `0 ok / N failed`, exit `1`, no traceback |
| AC-5 | `--timeout` set just below the observed duration | some attempts hit the deadline (FR-3.5), successful ones still counted, partial bytes excluded, exit `0` |
| AC-6 | `speedrun.py` with no arguments | usage, exit `2` |
| AC-7 | `speedrun.py ftp://x/y` | scheme error, exit `2`, no requests |
| AC-8 | `--requests 0` | usage error, exit `2` |
| AC-9 | `--quiet` | summary block only, no progress lines |
| AC-10 | `> out.txt 2> err.txt` on a successful run | numbers in `out.txt`, `err.txt` empty |
| AC-11 | Compare `Total size` against direct download of the file | within 1% |
| AC-12 | Recompute `Throughput` as `Total size / sum of successful times` | matches output |
| AC-13 | Ctrl-C mid-run | partial summary, exit `130` |
| AC-14 | Heavy resource ~200 MiB | process does not grow RSS with file size (NFR-2) |

AC-11 and AC-12 verify that the metric measures what it claims, rather than
looking plausible.

## 5. Units (required in README)

- Volume: binary prefixes - `KiB` = 1024 bytes, `MiB` = 1024^2, `GiB` = 1024^3.
- Bitrate: decimal prefixes as in the networking industry - `Mbps` = 10^6 bit/s.
- Conversion: `Mbps = MiB/s * 8 * 1024^2 / 10^6 ~= MiB/s * 8.389`.
- Time: seconds, three decimals.

Both families are shown because mixing `MB` and `Mb` on one line produces errors
by a factor, not by a percentage.

## 6. Open decisions

Deliberate trade-offs, not forgotten cases.

1. **stdlib-only was dropped in favour of HTTPX** (supersedes the original decision).
   The hand-rolled `http.client` transport cost ~300 lines - pooling, redirect hops,
   stale-connection retry, socket re-arming, drain - and still shipped two real bugs
   (a no-op socket timeout and a deadline that trickle streams could outrun). HTTPX
   provides all of it as supported behaviour; this module keeps only measurement.
   Price: one dependency and its tree (httpcore, anyio, certifi, h11), and an
   event loop per run() call.
4. **Absolute deadline via asyncio.wait_for, not client timeouts.** HTTPX timeouts
   are per-phase and the read timeout resets on every arrival, so a trickling body is
   unbounded under them (measured: 4.4 s of trickle under a 0.6 s read timeout).
   wait_for around the whole exchange including body consumption is what makes
   --timeout absolute. Same non-interruptible phase as before: DNS runs in a worker
   thread; the deadline fires on time but the resolver may linger.
   Resolution: request `Accept-Encoding: identity` (FR-2.5), so received bytes
   equal the body. `http.client` with manual reads stays a fallback if that ever
   proves unacceptable.
2. **Median in the output** goes beyond the literal brief ("average time").
   Kept: without it a single number misleads. Trivially removable.
3. **The first request includes the handshake** and is therefore slower than the
   rest. No warm-up skip: at N=10 that discards 10% of the sample to hide an
   effect the median already reveals. A `--skip-first` flag is a cheap addition
   if wanted.

## 7. Verification strategy

TDD is applied surgically: test first, then implement - wherever a test is
possible before code exists and asserts something substantive.

**Unit tests (pytest, no network)** cover the parts that are easy to get wrong:

| Subject | Assertion | Requirement |
|---|---|---|
| `format_bytes` | prefix boundaries (1023 vs 1024, KiB/MiB/GiB) | FR-6.3, section 5 |
| `format_bitrate` | decimal prefixes, conversion ~= x8.389 | section 5, FR-6.1 |
| `mean` / `median` | empty list, single element, outliers | FR-3.2, FR-3.3 |
| `summarize` | mean over successes only, failures excluded | FR-3.2, FR-5.2 |
| `throughput` | `total_bytes / total_time`, not mean of speeds | FR-6.5 |
| `validate_url` | http/https accepted, ftp/scheme-less rejected | FR-1.2 |
| `parse_args` | defaults N=10 and timeout=30, `-n 0` errors | FR-1.3, FR-1.4 |
| run loop on fake transport | failures do not abort, excluded from stats | FR-5.1-5.3 |
| `UrllibTransport.fetch` on stubbed response + injected clock | whole-request deadline, partial bytes dropped | FR-3.5, FR-5.2 |
| `main` with interrupting transport | exit 130 and partial summary preserved | NFR-4, AC-13 |
| run loop, all requests fail | exit code 1, `0 ok / N failed` | FR-5.6, AC-3 |

**Integration test** (marked `network`, skipped when offline): a live URL with
`--requests 2`, asserting volume > 0 and that the numbers reconcile. It covers
what a mock cannot: FR-2.2 (body read to end) and FR-2.3 (keep-alive).

**Manual check:** AC-11 (volume vs direct download) and AC-12 (throughput vs
hand division) - these verify agreement with reality, not code correctness.

Tests are not required by the brief but ship in the repo: `pytest` runs with one
command. Test-only dependency: pytest. Runtime dependency: HTTPX (NFR-1, revised).
