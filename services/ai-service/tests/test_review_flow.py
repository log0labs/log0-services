"""The review gate through the runner, the executor and a real Postgres: pause, decide, resume.

The in-memory checkpointer stands in for the Postgres one (it keeps state between runs of the same
thread, which is all these tests need; ``test_checkpoint_integration.py`` covers the real saver).
The helper ``decide`` does exactly what ``POST /resume`` does: flip the status, write the decision
event, resume the runner.
"""

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
from app.graph.builder import build_graph
from app.runner import InvestigationRunner, SessionFactory
from app.schemas.investigation import InvestigationStatus
from app.store import events as event_store
from app.store import investigations as store
from tests.graph_fakes import FakeModel, FakeModels, synthesis_answer, triage_answer
from tests.test_graph_executor import (
    TENANT,
    Backends,
    _create,
    _events,
    _row,
    _wait_for_end,
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]


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


def gated(saver: InMemorySaver | None = None) -> tuple[Backends, InMemorySaver]:
    saver = saver or InMemorySaver()
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(synthesis_answer()))
    return Backends(build_graph(saver), models=models, require_review=True), saver


async def decide(factory: SessionFactory, runner: InvestigationRunner, investigation_id: UUID, action: str, note: str | None = None) -> bool:
    """What the resume endpoint does. Returns whether this call won the AWAITING_REVIEW -> RUNNING change."""
    async with factory() as session:
        won = await store.transition_status_if(
            session,
            investigation_id=investigation_id,
            tenant_id=TENANT,
            from_status=InvestigationStatus.AWAITING_REVIEW,
            to_status=InvestigationStatus.RUNNING,
        )
        if not won:
            return False
        await event_store.append_event(session, investigation_id=investigation_id, type="review.decided", payload={"action": action, "note": note})
    await runner.wait_idle(investigation_id)
    runner.schedule(investigation_id, tenant_id=TENANT)
    return True


async def run_to_pause(factory: SessionFactory, backends: Backends) -> tuple[InvestigationRunner, Any]:
    row = await _create(factory)
    runner = InvestigationRunner(factory, execute=backends.executor.run)
    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    return runner, row


def types(events: list[Any]) -> list[str]:
    return [e.type for e in events]


# ------------------------------------------------------------------------------ pause


async def test_a_run_reaches_the_gate_and_waits_without_holding_anything(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)

    waiting = await _row(session_factory, row.id)
    assert waiting.status == InvestigationStatus.AWAITING_REVIEW and waiting.completed_at is None
    assert waiting.root_cause is not None and waiting.recommendation is not None  # visible to the reviewer already
    assert waiting.graph_version is not None and waiting.tool_call_count == 3 + 3 + 1
    assert not runner.is_running(row.id)  # no task, so no concurrency slot, while a human thinks

    events = await _events(session_factory, row.id)
    assert types(events).count("review.requested") == 1 and "investigation.completed" not in types(events)
    request = next(e for e in events if e.type == "review.requested").payload
    assert request["round"] == 1 and request["refinesLeft"] == 2 and request["confidenceBand"] == "low"
    assert request["rootCause"].startswith("The payment gateway stopped answering") and len(request["evidenceIds"]) == 2
    assert types(events).index("root_cause.identified") < types(events).index("review.requested")
    assert "pinnedCommit" not in request  # the event carries a summary, not internals
    await runner.shutdown()


async def test_a_waiting_investigation_still_blocks_a_second_one_for_the_same_incident(session_factory: SessionFactory) -> None:
    from app.store.exceptions import ActiveInvestigationError

    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)
    with pytest.raises(ActiveInvestigationError):
        await _create(session_factory, incident_id=row.incident_id)
    await runner.shutdown()


# --------------------------------------------------------------------------- decisions


async def test_approve_completes_the_investigation(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)

    assert await decide(session_factory, runner, row.id, "approve")
    await _wait_for_end(runner, row.id)

    done = await _row(session_factory, row.id)
    assert done.status == InvestigationStatus.COMPLETED and done.completed_at is not None
    events = await _events(session_factory, row.id)
    order = types(events)
    assert order.index("review.requested") < order.index("review.decided") < order.index("investigation.completed")
    assert order.count("review.requested") == 1 and order.count("investigation.completed") == 1
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert any(e.type == "gap.recorded" and "No fix was proposed" in e.payload["message"] for e in events)
    await runner.shutdown()


async def test_stop_completes_without_proposing_anything(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)
    await decide(session_factory, runner, row.id, "stop")
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    assert not any("No fix was proposed" in str(e.payload) for e in await _events(session_factory, row.id))
    await runner.shutdown()


