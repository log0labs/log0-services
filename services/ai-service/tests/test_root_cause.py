"""Unit tests for the root_cause node, the confidence rule and the row serialisation."""

import asyncio
from typing import Any

import pytest

from app.graph.nodes import root_cause as root_cause_module
from app.graph.nodes.root_cause import (
    MAX_ITEM_CHARS,
    MAX_ITEMS,
    MAX_STATEMENT_CHARS,
    SYSTEM_PROMPT,
    build_analysis,
    confidence_band,
    root_cause,
)
from app.graph.result import result_for_row
from app.llm.purpose import Purpose
from app.schemas.domain import (
    CodeLocation,
    Evidence,
    Recommendation,
    RootCause,
    RootCauseAnalysis,
)
from tests.graph_fakes import (
    FINGERPRINT,
    INCIDENT_ID,
    TENANT,
    FakeModel,
    FakeModels,
    analysis_answer,
    triage_answer,
)
from tests.test_verify import EV_A, EV_B, EV_C, _context, _hyp, _run, _state


def _ev(ev_id: str, kind: str) -> Evidence:
    return Evidence(id=ev_id, kind=kind, source_ref=f"ref {ev_id}", summary="s")  # type: ignore[arg-type]


def _cause(**over: Any) -> RootCause:
    data: dict[str, Any] = {"statement": "s", "evidence_ids": [EV_A, EV_B], "hypothesis_id": "hyp_1"}
    data.update(over)
    return RootCause(**data)


def _models(answer: Any) -> FakeModels:
    return FakeModels(FakeModel(triage_answer()), investigation=FakeModel(answers={RootCauseAnalysis: answer}))


KINDS = [_ev(EV_A, "log"), _ev(EV_B, "incident"), _ev(EV_C, "log")]


# ------------------------------------------------------------------ confidence_band


def test_high_needs_a_supported_hypothesis_two_kinds_of_evidence_and_nothing_against_it() -> None:
    assert confidence_band(_cause(), [_hyp("hyp_1", "supported")], KINDS) == "high"


def test_one_kind_of_evidence_is_only_medium() -> None:
    cause = _cause(evidence_ids=[EV_A, EV_C])  # both are logs
    assert confidence_band(cause, [_hyp("hyp_1", "supported")], KINDS) == "medium"


def test_contradicting_evidence_on_the_hypothesis_lowers_high_to_medium() -> None:
    hypothesis = _hyp("hyp_1", "supported", contradicting_evidence=[EV_C])
    assert confidence_band(_cause(), [hypothesis], KINDS) == "medium"


def test_a_named_code_location_is_unresolved_until_the_code_tools_exist() -> None:
    cause = _cause(affected_locations=[CodeLocation(file_path="a.py", line=1)])
    assert confidence_band(cause, [_hyp("hyp_1", "supported")], KINDS) == "medium"


@pytest.mark.parametrize("status", ["open", "refuted"])
def test_a_hypothesis_that_is_not_supported_can_only_be_low(status: str) -> None:
    assert confidence_band(_cause(), [_hyp("hyp_1", status)], KINDS) == "low"


def test_no_hypothesis_behind_the_root_cause_is_low() -> None:
    assert confidence_band(_cause(hypothesis_id=None), [_hyp("hyp_1", "supported")], KINDS) == "low"
    assert confidence_band(_cause(hypothesis_id="hyp_nope"), [_hyp("hyp_1", "supported")], KINDS) == "low"


# ------------------------------------------------------------------ build_analysis


def test_a_grounded_answer_becomes_a_root_cause_and_a_recommendation() -> None:
    raw = analysis_answer(evidence_ids=[EV_A, EV_B], hypothesis_id="hyp_1")
    cause, recommendation, band, gaps = build_analysis(raw, [_hyp("hyp_1", "supported")], KINDS)
    assert cause is not None and cause.evidence_ids == [EV_A, EV_B] and cause.hypothesis_id == "hyp_1"
    assert recommendation is not None and recommendation.summary.startswith("Check the gateway")
    assert band == "high" and gaps == []


def test_invented_evidence_ids_are_removed_and_none_left_means_no_root_cause() -> None:
    kept, _, _, _ = build_analysis(analysis_answer(evidence_ids=[EV_A, "ev_deadbeef"]), [_hyp()], KINDS)
    assert kept is not None and kept.evidence_ids == [EV_A]

    cause, recommendation, band, gaps = build_analysis(analysis_answer(evidence_ids=["ev_deadbeef"]), [_hyp()], KINDS)
    assert (cause, recommendation, band) == (None, None, None)
    assert any("cited real evidence" in g for g in gaps)


def test_an_empty_statement_is_no_root_cause() -> None:
    cause, _, _, gaps = build_analysis(analysis_answer(statement="   ", evidence_ids=[EV_A]), [_hyp()], KINDS)
    assert cause is None and gaps


def test_a_refuted_or_unknown_hypothesis_is_not_kept_as_the_source() -> None:
    hypotheses = [_hyp("hyp_1", "refuted"), _hyp("hyp_2", "supported")]
    refuted, _, band, _ = build_analysis(analysis_answer(hypothesis_id="hyp_1", evidence_ids=[EV_A]), hypotheses, KINDS)
    assert refuted is not None and refuted.hypothesis_id is None and band == "low"
    unknown, _, _, _ = build_analysis(analysis_answer(hypothesis_id="hyp_nope", evidence_ids=[EV_A]), hypotheses, KINDS)
    assert unknown is not None and unknown.hypothesis_id is None
    supported, _, _, _ = build_analysis(analysis_answer(hypothesis_id=" hyp_2 ", evidence_ids=[EV_A]), hypotheses, KINDS)
    assert supported is not None and supported.hypothesis_id == "hyp_2"


