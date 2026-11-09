"""The graph with the REAL Postgres checkpointer (needs the log0-postgres container)."""

import logging
import os
from collections.abc import AsyncGenerator
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import get_settings
from app.graph.builder import build_graph
from app.graph.checkpoint import (
    CHECKPOINT_SCHEMA,
    checkpoint_conninfo,
    open_checkpointer,
)
from app.graph.state import InvestigationState, new_state
from app.parsing.stacktrace import StackFrame
from app.schemas.domain import Evidence, Hypothesis, Recommendation, RootCause, Triage
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.tools.incidents import IncidentContext
from tests.graph_fakes import INCIDENT_ID, make_context

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def saver() -> AsyncGenerator[AsyncPostgresSaver]:
    async with open_checkpointer(get_settings()) as s:
        yield s


@pytest_asyncio.fixture
async def thread_id(saver: AsyncPostgresSaver) -> AsyncGenerator[str]:
    thread = str(uuid4())  # an investigation id
    yield thread
    await saver.adelete_thread(thread)


def _start() -> InvestigationState:
    return new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope())


async def test_state_survives_a_restart_and_comes_back_as_real_objects(
    saver: AsyncPostgresSaver, thread_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    with caplog.at_level(logging.WARNING):
        final = await build_graph(saver).ainvoke(_start(), config, context=make_context())

        # "restart": a brand-new connection and a brand-new graph object, same thread id
        async with open_checkpointer(get_settings()) as fresh_saver:
            snapshot = await build_graph(fresh_saver).aget_state(config)

    values = snapshot.values
    # our Pydantic models are rebuilt as models, not left as dicts: the 2.6 serializer works
    assert isinstance(values["triage"], Triage) and values["triage"] == final["triage"]
    assert isinstance(values["incident"], IncidentContext)
    assert all(isinstance(e, Evidence) for e in values["evidence"]) and len(values["evidence"]) == 7
    assert all(isinstance(f, StackFrame) for f in values["stack_frames"])
    assert isinstance(values["scope"], InvestigationScope)
    assert len(values["hypotheses"]) == 2 and all(isinstance(h, Hypothesis) for h in values["hypotheses"])
    assert values["hypotheses"] == final["hypotheses"]
    assert isinstance(values["root_cause"], RootCause) and values["root_cause"] == final["root_cause"]
    assert isinstance(values["recommendation"], Recommendation) and values["confidence_band"] == final["confidence_band"]
    assert values["status"] == InvestigationStatus.RUNNING
    assert values["tool_calls"] == final["tool_calls"] == 7
    assert snapshot.next == ()
    # LangGraph warns when it meets a type the serializer was not told about
    assert not [r for r in caplog.records if r.name.startswith("langgraph") and r.levelno >= logging.WARNING]


async def test_one_checkpoint_per_step_is_stored(saver: AsyncPostgresSaver, thread_id: str) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    graph = build_graph(saver)
    await graph.ainvoke(_start(), config, context=make_context())
    history = [s async for s in graph.aget_state_history(config)]
    assert len(history) >= 4  # input, load_context, triage, gather branches
    assert history[0].next == ()  # newest first: the finished state


async def test_checkpoint_tables_live_in_the_ai_schema(saver: AsyncPostgresSaver) -> None:
    async with await psycopg.AsyncConnection.connect(checkpoint_conninfo(get_settings())) as conn:
        cur = await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (CHECKPOINT_SCHEMA,)
        )
        tables = {row[0] for row in await cur.fetchall()}
    assert {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"} <= tables


async def test_two_investigations_do_not_see_each_others_state(
    saver: AsyncPostgresSaver, thread_id: str
) -> None:
    other = str(uuid4())
    graph = build_graph(saver)
    try:
        await graph.ainvoke(_start(), {"configurable": {"thread_id": thread_id}}, context=make_context())
        untouched = await graph.aget_state({"configurable": {"thread_id": other}})
        assert untouched.values == {} and untouched.next == ()  # nothing stored under another id
    finally:
        await saver.adelete_thread(other)


async def test_conninfo_quotes_awkward_passwords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://log0:p%40ss%20w%27rd@db.example:5433/log0")
    get_settings.cache_clear()
    info = psycopg.conninfo.conninfo_to_dict(checkpoint_conninfo(get_settings()))
    assert info["password"] == "p@ss w'rd" and info["host"] == "db.example" and info["port"] == "5433"
    assert info["dbname"] == "log0" and info["options"] == f"-c search_path={CHECKPOINT_SCHEMA}"
