"""The remediation store: guarded moves, undoing a half-made move, the sweeper query, and the counts the policy reads."""

import os
from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.store import approvals, deploy_audit, deployments
from app.store import remediations as store
from tests.remediation_support import (  # noqa: F401  (fixture)
    MERGE_SHA,
    Harness,
    build_harness,
    forget,
    session_factory,
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def h(session_factory) -> AsyncGenerator[Harness]:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        yield harness
    finally:
        await forget(session_factory, harness.seed.tenant)


async def test_a_move_the_diagram_forbids_is_an_error_not_a_silent_no_op(h: Harness) -> None:
    async with h.factory() as session:
        with pytest.raises(ValueError, match="cannot move"):
            await store.transition(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], from_status="OFFERED", to="DEPLOYING")


async def test_a_move_from_the_wrong_current_status_changes_nothing(h: Harness) -> None:
    async with h.factory() as session:
        moved = await store.transition(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], from_status="PREFLIGHT", to="FAILED")

    assert moved is False and (await h.row("staging")).status == "OFFERED"


async def test_a_terminal_move_stamps_the_finish_time_and_clears_the_wake_up(h: Harness) -> None:
    async with h.factory() as session:
        await store.start(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], started_by=h.seed.user)
        await store.set_fields(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], wake_at=h.clock.now)
        moved = await store.transition(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], to="FAILED", outcome={"failed": "x"})

    row = await h.row("staging")
    assert moved and row.finished_at is not None and row.wake_at is None and row.outcome == {"failed": "x"}


async def test_another_tenant_cannot_move_a_remediation(h: Harness) -> None:
    async with h.factory() as session:
        moved = await store.transition(session, tenant_id=uuid4(), remediation_id=h.seed.offers["staging"], to="CANCELLED")
        started = await store.start(session, tenant_id=uuid4(), remediation_id=h.seed.offers["staging"], started_by=uuid4())

    assert moved is False and started is None and (await h.row("staging")).status == "OFFERED"


async def test_undoing_a_move_restores_the_waiting_status_and_clears_the_finish_time(h: Harness) -> None:
    ids = {"tenant_id": h.seed.tenant, "remediation_id": h.seed.offers["staging"]}
    async with h.factory() as session:
        await store.start(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], started_by=h.seed.user)
        await store.transition(session, from_status="PREFLIGHT", to="AWAITING_DEPLOY_APPROVAL", **ids)
        await store.transition(session, from_status="AWAITING_DEPLOY_APPROVAL", to="DECLINED", **ids)

        undone = await store.revert(session, current="DECLINED", back="AWAITING_DEPLOY_APPROVAL", **ids)
        again = await store.revert(session, current="DECLINED", back="AWAITING_DEPLOY_APPROVAL", **ids)

    row = await h.row("staging")
    assert undone and not again and row.status == "AWAITING_DEPLOY_APPROVAL" and row.finished_at is None


async def test_deploys_are_counted_per_environment_and_per_hour(h: Harness) -> None:
    async with h.factory() as session:
        await deployments.ensure(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], kind="deploy", environment="staging", sha=MERGE_SHA, now=h.clock.now - timedelta(minutes=10))
        await deployments.ensure(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["production"], kind="deploy", environment="production", sha=MERGE_SHA, now=h.clock.now - timedelta(hours=3))
        ids = {"tenant_id": h.seed.tenant, "repository_id": h.seed.repository_id}

        staging = await store.deploys_since(session, environment="staging", since=h.clock.now - timedelta(hours=1), **ids)
        production_recent = await store.deploys_since(session, environment="production", since=h.clock.now - timedelta(hours=1), **ids)
        production_all = await store.deploys_since(session, environment="production", since=h.clock.now - timedelta(hours=4), **ids)
        elsewhere = await store.deploys_since(session, environment="staging", since=h.clock.now - timedelta(hours=1), tenant_id=uuid4(), repository_id=h.seed.repository_id)

    assert (staging, production_recent, production_all, elsewhere) == (1, 0, 1, 0)