def test_text_is_trimmed_lists_are_cleaned_and_confidence_is_clamped() -> None:
    raw = analysis_answer(
        statement="  Gateway   down.\n  " + "x" * 2000,
        evidence_ids=[EV_A],
        contributing_factors=["  a  ", "a", "", *[f"factor {n}" for n in range(20)], "y" * 1000],
        confidence=7.5,
        recommendation_summary="Do   this\nnow",
    )
    cause, recommendation, _, _ = build_analysis(raw, [_hyp()], KINDS)
    assert cause is not None and recommendation is not None
    assert len(cause.statement) == MAX_STATEMENT_CHARS and "\n" not in cause.statement
    assert cause.contributing_factors[0] == "a" and len(cause.contributing_factors) == MAX_ITEMS
    assert all(len(f) <= MAX_ITEM_CHARS for f in cause.contributing_factors)
    assert cause.model_confidence == 1.0
    assert recommendation.summary == "Do this now"
    assert build_analysis(analysis_answer(confidence=-3, evidence_ids=[EV_A]), [_hyp()], KINDS)[0].model_confidence == 0.0  # type: ignore[union-attr]


def test_a_missing_recommendation_is_a_gap_but_the_root_cause_stays() -> None:
    cause, recommendation, band, gaps = build_analysis(
        analysis_answer(recommendation_summary="  ", evidence_ids=[EV_A]), [_hyp()], KINDS
    )
    assert cause is not None and recommendation is None and band == "low"
    assert gaps == ["The model gave no recommendation"]


# ------------------------------------------------------------------------ the node


@pytest.mark.asyncio
async def test_the_node_fills_root_cause_recommendation_and_band() -> None:
    state = _state([_hyp("hyp_1", "supported")])
    state["evidence"] = KINDS
    models = _models(analysis_answer(evidence_ids=[EV_A, EV_B], hypothesis_id="hyp_1"))
    final = await _run(root_cause, state, _context(models))

    assert final["root_cause"].evidence_ids == [EV_A, EV_B]
    assert isinstance(final["recommendation"], Recommendation) and final["recommendation"].changes == []
    assert final["confidence_band"] == "high" and final["gaps"] == []
    assert models.purposes == [Purpose.INVESTIGATION]
    assert models.investigation.options == {"method": "function_calling"}


@pytest.mark.asyncio
async def test_the_prompt_shows_hypothesis_statuses_and_no_private_ids() -> None:
    models = _models(analysis_answer(evidence_ids=[EV_A]))
    state = _state([_hyp("hyp_1", "supported"), _hyp("hyp_2", "refuted"), _hyp("hyp_3")])
    await _run(root_cause, state, _context(models))

    system, human = models.investigation.messages[0]
    assert system[1] == SYSTEM_PROMPT
    text = human[1]
    assert "hyp_1 [supported]" in text and "hyp_2 [refuted]" in text and "hyp_3 [open]" in text
    assert str(TENANT) not in text and str(INCIDENT_ID) not in text and FINGERPRINT not in text


@pytest.mark.asyncio
async def test_no_hypotheses_means_no_model_call_and_a_gap() -> None:
    models = _models(analysis_answer())
    final = await _run(root_cause, _state([]), _context(models))
    assert final["root_cause"] is None and final["confidence_band"] is None
    assert any("no hypothesis" in g for g in final["gaps"]) and models.purposes == []


@pytest.mark.asyncio
async def test_a_model_error_is_a_gap_not_a_failure() -> None:
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(error=RuntimeError("503")))
    final = await _run(root_cause, _state(), _context(models))
    assert final["root_cause"] is None and any("could not be generated" in g for g in final["gaps"])


@pytest.mark.asyncio
async def test_an_unusable_answer_is_a_gap_too() -> None:
    models = _models({"statement": 5})
    final = await _run(root_cause, _state(), _context(models))
    assert final["root_cause"] is None and any("could not be generated" in g for g in final["gaps"])


@pytest.mark.asyncio
async def test_a_slow_model_times_out_into_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(root_cause_module, "ROOT_CAUSE_TIMEOUT_SECONDS", 0.05)
    models = FakeModels(FakeModel(triage_answer()), investigation=FakeModel(analysis_answer(), delay=5))
    final = await asyncio.wait_for(_run(root_cause, _state(), _context(models)), timeout=3)
    assert final["root_cause"] is None and final["gaps"]


@pytest.mark.asyncio
async def test_without_models_the_node_raises() -> None:
    with pytest.raises(RuntimeError, match="models is not set"):
        await _run(root_cause, _state(), _context(None))


# ------------------------------------------------------------------ result_for_row


def test_the_row_json_is_camel_case_and_never_holds_the_models_own_confidence() -> None:
    state = _state()
    state["root_cause"] = _cause(contributing_factors=["f"], unresolved_questions=["q"], model_confidence=0.9)
    state["recommendation"] = Recommendation(summary="do it", tests_required=["t"], verification_steps=["v"])
    state["confidence_band"] = "medium"

    row = result_for_row(state)
    assert row["root_cause"] == {
        "statement": "s",
        "hypothesisId": "hyp_1",
        "evidenceIds": [EV_A, EV_B],
        "affectedLocations": [],
        "contributingFactors": ["f"],
        "unresolvedQuestions": ["q"],
    }
    assert row["recommendation"]["testsRequired"] == ["t"] and row["recommendation"]["verificationSteps"] == ["v"]
    assert row["confidence_band"] == "medium"
    assert "modelConfidence" not in str(row) and "model_confidence" not in str(row)


def test_a_thin_run_stores_nothing() -> None:
    assert result_for_row(_state()) == {}
