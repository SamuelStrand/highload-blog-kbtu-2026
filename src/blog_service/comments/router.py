"""HTTP contract for post comments."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.comments import service
from blog_service.comments.schemas import CommentCreate, CommentPage
from blog_service.core.database import get_session
from blog_service.core.schemas import Created
from blog_service.idempotency.service import required_key
from blog_service.users.auth import current_user

router = APIRouter(prefix="/v1/posts/{post_id}/comments", tags=["comments"])


@router.get("", response_model=CommentPage)
async def list_comments(
    post_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session, scope="function"),
):
    return await service.list_comments(session, post_id, limit, cursor)


@router.post("", status_code=201, response_model=Created)
async def create_comment(
    post_id: UUID,
    payload: CommentCreate,
    user_id: UUID = Depends(current_user),
    key: str = Depends(required_key),
    session: AsyncSession = Depends(get_session, scope="function"),
):
    return await service.create_comment(session, user_id, post_id, key, payload)
