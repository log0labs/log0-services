"""``/api/v1/remediations``: who may read, start and approve, what a second click does, and what is recorded (real app and Postgres)."""

import asyncio
import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from tests.config_api_support import Env, make_env
from tests.jwt_helpers import TENANT_A, TENANT_B
from tests.remediation_support import PRODUCTION, STAGING, Seed, seed

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

BASE = "/api/v1/remediations"


class FakeRunner:
    def __init__(self) -> None:
        self.scheduled: list[tuple[UUID, UUID]] = []

    def schedule(self, remediation_id: UUID, tenant_id: UUID) -> None:
        self.scheduled.append((remediation_id, tenant_id))

    async def wait_idle(self, remediation_id: UUID, *, timeout: float = 5.0) -> bool:
        return True


def _seed(tenant: UUID = TENANT_A) -> Seed:
    async def go() -> Seed:
        engine = create_async_engine(get_settings().database_url_async)
        try:
            return await seed(create_session_factory(engine), tenant=tenant, environments=[STAGING, PRODUCTION])
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _sql(statement: str, **params: Any) -> list[Any]:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        result = conn.execute(text(statement), params)
        rows = list(result) if result.returns_rows else []
    engine.dispose()
    return rows


def _forget(*tenants: UUID) -> None:
    for tenant in tenants:
        for table in ("investigation", "repository_connection", "github_installation"):
            _sql(f"DELETE FROM ai.{table} WHERE tenant_id = :t", t=tenant)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Env, FakeRunner, Seed]]:
    _forget(TENANT_A, TENANT_B)
    with make_env(monkeypatch) as e:
        runner = FakeRunner()
        e.app.state.remediation_runner = runner  # type: ignore[attr-defined]
        try:
            yield e, runner, _seed()
        finally:
            _forget(TENANT_A, TENANT_B)


def _waiting(remediation_id: UUID) -> None:
    _sql("UPDATE ai.remediation SET status = 'AWAITING_DEPLOY_APPROVAL' WHERE id = :i", i=remediation_id)


def _status(remediation_id: UUID) -> str:
    return _sql("SELECT status FROM ai.remediation WHERE id = :i", i=remediation_id)[0][0]


# ---------------------------------------------------------------------------- reading


