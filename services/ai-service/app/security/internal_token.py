"""Shared secret for service-to-service calls (summaries ingress, callback egress)."""

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings

HEADER_NAME = "X-Internal-Token"


def require_internal_service_token(
    settings: Annotated[Settings, Depends(get_settings)],
    x_internal_token: Annotated[str | None, Header(alias=HEADER_NAME)] = None,
) -> None:
    """FastAPI dependency: only callers that know INTERNAL_SERVICE_TOKEN."""
    expected = settings.internal_service_token.get_secret_value()
    if x_internal_token is None or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal service token",
        )


