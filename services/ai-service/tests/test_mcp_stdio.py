"""Step 5.5: the stdio entry point: token checks, expiry during a session, and a real subprocess over stdio."""

import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app.config import get_settings
from app.mcp.server import ToolFailure, current_deadline, current_user, tenant_id
from app.mcp.stdio import TOKEN_ENV, user_from_token
from app.security.jwt import CurrentUser, Role
from tests.conftest import requires_postgres
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token
from tests.memory_support import make_investigation, wipe_investigations

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-jwt-secret-at-least-32-characters-long"


def test_a_valid_token_gives_the_user_and_its_expiry():
    user, expires = user_from_token(mint_access_token(SECRET, tenant_id=TENANT_A, role="VIEWER"), SECRET)
    assert user.tenant_id == TENANT_A and user.role == Role.VIEWER
    assert time.time() + 3000 < expires < time.time() + 4000  # the helper's one hour


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        (None, "is not set"),
        ("", "is not set"),
        ("garbage", "not a valid"),
        (mint_access_token(SECRET, exp_delta=timedelta(seconds=-5)), "not a valid"),
        (mint_access_token("another-secret-that-is-at-least-32-characters"), "not a valid"),
    ],
)
def test_a_missing_or_bad_token_stops_the_server_with_a_plain_message(token, fragment):
    with pytest.raises(SystemExit) as stop:
        user_from_token(token, SECRET)
    assert fragment in str(stop.value)
    assert token is None or not token or token not in str(stop.value)  # the token is never echoed


def test_tools_stop_working_when_the_session_token_expires():
    marker = current_user.set(CurrentUser(user_id=TENANT_A, tenant_id=TENANT_A, role=Role.VIEWER))
    try:
        deadline = current_deadline.set(time.time() + 60)
        assert tenant_id() == TENANT_A
        current_deadline.reset(deadline)
        deadline = current_deadline.set(time.time() - 1)
        with pytest.raises(ToolFailure, match="expired"):
            tenant_id()
        current_deadline.reset(deadline)
        assert tenant_id() == TENANT_A  # no deadline (HTTP): each request was already verified
    finally:
        current_user.reset(marker)


def params(token: str | None) -> StdioServerParameters:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.pop(TOKEN_ENV, None)
    if token:
        env[TOKEN_ENV] = token
    return StdioServerParameters(command=sys.executable, args=["-m", "app.mcp.stdio"], env=env, cwd=str(ROOT))


@pytest.mark.asyncio
@requires_postgres
async def test_a_real_client_talks_to_the_server_over_stdio_as_the_tokens_tenant():
    wipe_investigations()
    try:
        mine, _ = make_investigation(TENANT_A)
        theirs, _ = make_investigation(TENANT_B)
        async with Client(params(mint_access_token(SECRET, tenant_id=TENANT_A, role="VIEWER"))) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 10
            found = await client.call_tool("get_investigation", {"investigation_id": str(mine)})
            assert not found.is_error and json.loads(found.content[0].text)["status"] == "COMPLETED"
            other = await client.call_tool("get_investigation", {"investigation_id": str(theirs)})
            assert other.is_error and "not found" in other.content[0].text
    finally:
        wipe_investigations()


def test_the_process_refuses_to_start_without_a_token():
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "app.mcp.stdio"], cwd=ROOT, env=params(None).env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode != 0
    assert "LOG0_MCP_TOKEN is not set" in result.stderr
    assert result.stdout == ""  # stdout is the protocol: nothing else may be written there


def test_settings_secret_matches_the_test_secret():
    assert get_settings().jwt_secret.get_secret_value() == SECRET
