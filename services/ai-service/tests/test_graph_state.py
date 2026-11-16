"""Tests for the graph state, its reducers, and the context (no database, no LLM)."""

import operator
from types import SimpleNamespace
from typing import Annotated, TypedDict
from uuid import UUID

import pytest
from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.graph.state import (
    InvestigationContext,
    InvestigationState,
    add_evidence,
    append_unique,
    merge_by_id,
    new_state,
)
from app.schemas.domain import Evidence, Hypothesis, make_evidence_id
from app.schemas.investigation import InvestigationScope, InvestigationStatus

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def _ev(kind: str = "log", ref: str = "payment:ERROR", summary: str = "s") -> Evidence:
    return Evidence(id=make_evidence_id(kind, ref), kind=kind, source_ref=ref, summary=summary)  # type: ignore[arg-type]


def _hyp(id_: str, status: str = "open", supporting: list[str] | None = None) -> Hypothesis:
    return Hypothesis(id=id_, statement=f"statement {id_}", status=status, supporting_evidence=supporting or [])  # type: ignore[arg-type]


# --------------------------------------------------------------------- evidence ids


def test_evidence_id_is_stable_and_depends_on_what_the_evidence_is() -> None:
    assert make_evidence_id("log", "a") == make_evidence_id("log", "a")
    assert make_evidence_id("log", "a") != make_evidence_id("log", "b")
    assert make_evidence_id("log", "a") != make_evidence_id("code", "a")  # kind is part of the identity
    assert make_evidence_id("log", "a").startswith("ev_") and len(make_evidence_id("log", "a")) == 11


def test_evidence_content_is_bounded() -> None:
    with pytest.raises(ValueError, match="at most 4000"):
        Evidence(id="ev_1", kind="log", source_ref="r", summary="s", content="x" * 4001)


# ------------------------------------------------------------------------ reducers


def test_add_evidence_appends_in_order_and_ignores_none() -> None:
    a, b = _ev(ref="a"), _ev(ref="b")
    assert add_evidence(None, None) == []
    assert add_evidence([a], None) == [a]
    assert add_evidence(None, [a, b]) == [a, b]
    assert add_evidence([a], [b]) == [a, b]


def test_add_evidence_dedupes_by_id_and_keeps_the_first_version() -> None:
    first = _ev(ref="a", summary="first")
    again = _ev(ref="a", summary="second")  # same kind + source_ref -> same id
    assert first.id == again.id
    assert add_evidence([first], [again]) == [first]
    assert add_evidence([], [first, again]) == [first]  # also within one batch


def test_add_evidence_does_not_mutate_its_inputs() -> None:
    left = [_ev(ref="a")]
    add_evidence(left, [_ev(ref="b")])
    assert len(left) == 1


def test_merge_by_id_updates_in_place_and_appends_new() -> None:
    h1, h2 = _hyp("h1"), _hyp("h2")
    h1_updated = _hyp("h1", status="supported", supporting=["ev_1"])
    merged = merge_by_id([h1, h2], [h1_updated, _hyp("h3")])
    assert [h.id for h in merged] == ["h1", "h2", "h3"]  # h1 keeps its position
    assert merged[0].status == "supported" and merged[0].supporting_evidence == ["ev_1"]


def test_merge_by_id_handles_none() -> None:
    assert merge_by_id(None, None) == []
    assert [h.id for h in merge_by_id(None, [_hyp("h1")])] == ["h1"]


def test_append_unique_keeps_order_and_skips_duplicates() -> None:
    assert append_unique(None, ["a"]) == ["a"]
    assert append_unique(["a", "b"], ["b", "c", "a", "c"]) == ["a", "b", "c"]


# ------------------------------------------------------------------------ new_state


def test_new_state_has_every_key_with_safe_defaults() -> None:
    state = new_state(investigation_id="i1", incident_id="inc1", scope=InvestigationScope(), question="Why?")
    assert set(state) == set(InvestigationState.__annotations__)  # nothing missing, nothing extra
    assert state["question"] == "Why?" and state["status"] == InvestigationStatus.RUNNING
    assert (state["evidence"], state["hypotheses"], state["gaps"], state["stack_frames"]) == ([], [], [], [])
    assert (state["tool_calls"], state["verify_round"], state["change_attempts"]) == (0, 0, 0)
    assert state["incident"] is None and state["root_cause"] is None and state["diff"] is None


def test_tenant_id_is_never_part_of_the_state() -> None:
    assert "tenant_id" not in InvestigationState.__annotations__
    assert "tenant_id" in InvestigationContext.__dataclass_fields__
    context = InvestigationContext(tenant_id=TENANT, logs=object(), incidents=object(), history=object())  # type: ignore[arg-type]
    with pytest.raises(AttributeError):  # frozen: a node cannot reassign the tenant
        context.tenant_id = UUID(int=2)  # type: ignore[misc]


# --------------------------------------------------- a real parallel LangGraph run


def _context() -> InvestigationContext:
    fake = SimpleNamespace()
    return InvestigationContext(tenant_id=TENANT, logs=fake, incidents=fake, history=fake)  # type: ignore[arg-type]


