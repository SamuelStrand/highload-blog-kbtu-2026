import asyncio

import httpx
import pytest

from blog_service.retry_client import RetryDeadlineExceeded, RetryPolicy, retry_request


async def test_transient_statuses_are_retried_with_bounded_backoff(monkeypatch):
    attempts, delays = [], []

    async def send(request):
        attempts.append(request)
        return httpx.Response([503, 502, 200][len(attempts) - 1])

    async def record_delay(delay):
        delays.append(delay)

    monkeypatch.setattr("blog_service.retry_client.asyncio.sleep", record_delay)
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        response = await retry_request(
            client, "GET", "https://example.test", policy=RetryPolicy(jitter=0)
        )
    assert response.status_code == 200
    assert len(attempts) == 3
    assert delays == [0.1, 0.2]


async def test_key_and_body_are_preserved_after_ambiguous_write_timeout():
    attempts = []

    async def send(request):
        attempts.append((request.headers["Idempotency-Key"], request.content))
        if len(attempts) == 1:
            raise httpx.ReadTimeout("Response lost", request=request)
        return httpx.Response(201, json={"id": "same-resource"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        response = await retry_request(
            client,
            "POST",
            "https://example.test",
            content=b'{"body":"unchanged"}',
            headers={"Idempotency-Key": "stable-key"},
            policy=RetryPolicy(backoff=0, jitter=0),
        )
    assert response.status_code == 201
    assert attempts == [("stable-key", b'{"body":"unchanged"}')] * 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
async def test_client_errors_are_not_retried(status):
    attempts = 0

    async def send(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        response = await retry_request(client, "GET", "https://example.test")
    assert response.status_code == status and attempts == 1


async def test_unkeyed_post_is_never_retried():
    attempts = 0

    async def send(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        response = await retry_request(
            client, "POST", "https://example.test", json={"body": "write"}
        )
    assert response.status_code == 503 and attempts == 1


async def test_network_failure_stops_at_maximum_attempts():
    attempts = 0

    async def send(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("Unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with pytest.raises(httpx.ConnectError):
            await retry_request(
                client, "GET", "https://example.test", policy=RetryPolicy(backoff=0, jitter=0)
            )
    assert attempts == 3


async def test_total_deadline_cancels_inflight_request():
    cancelled = False

    async def send(request):
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        finally:
            cancelled = True

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with pytest.raises(RetryDeadlineExceeded):
            await retry_request(
                client, "GET", "https://example.test", policy=RetryPolicy(deadline=0.02)
            )
    assert cancelled


async def test_retry_after_cannot_exceed_total_budget():
    attempts = 0

    async def send(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with pytest.raises(RetryDeadlineExceeded):
            await retry_request(
                client, "GET", "https://example.test", policy=RetryPolicy(deadline=0.02)
            )
    assert attempts == 1
