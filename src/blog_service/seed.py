"""Deterministic fixtures shared by demonstrations, tests, and benchmarks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from blog_service.core.config import Settings
from blog_service.core.database import create_engine
from blog_service.core.model_registry import Comment, Post, User


def seed_id(kind: str, index: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://sis2.local/seed/{kind}/{index}")


DEMO_USER_ID = seed_id("user", 0)
DEMO_POST_ID = seed_id("post", 0)
POST_BODY = "p" * 2048
COMMENT_BODY = "c" * 256
SEED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


async def seed_database(
    settings: Settings,
    reset: bool = False,
    users: int = 100,
    posts: int = 10000,
    comments: int = 20000,
) -> dict[str, str | int]:
    if users < 1 or posts < 1 or comments < 0:
        raise ValueError("Seed requires at least one user and post; comments cannot be negative")
    url = make_url(settings.database_url)
    if reset and (
        url.database not in {"sis2_blog_test", "sis2_blog_bench"}
        or url.host not in {"localhost", "127.0.0.1"}
    ):
        raise ValueError(
            "Reset is limited to the local sis2_blog_test and sis2_blog_bench databases"
        )
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            if reset:
                await connection.execute(
                    text("TRUNCATE TABLE idempotency_records, comments, posts, users")
                )

            async def insert_rows(model: type, rows: list[dict]) -> None:
                for offset in range(0, len(rows), 1000):
                    statement = insert(model).values(rows[offset : offset + 1000])
                    await connection.execute(
                        statement.on_conflict_do_nothing(index_elements=["id"])
                    )

            await insert_rows(
                User,
                [
                    {
                        "id": seed_id("user", index),
                        "handle": f"seed_user_{index:06d}",
                        "created_at": SEED_TIME,
                    }
                    for index in range(users)
                ],
            )
            await insert_rows(
                Post,
                [
                    {
                        "id": seed_id("post", index),
                        "author_id": seed_id("user", index % users),
                        "title": f"Seed post {index:08d}",
                        "body": POST_BODY,
                        "created_at": SEED_TIME + timedelta(seconds=index),
                    }
                    for index in range(posts)
                ],
            )
            await insert_rows(
                Comment,
                [
                    {
                        "id": seed_id("comment", index),
                        "post_id": seed_id("post", index % posts),
                        "author_id": seed_id("user", (index + 1) % users),
                        "body": COMMENT_BODY,
                        "created_at": SEED_TIME + timedelta(seconds=index),
                    }
                    for index in range(comments)
                ],
            )
    finally:
        await engine.dispose()
    return {
        "user_id": str(DEMO_USER_ID),
        "post_id": str(DEMO_POST_ID),
        "users": users,
        "posts": posts,
        "comments": comments,
        "post_body_bytes": len(POST_BODY.encode("ascii")),
        "comment_body_bytes": len(COMMENT_BODY.encode("ascii")),
    }
