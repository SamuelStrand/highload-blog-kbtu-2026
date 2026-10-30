"""Post operations and author feeds."""

from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.core.errors import APIError
from blog_service.core.pagination import decode_cursor, encode_cursor
from blog_service.idempotency.service import execute_write
from blog_service.posts.models import Post
from blog_service.posts.schemas import PostCreate, PostPage, PostRead


async def require_post(session: AsyncSession, post_id: UUID) -> None:
    if await session.scalar(select(Post.id).where(Post.id == post_id)) is None:
        raise APIError(404, "post_not_found", "Post does not exist.")


async def get_post(session: AsyncSession, post_id: UUID) -> Post:
    post = await session.get(Post, post_id)
    if post is None:
        raise APIError(404, "post_not_found", "Post does not exist.")
    return post


async def author_posts(
    session: AsyncSession, author_id: UUID, limit: int, cursor: str | None
) -> PostPage:
    query = select(Post).where(Post.author_id == author_id)
    if cursor is not None:
        query = query.where(tuple_(Post.created_at, Post.id) < decode_cursor(cursor))
    rows = list(
        (
            await session.scalars(
                query.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return PostPage(items=[PostRead.model_validate(row) for row in page], next_cursor=next_cursor)


async def persist_post(session: AsyncSession, post: Post) -> None:
    session.add(post)
    await session.flush()


async def create_post(session: AsyncSession, user_id: UUID, key: str, payload: PostCreate):
    data = payload.model_dump()

    async def write() -> UUID:
        post = Post(id=uuid4(), author_id=user_id, **data)
        await persist_post(session, post)
        return post.id

    return await execute_write(session, user_id, key, "create_post", "/v1/posts", data, write)
