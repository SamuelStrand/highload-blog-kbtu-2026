"""Comments belong to existing posts; each write has an atomic replay result."""

from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.comments.models import Comment
from blog_service.comments.schemas import CommentCreate, CommentPage, CommentRead
from blog_service.core.pagination import decode_cursor, encode_cursor
from blog_service.idempotency.service import execute_write
from blog_service.posts.service import require_post


async def list_comments(
    session: AsyncSession, post_id: UUID, limit: int, cursor: str | None
) -> CommentPage:
    await require_post(session, post_id)
    query = select(Comment).where(Comment.post_id == post_id)
    if cursor is not None:
        query = query.where(tuple_(Comment.created_at, Comment.id) < decode_cursor(cursor))
    rows = list(
        (
            await session.scalars(
                query.order_by(Comment.created_at.desc(), Comment.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return CommentPage(
        items=[CommentRead.model_validate(row) for row in page], next_cursor=next_cursor
    )


async def create_comment(
    session: AsyncSession, user_id: UUID, post_id: UUID, key: str, payload: CommentCreate
):
    await require_post(session, post_id)
    data = payload.model_dump()

    async def write() -> UUID:
        comment = Comment(id=uuid4(), author_id=user_id, post_id=post_id, **data)
        session.add(comment)
        await session.flush()
        return comment.id

    return await execute_write(
        session, user_id, key, "create_comment", f"/v1/posts/{post_id}/comments", data, write
    )
