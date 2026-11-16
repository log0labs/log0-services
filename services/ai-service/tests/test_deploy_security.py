"""Safety properties of deploys: the audit endpoints, other workspaces' ids, and what can never happen without a human's approval."""

import os
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.store import deploy_audit
from tests.jwt_helpers import TENANT_B
from tests.remediation_support import (
    PREVIOUS_SHA,
    Harness,
    build_harness,
    forget,
    session_factory,  # noqa: F401  (fixture)
)
from tests.test_remediation_api import (  # noqa: F401  (fixture)
    BASE,
    FakeRunner,
    Seed,
    _sql,
    env,
)

needs_db = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)


# ------------------------------------------------------------------- the audit endpoints


@needs_db
def test_the_audit_is_for_admins_only(env) -> None:  # noqa: F811
    e, _, s = env
    for role in ("ENGINEER", "VIEWER"):
        assert e.client.get(f"{BASE}/audit", headers=e.headers(role=role)).status_code == 403
        assert e.client.get(f"{BASE}/{s.offers['staging']}/audit", headers=e.headers(role=role)).status_code == 403
    assert e.client.get(f"{BASE}/audit").status_code == 401


@needs_db
def test_an_approval_leaves_a_row_in_the_audit_of_the_workspace_and_of_the_deploy(env) -> None:  # noqa: F811
    e, _, s = env
    _sql("UPDATE ai.remediation SET status = 'AWAITING_DEPLOY_APPROVAL' WHERE id = :i", i=s.offers["staging"])
    assert e.client.post(f"{BASE}/{s.offers['staging']}/deploy/approve", headers=e.headers()).status_code == 202

    mine = e.client.get(f"{BASE}/{s.offers['staging']}/audit", headers=e.headers()).json()
    everything = e.client.get(f"{BASE}/audit", headers=e.headers()).json()

    assert [(r["action"], r["result"], r["environment"]) for r in mine] == [("deploy.approved", "ok", "staging")]
    assert mine[0]["actorId"] and mine[0]["sha"] == "c" * 40
    assert everything[0]["id"] == mine[0]["id"]  # newest first (rows of earlier runs stay: the table is append-only)


@needs_db
def test_another_workspace_sees_no_audit_and_cannot_ask_for_a_deploys(env) -> None:  # noqa: F811
    e, _, s = env
    _sql("UPDATE ai.remediation SET status = 'AWAITING_DEPLOY_APPROVAL' WHERE id = :i", i=s.offers["staging"])
    e.client.post(f"{BASE}/{s.offers['staging']}/deploy/approve", headers=e.headers())

    assert e.client.get(f"{BASE}/audit", headers=e.headers(tenant=TENANT_B)).json() == []
    assert e.client.get(f"{BASE}/{s.offers['staging']}/audit", headers=e.headers(tenant=TENANT_B)).status_code == 404


@needs_db
def test_another_workspaces_deploy_is_a_404_on_every_route_and_nothing_changes(env) -> None:  # noqa: F811
    e, runner, s = env
    _sql("UPDATE ai.remediation SET status = 'AWAITING_DEPLOY_APPROVAL' WHERE id = :i", i=s.offers["staging"])
    theirs = e.headers(tenant=TENANT_B)
    url = f"{BASE}/{s.offers['staging']}"

    calls = [
        e.client.get(url, headers=theirs),
        e.client.post(f"{url}/start", headers=theirs),
        e.client.post(f"{url}/deploy/approve", headers=theirs),
        e.client.post(f"{url}/deploy/decline", headers=theirs),
        e.client.post(f"{url}/decision", json={"action": "rollback"}, headers=theirs),
        e.client.get(f"{BASE}/{uuid4()}", headers=e.headers()),
    ]

    assert [c.status_code for c in calls] == [404] * 6
    assert _sql("SELECT status FROM ai.remediation WHERE id = :i", i=s.offers["staging"]) == [("AWAITING_DEPLOY_APPROVAL",)]
    assert runner.scheduled == []


@needs_db
@pytest.mark.asyncio
async def test_a_token_in_a_detail_never_reaches_the_audit(session_factory) -> None:  # noqa: F811
    tenant = uuid4()
    secret = "ghs_" + "A1b2C3d4" * 5
    async with session_factory() as session:
        await deploy_audit.append(session, tenant_id=tenant, remediation_id=None, action="deploy.failed", result="failed", detail=f"GitHub said: token {secret} is bad, Authorization: Bearer {secret}")
        rows = await deploy_audit.list_for_tenant(session, tenant_id=tenant)

    assert len(rows) == 1 and secret not in rows[0].detail and "ghs_" not in rows[0].detail


# --------------------------------------------------------- nothing deploys without an approval

@pytest_asyncio.fixture
async def h(session_factory) -> AsyncGenerator[Harness]:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        yield harness
    finally:
        await forget(session_factory, harness.seed.tenant)


@needs_db
@pytest.mark.asyncio
async def test_nothing_is_dispatched_before_somebody_approves(h: Harness) -> None:
    await h.start()
    await h.run("staging")
    await h.run("staging")  # a sweeper or a restart waking the run changes nothing

    assert (await h.row("staging")).status == "AWAITING_DEPLOY_APPROVAL" and h.provider.dispatches == []


@needs_db
@pytest.mark.asyncio
async def test_a_decline_never_dispatches_even_if_the_run_is_woken_again(h: Harness) -> None:
    await h.start()
    await h.answer("decline")
    await h.run("staging")

    assert h.provider.dispatches == []


@needs_db
@pytest.mark.asyncio
async def test_an_offer_nobody_started_never_dispatches(h: Harness) -> None:
    await h.run("staging")
    await h.run("production")

    assert h.provider.dispatches == [] and (await h.row("staging")).status == "OFFERED"


@needs_db
@pytest.mark.asyncio
async def test_only_the_listed_environments_are_ever_offered_or_deployed(h: Harness) -> None:
    async with h.factory() as session:
        names = (await session.execute(text("SELECT environment FROM ai.remediation WHERE tenant_id = :t ORDER BY environment"), {"t": h.seed.tenant})).scalars().all()
    assert names == ["production", "staging"]  # the two the target lists; "prod-eu" or "dev" cannot be asked for

    await h.deployed()
    assert {d.environment for d in h.provider.dispatches} == {"staging"}


@needs_db
@pytest.mark.asyncio
async def test_the_dispatch_carries_the_merge_commit_and_the_rollback_the_previous_one(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 10
    h.clock.advance(seconds=120 + 6 * 60)
    await h.run("staging")
    await h.decide("rollback")

    assert [d.sha for d in h.provider.dispatches] == ["c" * 40, PREVIOUS_SHA]
    assert all(len(d.sha) == 40 for d in h.provider.dispatches)
