"""The GitHub client, against a mocked GitHub (respx): no network."""

import logging

import httpx
import pytest
import respx

from app.config import Settings
from app.integrations.github import (
    GitHubAccessError,
    GitHubClient,
    GitHubUnavailableError,
)

pytestmark = pytest.mark.asyncio

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
URL = "https://api.github.com/repos/acme/shop"


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient(Settings(_env_file=None))  # type: ignore[call-arg]


def _repo(**overrides: object) -> dict:
    body: dict = {"name": "Shop", "owner": {"login": "Acme"}, "default_branch": "trunk", "private": True, "permissions": {"pull": True}}
    return body | overrides


@respx.mock
async def test_a_readable_repository_comes_back_with_githubs_spelling_and_default_branch(client: GitHubClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=_repo()))

    found = await client.get_repository("acme", "shop", TOKEN)

    assert (found.owner, found.name, found.default_branch, found.private) == ("Acme", "Shop", "trunk", True)
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"  # the token travels in the header...
    assert TOKEN not in str(request.url)  # ...and never in the URL
    assert request.headers["accept"] == "application/vnd.github+json" and request.headers["user-agent"]


@respx.mock
@pytest.mark.parametrize(
    ("status", "problem"),
    [(401, "bad_token"), (403, "forbidden"), (429, "forbidden"), (404, "not_found")],
)
async def test_githubs_no_is_an_access_error_with_a_reason(client: GitHubClient, status: int, problem: str) -> None:
    respx.get(URL).mock(return_value=httpx.Response(status, json={"message": f"secret detail {TOKEN}"}))
    with pytest.raises(GitHubAccessError) as caught:
        await client.get_repository("acme", "shop", TOKEN)
    assert caught.value.problem == problem
    assert TOKEN not in str(caught.value)


@respx.mock
async def test_a_token_without_read_permission_is_refused_even_though_the_repository_answered(client: GitHubClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_repo(permissions={"pull": False})))
    with pytest.raises(GitHubAccessError) as caught:
        await client.get_repository("acme", "shop", TOKEN)
    assert caught.value.problem == "forbidden"


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(502), httpx.Response(200, text="not json"), httpx.Response(200, json={"name": "x"}), httpx.Response(200, json=[1, 2])])
async def test_a_failing_or_unreadable_answer_is_unavailable_not_a_crash(client: GitHubClient, response: httpx.Response) -> None:
    respx.get(URL).mock(return_value=response)
    with pytest.raises(GitHubUnavailableError):
        await client.get_repository("acme", "shop", TOKEN)


@respx.mock
async def test_a_network_failure_is_unavailable_and_logs_no_token(client: GitHubClient, caplog: pytest.LogCaptureFixture) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"cannot connect, token {TOKEN}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(GitHubUnavailableError):
        await client.get_repository("acme", "shop", TOKEN)
    assert TOKEN not in caplog.text


@respx.mock
async def test_owner_and_name_are_url_encoded_so_they_cannot_change_the_path(client: GitHubClient) -> None:
    route = respx.get(url__startswith="https://api.github.com/repos/").mock(return_value=httpx.Response(404))
    with pytest.raises(GitHubAccessError):
        await client.get_repository("a/b", "c?d=e", TOKEN)
    assert route.calls.last.request.url.raw_path == b"/repos/a%2Fb/c%3Fd%3De"


@respx.mock
async def test_the_api_base_url_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3/")
    custom = GitHubClient(Settings(_env_file=None))  # type: ignore[call-arg]
    route = respx.get("https://ghe.example.com/api/v3/repos/acme/shop").mock(return_value=httpx.Response(200, json=_repo()))
    await custom.get_repository("acme", "shop", TOKEN)
    assert route.called
