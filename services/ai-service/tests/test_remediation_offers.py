"""A merged log0 pull request makes deploy *offers* and nothing else (real Postgres, signed webhooks, no GitHub)."""

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.store import remediations
from tests.config_api_support import Env
from tests.jwt_helpers import TENANT_A, USER_ID
from tests.test_github_app_api import FakeAppClient
from tests.test_github_webhook import (
    _add_pull_request,
    _post,
    _pr_payload,
)
from tests.test_github_webhook import (
    env as webhook_env,
)

env = webhook_env  # a real app with a signed-webhook secret, an installation and a repository
from tests.conftest import requires_postgres

MERGED = "c" * 40


def _sql(statement: str, **params: Any) -> list[Any]:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        result = conn.execute(text(statement), {"t": TENANT_A, "u": USER_ID} | params)
        rows = list(result) if result.returns_rows else []
    engine.dispose()
    return rows


def _target(*environments: dict[str, Any], workflow: str = "deploy.yml") -> None:
    _sql(
        "INSERT INTO ai.deploy_target (tenant_id, repository_id, workflow_file, environments, updated_by) "
        "SELECT :t, id, :w, CAST(:e AS jsonb), :u FROM ai.repository_connection WHERE tenant_id = :t",
        w=workflow,
        e=json.dumps(list(environments)),
    )


def _merged(sha: str | None = MERGED, *, merged: bool = True, number: int = 5) -> dict[str, Any]:
    payload = _pr_payload("closed", merged=merged, number=number)
    if sha is not None:
        payload["pull_request"]["merge_commit_sha"] = sha
    return payload


def _offers() -> list[tuple[str, str, str]]:
    return [(r[0], r[1], r[2]) for r in _sql("SELECT environment, status, merge_sha FROM ai.remediation WHERE tenant_id = :t ORDER BY environment")]


def _events(investigation_id: str) -> list[str]:
    return [r[0] for r in _sql("SELECT type FROM ai.investigation_event WHERE investigation_id = :i ORDER BY seq", i=investigation_id)]


STAGING = {"name": "staging", "enabled": True, "approver": "ENGINEER"}
PRODUCTION = {"name": "production", "enabled": True, "requiresPreviousVerified": True}


@requires_postgres
def test_a_merge_makes_one_offer_per_enabled_environment_and_remembers_the_merge_commit(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request()
    _target(STAGING, PRODUCTION, {"name": "canary", "enabled": False})

    response = _post(e, "pull_request", _merged())

    assert response.status_code == 202 and response.json() == {"handled": True}
    assert _offers() == [("production", "OFFERED", MERGED), ("staging", "OFFERED", MERGED)]  # canary is switched off
    assert _sql("SELECT merge_commit_sha, merged_at IS NOT NULL FROM ai.pull_request WHERE investigation_id = :i", i=investigation_id) == [(MERGED, True)]
    assert _events(investigation_id) == ["pr.merged", "remediation.offered"]


@requires_postgres
def test_the_same_webhook_twice_makes_each_offer_once(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request()
    _target(STAGING, PRODUCTION)

    _post(e, "pull_request", _merged())
    _post(e, "pull_request", _merged())

    assert len(_offers()) == 2
    assert _events(investigation_id).count("remediation.offered") == 1  # the second delivery created nothing, so it said nothing


@requires_postgres
def test_an_offer_deploys_nothing(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    _add_pull_request()
    _target(STAGING)

    _post(e, "pull_request", _merged())

    assert _sql("SELECT count(*) FROM ai.deployment WHERE tenant_id = :t") == [(0,)]
    assert _sql("SELECT count(*) FROM ai.release_ledger WHERE tenant_id = :t") == [(0,)]
    assert [status for _, status, _ in _offers()] == ["OFFERED"]
    assert fake.listed == []  # not a single call to GitHub


@requires_postgres
def test_a_repository_without_a_deploy_target_gets_no_offers(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request()

    assert _post(e, "pull_request", _merged()).json() == {"handled": True}

    assert _offers() == []
    assert _events(investigation_id) == ["pr.merged"]


@requires_postgres
def test_a_target_with_every_environment_off_gets_no_offers(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    _add_pull_request()
    _target({"name": "production", "enabled": False})

    _post(e, "pull_request", _merged())

    assert _offers() == []


@pytest.mark.parametrize("sha", [None, "", "abc123", "Z" * 40, "c" * 39, "c" * 41, "C" * 40])
@requires_postgres
def test_a_merge_without_a_usable_commit_is_recorded_but_not_offered(env: tuple[Env, FakeAppClient], sha: str | None) -> None:
    e, _ = env
    investigation_id = _add_pull_request()
    _target(STAGING)

    assert _post(e, "pull_request", _merged(sha)).json() == {"handled": True}

    assert _offers() == []
    assert _sql("SELECT state, merge_commit_sha FROM ai.pull_request WHERE investigation_id = :i", i=investigation_id) == [("merged", None)]


@requires_postgres
def test_a_pull_request_closed_without_merging_offers_nothing(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request()
    _target(STAGING)

    _post(e, "pull_request", _merged(merged=False))

    assert _offers() == []
    assert _sql("SELECT state, merge_commit_sha FROM ai.pull_request WHERE investigation_id = :i", i=investigation_id) == [("closed", None)]


@requires_postgres
def test_a_stored_target_that_no_longer_fits_its_shape_offers_nothing_and_does_not_fail_the_webhook(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request()
    _target({"name": "Not A Token!", "enabled": True})

    response = _post(e, "pull_request", _merged())

    assert response.status_code == 202  # GitHub must not retry for ever because of our data
    assert _offers() == []
    assert _events(investigation_id) == ["pr.merged"]


@pytest.mark.asyncio
@requires_postgres
async def test_the_offers_of_a_merge_are_listed_for_its_own_tenant_only(env: tuple[Env, FakeAppClient], db_session: AsyncSession) -> None:
    e, _ = env
    _add_pull_request()
    _target(STAGING, PRODUCTION)
    _post(e, "pull_request", _merged())
    pull_request_id = _sql("SELECT id FROM ai.pull_request WHERE tenant_id = :t")[0][0]

    own = await remediations.list_for_pull_request(db_session, tenant_id=TENANT_A, pull_request_id=pull_request_id)
    other = await remediations.list_for_pull_request(db_session, tenant_id=uuid4(), pull_request_id=pull_request_id)

    assert [r.environment for r in own] == ["production", "staging"] and {r.status for r in own} == {"OFFERED"}
    assert other == []


@pytest.mark.asyncio
@requires_postgres
async def test_offering_again_returns_only_what_is_new(env: tuple[Env, FakeAppClient], db_session: AsyncSession) -> None:
    e, _ = env
    _add_pull_request()
    _target(STAGING)
    _post(e, "pull_request", _merged())
    row = _sql("SELECT id, investigation_id, repository_id FROM ai.pull_request WHERE tenant_id = :t")[0]
    ids = {"tenant_id": TENANT_A, "pull_request_id": row[0], "investigation_id": row[1], "repository_id": row[2], "merge_sha": MERGED}

    created = await remediations.offer_for_merge(db_session, environments=["staging", "production"], **ids)
    nothing = await remediations.offer_for_merge(db_session, environments=["staging", "production"], **ids)
    empty = await remediations.offer_for_merge(db_session, environments=[], **ids)

    assert [r.environment for r in created] == ["production"]  # staging was offered by the webhook already
    assert nothing == [] and empty == []
