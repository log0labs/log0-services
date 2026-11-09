"""Unit tests for the verify node and the follow-up search. The chat model is a fake."""

import asyncio
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph

from app.graph.budget import Budget
from app.graph.nodes import verify as verify_module
from app.graph.nodes.gather_followup import gather_followup, search_evidence
from app.graph.nodes.verify import (
    MAX_FOLLOW_UPS,
    SYSTEM_PROMPT,
    apply_verdicts,
    clean_term,
    verify,
)
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.integrations.clickhouse import ClickHouseError
from app.llm.purpose import Purpose
from app.schemas.domain import Evidence, Hypothesis, Verification, make_evidence_id
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext
from tests.graph_fakes import (
    FINGERPRINT,
    FIRST,
    INCIDENT_ID,
    LAST,
    TENANT,
    FakeApi,
    FakeHistory,
    FakeLogs,
    FakeModel,
    FakeModels,
    triage_answer,
    verdict,
    verification_answer,
)

EV_A, EV_B, EV_C = (make_evidence_id("log", f"ref {n}") for n in "abc")


def _evidence(ref_id: str, summary: str = "something happened") -> Evidence:
    return Evidence(id=ref_id, kind="log", source_ref=f"ref {ref_id}", summary=summary, content="")


def _hyp(hyp_id: str = "hyp_1", status: str = "open", **over: Any) -> Hypothesis:
    data: dict[str, Any] = {"id": hyp_id, "statement": f"Statement of {hyp_id}", "supporting_evidence": [EV_A]}
    data.update(over)
    return Hypothesis(status=status, **data)  # type: ignore[arg-type]


def _state(hypotheses: list[Hypothesis] | None = None, **over: Any) -> InvestigationState:
    state = new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope())
    state["incident"] = IncidentContext(
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
        top_messages=["Connection timeout"],
        ai_summary=None,
    )
    state["evidence"] = [_evidence(EV_A), _evidence(EV_B), _evidence(EV_C)]
    state["triage"] = triage_answer()
    state["hypotheses"] = hypotheses if hypotheses is not None else [_hyp("hyp_1"), _hyp("hyp_2")]
    state.update(over)  # type: ignore[typeddict-item]
    return state


def _context(models: FakeModels | None, *, logs: FakeLogs | None = None, budget: Budget | None = None) -> InvestigationContext:
    return InvestigationContext(
        tenant_id=TENANT,
        logs=logs or FakeLogs(),
        incidents=FakeApi(),
        history=FakeHistory(),
        models=models,
        budget=budget or Budget(),
    )


async def _run(node: Any, state: InvestigationState, ctx: InvestigationContext) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return await builder.compile().ainvoke(state, context=ctx)


def _models(answer: Any) -> FakeModels:
    return FakeModels(FakeModel(triage_answer()), investigation=FakeModel(answers={Verification: answer}))


# ----------------------------------------------------------------- clean_term


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OutOfMemoryError", "OutOfMemoryError"),
        ('  "connection   reset"  ', "connection reset"),
        ("line one\nline two", "line one line two"),
        ("ab", None),  # too short to be a useful search
        ("   ", None),
        (None, None),
        ("x" * 500, "x" * 80),
    ],
)
def test_clean_term(raw: str | None, expected: str | None) -> None:
    assert clean_term(raw) == expected


# -------------------------------------------------------------- apply_verdicts


def test_supported_and_refuted_need_evidence_cited_by_that_verdict() -> None:
    hypotheses = [_hyp("hyp_1"), _hyp("hyp_2")]
    raw = verification_answer(
        verdict("hyp_1", "supported", supporting_evidence=[EV_B]),
        verdict("hyp_2", "refuted", contradicting_evidence=[EV_C]),
    )
    changed, searches = apply_verdicts(raw, hypotheses, {EV_A, EV_B, EV_C})
    by_id = {h.id: h for h in changed}
    assert by_id["hyp_1"].status == "supported" and by_id["hyp_1"].supporting_evidence == [EV_A, EV_B]
    assert by_id["hyp_2"].status == "refuted" and by_id["hyp_2"].contradicting_evidence == [EV_C]
    assert searches == []


@pytest.mark.parametrize("kind", ["supported", "refuted"])
def test_a_verdict_that_cites_nothing_real_stays_open(kind: str) -> None:
    raw = verification_answer(
        verdict("hyp_1", kind, supporting_evidence=["ev_deadbeef"], contradicting_evidence=["ev_deadbeef"])
    )
    changed, _ = apply_verdicts(raw, [_hyp("hyp_1")], {EV_A})
    assert changed == []  # still open and nothing new cited: nothing to report


def test_evidence_left_over_from_synthesize_does_not_make_a_hypothesis_supported() -> None:
    # hyp_1 already cites EV_A, but the verdict cites nothing: 'supported' is not earned
    changed, _ = apply_verdicts(verification_answer(verdict("hyp_1", "supported")), [_hyp("hyp_1")], {EV_A})
    assert changed == []


