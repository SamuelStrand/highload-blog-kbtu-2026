from uuid import UUID

from pydantic import BaseModel


class Created(BaseModel):
    id: UUID


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
