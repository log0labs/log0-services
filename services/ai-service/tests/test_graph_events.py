"""events_from_update: the pure mapping from graph updates to investigation events."""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.graph.builder import build_graph
from app.graph.events import MAX_TEXT, events_from_update
from app.graph.state import new_state
from app.parsing.stacktrace import StackFrame
from app.schemas.domain import Evidence, Triage
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext
from tests.graph_fakes import FINGERPRINT, FIRST, INCIDENT_ID, LAST, make_context


def _incident() -> IncidentContext:
    return IncidentContext(
        incident_id=INCIDENT_ID,
        fingerprint=FINGERPRINT,
        service_name="payment-service",
        environment="production",
        severity="HIGH",
        status="NEW",
        occurrence_count=1500,
        first_seen_at=FIRST,
        last_seen_at=LAST,
        resolved_at=None,
        top_messages=["Connection timeout after 30000ms"],
        ai_summary=None,
    )


def _frame(function: str) -> StackFrame:
    return StackFrame(
        language="java", function=function, module="com.log0.App", file="App.java",
        line=1, path_hint=None, in_app=True,
    )


def _evidence(n: int, summary: str = "one line") -> Evidence:
    return Evidence(id=f"ev_{n:08d}", kind="log", source_ref=f"ref {n}", summary=summary, content="SECRET LOG LINE")


def test_load_context_becomes_context_loaded_then_its_evidence_then_its_gaps() -> None:
    update = {
        "load_context": {
            "incident": _incident(),
            "stack_frames": [_frame("charge"), _frame("run")],
            "evidence": [_evidence(1), _evidence(2)],
            "gaps": ["No stack trace was recorded for this incident"],
            "tool_calls": 3,
        }
    }
    assert events_from_update(update) == [
        (
            "context.loaded",
            {
                "service": "payment-service",
                "environment": "production",
                "severity": "HIGH",
                "occurrenceCount": 1500,
                "stackFrames": 2,
            },
        ),
        ("evidence.added", {"id": "ev_00000001", "kind": "log", "summary": "one line"}),
        ("evidence.added", {"id": "ev_00000002", "kind": "log", "summary": "one line"}),
        ("gap.recorded", {"message": "No stack trace was recorded for this incident"}),
    ]


def test_triage_becomes_triage_completed() -> None:
    triage = Triage(
        category="dependency",
        summary="The payment gateway is timing out.",
        gather=["logs", "history"],
        initial_hypotheses=["Gateway is overloaded"],
    )
    assert events_from_update({"triage": {"triage": triage}}) == [
        (
            "triage.completed",
            {
                "category": "dependency",
                "summary": "The payment gateway is timing out.",
                "gather": ["logs", "history"],
                "hypotheses": ["Gateway is overloaded"],
            },
        )
    ]


def test_a_gather_node_reports_evidence_and_gaps_but_no_node_event() -> None:
    update = {"gather_logs": {"evidence": [_evidence(3)], "gaps": ["g"], "tool_calls": 3}}
    assert [t for t, _ in events_from_update(update)] == ["evidence.added", "gap.recorded"]


def test_a_failed_load_context_reports_nothing() -> None:
    # status/error/tool_calls only: the executor reports the failure once, at the end
    update = {"load_context": {"status": "FAILED", "error": "Incident not found", "tool_calls": 1}}
    assert events_from_update(update) == []


def test_nodes_that_returned_nothing_or_markers_are_ignored() -> None:
    assert events_from_update({"gather_code": {}, "triage": None, "__interrupt__": ("x",)}) == []
    assert events_from_update({}) == []


def test_free_text_is_capped() -> None:
    long = "x" * 1000
    update = {"gather_logs": {"evidence": [_evidence(4, summary=long)], "gaps": [long]}}
    (_, evidence), (_, gap) = events_from_update(update)
    assert len(evidence["summary"]) == MAX_TEXT and evidence["summary"].endswith("…")
    assert len(gap["message"]) == MAX_TEXT


def test_raw_evidence_content_never_reaches_an_event() -> None:
    update = {"gather_logs": {"evidence": [_evidence(5)]}}
    assert "SECRET LOG LINE" not in json.dumps(events_from_update(update))


@pytest.mark.asyncio
async def test_the_real_graph_streams_progress_as_each_node_finishes() -> None:
    graph = build_graph(InMemorySaver())
    state = new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope())
    config = {"configurable": {"thread_id": "inv"}}

    types: list[str] = []
    async for update in graph.astream(state, config, context=make_context(), stream_mode="updates"):
        types += [t for t, _ in events_from_update(update)]

    assert types[0] == "context.loaded"
    # triage comes after everything load_context found, and before the gather branches' evidence
    assert types.index("triage.completed") == 1 + 3  # context.loaded + incident, logstats, stacktrace
    assert types.count("evidence.added") == 7 and "gap.recorded" not in types
    assert set(types) == {"context.loaded", "triage.completed", "evidence.added"}
