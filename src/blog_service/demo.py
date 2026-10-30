"""Executable defence demo; fault injection exists only in this local harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from hypercorn.asyncio import serve
from hypercorn.config import Config
from sqlalchemy import func, select

from blog_service.core.config import Settings
from blog_service.core.model_registry import Post
from blog_service.main import create_app
from blog_service.retry_client import RetryPolicy, retry_request
from blog_service.seed import seed_database
from blog_service.users.auth import issue_token


class DelayFirstResponse:
    """Delay one POST response after the app has committed, before headers leave.

    This wrapper is never installed by the production/demo ``serve`` command.
    The random key is supplied by the harness, not an HTTP fault control.
    """

    def __init__(self, app, key: str, delay: float = 0.3):
        self.app = app
        self.key = key.encode("ascii")
        self.delay = delay
        self.delayed = False
        self.attempts = 0

    async def __call__(self, scope, receive, send):
        matches = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and dict(scope["headers"]).get(b"idempotency-key") == self.key
        )
        if matches:
            self.attempts += 1

        async def delayed_send(message):
            if matches and message["type"] == "http.response.start" and not self.delayed:
                self.delayed = True
                await asyncio.sleep(self.delay)
            await send(message)

        await self.app(scope, receive, delayed_send)


@asynccontextmanager
async def temporary_tls_server(app, settings: Settings, *, keep_alive_max_requests: int = 100_000):
    """Run a real TLS server on a free loopback port, with bounded shutdown."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    configuration = Config()
    configuration.bind = [f"127.0.0.1:{port}"]
    configuration.certfile = str(settings.cert_file)
    configuration.keyfile = str(settings.key_file)
    configuration.alpn_protocols = ["h2", "http/1.1"]
    configuration.keep_alive_max_requests = keep_alive_max_requests
    configuration.graceful_timeout = 1
    configuration.shutdown_timeout = 2
    configuration.accesslog = None
    configuration.errorlog = None
    stop = asyncio.Event()
    task = asyncio.create_task(serve(app, configuration, shutdown_trigger=stop.wait))
    base_url = f"https://127.0.0.1:{port}"
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    try:
        async with httpx.AsyncClient(verify=context, trust_env=False, timeout=1) as client:
            for _ in range(100):
                if task.done():
                    await task
                    raise RuntimeError("TLS server exited during startup")
                try:
                    response = await client.get(base_url + "/health")
                    response.raise_for_status()
                    break
                except (httpx.HTTPError, OSError):
                    await asyncio.sleep(0.05)
            else:
                raise RuntimeError("TLS server did not become healthy")
        yield base_url
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run_demo() -> dict:
    settings = Settings.load("demo")
    seed = await seed_database(settings, users=3, posts=30, comments=60)
    app = create_app(settings)
    delayed_key = f"demo-timeout-{uuid4().hex}"
    fault = DelayFirstResponse(app, delayed_key)
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    headers = {"Authorization": f"Bearer {issue_token(seed['user_id'], settings)}"}
    results = {"purpose": "Real TLS idempotency and retry demonstration", "checks": []}
    async with temporary_tls_server(fault, settings) as base_url:
        async with (
            httpx.AsyncClient(base_url=base_url, verify=context, trust_env=False) as h1,
            httpx.AsyncClient(base_url=base_url, verify=context, trust_env=False, http2=True) as h2,
        ):
            key = f"demo-post-{uuid4().hex}"
            write_headers = {**headers, "Idempotency-Key": key}
            payload = {"title": "SIS-2 demonstration", "body": "One logical write, safe retries."}
            first = await h1.post("/v1/posts", json=payload, headers=write_headers)
            replay = await h2.post("/v1/posts", json=payload, headers=write_headers)
            assert first.status_code == replay.status_code == 201
            assert first.http_version == "HTTP/1.1" and replay.http_version == "HTTP/2"
            assert first.content == replay.content
            post_id = first.json()["id"]
            results["checks"].append(
                {
                    "check": "cross_protocol_replay",
                    "id": post_id,
                    "versions": [first.http_version, replay.http_version],
                    "response_bytes": len(first.content),
                    "passed": True,
                }
            )
            conflict = await h2.post(
                "/v1/posts", json={**payload, "body": "Changed"}, headers=write_headers
            )
            assert conflict.status_code == 409
            results["checks"].append(
                {"check": "changed_body_conflict", "status": 409, "passed": True}
            )
            left = await h1.get(f"/v1/posts/{post_id}")
            right = await h2.get(f"/v1/posts/{post_id}")
            assert left.status_code == right.status_code == 200
            assert left.content == right.content
            results["checks"].append(
                {
                    "check": "identical_get_payload",
                    "response_bytes": len(left.content),
                    "passed": True,
                }
            )
            recovered = await retry_request(
                h2,
                "POST",
                "/v1/posts",
                json=payload,
                headers={**headers, "Idempotency-Key": delayed_key},
                policy=RetryPolicy(read_timeout=0.05, backoff=0.05, jitter=0, deadline=3),
            )
            assert recovered.status_code == 201 and fault.attempts == 2
            async with app.state.session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(Post)
                    .where(Post.id == UUID(recovered.json()["id"]))
                )
            assert count == 1
            results["checks"].append(
                {
                    "check": "timeout_after_commit_then_retry",
                    "attempts": fault.attempts,
                    "stored_resources": count,
                    "id": recovered.json()["id"],
                    "passed": True,
                }
            )
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON evidence output")
    args = parser.parse_args(argv)
    result = asyncio.run(run_demo())
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