def test_evidence_cited_on_both_sides_counts_for_neither() -> None:
    raw = verification_answer(
        verdict("hyp_1", "supported", supporting_evidence=[EV_B], contradicting_evidence=[EV_B])
    )
    changed, _ = apply_verdicts(raw, [_hyp("hyp_1")], {EV_A, EV_B})
    assert changed == []  # EV_B removed from both, so nothing earned the status


def test_unknown_duplicate_and_already_decided_hypotheses_are_ignored() -> None:
    hypotheses = [_hyp("hyp_1"), _hyp("hyp_2", status="supported")]
    raw = verification_answer(
        verdict("hyp_nope", "supported", supporting_evidence=[EV_A]),
        verdict("hyp_2", "refuted", contradicting_evidence=[EV_B]),  # already decided: never flips back
        verdict("hyp_1", "supported", supporting_evidence=[EV_B]),
        verdict("hyp_1", "refuted", contradicting_evidence=[EV_C]),  # second verdict for the same one
    )
    changed, _ = apply_verdicts(raw, hypotheses, {EV_A, EV_B, EV_C})
    assert [(h.id, h.status) for h in changed] == [("hyp_1", "supported")]


def test_follow_up_searches_come_only_from_hypotheses_left_open_and_are_cleaned_and_capped() -> None:
    hypotheses = [_hyp(f"hyp_{n}") for n in range(5)]
    raw = verification_answer(
        verdict("hyp_0", "open", follow_up_search='"OutOfMemoryError"'),
        verdict("hyp_1", "open", follow_up_search="outofmemoryerror"),  # same search, other case: once
        verdict("hyp_2", "open", follow_up_search="ab"),  # too short
        verdict("hyp_3", "supported", supporting_evidence=[EV_B], follow_up_search="ignored: it is decided"),
        verdict("hyp_4", "open", follow_up_search="pool exhausted"),
    )
    _, searches = apply_verdicts(raw, hypotheses, {EV_A, EV_B})
    assert searches == ["OutOfMemoryError", "pool exhausted"]
    many = verification_answer(*(verdict(f"hyp_{n}", "open", follow_up_search=f"search {n}") for n in range(5)))
    assert len(apply_verdicts(many, hypotheses, set())[1]) == MAX_FOLLOW_UPS


def test_new_open_evidence_is_added_to_an_open_hypothesis_without_changing_its_status() -> None:
    raw = verification_answer(verdict("hyp_1", "open", supporting_evidence=[EV_B, "ev_deadbeef"]))
    changed, _ = apply_verdicts(raw, [_hyp("hyp_1")], {EV_A, EV_B})
    assert changed[0].status == "open" and changed[0].supporting_evidence == [EV_A, EV_B]


# ------------------------------------------------------------------ the node


@pytest.mark.asyncio
async def test_verify_updates_hypotheses_and_counts_the_round() -> None:
    models = _models(verification_answer(verdict("hyp_1", "supported", supporting_evidence=[EV_B])))
    final = await _run(verify, _state(), _context(models))

    assert {h.id: h.status for h in final["hypotheses"]} == {"hyp_1": "supported", "hyp_2": "open"}
    assert final["verify_round"] == 1 and final["follow_ups"] == []
    assert models.purposes == [Purpose.INVESTIGATION]
    assert models.investigation.options == {"method": "function_calling"}  # the same method setting as synthesize


@pytest.mark.asyncio
async def test_the_prompt_lists_only_open_hypotheses_and_no_private_ids() -> None:
    models = _models(verification_answer())
    state = _state([_hyp("hyp_1"), _hyp("hyp_2", status="refuted")])
    await _run(verify, state, _context(models))

    system, human = models.investigation.messages[0]
    assert system[1] == SYSTEM_PROMPT
    text = human[1]
    assert "hyp_1" in text and "hyp_2" not in text  # a decided hypothesis is not judged again
    assert EV_A in text  # the evidence it may cite
    assert str(TENANT) not in text and str(INCIDENT_ID) not in text and FINGERPRINT not in text


@pytest.mark.asyncio
async def test_follow_up_searches_are_stored_for_the_loop() -> None:
    models = _models(verification_answer(verdict("hyp_1", "open", follow_up_search="OutOfMemoryError")))
    final = await _run(verify, _state(), _context(models))
    assert final["follow_ups"] == ["OutOfMemoryError"] and final["verify_round"] == 1


@pytest.mark.asyncio
async def test_nothing_open_means_no_model_call() -> None:
    models = _models(verification_answer())
    for hypotheses in ([], [_hyp("hyp_1", status="supported")]):
        final = await _run(verify, _state(hypotheses), _context(models))
        assert final["verify_round"] == 0 and final["follow_ups"] == [] and final["gaps"] == []
    assert models.purposes == []


@pytest.mark.asyncio
async def test_a_model_error_leaves_hypotheses_open_with_a_gap() -> None:
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(error=RuntimeError("503")))
    final = await _run(verify, _state(), _context(models))
    assert [h.status for h in final["hypotheses"]] == ["open", "open"]
    assert any("could not be verified" in g for g in final["gaps"])
    assert final["verify_round"] == 1 and final["follow_ups"] == []  # no loop after a failure


