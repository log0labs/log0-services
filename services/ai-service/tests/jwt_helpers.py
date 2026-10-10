"""Shared JWT minting for API tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

USER_ID = UUID("33333333-3333-4333-8333-333333333333")
TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")


def mint_access_token(
    secret: str,
    *,
    user_id: UUID = USER_ID,
    tenant_id: UUID = TENANT_A,
    role: str = "ENGINEER",
    exp_delta: timedelta | None = None,
    **claim_overrides: object,
) -> str:
    now = datetime.now(tz=UTC)
    payload = {
        "sub": str(user_id),
        "tenantId": str(tenant_id),
        "role": role,
        "iat": now,
        "exp": now + (exp_delta or timedelta(hours=1)),
    }
    payload.update(claim_overrides)
    return jwt.encode(payload, secret, algorithm="HS256")
