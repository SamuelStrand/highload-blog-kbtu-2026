"""Explicit table registration, without duplicating models across applications."""

from blog_service.comments.models import Comment
from blog_service.core.database import Base
from blog_service.idempotency.models import IdempotencyRecord
from blog_service.posts.models import Post
from blog_service.users.models import User

__all__ = ["Base", "User", "Post", "Comment", "IdempotencyRecord"]
