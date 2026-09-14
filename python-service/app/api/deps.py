"""FastAPI dependencies: auth and the per-request service graph."""

from __future__ import annotations

import secrets
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.container import Services, build_services, get_market_data_service
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.session import session_scope

logger = get_logger(__name__)


def settings_dependency() -> Settings:
    return get_settings()


def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(settings_dependency),
) -> None:
    """Shared-secret auth between n8n and this service.

    An empty ``SERVICE_API_KEY`` disables auth for local development, but is
    refused outright in production — an unauthenticated trading API on a shared
    network is a liability, not a convenience.
    """
    expected = settings.service_api_key
    if not expected:
        if settings.environment.lower() in {"production", "prod"}:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "AUTH_MISCONFIGURED",
                    "detail": "SERVICE_API_KEY must be set when ENVIRONMENT=production",
                },
            )
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "detail": "invalid or missing X-API-Key"},
        )


def require_dashboard_access(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    key: str | None = None,
    settings: Settings = Depends(settings_dependency),
) -> None:
    """Auth for the dashboard page and its summary endpoint.

    The browser cannot set headers on a plain navigation, so the key may also
    arrive as ``?key=...``. It still has to be the right key — account state,
    positions and P&L are not public.
    """
    expected = settings.service_api_key
    if not expected:
        if settings.environment.lower() in {"production", "prod"}:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "AUTH_MISCONFIGURED",
                    "detail": "SERVICE_API_KEY must be set when ENVIRONMENT=production",
                },
            )
        return
    supplied = x_api_key or key
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "UNAUTHORIZED",
                "detail": "dashboard requires X-API-Key header or ?key= query parameter",
            },
        )


def db_session() -> Generator[Session, None, None]:
    with session_scope() as session:
        yield session


def services_dependency(
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dependency),
) -> Services:
    return build_services(session, settings, market_data=get_market_data_service())


ServicesDep = Annotated[Services, Depends(services_dependency)]
SettingsDep = Annotated[Settings, Depends(settings_dependency)]
