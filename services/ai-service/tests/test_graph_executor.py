"""GraphExecutor with the runner, a real Postgres (rows + events) and fake backends."""

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from app.graph.builder import GRAPH_VERSION, build_graph
from app.graph.executor import GraphExecutor
from app.integrations.incident_service import IncidentNotFoundError
from app.runner import InvestigationRunner, SessionFactory
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.store import events as event_store
from app.store import investigations as store
from tests.graph_fakes import (
    FakeApi,
    FakeHistory,
    FakeLogs,
    FakeModel,
    FakeModels,
    incident_payload,
    triage_answer,
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT = UUID("55555555-5555-4555-8555-555555555555")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
INCIDENT = UUID("a0000030-0000-4000-8000-000000000030")


class HangingLogs(FakeLogs):
    """Hangs on the timeline query, so a test can stop the service in the middle of gather_logs."""

    def __init__(self) -> None:
        super().__init__()
        self.reached_timeline = asyncio.Event()

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None):
        if self.kind(sql) == "timeline":
            self.reached_timeline.set()
            await asyncio.Event().wait()
        return await super().query(sql, tenant_id=tenant_id, parameters=parameters)


class Backends:
    """One set of fakes, with the calls they received, plus an executor built on them."""

    def __init__(self, graph: Any, *, api: FakeApi | None = None, logs: FakeLogs | None = None) -> None:
        self.api = api or FakeApi(incident_payload(tenantId=str(TENANT), incidentId=str(INCIDENT)))
        self.logs = logs or FakeLogs()
        self.history = FakeHistory()
        self.models = FakeModels(FakeModel(triage_answer()))
        self.executor = GraphExecutor(
            graph, logs=self.logs, incidents=self.api, history=self.history, models=self.models
        )


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        async with factory() as session:  # events go with the row (ON DELETE CASCADE)
            await session.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": TENANT})
            await session.commit()
        await engine.dispose()


async def _create(factory: SessionFactory, incident_id: UUID = INCIDENT, **scope: bool):
    async with factory() as session:
        return await store.create(
            session,
            tenant_id=TENANT,
            incident_id=incident_id,
            requested_by=USER_ID,
            scope=InvestigationScope(**scope),
            question="Why?",
        )


async def _row(factory: SessionFactory, investigation_id: UUID):
    async with factory() as session:
        return await store.get_by_id(session, tenant_id=TENANT, investigation_id=investigation_id)


async def _events(factory: SessionFactory, investigation_id: UUID):
    async with factory() as session:
        return await event_store.list_after(session, tenant_id=TENANT, investigation_id=investigation_id)


async def _wait_for_end(runner: InvestigationRunner, investigation_id: UUID) -> None:
    """Wait until the runner's task is gone, i.e. after the final status AND the final event.

    Waiting for the status alone is racy: the runner writes the terminal event just after it.
    """
    for _ in range(200):
        if not runner.is_running(investigation_id):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the run did not finish in 10 seconds")


# ------------------------------------------------------------------------ happy path


async def test_a_run_completes_and_leaves_a_progress_trail(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()))
    runner = InvestigationRunner(session_factory, execute=backends.executor.run)

    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    await runner.shutdown()

    done = await _row(session_factory, row.id)
    assert done.graph_version == GRAPH_VERSION and done.tool_call_count == 3 + 3 + 1
    assert done.error is None and done.completed_at is not None

    events = await _events(session_factory, row.id)
    types = [e.type for e in events]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # gapless, in order
    assert types[:2] == ["investigation.started", "context.loaded"]
    assert types[-1] == "investigation.completed"
    assert types.count("triage.completed") == 1 and types.count("evidence.added") == 7
    assert types.index("context.loaded") < types.index("triage.completed") < types.index("investigation.completed")
    triage_event = next(e for e in events if e.type == "triage.completed")
    assert triage_event.payload["category"] == "dependency"
    assert triage_event.payload["gather"] == ["logs", "history"]


