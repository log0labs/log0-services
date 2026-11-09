"""The whole graph, end to end, with fake backends and a fake model (no network)."""

from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Send

from app.graph.builder import build_graph
from app.graph.routing import after_load_context, fan_out
from app.graph.state import InvestigationState, new_state
from app.integrations.clickhouse import ClickHouseError
from app.integrations.incident_service import IncidentNotFoundError
from app.llm.purpose import Purpose
from app.schemas.domain import HypothesisDraft, Triage
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from tests.graph_fakes import (
    INCIDENT_ID,
    FakeApi,
    FakeHistory,
    FakeLogs,
    FakeModel,
    FakeModels,
    make_context,
    synthesis_answer,
    triage_answer,
)


def _start(scope: InvestigationScope | None = None) -> InvestigationState:
    return new_state(investigation_id="inv-1", incident_id=str(INCIDENT_ID), scope=scope or InvestigationScope())


async def _run(ctx: Any, scope: InvestigationScope | None = None) -> InvestigationState:
    return await build_graph().ainvoke(_start(scope), context=ctx)


def _refs(state: InvestigationState) -> set[str]:
    return {e.source_ref.split()[0] for e in state["evidence"]}


# ------------------------------------------------------------------ the happy path


@pytest.mark.asyncio
async def test_logs_and_history_branches_run_in_parallel_and_both_contribute() -> None:
    final = await _run(make_context())  # triage answers ["logs", "history"]

    assert final["status"] == InvestigationStatus.RUNNING and final["error"] is None
    assert final["triage"].category == "dependency"
    assert [f.function for f in final["stack_frames"]] == ["charge", "run"]
    # load_context: incident, stats, stacktrace; gather_logs: timeline, cooccurring, samples;
    # gather_history: one earlier incident
    assert _refs(final) == {"incident", "logstats", "stacktrace", "timeline", "cooccurring", "samples"}
    assert sum(1 for e in final["evidence"] if e.summary.startswith("Earlier incident")) == 1
    assert len(final["evidence"]) == 7
    assert len({e.id for e in final["evidence"]}) == 7  # no id collisions between parallel branches
    assert final["tool_calls"] == 3 + 3 + 1  # load_context + gather_logs + gather_history
    assert final["gaps"] == []


@pytest.mark.asyncio
async def test_synthesize_runs_once_after_the_parallel_branches_and_cites_real_evidence() -> None:
    models = FakeModels(FakeModel(triage_answer(gather=["logs", "history", "code"])))
    final = await _run(make_context(models=models))

    assert models.purposes == [Purpose.TRIAGE, Purpose.INVESTIGATION]  # once each, though 3 branches ran
    assert models.investigation.options == {"method": "function_calling"}
    assert [h.status for h in final["hypotheses"]] == ["open", "open"]
    known = {e.id for e in final["evidence"]}
    cited = {i for h in final["hypotheses"] for i in h.supporting_evidence + h.contradicting_evidence}
    assert cited and cited <= known  # the grounding rule held
    # synthesize saw what the branches found, and the gaps, so it claims nothing about code
    sent = str(models.investigation.messages)
    assert "timeline" in sent and "Earlier incident" in sent
    assert "code investigation is not available" in sent


@pytest.mark.asyncio
async def test_a_model_that_cites_invented_evidence_yields_no_hypotheses_and_a_gap() -> None:
    bad = synthesis_answer(
        hypotheses=[HypothesisDraft(statement="It was the gremlins.", supporting_evidence=["ev_deadbeef"])]
    )
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(bad))
    final = await _run(make_context(models=models))
    assert final["hypotheses"] == []
    assert any("cited real evidence" in g for g in final["gaps"])
    assert final["status"] == InvestigationStatus.RUNNING  # a thin answer is not a failed run


@pytest.mark.asyncio
async def test_only_the_branches_triage_chose_run() -> None:
    # the user switched logs off: only history runs, even though triage would always add logs
    history, logs = FakeHistory(), FakeLogs()
    ctx = make_context(
        logs=logs, history=history, models=FakeModels(FakeModel(triage_answer(gather=["history"])))
    )
    final = await _run(ctx, InvestigationScope(logs=False))
    assert final["triage"].gather == ["history"]
    assert len(history.calls) == 1
    assert not any(e.source_ref.startswith("timeline") for e in final["evidence"])
    assert "timeline" not in [k for k, _, _ in logs.calls]


@pytest.mark.asyncio
async def test_logs_always_run_even_when_triage_only_asked_for_history() -> None:
    answer = triage_answer(gather=["history"])
    final = await _run(make_context(models=FakeModels(FakeModel(answer))))
    assert final["triage"].gather == ["logs", "history"]  # normalize_triage's rule from 2.8
    assert any(e.source_ref.startswith("timeline") for e in final["evidence"])


@pytest.mark.asyncio
async def test_code_and_runbook_branches_record_gaps_until_they_exist() -> None:
    answer = triage_answer(gather=["logs", "code", "runbooks"])
    final = await _run(make_context(models=FakeModels(FakeModel(answer))))
    assert any("code investigation is not available" in g for g in final["gaps"])
    assert any("Runbooks and docs were not searched" in g for g in final["gaps"])
    assert final["triage"].gather == ["logs", "code", "runbooks"]


