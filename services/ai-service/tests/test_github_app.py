"""GitHub App authentication, against a mocked GitHub (respx): no network."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from app.config import Settings
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import (
    TOKEN_REFRESH_MARGIN_SECONDS,
    GitHubAppAuth,
    GitHubAppNotConfiguredError,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
URL = "https://api.github.com/app/installations/42/access_tokens"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _auth(clock: Clock, *, key: str | None = PEM, app_id: str = "123") -> GitHubAppAuth:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        github_app_id=app_id,
        github_app_private_key=SecretStr(key) if key else None,
    )
    return GitHubAppAuth(settings, clock)


def _created(token: str = "ghs_secret_token_value", lifetime: timedelta = timedelta(hours=1)) -> httpx.Response:
    return httpx.Response(201, json={"token": token, "expires_at": (NOW + lifetime).isoformat().replace("+00:00", "Z")})


async def test_app_jwt_is_rs256_signed_for_our_app_and_short_lived() -> None:
    token = _auth(Clock()).app_jwt()

    claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] <= 600  # GitHub rejects anything longer than ten minutes
    assert claims["iat"] < NOW.timestamp()  # back-dated for clock drift


async def test_private_key_with_escaped_newlines_is_accepted() -> None:
    one_line = PEM.replace("\n", "\n")

    assert jwt.decode(_auth(Clock(), key=one_line).app_jwt(), PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})


@pytest.mark.parametrize("kwargs", [{"key": None}, {"app_id": ""}, {"key": "not a key"}])
async def test_a_missing_or_broken_configuration_is_reported_without_echoing_the_key(kwargs: dict) -> None:
    with pytest.raises(GitHubAppNotConfiguredError) as caught:
        _auth(Clock(), **kwargs).app_jwt()

    assert "not a key" not in str(caught.value)


@respx.mock
async def test_installation_token_is_requested_narrowed_and_signed_as_the_app() -> None:
    route = respx.post(URL).mock(return_value=_created())

    result = await _auth(Clock()).installation_token(42, [9, 7])

    request = route.calls.last.request
    assert json.loads(request.content)["repository_ids"] == [7, 9]
    assert json.loads(request.content)["permissions"]["contents"] == "write"
    assert "workflows" not in json.loads(request.content)["permissions"]
    bearer = request.headers["authorization"].removeprefix("Bearer ")
    assert jwt.decode(bearer, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})["iss"] == "123"
    assert result.token.get_secret_value() == "ghs_secret_token_value"
    assert "ghs_secret_token_value" not in repr(result)


@respx.mock
async def test_a_cached_token_is_reused_until_it_is_close_to_expiry() -> None:
    clock = Clock()
    route = respx.post(URL).mock(return_value=_created())
    auth = _auth(clock)

    await auth.installation_token(42, [7])
    await auth.installation_token(42, [7])
    assert route.call_count == 1

    clock.now = NOW + timedelta(hours=1) - timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS - 1)
    await auth.installation_token(42, [7])
    assert route.call_count == 2


@respx.mock
async def test_tokens_for_different_repositories_are_not_shared() -> None:
    route = respx.post(URL).mock(return_value=_created())
    auth = _auth(Clock())

    await auth.installation_token(42, [7])
    await auth.installation_token(42, [8])

    assert route.call_count == 2


@respx.mock
async def test_forget_drops_the_cached_tokens_of_an_installation() -> None:
    route = respx.post(URL).mock(return_value=_created())
    auth = _auth(Clock())
    await auth.installation_token(42, [7])

    auth.forget(42)
    await auth.installation_token(42, [7])

    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize(
    ("status", "problem"),
    [(401, "bad_token"), (403, "forbidden"), (422, "forbidden"), (404, "not_found")],
)
async def test_githubs_refusals_become_access_errors(status: int, problem: str) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status))

    with pytest.raises(GitHubAccessError) as caught:
        await _auth(Clock()).installation_token(42, [7])

    assert caught.value.problem == problem


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(201, json={"nope": 1}), httpx.Response(201, text="x")])
async def test_failures_and_unreadable_answers_are_unavailable(response: httpx.Response) -> None:
    respx.post(URL).mock(return_value=response)

    with pytest.raises(GitHubUnavailableError):
        await _auth(Clock()).installation_token(42, [7])


@respx.mock
async def test_a_network_error_is_unavailable_and_logs_no_url(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom https://api.github.com/secret"))

    with caplog.at_level(logging.WARNING), pytest.raises(GitHubUnavailableError):
        await _auth(Clock()).installation_token(42, [7])

    assert "secret" not in caplog.text
