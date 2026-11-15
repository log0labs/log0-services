"""The deploy flow end to end, through the real graph, executor and a real Postgres: refuse, wait for a human, dispatch once,
follow the run, record the release. The outside systems (GitHub, the logs) are fakes the test scripts; the clock is the test's.
"""

import os
from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.deploy.nodes import DISPATCH_GRACE, trigger_deploy, wait_deploy
from app.integrations.clickhouse import ClickHouseError
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import DEPLOY_PERMISSIONS
from app.integrations.github_writer import WriteRejectedError
from app.store import deployments, releases
from tests.remediation_support import (
    GOOD_DIFF,
    MERGE_SHA,
    PREVIOUS_SHA,
    Harness,
    build_harness,
    forget,
    session_factory,  # noqa: F401  (fixture)
    state_for,
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


# ------------------------------------------------------------------ the happy path


async def test_a_started_offer_passes_preflight_takes_a_baseline_and_waits_for_a_human(h: Harness) -> None:
    row = await h.start("staging")

    assert row.status == "AWAITING_DEPLOY_APPROVAL"
    assert row.rollback_target == PREVIOUS_SHA  # what the environment runs now, recorded before anything changes
    assert row.baseline is not None and row.baseline["errors_per_minute"] == 4.0 and row.baseline["total_logs"] == 6000
    assert row.policy == {"allowed": True, "reasons": []}
    assert await h.events() == ["remediation.preflight_passed", "remediation.baseline_captured", "remediation.approval_requested"]
    assert h.provider.dispatches == []  # nothing is asked of the pipeline before a human says yes


async def test_the_approval_request_shows_what_the_approver_needs(h: Harness) -> None:
    await h.start("staging")

    async with h.factory() as session:
        payload = (await session.execute(text("SELECT payload FROM ai.investigation_event WHERE type = 'remediation.approval_requested' AND investigation_id = :i"), {"i": h.seed.investigation_id})).scalar_one()

    assert payload["environment"] == "staging" and payload["sha"] == MERGE_SHA and payload["rollbackTarget"] == PREVIOUS_SHA
    assert payload["approver"] == "ENGINEER" and payload["soakSeconds"] == 600
    assert payload["baseline"]["errorsPerMinute"] == 4.0 and "gate" not in payload


async def test_an_approval_dispatches_the_pipeline_once_with_the_fixed_inputs(h: Harness) -> None:
    await h.start("staging")

    await h.answer("approve")

    [request] = h.provider.dispatches
    assert request.inputs() == {"sha": MERGE_SHA, "environment": "staging", "correlation_id": str(request.correlation_id)}
    assert request.workflow_file == "deploy.yml" and request.ref == "main"
    row = await h.row("staging")
    assert row.status == "DEPLOYING" and row.wake_at is not None  # the run is looking again in a while, not blocking
    assert (await h.row("staging")).wake_at == h.clock.now + timedelta(seconds=30)
    async with h.factory() as session:
        dep = await deployments.get(session, tenant_id=h.seed.tenant, remediation_id=row.id, kind="deploy")
    assert dep is not None and dep.run_id == 4242 and dep.sha == MERGE_SHA


async def test_a_waiting_run_is_woken_only_when_its_time_has_come(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status = "running"

    await h.run("staging")  # not due yet: nothing happens
    assert (await h.row("staging")).wake_at == h.clock.now + timedelta(seconds=30)
    assert await h.runner.sweep_once() == 0

    h.clock.advance(seconds=31)
    assert await h.runner.sweep_once() == 1
    await h.runner.wait_idle(h.seed.offers["staging"])

    row = await h.row("staging")
    assert row.status == "DEPLOYING" and row.wake_at == h.clock.now + timedelta(seconds=30)  # still running: wait again


async def test_a_successful_run_is_recorded_in_the_ledger_and_the_run_moves_on_to_verification(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status = "succeeded"
    h.clock.advance(seconds=31)

    await h.run("staging")

    row = await h.row("staging")
    assert row.status == "VERIFYING" and row.outcome == {"deployed": True}
    assert row.wake_at == h.clock.now + timedelta(seconds=30)  # the soak has begun: it looks again in a while
    async with h.factory() as session:
        current = await releases.current(session, tenant_id=h.seed.tenant, repository_id=h.seed.repository_id, environment="staging")
    assert current is not None and current.sha == MERGE_SHA and current.source == "log0" and current.remediation_id == row.id
    assert (await h.audit()) == [
        ("remediation.preflight", "ok")
    ][:0] + [("deploy.dispatching", "ok"), ("deploy.dispatched", "ok"), ("deploy.succeeded", "ok")]
    assert (await h.events())[-2:] == ["deploy.dispatched", "deploy.succeeded"]


async def test_a_decline_deploys_nothing_and_says_so(h: Harness) -> None:
    await h.start("staging")

    await h.answer("decline")

    row = await h.row("staging")
    assert row.status == "DECLINED" and row.finished_at is not None
    assert h.provider.dispatches == [] and (await h.audit()) == []
    async with h.factory() as session:
        decisions = (await session.execute(text("SELECT decision, role FROM ai.approval WHERE remediation_id = :r"), {"r": row.id})).all()
    assert [tuple(d) for d in decisions] == [("decline", "ADMIN")]


# ---------------------------------------------------------------- refusals leave the offer standing


async def _refused(h: Harness, fragment: str) -> None:
    row = await h.start("staging")
    assert row.status == "OFFERED", row.status  # still on offer: it can be started again when the reason is gone
    assert row.policy is not None and row.policy["allowed"] is False
    assert any(fragment in reason for reason in row.policy["reasons"]), row.policy
    assert h.provider.dispatches == []
    assert "remediation.refused" in await h.events()
    assert ("deploy.refused", "refused") in await h.audit()


async def test_a_server_with_deploys_switched_off_refuses(h: Harness) -> None:
    h.settings.deploys_enabled = False
    await _refused(h, "switched off on this server")


async def test_a_workspace_that_is_switched_off_refuses(h: Harness) -> None:
    h.settings.deploys_disabled_tenants = {h.seed.tenant}
    await _refused(h, "switched off for this workspace")


async def test_failing_checks_on_the_merge_commit_refuse(h: Harness) -> None:
    h.checks.conclusions = ("success", "failure")
    await _refused(h, "did not pass")


async def test_checks_that_are_still_running_refuse(h: Harness) -> None:
    h.checks.conclusions = ("success", None)
    await _refused(h, "still running")


async def test_a_repository_without_checks_is_not_refused(h: Harness) -> None:
    h.checks.empty = True

    assert (await h.start("staging")).status == "AWAITING_DEPLOY_APPROVAL"


async def test_the_checks_are_read_with_the_deploy_token_not_the_pull_request_one(h: Harness) -> None:
    await h.start("staging")

    assert h.checks.asked == [(MERGE_SHA, DEPLOY_PERMISSIONS)]


async def test_an_installation_that_has_not_accepted_the_deploy_permissions_refuses(h: Harness) -> None:
    h.installations.permissions = {"contents": "write", "pull_requests": "write", "checks": "read", "metadata": "read"}
    await _refused(h, "has not been granted: actions, deployments")


@pytest.mark.parametrize("error", [GitHubUnavailableError(), GitHubAccessError("forbidden")])
async def test_an_installation_that_cannot_be_read_refuses(h: Harness, error: Exception) -> None:
    h.installations.error = error
    await _refused(h, "could not check what the GitHub App may do")


async def test_unreadable_checks_refuse(h: Harness) -> None:
    h.checks.error = GitHubUnavailableError()
    await _refused(h, "checks of the merge commit could not be read")


async def test_not_knowing_what_the_environment_runs_refuses(h: Harness) -> None:
    h.deploys.error = GitHubUnavailableError()
    await _refused(h, "could not tell what 'staging' runs now")


async def test_an_environment_that_already_runs_the_commit_refuses(h: Harness) -> None:
    h.deploys.sha = MERGE_SHA
    await _refused(h, "already runs this commit")


async def test_a_change_that_touches_a_migration_refuses(session_factory) -> None:  # noqa: F811
    migrating = "--- a/db/migrate/001_users.rb\n+++ b/db/migrate/001_users.rb\n@@ -1 +1 @@\n-a\n+b\n" + GOOD_DIFF
    harness = await build_harness(session_factory, diff=migrating)
    try:
        await _refused(harness, "migration")
    finally:
        await forget(session_factory, harness.seed.tenant)


async def test_a_refused_offer_can_be_started_again_once_the_reason_is_gone(h: Harness) -> None:
    h.settings.deploys_enabled = False
    assert (await h.start("staging")).status == "OFFERED"

    h.settings.deploys_enabled = True
    row = await h.start("staging")

    assert row.status == "AWAITING_DEPLOY_APPROVAL"


async def test_no_known_release_is_a_gap_but_not_a_refusal(h: Harness) -> None:
    h.deploys.sha = None

    row = await h.start("staging")

    assert row.status == "AWAITING_DEPLOY_APPROVAL" and row.rollback_target is None


@pytest.mark.parametrize("error", [ClickHouseError("down")])
async def test_logs_that_cannot_be_read_refuse_because_nothing_could_be_verified(h: Harness, error: Exception) -> None:
    h.logs.error = error
    await _refused(h, "baseline")


async def test_the_other_errors_of_the_baseline_are_kept_as_rates(session_factory) -> None:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        harness.logs.others = [{"service_name": "gateway", "fingerprint": "e" * 64, "message_template": "x", "n": 60, "first_ts": harness.clock.now, "last_ts": harness.clock.now}]

        row = await harness.start("staging")

        assert row.baseline is not None and row.baseline["other_fingerprints"] == {"e" * 64: 2.0}  # 60 in 30 minutes
    finally:
        await forget(session_factory, harness.seed.tenant)


async def test_an_environment_in_a_freeze_window_refuses(session_factory) -> None:  # noqa: F811
    window = {"name": "staging", "enabled": True, "approver": "ENGINEER", "freezeWindows": [{"days": [0], "startHour": 9, "endHour": 18}]}  # Monday, now is noon
    harness = await build_harness(session_factory, environments=[window])
    try:
        await _refused(harness, "freeze window")
    finally:
        await forget(session_factory, harness.seed.tenant)


# ------------------------------------------------------------ the second environment


async def test_production_waits_for_staging_to_have_verified(h: Harness) -> None:
    row = await h.start("production")

    assert row.status == "OFFERED" and any("'staging' has not verified" in r for r in (row.policy or {})["reasons"])


async def test_production_may_start_once_staging_verified(h: Harness) -> None:
    async with h.factory() as session:
        await session.execute(text("UPDATE ai.remediation SET status = 'VERIFIED' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.commit()

    assert (await h.start("production")).status == "AWAITING_DEPLOY_APPROVAL"


async def test_two_runs_cannot_hold_one_environment(h: Harness) -> None:
    from app.store import remediations as store

    await h.start("staging")
    async with h.factory() as session:
        other_investigation = (
            await session.execute(
                text("INSERT INTO ai.investigation (tenant_id, incident_id, requested_by, status) VALUES (:t, gen_random_uuid(), :u, 'COMPLETED') RETURNING id"),
                {"t": h.seed.tenant, "u": h.seed.user},
            )
        ).scalar_one()
        other_pr = (
            await session.execute(
                text(
                    "INSERT INTO ai.pull_request (tenant_id, investigation_id, repository_id, branch, base_sha, number, state, approved_by, merge_commit_sha) "
                    "SELECT :t, :i, :r, 'log0/fix-c-d', 'a', 6, 'merged', :u, :m RETURNING id"
                ),
                {"t": h.seed.tenant, "i": other_investigation, "r": h.seed.repository_id, "u": h.seed.user, "m": "d" * 40},
            )
        ).scalar_one()
        await session.commit()
        [offer] = await store.offer_for_merge(
            session, tenant_id=h.seed.tenant, pull_request_id=other_pr, investigation_id=other_investigation, repository_id=h.seed.repository_id, merge_sha="d" * 40, environments=["staging"]
        )

        with pytest.raises(store.EnvironmentBusyError):  # the first holds staging while it waits for its approval
            await store.start(session, tenant_id=h.seed.tenant, remediation_id=offer.id, started_by=h.seed.user)


# -------------------------------------------------------------------- the pipeline fails


async def test_a_failed_pipeline_run_fails_the_remediation_and_records_nothing_as_released(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status = "failed"
    h.clock.advance(seconds=31)

    await h.run("staging")

    row = await h.row("staging")
    assert row.status == "FAILED" and row.outcome == {"failed": "the pipeline run failed"} and row.wake_at is None
    async with h.factory() as session:
        current = await releases.current(session, tenant_id=h.seed.tenant, repository_id=h.seed.repository_id, environment="staging")
    assert current is not None and current.sha == PREVIOUS_SHA and current.source == "github"  # what ran before is still what runs
    assert ("deploy.failed", "failed") in await h.audit()
    assert "deploy.failed" in await h.events()


@pytest.mark.parametrize("error", [WriteRejectedError("no workflow_dispatch trigger"), GitHubAccessError("forbidden")])
async def test_a_dispatch_github_refuses_fails_with_a_reason_a_person_can_act_on(h: Harness, error: Exception) -> None:
    await h.start("staging")
    h.provider.dispatch_error = error

    await h.answer("approve")

    row = await h.row("staging")
    assert row.status == "FAILED" and "workflow_dispatch" in (row.outcome or {})["failed"]
    assert h.provider.dispatches == []


async def test_a_deploy_that_never_finishes_is_cancelled_and_fails_when_its_time_is_up(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status = "running"
    h.clock.advance(seconds=601)  # the staging rule allows ten minutes

    await h.run("staging")

    row = await h.row("staging")
    assert row.status == "FAILED" and "in time" in (row.outcome or {})["failed"]
    assert h.provider.cancelled == [4242]
    async with h.factory() as session:
        dep = await deployments.get(session, tenant_id=h.seed.tenant, remediation_id=row.id, kind="deploy")
    assert dep is not None and dep.status == "timeout"


async def test_github_having_a_bad_moment_while_polling_does_not_fail_the_deploy(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status_error = GitHubUnavailableError()
    h.clock.advance(seconds=31)

    await h.run("staging")

    row = await h.row("staging")
    assert row.status == "DEPLOYING" and row.wake_at is not None  # it looks again later; the deadline still applies


async def test_losing_access_to_the_run_fails_the_deploy(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status_error = GitHubAccessError("forbidden")
    h.clock.advance(seconds=31)

    await h.run("staging")

    assert (await h.row("staging")).status == "FAILED"


# ------------------------------------------------------------- once, even after a crash


async def test_asking_the_pipeline_twice_dispatches_once(h: Harness) -> None:
    await h.start("staging")
    async with h.factory() as session:
        await session.execute(text("UPDATE ai.remediation SET status = 'DEPLOYING' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.commit()
    state = state_for(h, rule={"name": "staging", "approver": "ENGINEER"})

    await trigger_deploy(state, h.runtime())
    await trigger_deploy(state, h.runtime())  # a resume after a crash runs the node again

    assert len(h.provider.dispatches) == 1
    async with h.factory() as session:
        count = (await session.execute(text("SELECT count(*) FROM ai.deployment WHERE remediation_id = :r"), {"r": h.seed.offers["staging"]})).scalar_one()
    assert count == 1


async def test_a_dispatch_that_may_have_happened_is_looked_for_not_repeated(h: Harness) -> None:
    await h.start("staging")
    state = state_for(h, rule={"name": "staging", "approver": "ENGINEER"})
    async with h.factory() as session:
        await session.execute(text("UPDATE ai.remediation SET status = 'DEPLOYING' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.commit()
    h.provider.find_error = GitHubUnavailableError()  # the first attempt could not even look
    h.provider.dispatch_error = GitHubUnavailableError()  # and its dispatch left us unsure

    await trigger_deploy(state, h.runtime())

    h.provider.dispatch_error = None
    h.provider.find_error = None
    await trigger_deploy(state, h.runtime())  # the row exists: it only looks, it does not dispatch blindly again

    assert h.provider.dispatches == []


async def test_a_run_that_never_appears_fails_after_the_grace_period(h: Harness) -> None:
    await h.start("staging")
    h.provider.hide_run = True  # GitHub is slow to list the run
    await h.answer("approve")

    await h.run("staging")
    h.clock.advance(seconds=31)
    await h.run("staging")
    assert (await h.row("staging")).status == "DEPLOYING"  # GitHub may just be slow

    h.clock.advance(seconds=DISPATCH_GRACE.total_seconds())
    await h.run("staging")

    row = await h.row("staging")
    assert row.status == "FAILED" and "never appeared" in (row.outcome or {})["failed"]


async def test_waiting_again_after_a_finished_deployment_does_not_redeploy(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status = "succeeded"
    h.clock.advance(seconds=31)
    await h.run("staging")

    update = await wait_deploy(state_for(h, rule={"name": "staging", "approver": "ENGINEER"}), h.runtime())

    assert update["outcome"] == "deployed" and len(h.provider.dispatches) == 1
    assert (await h.row("staging")).status == "VERIFYING"


# ----------------------------------------------------------------------- who owns the run


async def test_another_tenant_cannot_run_a_remediation(h: Harness) -> None:
    with pytest.raises(LookupError):
        await h.executor.run(h.factory, h.seed.offers["staging"], uuid4())


async def test_an_offer_nobody_started_is_not_run(h: Harness) -> None:
    await h.run("staging")

    assert (await h.row("staging")).status == "OFFERED" and await h.events() == []
