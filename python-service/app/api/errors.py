"""Error handling: domain errors -> machine-readable HTTP responses.

n8n branches on ``error_code``, so every failure returns the same envelope:

    {"error_code": "...", "detail": "...", "context": {...}}

Unexpected exceptions are logged with a correlation id and returned as
``INTERNAL_ERROR`` without internals — an error body is not a place to leak
stack traces or configuration.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import TradingError
from app.core.events import EventType
from app.core.logging import get_logger, log_event, redact

logger = get_logger(__name__)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TradingError)
    async def _trading_error(request: Request, exc: TradingError) -> JSONResponse:
        log_event(
            logger,
            EventType.SYSTEM_ERROR,
            message=exc.detail,
            level=30,
            error_code=exc.error_code,
            path=request.url.path,
            context=redact(exc.context),
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error_code": exc.error_code,
                "detail": exc.detail,
                "context": redact(exc.context),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error_code": "REQUEST_VALIDATION_ERROR",
                "detail": "request payload failed validation",
                "context": {"errors": _compact_errors(exc)},
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error_code" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": f"HTTP_{exc.status_code}", "detail": str(detail)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        incident = uuid.uuid4().hex[:12]
        logger.exception(
            "unhandled error",
            extra={
                "event": str(EventType.SYSTEM_ERROR),
                "incident_id": incident,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "INTERNAL_ERROR",
                "detail": (
                    "unexpected server error; check service logs for incident "
                    f"{incident}"
                ),
                "context": {"incident_id": incident},
            },
        )


def _compact_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": ".".join(str(part) for part in error.get("loc", [])),
            "message": str(error.get("msg", "")),
        }
        for error in exc.errors()[:10]
    ]
