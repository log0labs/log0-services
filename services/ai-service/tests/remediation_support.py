"""Shared pieces for the deploy (V3) tests: fakes for every outside system, a database seed, and a harness that runs the real
graph, executor and runner against a real Postgres with an in-memory checkpointer and a clock the test controls.
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings, get_settings
from app.db.session import create_session_factory
from app.deploy.executor import RemediationExecutor
from app.deploy.graph import build_remediation_graph
from app.deploy.provider import DispatchRequest, RunInfo
from app.deploy.runner import RemediationRunner
from app.deploy.state import RemediationContext
from app.integrations.github import GitHubUnavailableError
from app.integrations.github_app import DEPLOY_PERMISSIONS
from app.integrations.github_app_client import InstallationInfo
from app.integrations.github_writer import WriteRejectedError, WriteTarget
from app.runner import SessionFactory
from app.schemas.domain import CheckResult
from app.store import remediations as remediation_store
from tests.graph_fakes import FINGERPRINT, INCIDENT_ID, FakeApi, incident_payload

MERGE_SHA = "c" * 40
PREVIOUS_SHA = "b" * 40
GOOD_DIFF = "--- a/services/api/src/db.js\n+++ b/services/api/src/db.js\n@@ -1 +1 @@\n-a\n+b\n"
NOON = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # a Monday


class Clock:
    def __init__(self, now: datetime = NOON) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeProvider:
    """A pipeline that does what the test scripts: dispatch makes a run (``queued``), ``status`` is what it reports next."""

    def __init__(self) -> None:
        self.dispatches: list[DispatchRequest] = []
        self.cancelled: list[int] = []
        self.status = "dispatched"
        self.dispatch_error: Exception | None = None
        self.status_error: Exception | None = None
        self.find_error: Exception | None = None
        self.hide_run = False  # GitHub is slow: the run does not show up in the list
        self.run_id = 4242
        self._runs: dict[UUID, int] = {}

    async def dispatch(self, target: WriteTarget, request: DispatchRequest) -> None:
        if self.dispatch_error:
            raise self.dispatch_error
        self.dispatches.append(request)
        self._runs[request.correlation_id] = self.run_id

    def _info(self, run_id: int) -> RunInfo:
        return RunInfo(run_id=run_id, url=f"https://github.com/acme/shop/actions/runs/{run_id}", status=self.status)  # type: ignore[arg-type]

    async def find_run(self, target: WriteTarget, request: DispatchRequest) -> RunInfo | None:
        if self.find_error:
            raise self.find_error
        run_id = self._runs.get(request.correlation_id)
        return None if run_id is None or self.hide_run else self._info(run_id)

    async def run_status(self, target: WriteTarget, run_id: int) -> RunInfo:
        if self.status_error:
            raise self.status_error
        return self._info(run_id)

    async def cancel(self, target: WriteTarget, run_id: int) -> None:
        self.cancelled.append(run_id)


class FakeChecks:
    def __init__(self, *conclusions: str | None) -> None:
        self.conclusions = conclusions or ("success",)
        self.error: Exception | None = None
        self.asked: list[Any] = []
        self.empty = False

    async def check_runs(self, target: WriteTarget, sha: str, permissions: Any = None) -> list[CheckResult]:
        self.asked.append((sha, permissions))
        if self.error:
            raise self.error
        if self.empty:
            return []
        return [
            CheckResult(name=f"ci-{i}", status="completed" if c else "in_progress", conclusion=c)
            for i, c in enumerate(self.conclusions)
        ]


class FakeInstallations:
    def __init__(self) -> None:
        self.permissions = dict(DEPLOY_PERMISSIONS)
        self.error: Exception | None = None

    async def get_installation(self, installation_id: int) -> InstallationInfo:
        if self.error:
            raise self.error
        return InstallationInfo(installation_id, "acme", "Organization", False, dict(self.permissions))


class FakeDeploys:
    def __init__(self, sha: str | None = PREVIOUS_SHA) -> None:
        self.sha = sha
        self.error: Exception | None = None

    async def last_successful_deployment(self, target: WriteTarget, environment: str) -> str | None:
        if self.error:
            raise self.error
        return self.sha


class ScriptedLogs:
    """Answers the three queries a baseline needs: the fingerprint's timeline, other errors, and the service's traffic."""

    def __init__(self, *, errors_per_minute: int = 4, traffic: int = 6000, others: list[dict[str, Any]] | None = None) -> None:
        self.errors_per_minute = errors_per_minute
        self.traffic = traffic
        self.others = others or []
        self.error: Exception | None = None

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self.error:
            raise self.error
        parameters = parameters or {}
        if "toStartOfInterval" in sql:
            start, end = parameters["start"], parameters["end"]
            rows, at = [], start
            while at < end - timedelta(minutes=1):  # the last bucket starts at "now": nothing has been written in it yet
                rows.append({"bucket_start": at, "n": self.errors_per_minute})
                at += timedelta(minutes=1)
            return rows
        if "GROUP BY service_name, fingerprint" in sql:
            return self.others
        if "service_name = {service" in sql:
            return [{"n": self.traffic}]
        return []


@dataclass
class Seed:
    tenant: UUID
    user: UUID
    repository_id: UUID
    investigation_id: UUID
    pull_request_id: UUID
    installation_id: int
    offers: dict[str, UUID] = field(default_factory=dict)


STAGING = {"name": "staging", "enabled": True, "approver": "ENGINEER", "soakSeconds": 60, "deployTimeoutSeconds": 600}
PRODUCTION = {"name": "production", "enabled": True, "requiresPreviousVerified": True, "soakSeconds": 60, "deployTimeoutSeconds": 600}


async def seed(
    factory: SessionFactory,
    *,
    environments: list[dict[str, Any]] | None = None,
    diff: str = GOOD_DIFF,
    merge_sha: str = MERGE_SHA,
    tenant: UUID | None = None,
    installation_id: int | None = None,
    repository_name: str = "shop",
) -> Seed:
    """A merged pull request of a repository with a deploy target, and the offers its merge made."""
    tenant = tenant or uuid4()
    user = uuid4()
    installation_id = installation_id or int(uuid4().int % 10**9)
    async with factory() as session:
        await session.execute(
            text("INSERT INTO ai.github_installation (tenant_id, installation_id, account_login, account_type, created_by) VALUES (:t, :i, 'acme', 'Organization', :u)"),
            {"t": tenant, "i": installation_id, "u": user},
        )
        repository_id = (
            await session.execute(
                text(
                    "INSERT INTO ai.repository_connection (tenant_id, owner, name, token_ciphertext, key_id, created_by, installation_id, github_repository_id, pr_writes_enabled, default_branch) "
                    "VALUES (:t, 'acme', :n, 'x', 'k1', :u, :i, 7, true, 'main') RETURNING id"
                ),
                {"t": tenant, "u": user, "i": installation_id, "n": repository_name},
            )
        ).scalar_one()
        investigation_id = (
            await session.execute(
                text("INSERT INTO ai.investigation (tenant_id, incident_id, requested_by, status, diff) VALUES (:t, :inc, :u, 'COMPLETED', :d) RETURNING id"),
                {"t": tenant, "inc": INCIDENT_ID, "u": user, "d": diff},
            )
        ).scalar_one()
        pull_request_id = (
            await session.execute(
                text(
                    "INSERT INTO ai.pull_request (tenant_id, investigation_id, repository_id, branch, base_sha, number, state, approved_by, merge_commit_sha, merged_at) "
                    "VALUES (:t, :i, :r, 'log0/fix-a-b', 'a', 5, 'merged', :u, :m, now()) RETURNING id"
                ),
                {"t": tenant, "i": investigation_id, "r": repository_id, "u": user, "m": merge_sha},
            )
        ).scalar_one()
        names = environments if environments is not None else [STAGING, PRODUCTION]
        await session.execute(
            text("INSERT INTO ai.deploy_target (tenant_id, repository_id, workflow_file, environments, updated_by) VALUES (:t, :r, 'deploy.yml', CAST(:e AS jsonb), :u)"),
            {"t": tenant, "r": repository_id, "e": json.dumps(names), "u": user},
        )
        await session.commit()
        created = await remediation_store.offer_for_merge(
            session,
            tenant_id=tenant,
            pull_request_id=pull_request_id,
            investigation_id=investigation_id,
            repository_id=repository_id,
            merge_sha=merge_sha,
            environments=[e["name"] for e in names if e.get("enabled")],
        )
    return Seed(tenant, user, repository_id, investigation_id, pull_request_id, installation_id, {r.environment: r.id for r in created})


async def forget(factory: SessionFactory, tenant: UUID) -> None:
    """Remove a test's rows (everything follows the repository and the investigation by cascade; the audit cannot be removed)."""
    async with factory() as session:
        for table in ("investigation", "repository_connection", "github_installation"):
            await session.execute(text(f"DELETE FROM ai.{table} WHERE tenant_id = :t"), {"t": tenant})
        await session.commit()


