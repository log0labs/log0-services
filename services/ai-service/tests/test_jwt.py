"""JWT decode + get_current_user dependency."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.security.jwt import CurrentUser, decode_access_token, get_current_user

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("22222222-2222-2222-2222-222222222222")
ROLE = "ENGINEER"


def _mint(secret: str, *, exp_delta: timedelta | None = None, **claim_overrides) -> str:
    now = datetime.now(tz=UTC)
    payload = {
        "sub": str(USER_ID),
        "tenantId": str(TENANT_ID),
        "role": ROLE,
        "iat": now,
        "exp": now + (exp_delta or timedelta(hours=1)),
    }
    payload.update(claim_overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_decode_access_token_ok():
    secret = get_settings().jwt_secret.get_secret_value()
    token = _mint(secret)
    user = decode_access_token(token, secret)
    assert user == CurrentUser(user_id=USER_ID, tenant_id=TENANT_ID, role="ENGINEER")


def test_decode_rejects_wrong_secret():
    token = _mint("wrong-secret-at-least-32-characters-xx")
    secret = get_settings().jwt_secret.get_secret_value()
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token, secret)


def test_decode_rejects_expired():
    secret = get_settings().jwt_secret.get_secret_value()
    token = _mint(secret, exp_delta=timedelta(seconds=-10))
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token, secret)


def test_get_current_user_dependency():
    app = FastAPI()

    @app.get("/whoami")
    def whoami(user: Annotated[CurrentUser, Depends(get_current_user)]):
        return {"tenantId": str(user.tenant_id), "role": user.role}
    
    client = TestClient(app)
    secret = get_settings().jwt_secret.get_secret_value()
    token = _mint(secret)
    
    assert client.get("/whoami").status_code == 401
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["tenantId"] == str(TENANT_ID)
    assert r.json()["role"] == "ENGINEER"
