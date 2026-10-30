"""HTTP contract for posts and author feeds."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.core.database import get_session
from blog_service.core.schemas import Created
from blog_service.idempotency.service import required_key
from blog_service.posts import service
from blog_service.posts.schemas import PostCreate, PostPage, PostRead
from blog_service.users.auth import current_user

router = APIRouter(prefix="/v1", tags=["posts"])


@router.get("/posts/{post_id}", response_model=PostRead)
async def read_post(post_id: UUID, session: AsyncSession = Depends(get_session, scope="function")):
    return await service.get_post(session, post_id)


@router.get("/authors/{author_id}/posts", response_model=PostPage)
async def author_posts(
    author_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session, scope="function"),
):
    return await service.author_posts(session, author_id, limit, cursor)


@router.post("/posts", status_code=201, response_model=Created)
async def create_post(
    payload: PostCreate,
    user_id: UUID = Depends(current_user),
    key: str = Depends(required_key),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    return await service.create_post(session, user_id, key, payload)
