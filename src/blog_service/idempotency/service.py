"""Atomic key reservation and result replay, independent of the written domain."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import Header, Response
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from blog_service.core.errors import APIError
from blog_service.idempotency.models import IdempotencyRecord

KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def required_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if idempotency_key is None or KEY_PATTERN.fullmatch(idempotency_key) is None:
        raise APIError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must contain 1-128 letters, digits, or ._:-.",
        )
    return idempotency_key


async def execute_write(
    session: AsyncSession,
    user_id: UUID,
    key: str,
    operation: str,
    path: str,
    payload: dict,
    create_resource: Callable[[], Awaitable[UUID]],
) -> Response:
    canonical = json.dumps(
        {"path": path, "body": payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    reservation_id = uuid4()
    reserved = await session.scalar(
        insert(IdempotencyRecord)
        .values(
            id=reservation_id,
            user_id=user_id,
            operation=operation,
            idem_key=key,
            request_hash=fingerprint,
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_scope")
        .returning(IdempotencyRecord.id)
    )
    if reserved is None:
        # A separate READ COMMITTED statement sees a competing transaction that
        # committed while INSERT waited. A single INSERT/SELECT CTE would not.
        saved = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.idem_key == key,
            )
        )
        if saved is None or saved.response_body is None or saved.response_status is None:
            raise APIError(503, "result_unavailable", "Retry the request.", {"Retry-After": "1"})
        if saved.request_hash != fingerprint:
            raise APIError(
                409, "idempotency_conflict", "The key was already used for a different request."
            )
        body, status = saved.response_body, saved.response_status
    else:
        resource_id = await create_resource()
        body, status = json.dumps({"id": str(resource_id)}, separators=(",", ":")), 201
        await session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == reserved)
            .values(response_status=status, response_body=body)
        )
    # Both the resource and replay result commit before ASGI sends headers.
    await session.commit()
    return Response(content=body.encode("utf-8"), status_code=status, media_type="application/json")