@pytest.mark.asyncio
async def test_an_unusable_answer_is_a_gap_too() -> None:
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(answers={Verification: {"verdicts": "x"}}))
    final = await _run(verify, _state(), _context(models))
    assert any("could not be verified" in g for g in final["gaps"])


@pytest.mark.asyncio
async def test_a_slow_model_times_out_into_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_module, "VERIFY_TIMEOUT_SECONDS", 0.05)
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(verification_answer(), delay=5))
    final = await asyncio.wait_for(_run(verify, _state(), _context(models)), timeout=3)
    assert any("could not be verified" in g for g in final["gaps"])


@pytest.mark.asyncio
async def test_without_models_verify_raises() -> None:
    with pytest.raises(RuntimeError, match="models is not set"):
        await _run(verify, _state(), _context(None))


# ------------------------------------------------------------ the budget stops it


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_over", "budget", "expected"),
    [
        ({"tool_calls": 30}, Budget(), "tool-call limit"),
        ({"verify_round": 2}, Budget(), "verification round limit"),
        ({}, Budget(max_wall_seconds=0), "time limit"),
    ],
)
async def test_an_exhausted_budget_skips_verification_with_a_gap(
    state_over: dict[str, Any], budget: Budget, expected: str
) -> None:
    models = _models(verification_answer(verdict("hyp_1", "supported", supporting_evidence=[EV_B])))
    final = await _run(verify, _state(**state_over), _context(models, budget=budget))
    assert models.purposes == []  # no tokens spent
    assert [h.status for h in final["hypotheses"]] == ["open", "open"]
    assert any("were not verified because the" in g and expected in g for g in final["gaps"])
    assert final["follow_ups"] == []


@pytest.mark.asyncio
async def test_wanted_searches_are_dropped_with_a_gap_when_there_is_no_room_for_another_round() -> None:
    models = _models(verification_answer(verdict("hyp_1", "open", follow_up_search="OutOfMemoryError")))
    final = await _run(verify, _state(verify_round=1), _context(models))  # this is round 2 of 2
    assert final["verify_round"] == 2 and final["follow_ups"] == []
    assert any("were not run because the verification round limit" in g for g in final["gaps"])


# --------------------------------------------------------------- gather_followup


@pytest.mark.asyncio
async def test_follow_up_searches_become_evidence_and_count_as_tool_calls() -> None:
    logs = FakeLogs()
    final = await _run(gather_followup, _state(follow_ups=["OutOfMemoryError", "pool exhausted"]), _context(None, logs=logs))

    searches = [(kind, params.get("query")) for kind, _, params in logs.calls if kind == "search"]
    assert searches == [("search", "OutOfMemoryError"), ("search", "pool exhausted")]
    assert logs.tenants_used() == {TENANT}  # the tenant comes from the context
    new = final["evidence"][3:]  # after the three that were already there
    assert len(new) == 2 and all(e.kind == "log" and e.id.startswith("ev_") for e in new)
    assert 'contain "OutOfMemoryError"' in new[0].summary
    assert final["tool_calls"] == 2 and final["follow_ups"] == []  # the loop ends unless verify asks again


@pytest.mark.asyncio
async def test_a_search_with_no_result_is_evidence_too() -> None:
    final = await _run(gather_followup, _state(follow_ups=["nothing like this"]), _context(None, logs=FakeLogs(search_rows=[])))
    assert final["evidence"][-1].summary.startswith('No log line contains "nothing like this"')


@pytest.mark.asyncio
async def test_a_log_store_outage_is_a_gap_and_a_bug_is_not_swallowed() -> None:
    down = FakeLogs(errors={"search": ClickHouseError("down")})
    final = await _run(gather_followup, _state(follow_ups=["OutOfMemoryError"]), _context(None, logs=down))
    assert len(final["evidence"]) == 3 and any("could not be run" in g for g in final["gaps"])
    assert final["tool_calls"] == 1  # the attempt still counts against the budget

    broken = FakeLogs(errors={"search": ValueError("bug")})
    with pytest.raises(ValueError, match="bug"):
        await _run(gather_followup, _state(follow_ups=["OutOfMemoryError"]), _context(None, logs=broken))


@pytest.mark.asyncio
async def test_no_searches_means_no_queries() -> None:
    logs = FakeLogs()
    final = await _run(gather_followup, _state(), _context(None, logs=logs))
    assert logs.calls == [] and final["follow_ups"] == []


def test_search_evidence_ids_are_stable_so_a_repeated_search_adds_nothing() -> None:
    from app.tools.logs import LogSearchResult

    empty = LogSearchResult(lines=[], limit_reached=False)
    first = search_evidence("OutOfMemoryError", FIRST, LAST, empty)
    again = search_evidence("outofmemoryerror", FIRST, LAST, empty)
    other = search_evidence("pool exhausted", FIRST, LAST, empty)
    assert first.id == again.id != other.id
