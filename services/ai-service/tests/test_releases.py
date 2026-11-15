"""What each environment runs: the GitHub reader (mocked), the ledger (real Postgres) and the answer built from both."""

import os
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deploy.releases import deployed_sha
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import (
    DEPLOY_PERMISSIONS,
    WRITE_PERMISSIONS,
    InstallationToken,
)
from app.integrations.github_deploys import DEPLOYMENTS_LOOKED_AT, GitHubDeploys
from app.integrations.github_writer import GitHubWriter, WriteTarget
from app.store import releases
from tests.test_github_app_tables import _repository

REPO = "https://api.github.com/repos/acme/shop"
TARGET = WriteTarget(owner="acme", name="shop", installation_id=42, github_repository_id=7, default_branch="main")
TENANT = UUID("34000000-0000-4000-8000-00000000000a")
OTHER = UUID("34000000-0000-4000-8000-00000000000b")
A, B, C = "a" * 40, "b" * 40, "c" * 40

needs_postgres = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)


class FakeAuth:
    def __init__(self) -> None:
        self.asked: list[Mapping[str, str] | None] = []

    async def installation_token(self, installation_id: int, repository_ids=None, permissions=None) -> InstallationToken:
        self.asked.append(permissions)
        return InstallationToken(SecretStr("ghs_deploy_token"), datetime.now(UTC) + timedelta(hours=1))


@pytest.fixture
def deploys() -> tuple[GitHubDeploys, FakeAuth]:
    auth = FakeAuth()
    return GitHubDeploys(GitHubWriter(Settings(_env_file=None), auth)), auth  # type: ignore[call-arg, arg-type]


def _deployments(*ids_and_shas: tuple[int, str]) -> httpx.Response:
    return httpx.Response(200, json=[{"id": i, "sha": sha} for i, sha in ids_and_shas])


def _status(state: str | None) -> httpx.Response:
    return httpx.Response(200, json=[{"state": state}] if state else [])


# --------------------------------------------------------------- reading GitHub


@pytest.mark.asyncio
@respx.mock
async def test_the_newest_successful_deployment_is_what_the_environment_runs(deploys) -> None:
    d, auth = deploys
    listing = respx.get(f"{REPO}/deployments").mock(return_value=_deployments((3, C), (2, B), (1, A)))
    respx.get(f"{REPO}/deployments/3/statuses").mock(return_value=_status("failure"))
    respx.get(f"{REPO}/deployments/2/statuses").mock(return_value=_status("success"))
    older = respx.get(f"{REPO}/deployments/1/statuses").mock(return_value=_status("success"))

    assert await d.last_successful_deployment(TARGET, "production") == B  # the failed newer one is not what runs

    assert listing.calls.last.request.url.params["environment"] == "production"
    assert not older.called  # it stops at the first success
    assert auth.asked == [DEPLOY_PERMISSIONS]  # a deploy token, never the pull request one
    assert auth.asked != [WRITE_PERMISSIONS]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("state", ["pending", "queued", "in_progress", "error", "failure", "inactive", None])
async def test_a_deployment_that_did_not_succeed_is_not_a_release(deploys, state: str | None) -> None:
    d, _ = deploys
    respx.get(f"{REPO}/deployments").mock(return_value=_deployments((1, A)))
    respx.get(f"{REPO}/deployments/1/statuses").mock(return_value=_status(state))

    assert await d.last_successful_deployment(TARGET, "production") is None


@pytest.mark.asyncio
@respx.mock
async def test_no_deployments_at_all_is_a_normal_answer(deploys) -> None:
    d, _ = deploys
    respx.get(f"{REPO}/deployments").mock(return_value=_deployments())

    assert await d.last_successful_deployment(TARGET, "production") is None


