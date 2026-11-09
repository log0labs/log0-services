"""GraphExecutor with the runner, a real Postgres (rows + events) and fake backends."""

import asyncio
import os
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
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
from app.llm.purpose import Purpose
from app.runner import InvestigationRunner, SessionFactory
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.store import events as event_store
from app.store import investigations as store
from tests.code_graph_support import FakeCode, Repo, checkout_for
from tests.graph_fakes import (
    FakeApi,
    FakeHistory,
    FakeLogs,
    FakeModel,
    FakeModels,
    incident_payload,
    synthesis_answer,
    triage_answer,
)
from tests.test_gather_code import tool_model

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

    def __init__(
        self,
        graph: Any,
        *,
        api: FakeApi | None = None,
        logs: FakeLogs | None = None,
        models: FakeModels | None = None,
        code: Any = None,
    ) -> None:
        self.api = api or FakeApi(incident_payload(tenantId=str(TENANT)))
        self.logs = logs or FakeLogs()
        self.history = FakeHistory()
        self.models = models or FakeModels(FakeModel(triage_answer()))
        self.executor = GraphExecutor(
            graph, logs=self.logs, incidents=self.api, history=self.history, models=self.models, code=code
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
    assert done.repo_sha is None  # the code was not read, so no commit is pinned

    events = await _events(session_factory, row.id)
    types = [e.type for e in events]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # gapless, in order
    assert types[:2] == ["investigation.started", "context.loaded"]
    assert types[-1] == "investigation.completed"
    assert types.count("triage.completed") == 1 and types.count("evidence.added") == 7
    assert types.count("hypothesis.proposed") == 2  # synthesize joined the branches and proposed two
    assert types.index("hypothesis.proposed") > max(i for i, t in enumerate(types) if t == "evidence.added")
    assert types.index("context.loaded") < types.index("triage.completed") < types.index("investigation.completed")
    triage_event = next(e for e in events if e.type == "triage.completed")
    assert triage_event.payload["category"] == "dependency"
    assert triage_event.payload["gather"] == ["logs", "history"]


async def test_the_root_cause_and_recommendation_are_stored_on_the_row(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)
    backends = Backends(build_graph(InMemorySaver()))

    await backends.executor.run(session_factory, row.id, TENANT)

    done = await _row(session_factory, row.id)
    assert done.graph_version == GRAPH_VERSION
    assert done.root_cause is not None and done.root_cause["statement"].startswith("The payment gateway stopped")
    assert len(done.root_cause["evidenceIds"]) == 2 and done.root_cause["contributingFactors"]
    assert done.recommendation is not None and done.recommendation["verificationSteps"]
    assert done.confidence_band == "low"  # the default fake verify settles nothing: no supported hypothesis
    assert "modelConfidence" not in str(done.root_cause)  # the model's own number never reaches the row
    assert done.input_tokens is None and done.cost_usd is None  # the fakes report no usage: nothing to store

    events = await _events(session_factory, row.id)
    identified = [e for e in events if e.type == "root_cause.identified"]
    assert len(identified) == 1 and identified[0].payload["confidenceBand"] == "low"
    assert "modelConfidence" not in str(identified[0].payload)
    assert [e.type for e in events].index("root_cause.identified") > max(
        i for i, e in enumerate(events) if e.type == "hypothesis.proposed"
    )


async def test_the_commit_the_code_was_read_at_is_stored_and_code_evidence_is_in_the_trail(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    repo = Repo(tmp_path / "repo")
    models = FakeModels(FakeModel(triage_answer(gather=["logs", "history", "code"])), investigation=tool_model(repo))
    backends = Backends(build_graph(InMemorySaver()), models=models, code=FakeCode(checkout_for(repo)))
    row = await _create(session_factory)

    await backends.executor.run(session_factory, row.id, TENANT)

    done = await _row(session_factory, row.id)
    assert done.repo_sha == repo.c2  # the exact code the investigation read, for the report and for later steps
    assert done.graph_version == GRAPH_VERSION == "m3-code-v1"
    assert done.tool_call_count == 3 + 3 + 1 + (1 + 4 + 1 + 2)
    events = await _events(session_factory, row.id)
    kinds = [e.payload["kind"] for e in events if e.type == "evidence.added"]
    assert kinds.count("code") == 1 and kinds.count("commit") == 1
    assert any(e.type == "gap.recorded" and "left open" in e.payload["message"] for e in events)
    assert repo.c2 not in str([e.payload for e in events])  # events carry summaries, never the pinned sha or excerpts


async def test_tokens_and_cost_are_added_to_what_the_row_already_holds(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)

    async def record(**usage: Any) -> None:
        async with session_factory() as session:
            await store.record_run_result(
                session, investigation_id=row.id, tenant_id=TENANT, graph_version=GRAPH_VERSION, tool_call_count=3, **usage
            )

    await record(input_tokens=1000, output_tokens=200, cost_usd=Decimal("0.001500"))
    first = await _row(session_factory, row.id)
    assert (first.input_tokens, first.output_tokens, first.cost_usd) == (1000, 200, Decimal("0.001500"))

    await record(input_tokens=500, output_tokens=50, cost_usd=Decimal("0.000500"))  # the resumed part of the run
    total = await _row(session_factory, row.id)
    assert (total.input_tokens, total.output_tokens, total.cost_usd) == (1500, 250, Decimal("0.002000"))

    await record()  # a run that measured nothing must not erase what is there
    assert (await _row(session_factory, row.id)).input_tokens == 1500


async def test_a_run_without_hypotheses_leaves_the_result_columns_empty(session_factory: SessionFactory) -> None:
    row = await _create(session_factory)
    nothing = synthesis_answer(hypotheses=[])
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(nothing))
    backends = Backends(build_graph(InMemorySaver()), models=models)

    await backends.executor.run(session_factory, row.id, TENANT)

    done = await _row(session_factory, row.id)
    assert done.root_cause is None and done.recommendation is None and done.confidence_band is None
    types = [e.type for e in await _events(session_factory, row.id)]
    assert "root_cause.identified" not in types


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

    assert second.api.calls == []  # load_context was not redone
    # triage was not redone either, but synthesize (the node after the interruption) ran, once
    assert second.models.purposes == [Purpose.INVESTIGATION] * 3  # synthesize, verify, root_cause
    done = await _row(session_factory, row.id)
    assert done.tool_call_count == 3 + 3 + 1
    types = [e.type for e in await _events(session_factory, row.id)]
    assert types.count("context.loaded") == 1 and types.count("triage.completed") == 1
    assert types.count("evidence.added") == 7  # nothing duplicated, nothing missing
    assert types.count("hypothesis.proposed") == 2  # produced by the resumed run
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
    assert backends.models.purposes == [Purpose.TRIAGE] + [Purpose.INVESTIGATION] * 3  # nothing asked twice
