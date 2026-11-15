"""The GitHub App's read calls, against a mocked GitHub (respx): no network."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import InstallationToken
from app.integrations.github_app_client import PAGE_SIZE, GitHubAppClient

pytestmark = pytest.mark.asyncio

BASE = "https://api.github.com"


class FakeAuth:
    def __init__(self) -> None:
        self.asked: list[tuple[int, dict]] = []

    def app_jwt(self) -> str:
        return "app.jwt.value"

    async def installation_token(self, installation_id: int, repository_ids=None, permissions=None) -> InstallationToken:
        self.asked.append((installation_id, dict(permissions or {})))
        return InstallationToken(SecretStr("ghs_installation"), datetime.now(UTC) + timedelta(hours=1))


@pytest.fixture
def client() -> tuple[GitHubAppClient, FakeAuth]:
    auth = FakeAuth()
    return GitHubAppClient(Settings(_env_file=None), auth), auth  # type: ignore[call-arg, arg-type]


def _repo(i: int) -> dict:
    return {"id": i, "name": f"r{i}", "owner": {"login": "acme"}, "default_branch": "main", "private": i % 2 == 0}


@respx.mock
async def test_installation_is_read_with_the_app_jwt(client) -> None:
    c, _ = client
    route = respx.get(f"{BASE}/app/installations/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "account": {"login": "acme", "type": "Organization"}, "suspended_at": None})
    )

    info = await c.get_installation(42)

    assert (info.installation_id, info.account_login, info.account_type, info.suspended) == (42, "acme", "Organization", False)
    assert route.calls.last.request.headers["authorization"] == "Bearer app.jwt.value"


@respx.mock
async def test_a_suspended_installation_is_reported(client) -> None:
    c, _ = client
    respx.get(f"{BASE}/app/installations/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "account": {"login": "a"}, "suspended_at": "2026-01-01T00:00:00Z"})
    )

    assert (await c.get_installation(42)).suspended is True


@respx.mock
async def test_repositories_use_a_read_only_installation_token_and_follow_pages(client) -> None:
    c, auth = client
    first = [_repo(i) for i in range(PAGE_SIZE)]
    route = respx.get(f"{BASE}/installation/repositories").mock(
        side_effect=[httpx.Response(200, json={"repositories": first}), httpx.Response(200, json={"repositories": [_repo(999)]})]
    )

    found = await c.list_repositories(42)

    assert len(found) == PAGE_SIZE + 1 and found[-1].github_id == 999
    assert auth.asked == [(42, {"metadata": "read"})]
    assert route.calls.last.request.headers["authorization"] == "Bearer ghs_installation"
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize(("status", "error"), [(404, GitHubAccessError), (403, GitHubAccessError), (500, GitHubUnavailableError)])
async def test_failures_map_to_the_package_errors(client, status: int, error: type[Exception]) -> None:
    c, _ = client
    respx.get(f"{BASE}/app/installations/42").mock(return_value=httpx.Response(status))

    with pytest.raises(error):
        await c.get_installation(42)


@respx.mock
async def test_unreadable_answers_and_network_errors_are_unavailable(client) -> None:
    c, _ = client
    respx.get(f"{BASE}/app/installations/1").mock(return_value=httpx.Response(200, json={"nope": 1}))
    respx.get(f"{BASE}/installation/repositories").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(GitHubUnavailableError):
        await c.get_installation(1)
    with pytest.raises(GitHubUnavailableError):
        await c.list_repositories(1)
