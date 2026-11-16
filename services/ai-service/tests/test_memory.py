"""Step 5.4: long-term memory (write, recall, feedback) on the real Postgres, plus the pure ranking rules."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from app.retrieval.embeddings import HashingEmbedder
from app.retrieval.memory import (
    MAX_DISTANCE,
    RECALL_LIMIT,
    NewMemory,
    PgMemory,
    memory_text,
    rank_memories,
)
from app.store.memory import MemoryRow
from tests.conftest import requires_postgres
from tests.jwt_helpers import TENANT_A, TENANT_B, USER_ID
from tests.memory_support import make_investigation, query_one, wipe_investigations

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 15, tzinfo=UTC)


def row(distance: float, verdict: str | None = None, *, age_days: int = 0, **over) -> MemoryRow:
    data = {
        "investigation_id": uuid4(),
        "incident_id": uuid4(),
        "service_name": "payment-service",
        "root_cause": "pool exhausted",
        "recommendation": None,
        "confidence_band": "medium",
        "feedback_verdict": verdict,
        "feedback_note": None,
        "created_at": NOW - timedelta(days=age_days),
        "distance": distance,
    }
    data.update(over)
    return MemoryRow(**data)


# ---------------------------------------------------------------------------- ranking


async def test_wrong_and_unrelated_memories_are_never_recalled():
    rows = [row(0.1, "wrong"), row(MAX_DISTANCE + 0.01), row(0.3)]
    ranked = rank_memories(rows)
    assert [r.distance for r in ranked] == [0.3]


async def test_helpful_ranks_above_a_slightly_closer_unrated_one_and_partly_below():
    unrated, helpful, partly = row(0.30), row(0.38, "helpful"), row(0.28, "partly")
    ranked = rank_memories([unrated, helpful, partly])
    assert [r.feedback_verdict for r in ranked] == ["helpful", None, "partly"]


async def test_a_much_closer_unrated_memory_still_beats_a_distant_helpful_one():
    ranked = rank_memories([row(0.6, "helpful"), row(0.1)])
    assert ranked[0].feedback_verdict is None


async def test_ties_go_to_the_newer_memory_and_the_list_is_capped():
    old, new = row(0.3, age_days=90), row(0.3, age_days=1)
    assert rank_memories([old, new])[0].investigation_id == new.investigation_id
    assert len(rank_memories([row(0.1 + i / 100) for i in range(10)])) == RECALL_LIMIT


async def test_memory_text_has_service_root_cause_and_fix():
    memory = NewMemory(TENANT_A, uuid4(), uuid4(), "payment-service", "pool exhausted", "raise the pool", "high")
    assert memory_text(memory) == "payment-service pool exhausted raise the pool"
    bare = NewMemory(TENANT_A, uuid4(), uuid4(), None, "pool exhausted", None, None)
    assert memory_text(bare) == "pool exhausted"


# ------------------------------------------------------------------- the real database


@pytest_asyncio.fixture
async def mem():
    wipe_investigations()
    engine = create_async_engine(get_settings().database_url_async)
    yield PgMemory(create_session_factory(engine), HashingEmbedder())
    wipe_investigations()
    await engine.dispose()


async def remember(mem: PgMemory, tenant=TENANT_A, *, root_cause="database connection pool exhausted", service="payment-service", **kw):
    inv, incident = make_investigation(tenant, root_cause=root_cause)
    await mem.remember(NewMemory(tenant, inv, incident, service, root_cause, kw.get("recommendation", "raise the pool size"), "medium"))
    return inv, incident


@requires_postgres
async def test_a_remembered_investigation_is_recalled_for_a_similar_incident(mem):
    inv, _ = await remember(mem)
    await remember(mem, root_cause="redis cache served stale keys after the deploy", service="shop-service")
    recalled = await mem.recall(tenant_id=TENANT_A, query="payment-service database connection pool timeout", exclude_incident_id=uuid4())
    assert recalled[0].investigation_id == inv
    assert recalled[0].root_cause == "database connection pool exhausted"
    assert recalled[0].recommendation == "raise the pool size"
    assert recalled[0].service_name == "payment-service"


@requires_postgres
async def test_the_incident_being_investigated_is_not_recalled(mem):
    _, incident = await remember(mem)
    recalled = await mem.recall(tenant_id=TENANT_A, query="database connection pool exhausted", exclude_incident_id=incident)
    assert recalled == []


@requires_postgres
async def test_memory_never_crosses_tenants(mem):
    await remember(mem, TENANT_A)
    assert await mem.recall(tenant_id=TENANT_B, query="database connection pool exhausted", exclude_incident_id=None) == []
    await remember(mem, TENANT_B, root_cause="database connection pool exhausted in the other company")
    mine = await mem.recall(tenant_id=TENANT_A, query="database connection pool exhausted", exclude_incident_id=None)
    assert all("other company" not in r.root_cause for r in mine)


@requires_postgres
async def test_a_wrong_verdict_removes_it_from_recall_and_helpful_keeps_a_note(mem):
    inv, _ = await remember(mem)
    assert await mem.record_feedback(tenant_id=TENANT_A, investigation_id=inv, verdict="helpful", note="that was it", user_id=USER_ID)
    recalled = await mem.recall(tenant_id=TENANT_A, query="database connection pool exhausted", exclude_incident_id=None)
    assert recalled[0].feedback_verdict == "helpful" and recalled[0].feedback_note == "that was it"
    assert await mem.record_feedback(tenant_id=TENANT_A, investigation_id=inv, verdict="wrong", note=None, user_id=USER_ID)
    assert await mem.recall(tenant_id=TENANT_A, query="database connection pool exhausted", exclude_incident_id=None) == []


@requires_postgres
async def test_feedback_for_a_missing_memory_or_another_tenant_is_refused(mem):
    inv, _ = await remember(mem)
    assert not await mem.record_feedback(tenant_id=TENANT_B, investigation_id=inv, verdict="wrong", note=None, user_id=USER_ID)
    assert not await mem.record_feedback(tenant_id=TENANT_A, investigation_id=uuid4(), verdict="wrong", note=None, user_id=USER_ID)
    row_ = query_one("SELECT feedback_verdict FROM ai.investigation_memory WHERE investigation_id = :i", i=inv)
    assert row_.feedback_verdict is None


@requires_postgres
async def test_an_unknown_verdict_is_refused_by_code_and_by_the_database(mem):
    inv, _ = await remember(mem)
    with pytest.raises(ValueError):
        await mem.record_feedback(tenant_id=TENANT_A, investigation_id=inv, verdict="great", note=None, user_id=USER_ID)
    with pytest.raises(Exception, match="ck_investigation_memory_verdict"):
        query_one("UPDATE ai.investigation_memory SET feedback_verdict = 'great' WHERE investigation_id = :i RETURNING 1", i=inv)


@requires_postgres
async def test_remembering_again_replaces_the_text_but_keeps_the_feedback(mem):
    inv, incident = await remember(mem)
    await mem.record_feedback(tenant_id=TENANT_A, investigation_id=inv, verdict="helpful", note=None, user_id=USER_ID)
    await mem.remember(NewMemory(TENANT_A, inv, incident, "payment-service", "a refined root cause", None, "high"))
    row_ = query_one("SELECT root_cause, feedback_verdict, confidence_band FROM ai.investigation_memory WHERE investigation_id = :i", i=inv)
    assert (row_.root_cause, row_.feedback_verdict, row_.confidence_band) == ("a refined root cause", "helpful", "high")
    assert query_one("SELECT count(*) AS n FROM ai.investigation_memory WHERE investigation_id = :i", i=inv).n == 1


@requires_postgres
async def test_another_tenant_cannot_overwrite_a_memory_by_reusing_the_investigation_id(mem):
    inv, incident = await remember(mem)
    await mem.remember(NewMemory(TENANT_B, inv, incident, "x", "hijacked", None, None))
    assert query_one("SELECT root_cause FROM ai.investigation_memory WHERE investigation_id = :i", i=inv).root_cause != "hijacked"


@requires_postgres
async def test_memories_made_with_another_embedding_model_are_not_compared(mem):
    await remember(mem)
    other = PgMemory(mem._session_factory, HashingEmbedder())
    other._embedder = type("E", (HashingEmbedder,), {"model_id": "another-model"})()
    assert await other.recall(tenant_id=TENANT_A, query="database connection pool exhausted", exclude_incident_id=None) == []


@requires_postgres
async def test_deleting_the_investigation_deletes_its_memory(mem):
    inv, _ = await remember(mem)
    wipe_investigations()
    assert query_one("SELECT count(*) AS n FROM ai.investigation_memory WHERE investigation_id = :i", i=inv).n == 0
