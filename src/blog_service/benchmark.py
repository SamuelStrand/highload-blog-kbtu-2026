"""Real TLS HTTP/1.1 versus HTTP/2 measurements; no automatic request retries."""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.metadata
import json
import math
import os
import platform
import ssl
import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psutil

OPERATIONS = ("get_post", "list_posts", "create_post")
RAW_FIELDS = (
    "trial",
    "protocol",
    "operation",
    "concurrency",
    "request_index",
    "status",
    "error",
    "latency_ms",
    "request_bytes",
    "response_bytes",
    "request_header_field_bytes",
    "response_header_field_bytes",
    "http_version",
)


def percentile(values: list[float], percent: float) -> float | None:
    """Linearly interpolated empirical quantile; empty successes stay missing."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize_trial(rows: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    if not rows or duration <= 0:
        raise ValueError("A trial requires observations and positive elapsed time")
    first = rows[0]
    successful = [row for row in rows if not row["error"]]
    latencies = [row["latency_ms"] for row in successful]
    return {
        **{key: first[key] for key in ("trial", "protocol", "operation", "concurrency")},
        "requests": len(rows),
        "successes": len(successful),
        "errors": len(rows) - len(successful),
        "error_rate": 1 - len(successful) / len(rows),
        "duration_s": duration,
        "successful_rps": len(successful) / duration,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "request_bytes": rows[0]["request_bytes"],
        "response_bytes_median": (
            statistics.median(row["response_bytes"] for row in successful) if successful else None
        ),
        "http_version_verified": any(row["status"] for row in rows)
        and all(row["http_version"] == first["protocol"] for row in rows if row["status"]),
    }


def aggregate_trials(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in summaries:
        grouped[(row["protocol"], row["operation"], row["concurrency"])].append(row)
    result = []
    metrics = ("p50_ms", "p95_ms", "p99_ms", "successful_rps", "error_rate")
    for (protocol, operation, concurrency), rows in grouped.items():
        entry = {
            "protocol": protocol,
            "operation": operation,
            "concurrency": concurrency,
            "repeats": len(rows),
            "requests": sum(row["requests"] for row in rows),
            "errors": sum(row["errors"] for row in rows),
            "response_bytes_median": statistics.median(
                row["response_bytes_median"]
                for row in rows
                if row["response_bytes_median"] is not None
            )
            if any(row["response_bytes_median"] is not None for row in rows)
            else None,
        }
        for metric in metrics:
            values = [row[metric] for row in rows if row[metric] is not None]
            entry[f"{metric}_median"] = statistics.median(values) if values else None
            entry[f"{metric}_min"] = min(values) if values else None
            entry[f"{metric}_max"] = max(values) if values else None
        result.append(entry)
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _number(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "N/A"


def _write_summary(output: Path, trials: list[dict[str, Any]]) -> None:
    aggregate = aggregate_trials(trials)
    _write_csv(output / "trials.csv", trials)
    _write_csv(output / "summary.csv", aggregate)
    lines = [
        "# HTTP transport benchmark results",
        "",
        "Medians of per-trial statistics; latency percentiles include successful requests only.",
        "Ranges and every individual trial are available in the accompanying CSV files.",
        "",
        "| Protocol | Operation | Concurrency | p50 ms | p95 ms | p99 ms | Success RPS | Errors / requests | JSON response bytes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate:
        lines.append(
            f"| {row['protocol']} | {row['operation']} | {row['concurrency']} | "
            f"{_number(row['p50_ms_median'])} | {_number(row['p95_ms_median'])} | "
            f"{_number(row['p99_ms_median'])} | {_number(row['successful_rps_median'])} | "
            f"{row['errors']} / {row['requests']} | {_number(row['response_bytes_median'])} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
            "- JSON byte counts are body sizes, not network/TLS/HPACK sizes.",
            "- Header-field bytes sum raw field-name and value lengths; they are not wire bytes.",
            "- Both protocols use the same connection ceilings; actual TCP connection counts may differ.",
            "- There are no automatic retries and no body compression in measured requests.",
            "- This is a local closed-loop experiment, not proof of the SIS-1 production capacity targets.",
            "- Per-request latency includes HTTPX connection-pool wait and complete response reading.",
            "- TLS connections and application paths are warmed up before each measured trial.",
            "",
        ]
    )
    (output / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    if not aggregate:
        return
    os.environ.setdefault("MPLCONFIGDIR", str(output / ".matplotlib-cache"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    groups = list(dict.fromkeys((r["operation"], r["concurrency"]) for r in aggregate))
    figure, axes = plt.subplots(1, 2, figsize=(13, 5), layout="constrained")
    for axis, metric, label in zip(
        axes, ("p95_ms", "p99_ms"), ("p95 latency (ms)", "p99 latency (ms)"), strict=True
    ):
        for offset, protocol in ((-0.2, "HTTP/1.1"), (0.2, "HTTP/2")):
            values = []
            for operation, concurrency in groups:
                record = next(
                    (
                        r
                        for r in aggregate
                        if r["protocol"] == protocol
                        and r["operation"] == operation
                        and r["concurrency"] == concurrency
                    ),
                    None,
                )
                value = record[f"{metric}_median"] if record else None
                values.append(value if value is not None else float("nan"))
            axis.bar([i + offset for i in range(len(groups))], values, 0.38, label=protocol)
        axis.set_xticks(range(len(groups)), [f"{op}\nc={c}" for op, c in groups], rotation=25)
        axis.set_ylabel(label)
        axis.set_title("Median of per-trial percentiles")
        axis.grid(axis="y", alpha=0.25)
        axis.legend()
    figure.savefig(output / "latency.png", dpi=150)
    plt.close(figure)


def _header_bytes(headers: httpx.Headers) -> int:
    return sum(len(name) + len(value) for name, value in headers.raw)


def _requests(
    operation: str, count: int, base_url: str, manifest: dict, token: str
) -> list[tuple[str, str, bytes, dict[str, str]]]:
    body = json.dumps(
        {"title": "Benchmark post", "body": "x" * 2048}, separators=(",", ":")
    ).encode("utf-8")
    result = []
    for _ in range(count):
        headers = {"Accept-Encoding": "identity", "Accept": "application/json"}
        if operation == "create_post":
            headers.update(
                {
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": uuid4().hex,
                    "Content-Type": "application/json",
                }
            )
            result.append(("POST", f"{base_url}/v1/posts", body, headers))
        elif operation == "get_post":
            result.append(("GET", f"{base_url}/v1/posts/{manifest['post_id']}", b"", headers))
        else:
            result.append(
                ("GET", f"{base_url}/v1/authors/{manifest['user_id']}/posts?limit=20", b"", headers)
            )
    return result


async def _measure_batch(
    client: httpx.AsyncClient,
    prepared: list[tuple[str, str, bytes, dict]],
    *,
    concurrency: int,
    protocol: str,
    operation: str,
    trial: int,
) -> tuple[list[dict[str, Any]], float]:
    rows: list[dict[str, Any] | None] = [None] * len(prepared)
    next_index = 0

    async def worker() -> None:
        nonlocal next_index
        while next_index < len(prepared):
            index = next_index
            next_index += 1
            method, url, body, headers = prepared[index]
            # Request JSON/key generation is outside the measured interval.
            request = client.build_request(method, url, content=body, headers=headers)
            started = time.perf_counter_ns()
            response = None
            error = ""
            try:
                response = await client.send(request)
            except httpx.HTTPError as exc:
                error = type(exc).__name__
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if response is not None:
                if response.http_version != protocol:
                    raise RuntimeError(
                        f"Protocol fallback invalidates benchmark: expected {protocol}, "
                        f"received {response.http_version}"
                    )
                expected_status = 201 if method == "POST" else 200
                if response.status_code != expected_status:
                    error = f"unexpected_status:{response.status_code}"
            rows[index] = {
                "trial": trial,
                "protocol": protocol,
                "operation": operation,
                "concurrency": concurrency,
                "request_index": index,
                "status": response.status_code if response is not None else 0,
                "error": error,
                "latency_ms": elapsed_ms,
                "request_bytes": len(body),
                "response_bytes": len(response.content) if response is not None else 0,
                "request_header_field_bytes": _header_bytes(request.headers),
                "response_header_field_bytes": _header_bytes(response.headers)
                if response is not None
                else 0,
                "http_version": response.http_version if response is not None else "",
            }

    started = time.perf_counter()
    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(prepared))):
            group.create_task(worker())
    duration = time.perf_counter() - started
    return [row for row in rows if row is not None], duration


async def run_benchmark(args: argparse.Namespace) -> Path:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from blog_service.core.config import Settings
    from blog_service.core.database import create_engine
    from blog_service.seed import seed_database
    from blog_service.users.auth import issue_token

    settings = Settings.load("bench")
    if make_url(settings.database_url).database != "sis2_blog_bench":
        raise RuntimeError("Benchmark refuses to reset a database other than sis2_blog_bench")
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    base_url = args.base_url.rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("The benchmark requires HTTPS and ALPN")
    async with httpx.AsyncClient(verify=context, trust_env=False) as probe:
        response = await probe.get(f"{base_url}/health")
        response.raise_for_status()
        if response.json().get("database") != "sis2_blog_bench":
            raise RuntimeError("Start the target server with: blogctl serve --database bench")

    output = args.output or settings.root / "reports" / "runs" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    metadata: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "base_url": base_url,
        "configuration": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "logical_cpu_count": psutil.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total,
        },
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("fastapi", "hypercorn", "httpx", "httpcore", "h2", "sqlalchemy", "asyncpg")
        },
        "server": {
            "workers": 1,
            "db_pool_size": settings.db_pool_size,
            "db_max_overflow": 0,
            "keep_alive_timeout_seconds": 30,
            "keep_alive_max_requests": 100000,
            "h2_max_concurrent_streams": 100,
        },
        "client": {
            "connection_limits": "max_connections=max_keepalive_connections=concurrency",
            "retries": 0,
            "body_compression": False,
            "timeout_seconds": {"connect": 1, "pool": 1, "read": 2, "write": 2},
        },
        "dataset": {
            "users": args.seed_users,
            "posts": args.seed_posts,
            "comments": args.seed_comments,
            "post_body_bytes": 2048,
            "comment_body_bytes": 256,
            "reset_before_every_trial": True,
        },
        "methodology": {
            "latency": "HTTPX send through full response read, including connection pool wait",
            "percentile": "linear interpolation, successful requests only",
            "rps": "successful requests / full measured batch duration",
            "header_sizes": "sum of field-name and value bytes, NOT wire/HPACK bytes",
            "wire_size_measured": False,
            "workload": "closed-loop fixed concurrency on localhost",
        },
    }
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            metadata["versions"]["postgresql"] = await connection.scalar(text("SELECT version()"))
    finally:
        await engine.dispose()
    summaries: list[dict[str, Any]] = []
    metadata_path = output / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    try:
        with (output / "observations.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=RAW_FIELDS)
            writer.writeheader()
            for operation in args.operations:
                for concurrency in args.concurrency:
                    for repeat in range(1, args.repeats + 1):
                        protocols = ("HTTP/1.1", "HTTP/2") if repeat % 2 else ("HTTP/2", "HTTP/1.1")
                        for protocol in protocols:
                            manifest = await seed_database(
                                settings,
                                reset=True,
                                users=args.seed_users,
                                posts=args.seed_posts,
                                comments=args.seed_comments,
                            )
                            token = issue_token(manifest["user_id"], settings, expires_seconds=3600)
                            prepared = _requests(
                                operation, args.warmup + args.requests, base_url, manifest, token
                            )
                            async with httpx.AsyncClient(
                                http2=protocol == "HTTP/2",
                                verify=context,
                                trust_env=False,
                                limits=httpx.Limits(
                                    max_connections=concurrency,
                                    max_keepalive_connections=concurrency,
                                ),
                                timeout=httpx.Timeout(connect=1, pool=1, read=2, write=2),
                            ) as client:
                                if args.warmup:
                                    warm_rows, _ = await _measure_batch(
                                        client,
                                        prepared[: args.warmup],
                                        concurrency=concurrency,
                                        protocol=protocol,
                                        operation=operation,
                                        trial=repeat,
                                    )
                                    if any(row["error"] for row in warm_rows):
                                        raise RuntimeError(
                                            "Warm-up failed; check server logs before measuring"
                                        )
                                rows, duration = await _measure_batch(
                                    client,
                                    prepared[args.warmup :],
                                    concurrency=concurrency,
                                    protocol=protocol,
                                    operation=operation,
                                    trial=repeat,
                                )
                            writer.writerows(rows)
                            stream.flush()
                            summary = summarize_trial(rows, duration)
                            summaries.append(summary)
                            _write_csv(output / "trials.csv", summaries)
                            print(
                                f"{operation} c={concurrency} repeat={repeat} {protocol}: "
                                f"p99={_number(summary['p99_ms'])}ms "
                                f"RPS={summary['successful_rps']:.1f} errors={summary['errors']}",
                                flush=True,
                            )
        _write_summary(output, summaries)
        metadata["status"] = "completed"
        metadata["completed_trials"] = len(summaries)
        metadata["measured_requests"] = sum(row["requests"] for row in summaries)
    except BaseException as exc:
        metadata["status"] = "failed"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        metadata["completed_trials"] = len(summaries)
        raise
    finally:
        metadata["finished_at"] = datetime.now(UTC).isoformat()
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Benchmark artifacts: {output}")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 20])
    parser.add_argument("--operations", nargs="+", choices=OPERATIONS, default=list(OPERATIONS))
    parser.add_argument("--seed-users", type=int, default=100)
    parser.add_argument("--seed-posts", type=int, default=10000)
    parser.add_argument("--seed-comments", type=int, default=20000)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name in ("requests", "repeats", "seed_users", "seed_posts", "seed_comments"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.warmup < 0 or any(value < 1 for value in args.concurrency):
        parser.error("warmup must be nonnegative and concurrency values positive")
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
