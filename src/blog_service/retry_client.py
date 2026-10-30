"""Bounded retries for read requests and explicitly idempotent JSON writes."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class RetryDeadlineExceeded(TimeoutError):
    """The complete operation (attempts and backoff) exceeded its budget."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    deadline: float = 8.0
    connect_timeout: float = 1.0
    pool_timeout: float = 1.0
    read_timeout: float = 2.0
    write_timeout: float = 2.0
    backoff: float = 0.1
    jitter: float = 0.05

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        for name in (
            "deadline",
            "connect_timeout",
            "pool_timeout",
            "read_timeout",
            "write_timeout",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.backoff < 0 or self.jitter < 0:
            raise ValueError("backoff and jitter cannot be negative")

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            pool=self.pool_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
        )


def retry_after_seconds(value: str | None) -> float | None:
    """Read either Retry-After form; malformed values do not change backoff."""
    if not value:
        return None
    try:
        seconds = float(value)
        if seconds >= 0 and seconds != float("inf"):
            return seconds
        return None
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return max(0.0, (timestamp - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


async def retry_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a replayable request, returning the last HTTP response.

    A final network failure is raised unchanged. Exhausting the overall budget
    raises RetryDeadlineExceeded. HTTP error statuses remain responses so callers
    can inspect them or call raise_for_status(). Use a client with transport
    retries disabled; HTTPX's default transport does not retry automatically.
    Streaming request bodies and multipart uploads are deliberately unsupported
    for retried requests: callers must provide reusable bytes/string/JSON.
    """
    policy = policy or RetryPolicy()
    method = method.upper()
    headers = httpx.Headers(client.headers)
    headers.update(kwargs.get("headers") or {})
    eligible = method == "GET" or (method == "POST" and bool(headers.get("Idempotency-Key")))
    if eligible:
        content = kwargs.get("content")
        if content is not None and not isinstance(content, (bytes, str)):
            raise ValueError("Retried requests require a reusable bytes or string body")
        if kwargs.get("files"):
            raise ValueError("Multipart uploads are not supported by the retry client")
    kwargs.setdefault("timeout", policy.timeout())
    attempts = policy.max_attempts if eligible else 1
    transient_exceptions = (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
    )
    try:
        async with asyncio.timeout(policy.deadline):
            for attempt in range(attempts):
                retry_after = None
                try:
                    response = await client.request(method, url, **kwargs)
                except transient_exceptions:
                    if attempt + 1 == attempts:
                        raise
                else:
                    if response.status_code not in {502, 503, 504} or attempt + 1 == attempts:
                        return response
                    retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                    await response.aclose()
                delay = policy.backoff * (2**attempt) + random.uniform(0.0, policy.jitter)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                # The outer timeout also bounds Retry-After and all backoff sleeps.
                await asyncio.sleep(delay)
    except TimeoutError as exc:
        raise RetryDeadlineExceeded(
            f"Operation exceeded its {policy.deadline:g}s total deadline"
        ) from exc
    raise AssertionError("Retry loop ended without a response or exception")