# ----------------------------------------------------------- degradation and stops


@pytest.mark.asyncio
async def test_an_unreadable_incident_stops_before_triage_and_spends_no_tokens() -> None:
    models = FakeModels(FakeModel(triage_answer()))
    logs, history = FakeLogs(), FakeHistory()
    ctx = make_context(api=FakeApi(IncidentNotFoundError("nope")), logs=logs, history=history, models=models)
    final = await _run(ctx)
    assert final["status"] == InvestigationStatus.FAILED and final["error"] == "Incident not found"
    assert final["triage"] is None
    assert models.purposes == [] and logs.calls == [] and history.calls == []


@pytest.mark.asyncio
async def test_incident_service_down_also_stops_cleanly() -> None:
    ctx = make_context(api=FakeApi(httpx.ConnectError("refused")))
    final = await _run(ctx)
    assert final["status"] == InvestigationStatus.FAILED
    assert final["error"] == "Incident service is unavailable"


@pytest.mark.asyncio
async def test_log_store_down_degrades_every_log_step_to_gaps_but_history_still_runs() -> None:
    down = ClickHouseError("down")
    logs = FakeLogs(errors={"stats": down, "trace": down, "timeline": down, "cooccurring": down, "search": down})
    final = await _run(make_context(logs=logs))
    assert final["status"] == InvestigationStatus.RUNNING
    assert len(final["gaps"]) == 5  # stats, trace, timeline, co-occurring, samples
    assert any(e.summary.startswith("Earlier incident") for e in final["evidence"])  # history unaffected
    assert final["triage"] is not None  # triage ran on the incident alone


@pytest.mark.asyncio
async def test_a_broken_model_still_gathers_everything_the_scope_allows() -> None:
    models = FakeModels(FakeModel(error=RuntimeError("503")))
    final = await _run(make_context(models=models))
    assert final["triage"].category == "unknown"
    assert final["triage"].gather == ["logs", "history", "code", "runbooks"]
    assert any(e.source_ref.startswith("timeline") for e in final["evidence"])
    assert any(e.summary.startswith("Earlier incident") for e in final["evidence"])
    assert any("Automatic triage was unavailable" in g for g in final["gaps"])
    assert len(final["hypotheses"]) == 2  # synthesize uses its own model, which was fine


@pytest.mark.asyncio
async def test_every_source_off_ends_after_triage() -> None:
    scope = InvestigationScope(logs=False, history=False, code=False, runbooks=False, docs=False)
    models = FakeModels(FakeModel(triage_answer()))
    final = await _run(make_context(models=models), scope)
    assert final["triage"].gather == []
    assert models.purposes == []  # neither triage nor synthesize called a model
    assert final["hypotheses"] == []
    assert "Every evidence source is turned off" in final["gaps"][-1]
    assert _refs(final) == {"incident", "logstats", "stacktrace"}  # load_context's facts only


@pytest.mark.asyncio
async def test_tenant_comes_only_from_the_context() -> None:
    logs, history, api = FakeLogs(), FakeHistory(), FakeApi()
    await _run(make_context(logs=logs, history=history, api=api))
    assert logs.tenants_used() == {make_context().tenant_id}
    assert {c["tenant_id"] for c in history.calls} == {make_context().tenant_id}
    assert {tenant for _, tenant in api.calls} == {make_context().tenant_id}


# ------------------------------------------------------------------------ routing


def test_routing_after_load_context() -> None:
    state = _start()
    assert after_load_context(state) == END  # no incident loaded
    state["incident"] = object()  # type: ignore[assignment]
    assert after_load_context(state) == "triage"
    state["status"] = InvestigationStatus.FAILED
    assert after_load_context(state) == END


def test_fan_out_sends_one_task_per_branch_and_nothing_without_triage() -> None:
    state = _start()
    assert fan_out(state) == []  # triage missing
    state["triage"] = Triage(category="unknown", summary="s", gather=["logs", "code"])
    sends = fan_out(state)
    assert all(isinstance(s, Send) for s in sends)
    assert [s.node for s in sends] == ["gather_logs", "gather_code"]
    state["triage"] = Triage(category="unknown", summary="s", gather=[])
    assert fan_out(state) == []


# --------------------------------------------------------------------- checkpoints


@pytest.mark.asyncio
async def test_the_state_is_checkpointed_under_the_investigation_id() -> None:
    saver = InMemorySaver()
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": "inv-1"}}
    final = await graph.ainvoke(_start(), config, context=make_context())

    snapshot = await graph.aget_state(config)
    assert snapshot.values["triage"] == final["triage"]
    assert len(snapshot.values["evidence"]) == len(final["evidence"]) == 7
    assert snapshot.next == ()  # finished: nothing left to run
    history = [s async for s in graph.aget_state_history(config)]
    assert len(history) >= 4  # one checkpoint per step: input, load_context, triage, gather
