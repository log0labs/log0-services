"""Step 5.4: memory in the graph (recall in gather_history), in the executor (write at the end) and in the API (feedback)."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import OperationalError

from app.graph.executor import GraphExecutor
from app.graph.nodes.gather_history import gather_history, memory_evidence
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.main import create_app
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.memory import NewMemory, RecalledInvestigation
from app.schemas.domain import Recommendation, RootCause
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext
from tests.conftest import requires_postgres
from tests.graph_fakes import (
    FINGERPRINT,
    FIRST,
    INCIDENT_ID,
    LAST,
    TENANT,
    FakeHistory,
    make_context,
)
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token
from tests.memory_support import make_investigation, query_one, wipe_investigations


def recalled(**over: Any) -> RecalledInvestigation:
    data: dict[str, Any] = {
        "investigation_id": uuid4(),
        "incident_id": uuid4(),
        "service_name": "payment-service",
        "root_cause": "The connection pool was too small for the batch job",
        "recommendation": "Raise the pool size",
        "confidence_band": "high",
        "feedback_verdict": "helpful",
        "feedback_note": "fixed it",
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        "distance": 0.3,
    }
    data.update(over)
    return RecalledInvestigation(**data)


class FakeMemory:
    def __init__(self, result: list[RecalledInvestigation] | Exception | None = None) -> None:
        self.result = result if result is not None else []
        self.recalls: list[dict[str, Any]] = []
        self.remembered: list[NewMemory] = []
        self.fail_remember: Exception | None = None

    async def recall(self, **kwargs: Any) -> list[RecalledInvestigation]:
        self.recalls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def remember(self, memory: NewMemory) -> None:
        if self.fail_remember:
            raise self.fail_remember
        self.remembered.append(memory)

    async def record_feedback(self, **kwargs: Any) -> bool:
        return True


def state() -> InvestigationState:
    s = new_state(investigation_id="i1", incident_id=str(INCIDENT_ID), scope=InvestigationScope())
    s["incident"] = IncidentContext(
        incident_id=INCIDENT_ID,
        fingerprint=FINGERPRINT,
        service_name="payment-service",
        environment="production",
        severity="HIGH",
        status="NEW",
        occurrence_count=10,
        first_seen_at=FIRST,
        last_seen_at=LAST,
        resolved_at=None,
        top_messages=["Connection timeout after 30000ms"],
        ai_summary=None,
    )
    return s


async def run(ctx: InvestigationContext) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("n", gather_history)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return await builder.compile().ainvoke(state(), context=ctx)


# ------------------------------------------------------------------ recall in the graph


@pytest.mark.asyncio
async def test_recalled_investigations_become_memory_evidence_next_to_related_incidents():
    memory = FakeMemory([recalled(), recalled(feedback_verdict=None, feedback_note=None)])
    out = await run(replace(make_context(), memory=memory))
    kinds = [e.kind for e in out["evidence"]]
    assert kinds.count("memory") == 2 and "incident" in kinds
    assert out["tool_calls"] == 2  # incident history + memory
    (call,) = memory.recalls
    assert call["tenant_id"] == TENANT and call["exclude_incident_id"] == INCIDENT_ID
    assert "payment-service" in call["query"]


@pytest.mark.asyncio
async def test_memory_evidence_says_it_is_a_lead_and_carries_the_feedback():
    ev = memory_evidence(recalled())
    assert ev.kind == "memory" and ev.source_ref.startswith("past investigation ")
    assert "helpful" in ev.summary and "high confidence" in ev.summary and "2026-10-01" in ev.summary
    assert "not proof" in ev.content and "Raise the pool size" in ev.content and "fixed it" in ev.content
    bare = memory_evidence(recalled(service_name=None, recommendation=None, feedback_verdict=None, feedback_note=None, confidence_band=None))
    assert "Recommendation then" not in bare.content and "engineers said" not in bare.summary


@pytest.mark.asyncio
async def test_without_memory_nothing_changes():
    out = await run(make_context())
    assert out["tool_calls"] == 1 and all(e.kind != "memory" for e in out["evidence"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [EmbeddingError("down"), OperationalError("s", {}, Exception("db")), OSError("refused")])
async def test_a_memory_failure_is_a_gap_and_the_incident_history_is_kept(error):
    out = await run(replace(make_context(), memory=FakeMemory(error)))
    assert any("Past investigations could not be recalled" in g for g in out["gaps"])
    assert any(e.kind == "incident" for e in out["evidence"])


@pytest.mark.asyncio
async def test_a_history_failure_does_not_stop_memory_recall():
    ctx = replace(make_context(history=FakeHistory(error=OSError("db down"))), memory=FakeMemory([recalled()]))
    out = await run(ctx)
    assert any("Earlier incidents could not be read" in g for g in out["gaps"])
    assert [e.kind for e in out["evidence"]] == ["memory"]


@pytest.mark.asyncio
async def test_a_bug_in_memory_is_not_swallowed():
    with pytest.raises(ValueError):
        await run(replace(make_context(), memory=FakeMemory(ValueError("bug"))))


# ------------------------------------------------------------- write in the executor


def executor(memory: Any) -> GraphExecutor:
    return GraphExecutor(None, logs=None, incidents=None, history=None, models=None, memory=memory)  # type: ignore[arg-type]


def final_state(**over: Any) -> dict[str, Any]:
    incident = SimpleNamespace(incident_id=INCIDENT_ID, service_name="payment-service")
    data: dict[str, Any] = {
        "incident": incident,
        "root_cause": RootCause(statement="Pool exhausted", evidence_ids=["ev_1"]),
        "recommendation": Recommendation(
            summary="Raise the pool", rationale="r", changes=[], risks=[], tests_required=[], verification_steps=[]
        ),
        "confidence_band": "medium",
    }
    data.update(over)
    return data


@pytest.mark.asyncio
async def test_a_finished_investigation_is_remembered():
    memory = FakeMemory()
    inv = uuid4()
    await executor(memory)._remember(final_state(), inv, TENANT)
    (saved,) = memory.remembered
    assert (saved.tenant_id, saved.investigation_id, saved.incident_id) == (TENANT, inv, INCIDENT_ID)
    assert (saved.service_name, saved.root_cause, saved.recommendation, saved.confidence_band) == (
        "payment-service", "Pool exhausted", "Raise the pool", "medium"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [{"root_cause": None}, {"incident": None}])
async def test_nothing_is_remembered_without_a_root_cause_or_an_incident(missing):
    memory = FakeMemory()
    await executor(memory)._remember(final_state(**missing), uuid4(), TENANT)
    assert memory.remembered == []


@pytest.mark.asyncio
async def test_a_run_without_a_recommendation_is_remembered_with_the_root_cause_only():
    memory = FakeMemory()
    await executor(memory)._remember(final_state(recommendation=None), uuid4(), TENANT)
    assert memory.remembered[0].recommendation is None


@pytest.mark.asyncio
async def test_a_failing_memory_write_never_fails_the_investigation():
    memory = FakeMemory()
    memory.fail_remember = EmbeddingError("down")
    await executor(memory)._remember(final_state(), uuid4(), TENANT)  # must not raise


@pytest.mark.asyncio
async def test_without_memory_the_executor_does_nothing():
    await executor(None)._remember(final_state(), uuid4(), TENANT)


# ------------------------------------------------------------------- feedback API


@pytest.fixture
def client():
    wipe_investigations()
    with TestClient(create_app()) as c:
        yield c
    wipe_investigations()


def auth(tenant=TENANT_A, role="ENGINEER") -> dict[str, str]:
    from app.config import get_settings

    return {"Authorization": f"Bearer {mint_access_token(get_settings().jwt_secret.get_secret_value(), tenant_id=tenant, role=role)}"}


def post(client: TestClient, inv, verdict="helpful", note=None, **kw):
    body = {"verdict": verdict, **({"note": note} if note is not None else {})}
    return client.post(f"/api/v1/investigations/{inv}/feedback", json=body, headers=auth(**kw))


@requires_postgres
def test_feedback_creates_the_memory_when_there_is_none_and_stores_the_verdict(client):
    inv, _ = make_investigation()
    assert post(client, inv, "partly", "  close but not exact  ").status_code == 204
    row = query_one("SELECT root_cause, recommendation, feedback_verdict, feedback_note FROM ai.investigation_memory WHERE investigation_id = :i", i=inv)
    assert (row.root_cause, row.recommendation) == ("The connection pool was exhausted", "Raise the pool size")
    assert (row.feedback_verdict, row.feedback_note) == ("partly", "close but not exact")


@requires_postgres
def test_feedback_can_be_changed(client):
    inv, _ = make_investigation()
    post(client, inv, "helpful")
    post(client, inv, "wrong", "turned out to be DNS")
    row = query_one("SELECT feedback_verdict, feedback_note FROM ai.investigation_memory WHERE investigation_id = :i", i=inv)
    assert (row.feedback_verdict, row.feedback_note) == ("wrong", "turned out to be DNS")


@requires_postgres
def test_only_admins_and_engineers_can_give_feedback(client):
    inv, _ = make_investigation()
    assert post(client, inv, role="VIEWER").status_code == 403
    assert post(client, inv, role="ADMIN").status_code == 204
    assert client.post(f"/api/v1/investigations/{inv}/feedback", json={"verdict": "helpful"}).status_code == 401


@requires_postgres
def test_feedback_for_another_tenants_or_unknown_investigation_is_404(client):
    inv, _ = make_investigation()
    assert post(client, inv, tenant=TENANT_B).status_code == 404
    assert post(client, uuid4()).status_code == 404
    assert query_one("SELECT count(*) AS n FROM ai.investigation_memory WHERE tenant_id IN (:a, :b)", a=TENANT_A, b=TENANT_B).n == 0


@requires_postgres
@pytest.mark.parametrize("kwargs", [{"status": "RUNNING"}, {"status": "FAILED"}, {"root_cause": None}])
def test_only_a_completed_investigation_with_a_root_cause_can_be_rated(client, kwargs):
    inv, _ = make_investigation(**kwargs)
    assert post(client, inv).status_code == 409


@requires_postgres
@pytest.mark.parametrize("body", [{"verdict": "great"}, {}, {"verdict": "helpful", "note": "x" * 1001}])
def test_a_bad_feedback_body_is_refused(client, body):
    inv, _ = make_investigation()
    assert client.post(f"/api/v1/investigations/{inv}/feedback", json=body, headers=auth()).status_code == 422
