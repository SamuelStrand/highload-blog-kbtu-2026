# HTTP transport benchmark results

Medians of per-trial statistics; latency percentiles include successful requests only.
Ranges and every individual trial are available in the accompanying CSV files.

| Protocol | Operation | Concurrency | p50 ms | p95 ms | p99 ms | Success RPS | Errors / requests | JSON response bytes |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| HTTP/1.1 | get_post | 20 | 45.395 | 293.280 | 350.373 | 219.915 | 0 / 100 | 2219.000 |
| HTTP/2 | get_post | 20 | 30.107 | 73.001 | 76.166 | 522.130 | 0 / 100 | 2219.000 |

## Interpretation limits

- JSON byte counts are body sizes, not network/TLS/HPACK sizes.
- Header-field bytes sum raw field-name and value lengths; they are not wire bytes.
- Both protocols use the same connection ceilings; actual TCP connection counts may differ.
- There are no automatic retries and no body compression in measured requests.
- This is a local closed-loop experiment, not proof of the SIS-1 production capacity targets.
- Per-request latency includes HTTPX connection-pool wait and complete response reading.
- TLS connections and application paths are warmed up before each measured trial.
