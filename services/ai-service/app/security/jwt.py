"""JWT access tokens (same contract as auth-service JwtUtil)."""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)

ALGORITHM = "HS256"


class Role(StrEnum):
    ADMIN = "ADMIN"
    ENGINEER = "ENGINEER"
    VIEWER = "VIEWER"


class CurrentUser(BaseModel):
    """Principal derived only from a verified access token."""
    
    user_id: UUID
    tenant_id: UUID
    role: Role


def decode_access_token(token: str, secret: str) -> CurrentUser:
    """Verify HS256 token and map claims to CurrentUser. Raises jwt.PyJWTError on failure."""
    payload = jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
    try:
        tenant_raw = payload["tenantId"]
        role_raw = payload["role"]
        return CurrentUser(
            user_id=UUID(payload["sub"]),
            tenant_id=UUID(tenant_raw if isinstance(tenant_raw, str) else str(tenant_raw)),
            role=Role(role_raw),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise jwt.InvalidTokenError("missing or invalid claims") from exc


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentUser:
    """FastAPI dependency: Bearer access token → CurrentUser."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    secret = settings.jwt_secret.get_secret_value()
    try:
        return decode_access_token(credentials.credentials, secret)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
