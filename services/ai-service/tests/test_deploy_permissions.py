"""Deploy permissions (step 12.2): kept apart from the pull request ones, and checked against what an installation accepted."""

import json
from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.integrations.github_app import (
    DEPLOY_PERMISSIONS,
    WRITE_PERMISSIONS,
    GitHubAppAuth,
    missing_permissions,
)
from app.integrations.github_app_client import InstallationInfo
from tests.conftest import requires_postgres
from tests.test_github_app import NOW, PEM, Clock
from tests.test_github_app_api import (
    BASE,
    INSTALLATION,
    TENANT_B,
    Env,
    FakeAppClient,
    _link,
)
from tests.test_github_app_api import (
    env as app_env,
)

env = app_env  # the fixture of the GitHub API tests, reused here


def test_deploying_can_dispatch_a_workflow_but_not_write_a_commit_and_pull_requests_cannot_dispatch() -> (
    None
):
    assert (
        DEPLOY_PERMISSIONS["actions"] == "write"
        and DEPLOY_PERMISSIONS["deployments"] == "write"
    )
    assert (
        "contents" not in DEPLOY_PERMISSIONS
        and "pull_requests" not in DEPLOY_PERMISSIONS
    )
    assert "actions" not in WRITE_PERMISSIONS and "deployments" not in WRITE_PERMISSIONS


@pytest.mark.parametrize(
    ("granted", "missing"),
    [
        (
            {
                "actions": "write",
                "deployments": "write",
                "checks": "read",
                "metadata": "read",
            },
            {},
        ),
        (
            {
                "actions": "write",
                "deployments": "write",
                "checks": "write",
                "metadata": "read",
                "contents": "write",
            },
            {},
        ),  # more is fine
        (
            {
                "contents": "write",
                "pull_requests": "write",
                "checks": "read",
                "metadata": "read",
            },
            {"actions": "write", "deployments": "write"},
        ),
        (
            {
                "actions": "read",
                "deployments": "write",
                "checks": "read",
                "metadata": "read",
            },
            {"actions": "write"},
        ),  # read is not enough
        ({}, dict(DEPLOY_PERMISSIONS)),
    ],
)
def test_what_an_installation_has_not_accepted_yet(
    granted: dict[str, str], missing: dict[str, str]
) -> None:
    assert missing_permissions(granted) == missing


@pytest.mark.asyncio
async def test_a_deploy_token_asks_for_the_deploy_permissions_only() -> None:
    settings = Settings(
        _env_file=None, github_app_id="123", github_app_private_key=SecretStr(PEM)
    )  # type: ignore[call-arg]
    auth = GitHubAppAuth(settings, Clock())
    reply = httpx.Response(
        201,
        json={
            "token": "ghs_x",
            "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
    )
    with respx.mock:
        route = respx.post(
            "https://api.github.com/app/installations/42/access_tokens"
        ).mock(return_value=reply)

        await auth.installation_token(42, [7], permissions=DEPLOY_PERMISSIONS)
        await auth.installation_token(
            42, [7]
        )  # a pull request token for the same repository

    sent = [json.loads(call.request.content)["permissions"] for call in route.calls]
    assert sent == [
        dict(DEPLOY_PERMISSIONS),
        dict(WRITE_PERMISSIONS),
    ]  # two scopes, two tokens, never mixed


@requires_postgres
def test_an_installation_that_accepted_the_permissions_can_deploy(
    env: tuple[Env, FakeAppClient],
) -> None:
    e, fake = env
    fake.info = InstallationInfo(
        INSTALLATION, "acme", "Organization", False, dict(DEPLOY_PERMISSIONS)
    )
    _link(e)

    body = e.client.get(
        f"{BASE}/installations/{INSTALLATION}/deploy-access", headers=e.headers()
    ).json()

    assert body == {"canDeploy": True, "missing": {}}


@requires_postgres
def test_an_installation_that_has_not_re_approved_is_told_what_is_missing(
    env: tuple[Env, FakeAppClient],
) -> None:
    e, fake = env
    fake.info = InstallationInfo(
        INSTALLATION, "acme", "Organization", False, dict(WRITE_PERMISSIONS)
    )
    _link(e)

    body = e.client.get(
        f"{BASE}/installations/{INSTALLATION}/deploy-access", headers=e.headers()
    ).json()

    assert body == {
        "canDeploy": False,
        "missing": {"actions": "write", "deployments": "write"},
    }


@requires_postgres
def test_a_suspended_installation_cannot_deploy_even_with_the_permissions(
    env: tuple[Env, FakeAppClient],
) -> None:
    e, fake = env
    fake.info = InstallationInfo(
        INSTALLATION, "acme", "Organization", True, dict(DEPLOY_PERMISSIONS)
    )
    _link(e)

    assert (
        e.client.get(
            f"{BASE}/installations/{INSTALLATION}/deploy-access", headers=e.headers()
        ).json()["canDeploy"]
        is False
    )


@requires_postgres
def test_deploy_access_is_admin_only_and_another_tenant_gets_404(
    env: tuple[Env, FakeAppClient],
) -> None:
    e, _ = env
    _link(e)
    url = f"{BASE}/installations/{INSTALLATION}/deploy-access"

    assert e.client.get(url).status_code == 401
    assert e.client.get(url, headers=e.headers(role="ENGINEER")).status_code == 403
    assert e.client.get(url, headers=e.headers(tenant=TENANT_B)).status_code == 404
