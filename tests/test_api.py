"""API correctness against real PostgreSQL (ASGI transport is intentional here)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from blog_service import main as api
from blog_service.core.model_registry import Comment, IdempotencyRecord, Post, User
from blog_service.posts import service as posts_service
from blog_service.users.auth import issue_token


def headers(auth_headers, key=None):
    return {**auth_headers, "Idempotency-Key": key or str(uuid4())}


async def test_create_and_read_post(client, auth_headers, seeded):
    payload = {"title": "Unicode title: Привет", "body": "A blog post with Unicode: 世界"}
    created = await client.post("/v1/posts", json=payload, headers=headers(auth_headers))
    assert created.status_code == 201
    assert set(created.json()) == {"id"}
    response = await client.get(f"/v1/posts/{created.json()['id']}")
    assert response.status_code == 200
    assert response.json()["author_id"] == str(seeded["user_id"])
    assert response.json()["title"] == payload["title"]
    assert response.json()["body"] == payload["body"]
    assert response.json()["created_at"].endswith("Z")


async def test_create_and_list_comment(client, auth_headers, seeded):
    path = f"/v1/posts/{seeded['post_id']}/comments"
    response = await client.post(path, json={"body": "New comment"}, headers=headers(auth_headers))
    assert response.status_code == 201
    listed = await client.get(path)
    assert listed.status_code == 200
    comment = next(item for item in listed.json()["items"] if item["id"] == response.json()["id"])
    assert comment["author_id"] == str(seeded["user_id"])
    assert comment["body"] == "New comment"


@pytest.mark.parametrize("kind", ["post", "comment"])
async def test_idempotent_exact_replay(client, auth_headers, seeded, session_factory, kind):
    path = "/v1/posts" if kind == "post" else f"/v1/posts/{seeded['post_id']}/comments"
    payload = {"title": "Once", "body": "only once"} if kind == "post" else {"body": "only once"}
    request_headers = headers(auth_headers)
    first = await client.post(path, json=payload, headers=request_headers)
    replay = await client.post(path, json=payload, headers=request_headers)
    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content
    model = Post if kind == "post" else Comment
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(model).where(model.id == UUID(first.json()["id"]))
            )
            == 1
        )
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idem_key == request_headers["Idempotency-Key"]
            )
        )
        assert record.response_body.encode() == first.content
        assert record.response_status == 201


async def test_changed_payload_conflicts(client, auth_headers):
    request_headers = headers(auth_headers)
    first = await client.post(
        "/v1/posts", json={"title": "First", "body": "text"}, headers=request_headers
    )
    conflict = await client.post(
        "/v1/posts", json={"title": "Changed", "body": "text"}, headers=request_headers
    )
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_canonical_json_replays(client, auth_headers):
    request_headers = {**headers(auth_headers), "Content-Type": "application/json"}
    first = await client.post(
        "/v1/posts", content=b'{"title":"Same", "body":"text"}', headers=request_headers
    )
    replay = await client.post(
        "/v1/posts", content=b'{ "body": "text", "title": "Same" }', headers=request_headers
    )
    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content


async def test_concurrent_duplicates_create_one(client, auth_headers, session_factory):
    request_headers = headers(auth_headers)
    payload = {"title": "Concurrent unique title", "body": "One resource"}
    responses = await asyncio.gather(
        *(client.post("/v1/posts", json=payload, headers=request_headers) for _ in range(20))
    )
    assert [response.status_code for response in responses] == [201] * 20
    assert len({response.content for response in responses}) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Post).where(Post.title == payload["title"])
            )
            == 1
        )


async def test_key_is_scoped_by_user(client, auth_headers, settings, session_factory):
    user_id = uuid4()
    async with session_factory.begin() as session:
        session.add(User(id=user_id, handle=f"scope-{user_id.hex}"))
    key = str(uuid4())
    first = await client.post(
        "/v1/posts", json={"title": "User one", "body": "body"}, headers=headers(auth_headers, key)
    )
    second = await client.post(
        "/v1/posts",
        json={"title": "User two", "body": "body"},
        headers={
            "Authorization": f"Bearer {issue_token(user_id, settings)}",
            "Idempotency-Key": key,
        },
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_key_is_scoped_by_operation(client, auth_headers, seeded):
    request_headers = headers(auth_headers)
    post = await client.post(
        "/v1/posts", json={"title": "Post", "body": "text"}, headers=request_headers
    )
    comment = await client.post(
        f"/v1/posts/{seeded['post_id']}/comments", json={"body": "text"}, headers=request_headers
    )
    assert post.status_code == comment.status_code == 201


async def test_comment_key_includes_target_in_fingerprint(client, auth_headers, seeded):
    other_post = await client.post(
        "/v1/posts", json={"title": "Other", "body": "text"}, headers=headers(auth_headers)
    )
    request_headers = headers(auth_headers)
    first = await client.post(
        f"/v1/posts/{seeded['post_id']}/comments", json={"body": "text"}, headers=request_headers
    )
    conflict = await client.post(
        f"/v1/posts/{other_post.json()['id']}/comments",
        json={"body": "text"},
        headers=request_headers,
    )
    assert first.status_code == 201
    assert conflict.status_code == 409


async def test_rollback_removes_reservation_and_resource(
    client, auth_headers, session_factory, monkeypatch
):
    original = posts_service.persist_post

    async def fail_after_flush(session, resource):
        await original(session, resource)
        raise RuntimeError("Injected failure before commit")

    monkeypatch.setattr(posts_service, "persist_post", fail_after_flush)
    request_headers = headers(auth_headers)
    payload = {"title": "Rollback marker", "body": "not committed"}
    failed = await client.post("/v1/posts", json=payload, headers=request_headers)
    assert failed.status_code == 500
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Post).where(Post.title == payload["title"])
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.idem_key == request_headers["Idempotency-Key"])
            )
            == 0
        )
    monkeypatch.setattr(posts_service, "persist_post", original)
    retry = await client.post("/v1/posts", json=payload, headers=request_headers)
    assert retry.status_code == 201


async def test_saved_result_survives_new_app_instance(client, auth_headers, settings):
    request_headers = headers(auth_headers)
    payload = {"title": "Durable", "body": "survives restart"}
    first = await client.post("/v1/posts", json=payload, headers=request_headers)
    restarted = api.create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as other_client:
            replay = await other_client.post("/v1/posts", json=payload, headers=request_headers)
    assert first.status_code == replay.status_code == 201
    assert first.content == replay.content


@pytest.mark.parametrize("kind", ["posts", "comments"])
async def test_pagination_tied_timestamps(client, session_factory, seeded, kind):
    user_id, post_id = uuid4(), uuid4()
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    item_ids = [uuid4() for _ in range(7)]
    async with session_factory.begin() as session:
        session.add(User(id=user_id, handle=f"pagination-{user_id.hex}"))
        await session.flush()
        if kind == "posts":
            session.add_all(
                [
                    Post(
                        id=item_id,
                        author_id=user_id,
                        title="Tie",
                        body="body",
                        created_at=timestamp,
                    )
                    for item_id in item_ids
                ]
            )
        else:
            session.add(Post(id=post_id, author_id=user_id, title="Parent", body="body"))
            await session.flush()
            session.add_all(
                [
                    Comment(
                        id=item_id,
                        post_id=post_id,
                        author_id=user_id,
                        body="Tie",
                        created_at=timestamp,
                    )
                    for item_id in item_ids
                ]
            )
    path = f"/v1/authors/{user_id}/posts" if kind == "posts" else f"/v1/posts/{post_id}/comments"
    received, cursor = [], None
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(path, params=params)
        assert response.status_code == 200
        received.extend(item["id"] for item in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
        assert len(received) < 8
    assert received == [str(value) for value in sorted(item_ids, reverse=True)]


@pytest.mark.parametrize("cursor", ["", "bad!", "W10", "W251bGwsbnVsbF0", "☃"])
async def test_invalid_cursor(client, seeded, cursor):
    response = await client.get(f"/v1/authors/{seeded['user_id']}/posts", params={"cursor": cursor})
    assert response.status_code == 400


@pytest.mark.parametrize("limit", [0, -1, 101, "wrong"])
async def test_invalid_limit(client, seeded, limit):
    response = await client.get(f"/v1/authors/{seeded['user_id']}/posts", params={"limit": limit})
    assert response.status_code == 422
    assert set(response.json()) == {"error"}


@pytest.mark.parametrize("key", [None, "", "space key", "x" * 129])
async def test_invalid_or_missing_key(client, auth_headers, key):
    request_headers = dict(auth_headers)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    response = await client.post(
        "/v1/posts", json={"title": "Title", "body": "Body"}, headers=request_headers
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "case", ["missing", "expired", "bad_signature", "unknown_user", "malformed"]
)
async def test_authentication(client, settings, seeded, case):
    request_headers = {"Idempotency-Key": str(uuid4())}
    if case == "expired":
        token = issue_token(seeded["user_id"], settings, expires_seconds=-60)
    elif case == "bad_signature":
        token = issue_token(
            seeded["user_id"],
            replace(settings, jwt_secret="different-secret-that-is-at-least-32-bytes"),
        )
    elif case == "unknown_user":
        token = issue_token(uuid4(), settings)
    elif case == "malformed":
        token = "invalid.jwt.value"
    else:
        token = None
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    response = await client.post(
        "/v1/posts", json={"title": "Title", "body": "Body"}, headers=request_headers
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "body": "Body"},
        {"title": "x" * 201, "body": "Body"},
        {"title": "Title", "body": ""},
        {"title": "Title", "body": "Body", "author_id": str(uuid4())},
        {"title": "Title"},
    ],
)
async def test_invalid_post_body(client, auth_headers, payload):
    response = await client.post("/v1/posts", json=payload, headers=headers(auth_headers))
    assert response.status_code == 422


async def test_missing_and_invalid_resources(client, auth_headers):
    missing = uuid4()
    assert (await client.get("/v1/posts/not-a-uuid")).status_code == 422
    assert (await client.get(f"/v1/posts/{missing}")).status_code == 404
    assert (await client.get(f"/v1/posts/{missing}/comments")).status_code == 404
    assert (
        await client.post(
            f"/v1/posts/{missing}/comments", json={"body": "Body"}, headers=headers(auth_headers)
        )
    ).status_code == 404
    empty = await client.get(f"/v1/authors/{missing}/posts")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}


async def test_health_and_openapi(client):
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "sis2_blog_test"}
    schema = (await client.get("/openapi.json")).json()
    assert "/v1/posts" in schema["paths"]
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
