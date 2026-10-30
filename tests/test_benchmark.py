import httpx
import pytest

from blog_service.benchmark import _measure_batch, aggregate_trials, summarize_trial


async def test_measurement_keeps_errors_and_real_body_lengths():
    attempts = 0

    async def send(request):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise httpx.ReadTimeout("Simulated failure", request=request)
        return httpx.Response(200, content=b'{"ok":true}', extensions={"http_version": b"HTTP/2"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        rows, duration = await _measure_batch(
            client,
            [("GET", "https://example.test", b"", {})] * 3,
            concurrency=1,
            protocol="HTTP/2",
            operation="get_post",
            trial=1,
        )
    summary = summarize_trial(rows, duration)
    assert attempts == 3  # No hidden retries in measurement.
    assert summary["successes"] == 2 and summary["errors"] == 1
    assert summary["response_bytes_median"] == 11
    assert rows[1]["error"] == "ReadTimeout"
    assert summary["successful_rps"] == 2 / duration


async def test_http2_fallback_invalidates_trial():
    async def send(request):
        return httpx.Response(200, extensions={"http_version": b"HTTP/1.1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with pytest.raises(ExceptionGroup) as caught:
            await _measure_batch(
                client,
                [("GET", "https://example.test", b"", {})],
                concurrency=1,
                protocol="HTTP/2",
                operation="get_post",
                trial=1,
            )
    assert "Protocol fallback" in str(caught.value.exceptions[0])


def test_latency_excludes_failed_requests_and_aggregation_reports_ranges():
    base = {
        "trial": 1,
        "protocol": "HTTP/2",
        "operation": "get_post",
        "concurrency": 1,
        "request_bytes": 0,
        "response_bytes": 10,
        "http_version": "HTTP/2",
        "status": 200,
    }
    rows = [
        {**base, "latency_ms": value, "error": error}
        for value, error in [(1, ""), (3, ""), (999, "unexpected_status:503")]
    ]
    summary = summarize_trial(rows, 1)
    assert summary["p50_ms"] == 2
    assert summary["p99_ms"] < 3
    assert summary["errors"] == 1 and summary["successful_rps"] == 2
    aggregate = aggregate_trials([summary, {**summary, "p99_ms": 100}, {**summary, "p99_ms": 200}])[
        0
    ]
    assert aggregate["p99_ms_median"] == 100
    assert aggregate["p99_ms_min"] == summary["p99_ms"]
    assert aggregate["p99_ms_max"] == 200


def test_all_network_failures_cannot_claim_verified_protocol():
    row = {
        "trial": 1,
        "protocol": "HTTP/2",
        "operation": "get_post",
        "concurrency": 1,
        "request_bytes": 0,
        "response_bytes": 0,
        "http_version": "",
        "status": 0,
        "latency_ms": 2,
        "error": "ConnectError",
    }
    summary = summarize_trial([row], 1)
    assert summary["http_version_verified"] is False
    assert summary["p99_ms"] is None
    assert summary["error_rate"] == 1