async def test_only_a_verified_remediation_counts_as_verified(h: Harness) -> None:
    ids = {"tenant_id": h.seed.tenant, "pull_request_id": h.seed.pull_request_id, "environment": "staging"}
    async with h.factory() as session:
        assert await store.verified(session, **ids) is False
        await session.execute(text("UPDATE ai.remediation SET status = 'ROLLED_BACK' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.commit()
        assert await store.verified(session, **ids) is False
        await session.execute(text("UPDATE ai.remediation SET status = 'VERIFIED' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.commit()
        assert await store.verified(session, **ids) is True
        assert await store.verified(session, **{**ids, "tenant_id": uuid4()}) is False


async def test_the_correlation_id_is_made_once_and_every_later_attempt_gets_the_same_one(h: Harness) -> None:
    args = {"tenant_id": h.seed.tenant, "remediation_id": h.seed.offers["staging"], "kind": "deploy", "environment": "staging", "sha": MERGE_SHA, "now": h.clock.now}
    async with h.factory() as session:
        first, created = await deployments.ensure(session, **args)
        second, created_again = await deployments.ensure(session, **args)

    assert created and not created_again and first.correlation_id == second.correlation_id and first.dispatched_at == h.clock.now


async def test_a_run_is_recorded_without_losing_what_was_known(h: Harness) -> None:
    args = {"tenant_id": h.seed.tenant, "remediation_id": h.seed.offers["staging"], "kind": "deploy", "environment": "staging", "sha": MERGE_SHA, "now": h.clock.now}
    async with h.factory() as session:
        dep, _ = await deployments.ensure(session, **args)
        await deployments.update_run(session, tenant_id=h.seed.tenant, deployment_id=dep.id, status="running", run_id=7, run_url="https://x/7")
        await deployments.update_run(session, tenant_id=h.seed.tenant, deployment_id=dep.id, status="succeeded")  # no run id this time
        after = await deployments.get(session, tenant_id=h.seed.tenant, remediation_id=dep.remediation_id, kind="deploy")

    assert after is not None and (after.run_id, after.run_url, after.status) == (7, "https://x/7", "succeeded") and after.finished and after.finished_at is not None


async def test_the_latest_answer_at_a_gate_is_the_one_that_counts(h: Harness) -> None:
    args = {"tenant_id": h.seed.tenant, "remediation_id": h.seed.offers["staging"], "gate": "deploy", "user_id": h.seed.user, "role": "ADMIN", "environment": "staging", "sha": MERGE_SHA}
    async with h.factory() as session:
        assert await approvals.latest(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], gate="deploy") is None
        await approvals.record(session, decision="decline", **args)
        await approvals.record(session, decision="approve", **args)
        latest = await approvals.latest(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], gate="deploy")
        other = await approvals.latest(session, tenant_id=uuid4(), remediation_id=h.seed.offers["staging"], gate="deploy")

    assert latest is not None and latest.decision == "approve" and other is None


async def test_the_audit_redacts_secrets_and_is_read_back_in_order(h: Harness) -> None:
    async with h.factory() as session:
        await deploy_audit.append(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], action="deploy.failed", result="failed", detail="boom ghp_0123456789abcdefghijklmnopqrstuvwxyz here")
        await deploy_audit.append(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"], action="second", result="ok", detail="y" * 5000)
        rows = await deploy_audit.list_for_remediation(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"])
        elsewhere = await deploy_audit.list_for_remediation(session, tenant_id=uuid4(), remediation_id=h.seed.offers["staging"])

    assert [r.action for r in rows] == ["deploy.failed", "second"]
    assert "ghp_" not in rows[0].detail and len(rows[1].detail) == deploy_audit.MAX_DETAIL
    assert elsewhere == []
