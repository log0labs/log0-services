"""The deploy provider: what a request may contain, how GitHub's runs read, and the Actions adapter against a mocked GitHub."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.deploy.provider import DispatchRequest, status_of
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_actions import GitHubActionsProvider
from app.integrations.github_app import DEPLOY_PERMISSIONS, InstallationToken
from app.integrations.github_writer import GitHubWriter, WriteRejectedError, WriteTarget

REPO = "https://api.github.com/repos/acme/shop"
WORKFLOW = f"{REPO}/actions/workflows/deploy.yml"
TARGET = WriteTarget(owner="acme", name="shop", installation_id=42, github_repository_id=7, default_branch="main")
SHA = "a" * 40
CORRELATION = UUID("11111111-2222-4333-8444-555555555555")


def _request(**overrides: Any) -> DispatchRequest:
    values: dict[str, Any] = {"workflow_file": "deploy.yml", "ref": "main", "sha": SHA, "environment": "production", "correlation_id": CORRELATION}
    return DispatchRequest(**(values | overrides))


class FakeAuth:
    def __init__(self) -> None:
        self.asked: list[Any] = []

    async def installation_token(self, installation_id: int, repository_ids=None, permissions=None) -> InstallationToken:
        self.asked.append(permissions)
        return InstallationToken(SecretStr("ghs_deploy"), datetime.now(UTC) + timedelta(hours=1))


@pytest.fixture
def provider() -> tuple[GitHubActionsProvider, FakeAuth]:
    auth = FakeAuth()
    return GitHubActionsProvider(GitHubWriter(Settings(_env_file=None), auth)), auth  # type: ignore[call-arg, arg-type]


# ------------------------------------------------------------------ the request


def test_a_request_carries_exactly_three_inputs() -> None:
    assert _request().inputs() == {"sha": SHA, "environment": "production", "correlation_id": str(CORRELATION)}


@pytest.mark.parametrize("sha", ["main", "HEAD", "abc", "A" * 40, "a" * 39, "a" * 41, SHA + "\n", f"{SHA}; rm -rf /", "", "$(whoami)" + "a" * 31])
def test_only_a_full_lowercase_commit_sha_can_be_deployed(sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        _request(sha=sha)


@pytest.mark.parametrize("environment", ["Production", "prod uction", "prod;ls", "-x", "", "a" * 64, "production\n", "${{ secrets.X }}"])
def test_only_an_environment_token_can_be_named(environment: str) -> None:
    with pytest.raises(ValueError, match="environment"):
        _request(environment=environment)


@pytest.mark.parametrize("workflow", ["../deploy.yml", "deploy.sh", "dir/deploy.yml", "deploy yml", "deploy.yml;id", ""])
def test_only_a_plain_workflow_file_can_be_run(workflow: str) -> None:
    with pytest.raises(ValueError, match="workflow"):
        _request(workflow_file=workflow)


# --------------------------------------------------------------- reading a run


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ({"status": "queued"}, "dispatched"),
        ({"status": "requested"}, "dispatched"),
        ({"status": "in_progress"}, "running"),
        ({"status": "waiting"}, "running"),  # an environment's required reviewers have not approved yet
        ({"status": "pending"}, "running"),
        ({"status": "completed", "conclusion": "success"}, "succeeded"),
        ({"status": "completed", "conclusion": "failure"}, "failed"),
        ({"status": "completed", "conclusion": "timed_out"}, "failed"),
        ({"status": "completed", "conclusion": "cancelled"}, "cancelled"),
        ({"status": "completed", "conclusion": "skipped"}, "failed"),  # nothing deployed
        ({"status": "completed", "conclusion": "neutral"}, "failed"),
        ({"status": "completed", "conclusion": None}, "failed"),
        ({"status": "completed"}, "failed"),
        ({}, "dispatched"),
    ],
)
def test_a_run_as_a_deployment_status(run: dict, expected: str) -> None:
    assert status_of(run) == expected


# ------------------------------------------------------------------- the adapter


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_asks_for_one_workflow_with_the_fixed_inputs_and_a_deploy_token(provider) -> None:
    p, auth = provider
    route = respx.post(f"{WORKFLOW}/dispatches").mock(return_value=httpx.Response(204))

    await p.dispatch(TARGET, _request())

    sent = route.calls.last.request
    assert json.loads(sent.content) == {"ref": "main", "inputs": {"sha": SHA, "environment": "production", "correlation_id": str(CORRELATION)}}
    assert sent.headers["authorization"] == "Bearer ghs_deploy"
    assert auth.asked == [DEPLOY_PERMISSIONS]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(("status", "error"), [(422, WriteRejectedError), (409, WriteRejectedError), (404, GitHubAccessError), (403, GitHubAccessError), (500, GitHubUnavailableError)])
async def test_dispatch_failures_are_translated(provider, status: int, error: type[Exception]) -> None:
    p, _ = provider
    respx.post(f"{WORKFLOW}/dispatches").mock(return_value=httpx.Response(status, json={"message": "no workflow_dispatch trigger"}))

    with pytest.raises(error):
        await p.dispatch(TARGET, _request())


def _runs(*rows: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"workflow_runs": list(rows)})


@pytest.mark.asyncio
@respx.mock
async def test_our_run_is_found_by_the_correlation_id_in_its_name(provider) -> None:
    p, _ = provider
    route = respx.get(f"{WORKFLOW}/runs").mock(
        return_value=_runs(
            {"id": 3, "display_title": f"Deploy {uuid4()}", "status": "completed", "conclusion": "success", "html_url": "u3"},
            {"id": 2, "display_title": f"Deploy {CORRELATION}", "status": "in_progress", "html_url": "https://github.com/acme/shop/actions/runs/2"},
            {"id": 1, "display_title": "Deploy by hand", "status": "completed", "conclusion": "failure"},
        )
    )

    run = await p.find_run(TARGET, _request())

    assert run is not None and (run.run_id, run.status, run.url) == (2, "running", "https://github.com/acme/shop/actions/runs/2")
    assert route.calls.last.request.url.params["event"] == "workflow_dispatch"


@pytest.mark.asyncio
@respx.mock
async def test_a_run_is_not_found_before_github_makes_it(provider) -> None:
    p, _ = provider
    respx.get(f"{WORKFLOW}/runs").mock(return_value=_runs({"id": 1, "display_title": "somebody else run", "status": "queued"}))

    assert await p.find_run(TARGET, _request()) is None


@pytest.mark.asyncio
@respx.mock
async def test_the_name_field_is_a_fallback_and_a_missing_title_is_survived(provider) -> None:
    p, _ = provider
    respx.get(f"{WORKFLOW}/runs").mock(return_value=_runs({"id": 5, "display_title": None, "name": f"deploy-{CORRELATION}", "status": "queued"}, {"id": 6, "status": "queued"}))

    run = await p.find_run(TARGET, _request())

    assert run is not None and run.run_id == 5


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("body", [{"nope": []}, [], "text", {"workflow_runs": [{"display_title": f"{CORRELATION}"}]}])
async def test_an_unreadable_run_list_is_unavailable(provider, body: Any) -> None:
    p, _ = provider
    respx.get(f"{WORKFLOW}/runs").mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(GitHubUnavailableError):
        await p.find_run(TARGET, _request())


@pytest.mark.asyncio
@respx.mock
async def test_a_run_is_followed_by_its_id(provider) -> None:
    p, _ = provider
    respx.get(f"{REPO}/actions/runs/9").mock(return_value=httpx.Response(200, json={"id": 9, "status": "completed", "conclusion": "success", "html_url": "u"}))

    run = await p.run_status(TARGET, 9)

    assert (run.run_id, run.status) == (9, "succeeded")


@pytest.mark.asyncio
@respx.mock
async def test_an_unreadable_run_is_unavailable(provider) -> None:
    p, _ = provider
    respx.get(f"{REPO}/actions/runs/9").mock(return_value=httpx.Response(200, json={"status": "completed"}))

    with pytest.raises(GitHubUnavailableError):
        await p.run_status(TARGET, 9)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status", [202, 409])
async def test_cancelling_a_run_that_is_cancelled_or_already_finished_is_fine(provider, status: int) -> None:
    p, _ = provider
    route = respx.post(f"{REPO}/actions/runs/9/cancel").mock(return_value=httpx.Response(status))

    await p.cancel(TARGET, 9)

    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_cancelling_without_permission_is_reported(provider) -> None:
    p, _ = provider
    respx.post(f"{REPO}/actions/runs/9/cancel").mock(return_value=httpx.Response(403))

    with pytest.raises(GitHubAccessError):
        await p.cancel(TARGET, 9)