@dataclass
class Harness:
    factory: SessionFactory
    seed: Seed
    clock: Clock
    provider: FakeProvider
    checks: FakeChecks
    installations: FakeInstallations
    deploys: FakeDeploys
    logs: ScriptedLogs
    settings: Settings
    executor: RemediationExecutor
    runner: RemediationRunner

    @property
    def context(self) -> RemediationContext:
        return RemediationContext(
            tenant_id=self.seed.tenant,
            sessions=self.factory,
            provider=self.provider,
            deploys=self.deploys,
            checks=self.checks,
            installations=self.installations,
            logs=self.logs,
            incidents=FakeApi(incident_payload(tenantId=str(self.seed.tenant))),
            settings=self.settings,
            now=self.clock,
        )

    async def run(self, environment: str) -> None:
        await self.executor.run(self.factory, self.seed.offers[environment], self.seed.tenant)

    async def row(self, environment: str) -> remediation_store.Remediation:
        async with self.factory() as session:
            row = await remediation_store.get(session, tenant_id=self.seed.tenant, remediation_id=self.seed.offers[environment])
        assert row is not None
        return row

    async def start(self, environment: str = "staging") -> remediation_store.Remediation:
        async with self.factory() as session:
            started = await remediation_store.start(session, tenant_id=self.seed.tenant, remediation_id=self.seed.offers[environment], started_by=self.seed.user)
        assert started is not None
        await self.run(environment)
        return await self.row(environment)

    async def answer(self, action: str, environment: str = "staging", role: str = "ADMIN") -> None:
        """What ``POST /deploy/approve`` and ``/deploy/decline`` do, minus the HTTP."""
        from app.store import approvals

        row = await self.row(environment)
        async with self.factory() as session:
            won = await remediation_store.transition(
                session,
                tenant_id=self.seed.tenant,
                remediation_id=row.id,
                from_status="AWAITING_DEPLOY_APPROVAL",
                to="DEPLOYING" if action == "approve" else "DECLINED",
            )
            assert won
            await approvals.record(session, tenant_id=self.seed.tenant, remediation_id=row.id, gate="deploy", decision=action, user_id=self.seed.user, role=role, environment=environment, sha=row.merge_sha)  # type: ignore[arg-type]
        await self.run(environment)

    async def events(self) -> list[str]:
        async with self.factory() as session:
            rows = await session.execute(text("SELECT type FROM ai.investigation_event WHERE investigation_id = :i ORDER BY seq"), {"i": self.seed.investigation_id})
            return [r[0] for r in rows]

    async def audit(self) -> list[tuple[str, str]]:
        async with self.factory() as session:
            rows = await session.execute(text("SELECT action, result FROM ai.deploy_audit WHERE tenant_id = :t ORDER BY id"), {"t": self.seed.tenant})
            return [(r[0], r[1]) for r in rows]

    def runtime(self) -> Any:
        return SimpleNamespace(context=self.context)


