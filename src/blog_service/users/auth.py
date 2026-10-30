"""JWT verification and identity for locally seeded demonstration users."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.core.config import Settings
from blog_service.core.database import get_session
from blog_service.core.errors import APIError
from blog_service.users.models import User


def issue_token(user_id: UUID | str, settings: Settings, expires_seconds: int = 3600) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + timedelta(seconds=expires_seconds)},
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_token(token: str, settings: Settings) -> UUID:
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        options={"require": ["sub", "exp"]},
    )
    return UUID(claims["sub"])


bearer = HTTPBearer(auto_error=False)


async def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UUID:
    if credentials is None:
        raise APIError(
            401, "unauthorized", "A valid Bearer token is required.", {"WWW-Authenticate": "Bearer"}
        )
    try:
        user_id = decode_token(credentials.credentials, request.app.state.settings)
    except (PyJWTError, ValueError, TypeError) as exc:
        raise APIError(
            401, "unauthorized", "Invalid or expired Bearer token.", {"WWW-Authenticate": "Bearer"}
        ) from exc
    if await session.get(User, user_id) is None:
        raise APIError(
            401, "unauthorized", "Token user does not exist.", {"WWW-Authenticate": "Bearer"}
        )
    return user_id
