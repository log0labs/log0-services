"""The ``state`` value that ties a GitHub App installation to the workspace and admin who started it.

The install link carries a signed, short-lived token. When GitHub sends the admin back with an
``installation_id``, the token proves which tenant and user asked for that installation, so a
tenant cannot be tricked into (or claim an installation without) going through the flow. The key
is derived from the JWT secret for this purpose only, so a state token is never a valid access token.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

AUDIENCE = "log0-github-install"
LIFETIME = timedelta(minutes=15)
_KEY_LABEL = b"log0:github-install-state:v1"


class InvalidInstallStateError(Exception):
    """The state is missing, altered, expired or belongs to someone else."""


def _key(secret: str) -> bytes:
    return hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()


def issue_state(secret: str, *, tenant_id: UUID, user_id: UUID, now: datetime | None = None) -> str:
    issued = now or datetime.now(UTC)
    claims = {"aud": AUDIENCE, "tenantId": str(tenant_id), "sub": str(user_id), "exp": issued + LIFETIME}
    return jwt.encode(claims, _key(secret), algorithm="HS256")


def verify_state(secret: str, state: str, *, tenant_id: UUID, user_id: UUID) -> None:
    """Raise ``InvalidInstallStateError`` unless ``state`` was issued to this tenant and user."""
    try:
        claims = jwt.decode(state, _key(secret), algorithms=["HS256"], audience=AUDIENCE, options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        raise InvalidInstallStateError from None
    if claims.get("tenantId") != str(tenant_id) or claims.get("sub") != str(user_id):
        raise InvalidInstallStateError