def state_for(h: Harness, environment: str = "staging", **extra: Any) -> dict[str, Any]:
    return {"remediation_id": str(h.seed.offers[environment]), "investigation_id": str(h.seed.investigation_id), "environment": environment, "merge_sha": MERGE_SHA, "gaps": [], **extra}


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


async def build_harness(factory: SessionFactory, **seed_args: Any) -> Harness:
    clock = Clock()
    settings = Settings(_env_file=None, deploys_enabled=True, deploy_poll_seconds=30.0)  # type: ignore[call-arg]
    provider, checks, installations, deploys, logs = FakeProvider(), FakeChecks(), FakeInstallations(), FakeDeploys(), ScriptedLogs()
    seeded = await seed(factory, **seed_args)
    saver = InMemorySaver()
    incidents = FakeApi(incident_payload(tenantId=str(seeded.tenant)))
    executor = RemediationExecutor(
        build_remediation_graph(saver),
        provider=provider,
        deploys=deploys,
        checks=checks,
        installations=installations,
        logs=logs,
        incidents=incidents,
        settings=settings,
        now=clock,
    )
    runner = RemediationRunner(factory, executor.run, sweep_seconds=0.05, now=clock)
    return Harness(factory, seeded, clock, provider, checks, installations, deploys, logs, settings, executor, runner)


__all__ = ["FINGERPRINT", "GitHubUnavailableError", "WriteRejectedError"]