@pytest.mark.asyncio
@respx.mock
async def test_only_the_newest_deployments_are_looked_at(deploys) -> None:
    d, _ = deploys
    respx.get(f"{REPO}/deployments").mock(return_value=_deployments(*[(i, A) for i in range(1, 30)]))
    statuses = respx.get(url__regex=rf"{REPO}/deployments/\d+/statuses").mock(return_value=_status("failure"))

    assert await d.last_successful_deployment(TARGET, "production") is None

    assert statuses.call_count == DEPLOYMENTS_LOOKED_AT == 10  # the listing is trimmed even if GitHub ignored per_page


@pytest.mark.asyncio
@respx.mock
async def test_a_sha_that_is_not_a_full_commit_is_never_returned(deploys) -> None:
    d, _ = deploys
    respx.get(f"{REPO}/deployments").mock(return_value=_deployments((1, "main"), (2, "ABC"), (3, B)))
    respx.get(url__regex=rf"{REPO}/deployments/\d+/statuses").mock(return_value=_status("success"))

    assert await d.last_successful_deployment(TARGET, "production") == B


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("body", [{"not": "a list"}, [{"id": "x"}], [{"sha": A}], "text"])
async def test_an_unreadable_listing_is_reported_as_unavailable(deploys, body) -> None:
    d, _ = deploys
    respx.get(f"{REPO}/deployments").mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(GitHubUnavailableError):
        await d.last_successful_deployment(TARGET, "production")


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(("status", "error"), [(403, GitHubAccessError), (404, GitHubAccessError), (500, GitHubUnavailableError)])
async def test_github_errors_are_translated_not_leaked(deploys, status: int, error: type[Exception]) -> None:
    d, _ = deploys
    respx.get(f"{REPO}/deployments").mock(return_value=httpx.Response(status, json={"message": "nope"}))

    with pytest.raises(error):
        await d.last_successful_deployment(TARGET, "production")


# ------------------------------------------------------------------- the ledger


@pytest_asyncio.fixture
async def session(db_session: AsyncSession) -> AsyncGenerator[AsyncSession]:
    async def wipe() -> None:
        for tenant in (TENANT, OTHER):
            await db_session.execute(text("DELETE FROM ai.repository_connection WHERE tenant_id = :t"), {"t": tenant})
        await db_session.commit()

    await wipe()
    yield db_session
    await db_session.rollback()
    await wipe()


async def _repo(session: AsyncSession, tenant: UUID = TENANT) -> UUID:
    repository_id = await _repository(session, tenant)
    await session.commit()
    return repository_id