def _build_parallel_graph() -> object:
    async def gather_logs(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict:
        assert runtime.context.tenant_id == TENANT  # the tenant arrives through context
        return {
            "evidence": [_ev("log", "timeline"), _ev("log", "shared")],
            "tool_calls": 2,
            "gaps": ["no stack trace in logs"],
            "hypotheses": [_hyp("h1")],
        }

    async def gather_history(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict:
        return {
            "evidence": [_ev("incident", "recurrence"), _ev("log", "shared", summary="duplicate")],
            "tool_calls": 3,
            "gaps": ["no stack trace in logs", "no runbook found"],
            "hypotheses": [_hyp("h1", status="supported"), _hyp("h2")],
        }

    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("gather_logs", gather_logs)
    builder.add_node("gather_history", gather_history)
    builder.add_edge(START, "gather_logs")  # two edges out of START = both run in the same step
    builder.add_edge(START, "gather_history")
    builder.add_edge("gather_logs", END)
    builder.add_edge("gather_history", END)
    return builder.compile()


@pytest.mark.asyncio
async def test_parallel_nodes_merge_through_the_reducers() -> None:
    graph = _build_parallel_graph()
    start = new_state(investigation_id="i1", incident_id="inc1", scope=InvestigationScope())
    final = await graph.ainvoke(start, context=_context())  # type: ignore[attr-defined]

    refs = sorted(e.source_ref for e in final["evidence"])
    assert refs == ["recurrence", "shared", "timeline"]  # 4 pieces written, the duplicate dropped
    assert final["tool_calls"] == 5  # operator.add: 2 + 3
    assert sorted(final["gaps"]) == ["no runbook found", "no stack trace in logs"]  # deduped
    by_id = {h.id: h for h in final["hypotheses"]}
    assert sorted(by_id) == ["h1", "h2"] and by_id["h1"].status in ("open", "supported")
    assert final["status"] == InvestigationStatus.RUNNING  # untouched keys keep their value


@pytest.mark.asyncio
async def test_without_a_reducer_parallel_writes_to_one_key_are_rejected() -> None:
    """The reason reducers exist: two parallel nodes cannot both set a plain key."""

    class Plain(TypedDict):
        items: list[str]

    async def a(state: Plain) -> dict:
        return {"items": ["from a"]}

    async def b(state: Plain) -> dict:
        return {"items": ["from b"]}

    builder = StateGraph(Plain)
    builder.add_node("a", a)
    builder.add_node("b", b)
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    builder.add_edge("a", END)
    builder.add_edge("b", END)
    with pytest.raises(InvalidUpdateError):
        await builder.compile().ainvoke({"items": []})


@pytest.mark.asyncio
async def test_a_counter_with_operator_add_sums_parallel_writes() -> None:
    class Counted(TypedDict):
        calls: Annotated[int, operator.add]

    async def a(state: Counted) -> dict:
        return {"calls": 2}

    async def b(state: Counted) -> dict:
        return {"calls": 3}

    builder = StateGraph(Counted)
    builder.add_node("a", a)
    builder.add_node("b", b)
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    builder.add_edge("a", END)
    builder.add_edge("b", END)
    assert (await builder.compile().ainvoke({"calls": 0}))["calls"] == 5


# ------------------------------------------------------------------- checkpoints


async def _run_with_checkpointer(saver) -> dict:
    from datetime import UTC, datetime

    from app.parsing.stacktrace import StackFrame
    from app.schemas.domain import CodeLocation, RootCause, Triage
    from app.tools.incidents import IncidentContext

    async def node(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict:
        return {
            "evidence": [_ev()],
            "hypotheses": [_hyp("h1", status="supported")],
            "stack_frames": [
                StackFrame(language="java", function="f", module="m", file="F.java", line=1, path_hint=None, in_app=True)
            ],
            "triage": Triage(category="code_bug", summary="s", gather=["logs"]),
            "root_cause": RootCause(
                statement="s", evidence_ids=["ev"], affected_locations=[CodeLocation(file_path="a.py", line=3)]
            ),
            "incident": IncidentContext(
                incident_id=UUID(int=1), fingerprint="fp", service_name="s", environment="e", severity="HIGH",
                status="NEW", occurrence_count=1, first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
                resolved_at=None, top_messages=[], ai_summary=None,
            ),
        }

    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "t1"}}
    await graph.ainvoke(
        new_state(investigation_id="i", incident_id="c", scope=InvestigationScope()), config, context=_context()
    )
    return (await graph.aget_state(config)).values


@pytest.mark.asyncio
async def test_state_survives_a_checkpoint_round_trip_without_warnings(caplog: pytest.LogCaptureFixture) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from app.graph.state import checkpoint_serde

    caplog.set_level("WARNING")
    values = await _run_with_checkpointer(InMemorySaver(serde=checkpoint_serde()))
    assert [type(e).__name__ for e in values["evidence"]] == ["Evidence"]  # real models again, not dicts
    assert type(values["root_cause"]).__name__ == "RootCause" and values["root_cause"].affected_locations[0].line == 3
    assert values["incident"].fingerprint == "fp" and values["stack_frames"][0].function == "f"
    assert values["status"] == InvestigationStatus.RUNNING and "tenant_id" not in values
    assert "unregistered type" not in caplog.text


def test_every_type_that_can_be_in_the_state_is_registered_for_checkpoints() -> None:
    from app.graph.state import CHECKPOINT_TYPES

    registered = {t.__name__ for t in CHECKPOINT_TYPES}
    assert {"Evidence", "Hypothesis", "RootCause", "Triage", "IncidentContext", "StackFrame", "InvestigationScope"} <= registered
