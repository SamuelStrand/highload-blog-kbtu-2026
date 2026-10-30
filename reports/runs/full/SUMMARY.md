# HTTP transport benchmark results

Medians of per-trial statistics; latency percentiles include successful requests only.
Ranges and every individual trial are available in the accompanying CSV files.

| Protocol | Operation | Concurrency | p50 ms | p95 ms | p99 ms | Success RPS | Errors / requests | JSON response bytes |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| HTTP/1.1 | get_post | 1 | 6.463 | 8.644 | 9.635 | 149.564 | 0 / 15000 | 2219.000 |
| HTTP/2 | get_post | 1 | 6.445 | 8.864 | 9.966 | 145.948 | 0 / 15000 | 2219.000 |
| HTTP/1.1 | get_post | 20 | 60.308 | 419.886 | 692.379 | 161.492 | 0 / 15000 | 2219.000 |
| HTTP/2 | get_post | 20 | 57.664 | 95.019 | 129.230 | 321.732 | 0 / 15000 | 2219.000 |
| HTTP/1.1 | list_posts | 1 | 5.211 | 8.647 | 9.886 | 165.158 | 0 / 15000 | 44519.000 |
| HTTP/2 | list_posts | 1 | 5.696 | 9.079 | 10.684 | 162.673 | 0 / 15000 | 44519.000 |
| HTTP/1.1 | list_posts | 20 | 66.071 | 474.860 | 787.813 | 142.867 | 0 / 15000 | 44519.000 |
| HTTP/2 | list_posts | 20 | 63.141 | 112.780 | 155.364 | 284.797 | 0 / 15000 | 44519.000 |
| HTTP/1.1 | create_post | 1 | 7.979 | 12.208 | 14.938 | 109.650 | 0 / 15000 | 45.000 |
| HTTP/2 | create_post | 1 | 9.075 | 16.744 | 20.721 | 95.286 | 0 / 15000 | 45.000 |
| HTTP/1.1 | create_post | 20 | 84.914 | 178.527 | 396.057 | 206.034 | 0 / 15000 | 45.000 |
| HTTP/2 | create_post | 20 | 85.744 | 116.926 | 135.669 | 232.914 | 0 / 15000 | 45.000 |

## Interpretation limits

- JSON byte counts are body sizes, not network/TLS/HPACK sizes.
- Header-field bytes sum raw field-name and value lengths; they are not wire bytes.
- Both protocols use the same connection ceilings; actual TCP connection counts may differ.
- There are no automatic retries and no body compression in measured requests.
- This is a local closed-loop experiment, not proof of the SIS-1 production capacity targets.
- Per-request latency includes HTTPX connection-pool wait and complete response reading.
- TLS connections and application paths are warmed up before each measured trial.
