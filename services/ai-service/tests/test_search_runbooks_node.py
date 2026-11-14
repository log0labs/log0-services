"""Step 5.3: the search_runbooks graph node, with a fake runbook source (no database, no model)."""

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import OperationalError

from app.graph.nodes.search_runbooks import (
    build_query,
    passage_evidence,
    search_runbooks,
)
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.runbooks import RunbookPassage, RunbookSearchResult
from app.schemas.domain import Triage
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext
from tests.graph_fakes import (
    FINGERPRINT,
    FIRST,
    INCIDENT_ID,
    LAST,
    TENANT,
    FakeRunbooks,
    make_context,
)

pytestmark = pytest.mark.asyncio


def passage(**over: Any) -> RunbookPassage:
    data: dict[str, Any] = {
        "chunk_id": uuid4(),
        "runbook_id": uuid4(),
        "title": "Database playbook",
        "heading_path": "Database > Connection pool",
        "ordinal": 2,
        "content": "When the pool is exhausted, raise the maximum pool size.",
        "distance": 0.3,
    }
    data.update(over)
    return RunbookPassage(**data)


def state(*, question: str | None = None, with_triage: bool = True) -> InvestigationState:
    s = new_state(investigation_id="i1", incident_id=str(INCIDENT_ID), scope=InvestigationScope(), question=question)
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
        top_messages=["Connection timeout after 30000ms", "pool exhausted", "third message"],
        ai_summary=None,
    )
    if with_triage:
        s["triage"] = Triage(category="resource", summary="The connection pool seems exhausted.", gather=["runbooks"], initial_hypotheses=[])
    return s


async def run(st: InvestigationState, ctx: InvestigationContext) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("n", search_runbooks)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return await builder.compile().ainvoke(st, context=ctx)


def ctx_with(runbooks: FakeRunbooks | None) -> InvestigationContext:
    return replace(make_context(), runbooks=runbooks)


async def test_each_relevant_passage_becomes_citable_runbook_evidence():
    fake = FakeRunbooks(RunbookSearchResult(outcome="found", passages=[passage(), passage(ordinal=3, heading_path="")]))
    out = await run(state(), ctx_with(fake))
    assert [e.kind for e in out["evidence"]] == ["runbook", "runbook"]
    first = out["evidence"][0]
    assert first.summary.startswith('Runbook "Database playbook" > Database > Connection pool: When the pool')
    assert first.source_ref == 'runbook "Database playbook" #2'
    assert "raise the maximum pool size" in first.content
    assert out["tool_calls"] == 1
    assert out["gaps"] == []


async def test_evidence_ids_are_stable_for_the_same_passage():
    p = passage()
    assert passage_evidence(p).id == passage_evidence(p).id
    assert passage_evidence(p).id != passage_evidence(replace(p, ordinal=9)).id


async def test_the_search_gets_the_tenant_from_the_context_and_a_query_built_from_the_facts():
    fake = FakeRunbooks(RunbookSearchResult(outcome="found", passages=[passage()]))
    await run(state(question="why are payments slow?"), ctx_with(fake))
    (call,) = fake.calls
    assert call["tenant_id"] == TENANT
    assert call["query"].startswith("why are payments slow? The connection pool seems exhausted. payment-service")
    assert "Connection timeout after 30000ms pool exhausted" in call["query"]
    assert "third message" not in call["query"]  # only the top two messages


async def test_build_query_without_a_question_or_triage_and_its_length_cap():
    assert build_query(state(with_triage=False)).startswith("payment-service Connection timeout")
    long = state(question="x" * 2000)
    assert len(build_query(long)) == 500


@pytest.mark.parametrize(
    ("result", "gap"),
    [
        (RunbookSearchResult(outcome="no_runbooks"), "No runbooks have been uploaded"),
        (RunbookSearchResult(outcome="none_relevant"), "No runbook matched this incident"),
    ],
)
async def test_an_empty_outcome_is_a_gap_not_evidence(result, gap):
    out = await run(state(), ctx_with(FakeRunbooks(result)))
    assert out["evidence"] == []
    assert any(gap in g for g in out["gaps"])


async def test_an_ungraded_result_still_gives_evidence_but_says_it_was_not_checked():
    fake = FakeRunbooks(RunbookSearchResult(outcome="found", passages=[passage()], graded=False))
    out = await run(state(), ctx_with(fake))
    assert len(out["evidence"]) == 1
    assert any("similarity alone" in g for g in out["gaps"])


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (EmbeddingError("down"), "embedding service"),
        (OperationalError("select", {}, Exception("db down")), "database"),
        (OSError("refused"), "database"),
    ],
)
async def test_an_infrastructure_failure_is_a_gap_that_never_fails_the_run(error, fragment):
    out = await run(state(), ctx_with(FakeRunbooks(error)))
    assert out["evidence"] == []
    assert any(fragment in g for g in out["gaps"])
    assert out["status"].value == "RUNNING"


async def test_a_bug_is_not_swallowed():
    with pytest.raises(ValueError):
        await run(state(), ctx_with(FakeRunbooks(ValueError("bug"))))


async def test_without_a_configured_search_the_gap_says_so():
    out = await run(state(), ctx_with(None))
    assert any("not configured" in g for g in out["gaps"])
    assert out["evidence"] == []


async def test_without_an_incident_the_node_does_nothing():
    st = state()
    st["incident"] = None
    fake = FakeRunbooks(RunbookSearchResult(outcome="found", passages=[passage()]))
    out = await run(st, ctx_with(fake))
    assert out["gaps"] == [] and fake.calls == []
