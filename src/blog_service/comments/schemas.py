from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1)


class CommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    post_id: UUID
    author_id: UUID
    body: str
    created_at: datetime


class CommentPage(BaseModel):
    items: list[CommentRead]
    next_cursor: str | None