async def test_tenant_and_scope_come_from_the_investigation_row(session_factory: SessionFactory) -> None:
    row = await _create(session_factory, history=False, code=False, runbooks=False, docs=False)
    backends = Backends(build_graph(InMemorySaver()))  # the fake model asks for logs AND history

    await backends.executor.run(session_factory, row.id, TENANT)

    assert {tenant for _, tenant in backends.api.calls} == {TENANT}
    assert backends.logs.tenants_used() == {TENANT}
    assert backends.history.calls == []  # the row's scope switched history off


# ---------------------------------------------------------------------------- failure


async def test_an_unreadable_incident_fails_the_investigation_with_one_failed_event(
    session_factory: SessionFactory,
) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()), api=FakeApi(IncidentNotFoundError("nope")))
    runner = InvestigationRunner(session_factory, execute=backends.executor.run)

    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.FAILED
    await runner.shutdown()

    failed = await _row(session_factory, row.id)
    assert failed.error == "Incident not found" and failed.tool_call_count == 1
    assert failed.graph_version == GRAPH_VERSION
    types = [e.type for e in await _events(session_factory, row.id)]
    assert types == ["investigation.started", "investigation.failed"]  # no completed, no triage
    assert backends.models.purposes == []  # no tokens spent on an unreadable incident


async def test_a_bug_in_a_node_fails_the_run_through_the_runner(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()), logs=FakeLogs(errors={"search": KeyError("boom")}))
    runner = InvestigationRunner(session_factory, execute=backends.executor.run)

    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.FAILED
    await runner.shutdown()

    types = [e.type for e in await _events(session_factory, row.id)]
    assert types[-1] == "investigation.failed" and "investigation.completed" not in types


async def test_another_tenant_cannot_run_an_investigation(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()))
    other = UUID("66666666-6666-4666-8666-666666666666")
    with pytest.raises(LookupError):
        await backends.executor.run(session_factory, row.id, other)
    assert backends.api.calls == [] and backends.models.purposes == []


async def test_a_lost_event_does_not_fail_the_investigation(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()))
    runner = InvestigationRunner(session_factory, execute=backends.executor.run)

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("event table hiccup")

    monkeypatch.setattr(event_store, "append_event", broken)
    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    await runner.shutdown()
    assert await _events(session_factory, row.id) == []


# --------------------------------------------------------------------------- recovery


async def test_a_run_interrupted_by_shutdown_resumes_without_repeating_finished_steps(
    session_factory: SessionFactory,
) -> None:
    row = await _create(session_factory)
    saver = InMemorySaver()  # stands in for Postgres: it outlives the "restart"

    first = Backends(build_graph(saver), logs=HangingLogs())
    runner = InvestigationRunner(session_factory, execute=first.executor.run)
    runner.schedule(row.id, tenant_id=TENANT)
    await asyncio.wait_for(first.logs.reached_timeline.wait(), timeout=10)
    await runner.shutdown()  # the service stops while gather_logs is waiting for ClickHouse
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.RUNNING

    # "restart": new runner, new executor, new backends; the same checkpoint store
    second = Backends(build_graph(saver))
    recovered = InvestigationRunner(session_factory, execute=second.executor.run)
    recovered.schedule(row.id, tenant_id=TENANT)  # what recover_on_startup does for RUNNING rows
    await _wait_for_end(recovered, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    await recovered.shutdown()

    assert second.api.calls == [] and second.models.purposes == []  # load_context and triage were not redone
    done = await _row(session_factory, row.id)
    assert done.tool_call_count == 3 + 3 + 1
    types = [e.type for e in await _events(session_factory, row.id)]
    assert types.count("context.loaded") == 1 and types.count("triage.completed") == 1
    assert types.count("evidence.added") == 7  # nothing duplicated, nothing missing
    assert types[-1] == "investigation.completed"


async def test_a_graph_that_already_finished_is_not_run_again(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()))

    await backends.executor.run(session_factory, row.id, TENANT)
    calls_after_first = len(backends.api.calls)
    events_after_first = len(await _events(session_factory, row.id))
    await backends.executor.run(session_factory, row.id, TENANT)  # e.g. a crash before the row was updated

    assert len(backends.api.calls) == calls_after_first == 1
    assert len(await _events(session_factory, row.id)) == events_after_first
    assert len(backends.models.purposes) == 1  # triage ran once
