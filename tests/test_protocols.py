"""Protocol negotiation and timeout checks use actual TLS sockets."""

import ssl
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text

from blog_service.core.database import create_engine
from blog_service.core.model_registry import Post
from blog_service.demo import DelayFirstResponse, temporary_tls_server
from blog_service.main import create_app
from blog_service.retry_client import RetryPolicy, retry_request


@pytest.mark.integration
@pytest.mark.parametrize("route", ["post", "author", "comments"])
async def test_read_transaction_closes_before_network_response(settings, seeded, route):
    application = create_app(settings)
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    paths = {
        "post": f"/v1/posts/{seeded['post_id']}",
        "author": f"/v1/authors/{seeded['user_id']}/posts",
        "comments": f"/v1/posts/{seeded['post_id']}/comments",
    }
    path = paths[route]
    observed = []
    probe_engine = create_engine(settings)

    async def inspect_response(scope, receive, send):
        async def inspect_send(message):
            if scope.get("path") == path and message["type"] == "http.response.start":
                # Check while the response has not reached the client yet. A
                # network disconnect at this point must not pin a read lock.
                async with probe_engine.connect() as connection:
                    observed.append(
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                                "AND state='idle in transaction'"
                            )
                        )
                    )
            await send(message)

        await application(scope, receive, inspect_send)

    try:
        async with temporary_tls_server(inspect_response, settings) as base_url:
            async with httpx.AsyncClient(verify=context, trust_env=False, http2=True) as client:
                response = await client.get(base_url + path)
                assert response.status_code == 200
        assert observed == [0]
    finally:
        await probe_engine.dispose()


@pytest.mark.integration
async def test_both_protocols_share_payload_and_write_result(settings, seeded, auth_headers):
    application = create_app(settings)
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    async with temporary_tls_server(application, settings) as base_url:
        async with (
            httpx.AsyncClient(base_url=base_url, verify=context, trust_env=False) as h1,
            httpx.AsyncClient(base_url=base_url, verify=context, trust_env=False, http2=True) as h2,
        ):
            path = f"/v1/posts/{seeded['post_id']}"
            first, second = await h1.get(path), await h2.get(path)
            assert first.http_version == "HTTP/1.1"
            assert second.http_version == "HTTP/2"
            assert first.status_code == second.status_code == 200
            assert first.content == second.content
            for path in (
                f"/v1/authors/{seeded['user_id']}/posts?limit=2",
                f"/v1/posts/{seeded['post_id']}/comments?limit=1",
            ):
                left, right = await h1.get(path), await h2.get(path)
                assert left.content == right.content
            headers = {**auth_headers, "Idempotency-Key": uuid4().hex}
            payload = {"title": "Across protocols", "body": "same JSON"}
            original = await h1.post("/v1/posts", json=payload, headers=headers)
            replay = await h2.post("/v1/posts", json=payload, headers=headers)
            assert original.status_code == replay.status_code == 201
            assert original.content == replay.content
            comment_headers = {**auth_headers, "Idempotency-Key": uuid4().hex}
            path = f"/v1/posts/{seeded['post_id']}/comments"
            original = await h2.post(path, json={"body": "Shared comment"}, headers=comment_headers)
            replay = await h1.post(path, json={"body": "Shared comment"}, headers=comment_headers)
            assert original.status_code == replay.status_code == 201
            assert original.content == replay.content


@pytest.mark.integration
@pytest.mark.parametrize("http2", [False, True])
async def test_socket_timeout_after_commit_does_not_duplicate(
    settings, seeded, auth_headers, http2
):
    application = create_app(settings)
    key = uuid4().hex
    wrapper = DelayFirstResponse(application, key, delay=0.3)
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    async with temporary_tls_server(wrapper, settings) as base_url:
        async with httpx.AsyncClient(
            base_url=base_url, verify=context, trust_env=False, http2=http2
        ) as client:
            response = await retry_request(
                client,
                "POST",
                "/v1/posts",
                headers={**auth_headers, "Idempotency-Key": key},
                json={"title": "Lost response", "body": "The database committed already"},
                policy=RetryPolicy(read_timeout=0.05, backoff=0.05, jitter=0, deadline=3),
            )
            assert response.status_code == 201
            assert response.http_version == ("HTTP/2" if http2 else "HTTP/1.1")
            assert wrapper.delayed and wrapper.attempts == 2
            async with application.state.session_factory() as session:
                count = await session.scalar(
                    select(func.count()).select_from(Post).where(Post.title == "Lost response")
                )
                assert count == 1
                assert await session.get(Post, UUID(response.json()["id"])) is not None


@pytest.mark.integration
async def test_saved_response_survives_app_restart(settings, seeded, auth_headers):
    headers = {**auth_headers, "Idempotency-Key": uuid4().hex}
    payload = {"title": "Durable retry", "body": "Across engine and server restart"}
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    bodies = []
    for _ in range(2):
        application = create_app(settings)
        async with temporary_tls_server(application, settings) as base_url:
            async with httpx.AsyncClient(verify=context, trust_env=False, http2=True) as client:
                response = await client.post(base_url + "/v1/posts", json=payload, headers=headers)
                assert response.status_code == 201
                bodies.append(response.content)
    assert bodies[0] == bodies[1]