@needs_postgres
@pytest.mark.asyncio
async def test_the_newest_entry_is_what_runs_and_the_one_before_it_is_the_history(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    ids = {"tenant_id": TENANT, "repository_id": repository_id, "environment": "production"}

    for sha in (A, B, C):
        await releases.record(session, sha=sha, source="log0", **ids)

    assert (await releases.current(session, **ids)).sha == C
    assert [r.sha for r in await releases.history(session, **ids)] == [C, B, A]
    assert [r.sha for r in await releases.history(session, limit=2, **ids)] == [C, B]


@needs_postgres
@pytest.mark.asyncio
async def test_recording_the_commit_that_already_runs_changes_nothing(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    ids = {"tenant_id": TENANT, "repository_id": repository_id, "environment": "production"}
    await releases.record(session, sha=A, source="github", **ids)

    again = await releases.record(session, sha=A, source="log0", **ids)  # our deploy of what was already there

    assert again is None
    assert len(await releases.history(session, **ids)) == 1


@needs_postgres
@pytest.mark.asyncio
async def test_going_back_to_an_older_commit_is_a_new_entry(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    ids = {"tenant_id": TENANT, "repository_id": repository_id, "environment": "production"}

    for sha in (A, B, A):  # deployed A, then B, then rolled back to A
        await releases.record(session, sha=sha, source="log0", **ids)

    assert [r.sha for r in await releases.history(session, **ids)] == [A, B, A]


@needs_postgres
@pytest.mark.asyncio
async def test_environments_and_tenants_do_not_see_each_other(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    await releases.record(session, tenant_id=TENANT, repository_id=repository_id, environment="production", sha=A, source="log0")

    assert await releases.current(session, tenant_id=TENANT, repository_id=repository_id, environment="staging") is None
    assert await releases.current(session, tenant_id=OTHER, repository_id=repository_id, environment="production") is None
    assert await releases.history(session, tenant_id=OTHER, repository_id=repository_id, environment="production") == []


@needs_postgres
@pytest.mark.asyncio
async def test_a_release_remembers_who_told_us(session: AsyncSession) -> None:
    repository_id = await _repo(session)

    row = await releases.record(session, tenant_id=TENANT, repository_id=repository_id, environment="production", sha=A, source="github")

    assert row is not None and row.source == "github" and row.remediation_id is None and row.recorded_at is not None


# ---------------------------------------------------------- the answer from both


class FakeDeploys:
    def __init__(self, answer: str | None = None, error: Exception | None = None) -> None:
        self.answer, self.error, self.asked = answer, error, 0

    async def last_successful_deployment(self, target: WriteTarget, environment: str) -> str | None:
        self.asked += 1
        if self.error:
            raise self.error
        return self.answer


async def _ask(session: AsyncSession, repository_id: UUID, port: FakeDeploys) -> str | None:
    return await deployed_sha(session, port, TARGET, tenant_id=TENANT, repository_id=repository_id, environment="production")


@needs_postgres
@pytest.mark.asyncio
async def test_what_github_reports_is_the_answer_and_is_written_into_the_ledger(session: AsyncSession) -> None:
    repository_id = await _repo(session)

    assert await _ask(session, repository_id, FakeDeploys(B)) == B

    assert (await releases.current(session, tenant_id=TENANT, repository_id=repository_id, environment="production")).source == "github"


@needs_postgres
@pytest.mark.asyncio
async def test_when_github_knows_nothing_the_ledger_answers(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    await releases.record(session, tenant_id=TENANT, repository_id=repository_id, environment="production", sha=A, source="log0")

    assert await _ask(session, repository_id, FakeDeploys(None)) == A


@needs_postgres
@pytest.mark.asyncio
async def test_when_nobody_knows_there_is_no_answer(session: AsyncSession) -> None:
    repository_id = await _repo(session)

    assert await _ask(session, repository_id, FakeDeploys(None)) is None


@needs_postgres
@pytest.mark.asyncio
async def test_a_deploy_made_outside_log0_wins_over_an_older_ledger_entry(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    ids = {"tenant_id": TENANT, "repository_id": repository_id, "environment": "production"}
    await releases.record(session, sha=A, source="log0", **ids)

    assert await _ask(session, repository_id, FakeDeploys(B)) == B  # somebody deployed B by hand after our deploy of A

    assert [r.sha for r in await releases.history(session, **ids)] == [B, A]


@needs_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("error", [GitHubUnavailableError(), GitHubAccessError("forbidden")])
async def test_when_github_cannot_be_asked_the_error_reaches_the_caller_and_the_ledger_is_not_trusted_blindly(session: AsyncSession, error: Exception) -> None:
    repository_id = await _repo(session)
    await releases.record(session, tenant_id=TENANT, repository_id=repository_id, environment="production", sha=A, source="log0")

    with pytest.raises(type(error)):
        await _ask(session, repository_id, FakeDeploys(error=error))


@needs_postgres
@pytest.mark.asyncio
async def test_asking_twice_does_not_grow_the_ledger(session: AsyncSession) -> None:
    repository_id = await _repo(session)
    port = FakeDeploys(B)

    await _ask(session, repository_id, port)
    await _ask(session, repository_id, port)

    ids = {"tenant_id": TENANT, "repository_id": repository_id, "environment": "production"}
    assert len(await releases.history(session, **ids)) == 1