async def test_refine_runs_another_round_and_waits_again_without_reusing_the_old_decision(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)

    await decide(session_factory, runner, row.id, "refine", "Look at the connection pool")
    await _wait_for_end(runner, row.id)

    # a second pause, not an automatic answer: the first decision belongs to the first request
    again = await _row(session_factory, row.id)
    assert again.status == InvestigationStatus.AWAITING_REVIEW
    events = await _events(session_factory, row.id)
    requests = [e.payload for e in events if e.type == "review.requested"]
    assert [r["round"] for r in requests] == [1, 2] and [r["refinesLeft"] for r in requests] == [2, 1]
    assert [e.payload["note"] for e in events if e.type == "review.decided"] == ["Look at the connection pool"]
    assert types(events).count("hypothesis.proposed") == 4  # synthesize ran again: two hypotheses per round

    await decide(session_factory, runner, row.id, "approve")
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    assert types(await _events(session_factory, row.id)).count("review.decided") == 2
    await runner.shutdown()


# ----------------------------------------------------------------- durability and races


async def test_a_new_process_resumes_the_run_from_the_checkpoint(session_factory: SessionFactory) -> None:
    first, saver = gated()
    runner, row = await run_to_pause(session_factory, first)
    await runner.shutdown()  # the service stops while the engineer is thinking

    second, _ = gated(saver)  # a new process: a new graph, executor and runner on the same checkpoints
    revived = InvestigationRunner(session_factory, execute=second.executor.run)
    await decide(session_factory, revived, row.id, "approve")
    await _wait_for_end(revived, row.id)

    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    assert second.logs.calls == []  # nothing was gathered again: the run continued, it did not restart
    await revived.shutdown()


async def test_a_resume_that_lost_its_decision_goes_back_to_waiting_instead_of_guessing(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)

    async with session_factory() as session:  # status flipped but the decision event never got written
        await store.transition_status_if(
            session, investigation_id=row.id, tenant_id=TENANT, from_status=InvestigationStatus.AWAITING_REVIEW, to_status=InvestigationStatus.RUNNING
        )
    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)

    assert (await _row(session_factory, row.id)).status == InvestigationStatus.AWAITING_REVIEW
    assert types(await _events(session_factory, row.id)).count("review.requested") == 1  # not announced twice

    await decide(session_factory, runner, row.id, "stop")  # and a real decision still works afterwards
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    await runner.shutdown()


async def test_only_one_of_several_simultaneous_decisions_wins(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)

    results = await asyncio.gather(*(decide(session_factory, runner, row.id, "approve") for _ in range(5)))
    await _wait_for_end(runner, row.id)

    assert results.count(True) == 1
    assert types(await _events(session_factory, row.id)).count("review.decided") == 1
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    await runner.shutdown()


async def test_cancelling_a_waiting_investigation_works_and_closes_the_gate(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)

    await runner.cancel(row.id, tenant_id=TENANT)

    assert (await _row(session_factory, row.id)).status == InvestigationStatus.CANCELLED
    assert not await decide(session_factory, runner, row.id, "approve")  # too late: nothing to resume
    assert "investigation.cancelled" in types(await _events(session_factory, row.id))
    await runner.shutdown()


async def test_startup_recovery_does_not_restart_a_waiting_investigation(session_factory: SessionFactory) -> None:
    backends, _ = gated()
    runner, row = await run_to_pause(session_factory, backends)
    async with session_factory() as session:
        pending = await store.list_pending_for_runner(session)
    assert row.id not in [p[0] for p in pending]  # it waits for a human, not for a worker
    await runner.shutdown()


async def test_with_the_gate_off_nothing_changes(session_factory: SessionFactory) -> None:
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(synthesis_answer()))
    backends = Backends(build_graph(InMemorySaver()), models=models)  # require_review defaults to False
    row = await _create(session_factory)
    runner = InvestigationRunner(session_factory, execute=backends.executor.run)
    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    assert "review.requested" not in types(await _events(session_factory, row.id))
    await runner.shutdown()


# ------------------------------------------------------------------------- wait_idle


async def test_wait_idle_waits_for_a_finishing_task_and_reports_a_stuck_one(session_factory: SessionFactory) -> None:
    release = asyncio.Event()

    async def slow(_factory: SessionFactory, _investigation_id: UUID, _tenant: UUID) -> None:
        await release.wait()

    row = await _create(session_factory)
    runner = InvestigationRunner(session_factory, execute=slow)
    assert await runner.wait_idle(row.id)  # nothing scheduled: idle
    runner.schedule(row.id, tenant_id=TENANT)
    await asyncio.sleep(0.1)
    assert runner.is_running(row.id) and not await runner.wait_idle(row.id, timeout=0.05)  # still busy after the timeout
    release.set()
    assert await runner.wait_idle(row.id, timeout=5)  # finished: idle again
    await runner.shutdown()
