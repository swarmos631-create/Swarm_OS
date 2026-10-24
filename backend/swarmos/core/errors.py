"""Consistent API error format: ``{"error": {"code", "message", "details", "correlation_id"}}``."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from swarmos.core.logging import correlation_id_var

log = logging.getLogger("swarmos.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def forbidden(message: str = "You do not have permission to perform this action") -> ApiError:
    return ApiError(403, "forbidden", message)


def conflict(message: str, details: Any = None) -> ApiError:
    return ApiError(409, "conflict", message, details)


def bad_request(message: str, details: Any = None) -> ApiError:
    return ApiError(400, "bad_request", message, details)


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "correlation_id": correlation_id_var.get(),
        }
    }


def _is_missing_table(exc: DBAPIError) -> bool:
    """PostgreSQL reports SQLSTATE 42P01 (undefined_table); SQLite says "no such table"."""
    return getattr(exc.orig, "sqlstate", None) == "42P01" or "no such table" in str(exc.orig)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return JSONResponse(_body("validation_error", "Request validation failed", details), status_code=422)

    @app.exception_handler(DBAPIError)
    async def _database(_: Request, exc: DBAPIError) -> JSONResponse:
        # Infrastructure faults get a specific, actionable 503 instead of a generic 500 (no internals leaked).
        if _is_missing_table(exc):
            log.error("database_not_migrated", extra={"error_type": type(exc.orig).__name__})
            return JSONResponse(_body("database_not_migrated",
                                      "The database schema is missing. Run `swarmos migrate` against this database."),
                                status_code=503)
        if isinstance(exc, OperationalError):
            log.error("database_unavailable", extra={"error_type": type(exc.orig).__name__})
            return JSONResponse(_body("database_unavailable", "The database is unreachable. Try again shortly."),
                                status_code=503)
        log.exception("unhandled_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(_body("internal_error", "An unexpected error occurred"), status_code=500)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals; the correlation id links the client error to the server log.
        log.exception("unhandled_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(_body("internal_error", "An unexpected error occurred"), status_code=500)
