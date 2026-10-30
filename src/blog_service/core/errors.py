"""Consistent HTTP errors shared by domain routers."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers


def error_response(status, code, message, headers=None):
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}, headers=headers
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return error_response(422, "validation_error", "Invalid path, query, or request body.")

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        return error_response(exc.status_code, "http_error", str(exc.detail), exc.headers)

    async def handle_database_error(request: Request, exc: Exception):
        logger.warning("Database operation failed (%s)", type(exc).__name__)
        return error_response(
            503,
            "database_unavailable",
            "Database operation is temporarily unavailable.",
            {"Retry-After": "1"},
        )

    for error_type in (DBAPIError, SQLTimeoutError, ConnectionError, OSError):
        app.add_exception_handler(error_type, handle_database_error)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.error("Unhandled request failure (%s)", type(exc).__name__)
        return error_response(500, "internal_error", "Unexpected server error.")
