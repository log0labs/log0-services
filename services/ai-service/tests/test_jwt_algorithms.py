import time

import jwt
import pytest

from app.security.jwt import decode_access_token

SECRET = "x" * 64
USER = "6f1d3c1e-0b0a-4b8e-9d57-1f6a7c3b2a10"
TENANT = "0a7b2d4e-5c6f-4a1b-8c9d-3e2f1a0b9c8d"


def _token(algorithm: str) -> str:
    claims = {"sub": USER, "tenantId": TENANT, "role": "ADMIN", "exp": int(time.time()) + 600}
    return jwt.encode(claims, SECRET, algorithm=algorithm)


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512"])
def test_accepts_every_hmac_size_the_auth_service_may_choose(algorithm):
    assert str(decode_access_token(_token(algorithm), SECRET).tenant_id) == TENANT


def test_still_rejects_an_unsigned_token():
    unsigned = jwt.encode({"sub": USER, "tenantId": TENANT, "role": "ADMIN", "exp": int(time.time()) + 600}, None, algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(unsigned, SECRET)
