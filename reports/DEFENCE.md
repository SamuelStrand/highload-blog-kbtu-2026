# SIS-2 defence walkthrough

## 1. Explain the scope

The service implements the posts, author feeds and comments designed in SIS-1.
Python was approved by the instructor. HTTP/2 is a refinement for this assignment.
The evaluated protocol and baseline carry the same REST/JSON messages, isolating
transport effects while sharing business code, serialization and persistence.

The SIS-1 Redis, replicas, outbox and Kubernetes deployment describe a production
design. The submitted prototype implements one application and PostgreSQL.

## 2. Start the service

```powershell
uv sync --frozen
uv run --frozen blogctl setup
uv run --frozen blogctl serve
```

In another terminal:

```powershell
uv run --frozen blogctl verify-protocols
uv run --frozen blogctl demo
```

Point to the verified `HTTP/1.1` and `HTTP/2` values. Enabling HTTP/2 on a client
alone does not prove negotiation.

## 3. Explain the live demonstration

The automated demo performs these requests through a real TLS server:

1. Create a post over HTTP/1.1 using a Bearer token and an idempotency key.
2. Repeat the same write over HTTP/2; the saved ID, status and response bytes match.
3. Change the body with the same key; the service returns `409`.
4. Read the created post over both protocols; the JSON bytes match.
5. Delay one response after its transaction commits. The client hits a read
   timeout, retries with the same key, and retrieves the saved result. The
   database contains one resource.

The delay wrapper belongs to the local demo/tests. Normal API routes expose no
fault injection control. The demo's short timeout makes the scenario visible;
the ordinary client uses the documented 1/2-second timeouts and 8-second budget.

Explain that a timeout reports what the client observed. It cannot establish
whether a server-side write committed. Atomic idempotency makes that ambiguity safe.

## 4. Show correctness evidence

```powershell
uv run --frozen pytest -q
```

Discuss concurrent duplicate submissions, rollback before commit, reuse of a
saved response after app restart, JWT validation, cursor ordering and bounded
retries. These checks support the implementation; the measured comparison is
the assignment's required performance evidence.

## 5. Show the transport comparison

Use the completed artifacts linked in [SIS2_REPORT.md](SIS2_REPORT.md). Explain
the dataset, warm-up, 36 trials, concurrency 1/20 and alternating protocol order.
Show `SUMMARY.md`, `trials.csv`, `metadata.json` and `latency.png`; a selected row
in `observations.csv` provides the raw status, latency, bytes and negotiated version.

To reproduce all measurements, start a separate benchmark server:

```powershell
uv run --frozen blogctl serve --database bench --port 8444
```

In another terminal:

```powershell
uv run --frozen blogctl benchmark --base-url https://localhost:8444
```

For a short live check, add `--requests 40 --warmup 20 --repeats 1`. A short run
checks the pipeline; the full recorded matrix supplies the submitted statistics.

## 6. Explain the findings honestly

- JSON serialization is shared, so the same content has the same body size.
- HTTP/2 multiplexes streams and compresses headers, while actual speed depends
  on concurrency, connection handling, the database, client and operating system.
- Recorded header-field lengths are logical sizes. TLS/HPACK wire sizes were not measured.
- The p50/p95/p99 columns cover successful requests; errors and successful RPS
  are reported separately. Summaries use medians of per-trial statistics.
- These are local, fixed-concurrency measurements. The SIS-1 production goals
  of 4,000 read RPS, 200 write RPS and p99 below 120 ms remain design targets.
