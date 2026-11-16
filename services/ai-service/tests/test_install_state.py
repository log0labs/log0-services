"""The signed state that ties an installation to the tenant and user who started it."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.security.install_state import (
    InvalidInstallStateError,
    issue_state,
    verify_state,
)

SECRET = "s" * 48
TENANT, USER = uuid4(), uuid4()


def test_a_state_verifies_for_the_tenant_and_user_it_was_issued_to() -> None:
    verify_state(SECRET, issue_state(SECRET, tenant_id=TENANT, user_id=USER), tenant_id=TENANT, user_id=USER)


@pytest.mark.parametrize("other", ["tenant", "user"])
def test_a_state_is_refused_for_anyone_else(other: str) -> None:
    state = issue_state(SECRET, tenant_id=TENANT, user_id=USER)

    with pytest.raises(InvalidInstallStateError):
        verify_state(SECRET, state, tenant_id=uuid4() if other == "tenant" else TENANT, user_id=uuid4() if other == "user" else USER)


def test_an_expired_or_forged_state_is_refused() -> None:
    old = issue_state(SECRET, tenant_id=TENANT, user_id=USER, now=datetime.now(UTC) - timedelta(hours=1))
    forged = issue_state("another-secret" * 4, tenant_id=TENANT, user_id=USER)

    for state in (old, forged, "garbage", ""):
        with pytest.raises(InvalidInstallStateError):
            verify_state(SECRET, state, tenant_id=TENANT, user_id=USER)


def test_an_access_token_signed_with_the_same_secret_is_not_a_valid_state() -> None:
    access = jwt.encode(
        {"sub": str(USER), "tenantId": str(TENANT), "role": "ADMIN", "exp": datetime.now(UTC) + timedelta(hours=1)}, SECRET, algorithm="HS256"
    )

    with pytest.raises(InvalidInstallStateError):
        verify_state(SECRET, access, tenant_id=TENANT, user_id=USER)