def test_everything_needs_a_token(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, s = env
    url = f"{BASE}/{s.offers['staging']}"

    assert e.client.get(url).status_code == 401
    assert e.client.get(f"{BASE}?investigationId={s.investigation_id}").status_code == 401
    for verb in ("start", "deploy/approve", "deploy/decline"):
        assert e.client.post(f"{url}/{verb}").status_code == 401


def test_the_offers_of_an_investigation_are_listed_for_anyone_in_the_workspace(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, s = env

    body = e.client.get(f"{BASE}?investigationId={s.investigation_id}", headers=e.headers(role="VIEWER")).json()

    assert sorted(r["environment"] for r in body) == ["production", "staging"]
    assert {r["status"] for r in body} == {"OFFERED"} and body[0]["mergeSha"] == "c" * 40 and body[0]["deployment"] is None


def test_one_remediation_is_read_by_id(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, s = env

    body = e.client.get(f"{BASE}/{s.offers['staging']}", headers=e.headers(role="VIEWER")).json()

    assert body["id"] == str(s.offers["staging"]) and body["environment"] == "staging" and body["investigationId"] == str(s.investigation_id)


def test_another_workspace_sees_nothing_and_gets_404(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env
    other = e.headers(tenant=TENANT_B)

    assert e.client.get(f"{BASE}?investigationId={s.investigation_id}", headers=other).json() == []
    assert e.client.get(f"{BASE}/{s.offers['staging']}", headers=other).status_code == 404
    for verb in ("start", "deploy/approve", "deploy/decline"):
        assert e.client.post(f"{BASE}/{s.offers['staging']}/{verb}", headers=other).status_code == 404
    assert runner.scheduled == [] and _status(s.offers["staging"]) == "OFFERED"


def test_an_unknown_id_is_404(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, _ = env

    assert e.client.get(f"{BASE}/{uuid4()}", headers=e.headers()).status_code == 404


# ---------------------------------------------------------------------------- starting


def test_a_viewer_cannot_start_an_offer(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env

    assert e.client.post(f"{BASE}/{s.offers['staging']}/start", headers=e.headers(role="VIEWER")).status_code == 403
    assert runner.scheduled == [] and _status(s.offers["staging"]) == "OFFERED"


@pytest.mark.parametrize("role", ["ENGINEER", "ADMIN"])
def test_starting_an_offer_takes_the_environment_runs_the_preflight_and_is_audited(env: tuple[Env, FakeRunner, Seed], role: str) -> None:
    e, runner, s = env

    response = e.client.post(f"{BASE}/{s.offers['staging']}/start", headers=e.headers(role=role))

    assert response.status_code == 202 and response.json()["status"] == "PREFLIGHT"
    assert runner.scheduled == [(s.offers["staging"], TENANT_A)]
    assert _sql("SELECT action FROM ai.deploy_audit WHERE remediation_id = :i", i=s.offers["staging"]) == [("remediation.started",)]


def test_starting_twice_is_a_conflict_and_schedules_once(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env
    url = f"{BASE}/{s.offers['staging']}/start"

    assert e.client.post(url, headers=e.headers()).status_code == 202
    assert e.client.post(url, headers=e.headers()).status_code == 409

    assert len(runner.scheduled) == 1


def test_a_held_environment_is_a_conflict(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, s = env
    other_investigation, other_pr = uuid4(), uuid4()
    _sql("INSERT INTO ai.investigation (id, tenant_id, incident_id, requested_by, status) VALUES (:i, :t, gen_random_uuid(), :u, 'COMPLETED')", i=other_investigation, t=TENANT_A, u=s.user)
    _sql(
        "INSERT INTO ai.pull_request (id, tenant_id, investigation_id, repository_id, branch, base_sha, number, state, approved_by, merge_commit_sha) VALUES (:p, :t, :i, :r, 'log0/x', 'a', 9, 'merged', :u, :m)",
        p=other_pr, t=TENANT_A, i=other_investigation, r=s.repository_id, u=s.user, m="d" * 40,
    )
    _sql(
        "INSERT INTO ai.remediation (tenant_id, pull_request_id, investigation_id, repository_id, environment, merge_sha) VALUES (:t, :p, :i, :r, 'staging', :m)",
        t=TENANT_A, p=other_pr, i=other_investigation, r=s.repository_id, m="d" * 40,
    )
    other_offer = _sql("SELECT id FROM ai.remediation WHERE pull_request_id = :p", p=other_pr)[0][0]
    assert e.client.post(f"{BASE}/{s.offers['staging']}/start", headers=e.headers()).status_code == 202

    response = e.client.post(f"{BASE}/{other_offer}/start", headers=e.headers())

    assert response.status_code == 409 and "already running" in response.json()["detail"]
    assert _status(other_offer) == "OFFERED"


# --------------------------------------------------------------------------- answering


def test_a_viewer_cannot_answer(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env
    _waiting(s.offers["staging"])

    for verb in ("approve", "decline"):
        assert e.client.post(f"{BASE}/{s.offers['staging']}/deploy/{verb}", headers=e.headers(role="VIEWER")).status_code == 403
    assert _status(s.offers["staging"]) == "AWAITING_DEPLOY_APPROVAL" and runner.scheduled == []


def test_an_engineer_may_approve_staging_but_only_an_admin_production(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env
    _waiting(s.offers["staging"])
    _waiting(s.offers["production"])

    refused = e.client.post(f"{BASE}/{s.offers['production']}/deploy/approve", headers=e.headers(role="ENGINEER"))
    allowed_for_staging = e.client.post(f"{BASE}/{s.offers['staging']}/deploy/approve", headers=e.headers(role="ENGINEER"))
    allowed_for_production = e.client.post(f"{BASE}/{s.offers['production']}/deploy/approve", headers=e.headers(role="ADMIN"))

    assert refused.status_code == 403 and "ADMIN" in refused.json()["detail"]
    assert allowed_for_staging.status_code == 202 and allowed_for_production.status_code == 202
    assert _status(s.offers["staging"]) == "DEPLOYING" and _status(s.offers["production"]) == "DEPLOYING"
    assert len(runner.scheduled) == 2


def test_an_approval_is_recorded_with_who_their_role_and_the_commit(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, s = env
    _waiting(s.offers["staging"])

    e.client.post(f"{BASE}/{s.offers['staging']}/deploy/approve", headers=e.headers(role="ENGINEER"))

    rows = _sql("SELECT gate, decision, role, environment, sha FROM ai.approval WHERE remediation_id = :i", i=s.offers["staging"])
    assert [tuple(r) for r in rows] == [("deploy", "approve", "ENGINEER", "staging", "c" * 40)]
    assert _sql("SELECT action, result FROM ai.deploy_audit WHERE remediation_id = :i", i=s.offers["staging"]) == [("deploy.approved", "ok")]


def test_declining_ends_the_remediation_and_is_recorded(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env
    _waiting(s.offers["staging"])

    response = e.client.post(f"{BASE}/{s.offers['staging']}/deploy/decline", headers=e.headers(role="ENGINEER"))

    assert response.status_code == 202 and response.json()["status"] == "DECLINED" and response.json()["finishedAt"] is not None
    assert [tuple(r) for r in _sql("SELECT decision FROM ai.approval WHERE remediation_id = :i", i=s.offers["staging"])] == [("decline",)]
    assert len(runner.scheduled) == 1  # the run is resumed to end its graph


def test_a_second_answer_is_a_conflict_and_records_nothing(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, runner, s = env
    _waiting(s.offers["staging"])
    url = f"{BASE}/{s.offers['staging']}/deploy"

    first = e.client.post(f"{url}/approve", headers=e.headers())
    second = e.client.post(f"{url}/approve", headers=e.headers())
    opposite = e.client.post(f"{url}/decline", headers=e.headers())

    assert (first.status_code, second.status_code, opposite.status_code) == (202, 409, 409)
    assert _sql("SELECT count(*) FROM ai.approval WHERE remediation_id = :i", i=s.offers["staging"]) == [(1,)]
    assert len(runner.scheduled) == 1


@pytest.mark.parametrize("status", ["OFFERED", "PREFLIGHT", "DEPLOYING", "VERIFYING", "FAILED", "VERIFIED"])
def test_nothing_can_be_answered_unless_the_run_is_waiting_for_it(env: tuple[Env, FakeRunner, Seed], status: str) -> None:
    e, runner, s = env
    _sql("UPDATE ai.remediation SET status = :s WHERE id = :i", s=status, i=s.offers["staging"])

    assert e.client.post(f"{BASE}/{s.offers['staging']}/deploy/approve", headers=e.headers()).status_code == 409
    assert runner.scheduled == [] and _status(s.offers["staging"]) == status
    assert _sql("SELECT count(*) FROM ai.approval WHERE remediation_id = :i", i=s.offers["staging"]) == [(0,)]


def test_an_environment_taken_out_of_the_target_cannot_be_approved(env: tuple[Env, FakeRunner, Seed]) -> None:
    e, _, s = env
    _waiting(s.offers["staging"])
    _sql("DELETE FROM ai.deploy_target WHERE repository_id = :r", r=s.repository_id)

    response = e.client.post(f"{BASE}/{s.offers['staging']}/deploy/approve", headers=e.headers())

    assert response.status_code == 409 and _status(s.offers["staging"]) == "AWAITING_DEPLOY_APPROVAL"
