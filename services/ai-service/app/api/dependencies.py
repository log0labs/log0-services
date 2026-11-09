"""Shared FastAPI dependencies for the settings and repository endpoints.

The objects live on ``app.state`` (built once in the lifespan); tests replace them through
``app.dependency_overrides``, so no test talks to GitHub or to a model provider.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.integrations.github import GitHubClient
from app.llm.connection_check import ConnectionChecker
from app.security.crypto import SecretBox


def get_optional_secret_box(request: Request) -> SecretBox | None:
    """The secret box, or None when no master key is configured."""
    return request.app.state.secret_box


def require_secret_box(box: Annotated[SecretBox | None, Depends(get_optional_secret_box)]) -> SecretBox:
    """For endpoints that must store or read a tenant secret: 503 when no master key is set."""
    if box is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Secret storage is not configured on this server",
        )
    return box


def get_github(request: Request) -> GitHubClient:
    return request.app.state.github


def get_connection_checker(request: Request) -> ConnectionChecker:
    return request.app.state.connection_checker
