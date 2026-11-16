"""The propose_changes node and the whole graph with the review gate, approval and a proposed fix."""

import dataclasses
import shutil
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graph.builder import build_graph
from app.graph.code_access import CodeAccessError
from app.graph.events import events_from_update
from app.graph.nodes.propose_changes import (
    MAX_CHANGE_CALLS,
    build_brief,
    code_focus,
    propose_changes,
)
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.graph.subgraphs.change_proposer import CodeFocus
from app.schemas.change_proposal import ChangeProposalAnswer, CodeChangeDraft
from app.schemas.code_findings import CodeFindingDraft, CodeFindingsAnswer, CodeRef
from app.schemas.domain import (
    CodeChange,
    Evidence,
    Recommendation,
    ReviewDecision,
    RootCause,
    make_evidence_id,
)
from app.schemas.investigation import InvestigationScope
from tests.code_graph_support import (
    PAYMENT,
    FakeCode,
    Repo,
    ToolFakeModel,
    ai,
    call,
    checkout_for,
    context_with_code,
)
from tests.graph_fakes import INCIDENT_ID, synthesis_answer
from tests.test_change_proposer import NEW, OLD
from tests.test_gather_code import state_with

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("propose") / "repo")


def code_evidence(repo: Repo) -> Evidence:
    ref = f"{PAYMENT}:118-122@{repo.c2[:8]}"
    return Evidence(id=make_evidence_id("code", ref), kind="code", source_ref=ref, summary="The gateway call has no timeout.", content="")


def approved_state(source: Repo, **over: Any) -> InvestigationState:
    """The state after the engineer approved a root cause that cites code (as gather_code would have left it)."""
    evidence = code_evidence(source)
    state = state_with()
    state["evidence"] = [evidence]
    state["root_cause"] = RootCause(
        statement="The gateway call has no timeout, so a slow gateway blocks the request.",
        evidence_ids=[evidence.id],
        contributing_factors=["No circuit breaker"],
    )
    state["recommendation"] = Recommendation(summary="Pass a timeout to the gateway call.", rationale="It is the only unbounded call.", risks=["A timeout that is too short fails valid payments"])
    state["repo"] = checkout_for(source).repo
    state["review"] = ReviewDecision(action="approve")
    state["tool_calls"] = 10
    state.update(over)  # type: ignore[typeddict-item]
    return state


def proposer_model(*, answer: ChangeProposalAnswer | Exception | None = None, script: list[Any] | None = None) -> ToolFakeModel:
    good = ChangeProposalAnswer(
        changes=[CodeChangeDraft(file_path=PAYMENT, operation="modify", explanation="Pass the timeout again.", old_code=OLD, new_code=NEW)],
        notes=["Add a regression test."],
    )
    return ToolFakeModel(synthesis_answer(), script=script if script is not None else [AIMessage(content="done")], answers={ChangeProposalAnswer: answer or good})


async def run_node(state: InvestigationState, ctx: InvestigationContext) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("propose_changes", propose_changes)
    builder.add_edge(START, "propose_changes")
    builder.add_edge("propose_changes", END)
    return await builder.compile().ainvoke(state, context=ctx)


# -------------------------------------------------------------------------- the node


async def test_an_approved_root_cause_gets_a_grounded_change_against_the_pinned_commit(repo: Repo) -> None:
    code = FakeCode(checkout_for(repo))
    model = proposer_model()
    ctx, _ = context_with_code(code, investigation=model)

    final = await run_node(approved_state(repo), ctx)

    assert final["proposed_changes"] == [CodeChange(file_path=PAYMENT, operation="modify", explanation="Pass the timeout again.", old_code=OLD, new_code=NEW)]
    assert code.snapshot_calls == [(ctx.tenant_id, "payment-service", repo.c2)]  # the pinned commit, for the context's tenant
    assert code.calls == [] and code.snapshots_open == 0  # the shared checkout was not touched; the snapshot was released
    assert final["change_attempts"] == 1 and final["gaps"] == ["Fix proposal note: Add a regression test."]
    assert model.bound == ["list_files", "search_code", "read_file", "apply_patch"]  # read-only tools, plus the apply_patch decoy
    assert final["tool_calls"] == 10 + 1 + 1  # before + snapshot + the seeding read


async def test_the_proposal_has_its_own_allowance_even_when_the_investigation_budget_is_spent(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model())
    final = await run_node(approved_state(repo, tool_calls=30), ctx)  # the investigation's 30 calls are gone
    assert final["proposed_changes"] and final["tool_calls"] == 32


async def test_the_proposer_stops_at_its_own_call_limit(repo: Repo) -> None:
    script = [ai(*(call("list_files", f"g{turn}{i}") for i in range(MAX_CHANGE_CALLS))) for turn in range(2)]
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model(script=script))
    final = await run_node(approved_state(repo), ctx)
    assert final["tool_calls"] == 10 + 1 + MAX_CHANGE_CALLS
    assert any(f"tool-call limit ({MAX_CHANGE_CALLS})" in g for g in final["gaps"])


async def test_the_attempt_counter_goes_up_on_every_run(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model())
    assert (await run_node(approved_state(repo, change_attempts=1), ctx))["change_attempts"] == 2


async def test_a_model_that_proposes_nothing_usable_leaves_a_gap_and_no_changes(repo: Repo) -> None:
    answer = ChangeProposalAnswer(changes=[CodeChangeDraft(file_path="services/payment/Unread.java", operation="delete", explanation="x")], notes=["Not sure."])
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model(answer=answer))
    final = await run_node(approved_state(repo), ctx)
    assert final["proposed_changes"] == []
    assert final["gaps"][0] == "The model proposed no change that could be grounded in the code it read"
    assert any("1 proposed change(s) were discarded" in g for g in final["gaps"]) and "Fix proposal note: Not sure." in final["gaps"]


async def test_a_model_that_is_down_is_a_gap_and_nothing_else(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model(script=[RuntimeError("503")]))
    final = await run_node(approved_state(repo), ctx)
    assert final["proposed_changes"] == [] and any("No fix was proposed (the model was unavailable" in g for g in final["gaps"])
    assert not any("could be grounded" in g for g in final["gaps"])  # an outage is not the model's answer


# ------------------------------------------------------------------------------ gaps


async def test_without_code_access_configured_there_is_a_gap(repo: Repo) -> None:
    ctx, _ = context_with_code(None, investigation=proposer_model())
    final = await run_node(approved_state(repo), ctx)
    assert final["gaps"] == ["No fix was proposed: code access is not available (this server has no repository access configured)"]
    assert final["proposed_changes"] == [] and final["change_attempts"] == 0


async def test_with_no_pinned_commit_nothing_is_proposed(repo: Repo) -> None:
    code = FakeCode(checkout_for(repo))
    ctx, models = context_with_code(code, investigation=proposer_model())
    final = await run_node(approved_state(repo, repo=None), ctx)
    assert "no code was read during the investigation" in final["gaps"][0]
    assert code.snapshot_calls == [] and models.purposes == []


async def test_a_root_cause_that_cites_no_code_gets_no_fix(repo: Repo) -> None:
    ctx, models = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model())
    final = await run_node(approved_state(repo, evidence=[]), ctx)
    assert "does not cite any code location" in final["gaps"][0] and models.purposes == []


async def test_no_root_cause_or_incident_means_no_fix(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=proposer_model())
    final = await run_node(approved_state(repo, root_cause=None), ctx)
    assert final["gaps"] == ["No fix was proposed because there is no root cause to base one on"]


async def test_a_repository_that_was_disconnected_meanwhile_is_a_gap(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(None), investigation=proposer_model())
    final = await run_node(approved_state(repo), ctx)
    assert final["gaps"] == ["No fix was proposed: the repository for the service 'payment-service' is no longer connected"]
    assert final["tool_calls"] == 10 + 1 and final["change_attempts"] == 1


async def test_a_snapshot_that_cannot_be_made_is_a_gap_with_the_reason(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(CodeAccessError("the pinned commit is no longer available (git worktree failed (128): ...)")), investigation=proposer_model())
    final = await run_node(approved_state(repo), ctx)
    assert final["gaps"] == ["No fix was proposed: the pinned commit is no longer available (git worktree failed (128): ...)"]


async def test_an_unexpected_error_making_the_snapshot_does_not_leak_its_text(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(RuntimeError("path C:\\secret\\token-12345 not found")), investigation=proposer_model())
    final = await run_node(approved_state(repo), ctx)
    assert final["gaps"] == ["No fix was proposed: the code at the pinned commit could not be opened"]
    assert "token-12345" not in str(final["gaps"])


# ------------------------------------------------------------------- focus and brief


async def test_the_focus_is_the_cited_code_evidence_else_any_code_evidence(repo: Repo) -> None:
    state = approved_state(repo)
    other_ref = "services/payment/Other.java:5-9@abcdef12"
    other = Evidence(id=make_evidence_id("code", other_ref), kind="code", source_ref=other_ref, summary="s")
    log = Evidence(id="ev_log", kind="log", source_ref="stacktrace abc", summary="s")
    state["evidence"] = [*state["evidence"], other, log]
    assert code_focus(state) == [CodeFocus(PAYMENT, 118, 122)]  # only what the root cause cites
    state["root_cause"] = state["root_cause"].model_copy(update={"evidence_ids": ["ev_log"]})  # type: ignore[union-attr]
    assert code_focus(state) == [CodeFocus(PAYMENT, 118, 122), CodeFocus("services/payment/Other.java", 5, 9)]  # else every code finding
    state["evidence"] = [log, Evidence(id="ev_c", kind="commit", source_ref="commit abcdef12", summary="s")]
    assert code_focus(state) == []


async def test_the_brief_has_the_approved_cause_the_advice_and_the_engineers_guidance(repo: Repo) -> None:
    state = approved_state(repo, review=ReviewDecision(action="approve", note="keep the public API unchanged"))
    brief = build_brief(state)
    assert "approved by the engineer" in brief and "no timeout, so a slow gateway" in brief and "No circuit breaker" in brief
    assert "Pass a timeout to the gateway call." in brief and "only unbounded call" in brief and "too short fails valid payments" in brief
    assert "keep the public API unchanged" in brief
    assert "EARLIER MODEL GUESS" not in brief  # the earlier AI summary never reaches the proposer


# ---------------------------------------------------------------------------- events


async def test_the_event_names_the_files_and_never_carries_the_code() -> None:
    change = CodeChange(file_path=PAYMENT, operation="modify", explanation="why", old_code="SECRET_OLD_TEXT", new_code="SECRET_NEW_TEXT")
    events = events_from_update({"propose_changes": {"proposed_changes": [change], "gaps": []}})
    assert events == [("changes.proposed", {"count": 1, "files": [PAYMENT], "operations": ["modify"]})]
    assert "SECRET_" not in str(events)
    assert events_from_update({"propose_changes": {"proposed_changes": [], "gaps": []}}) == []  # nothing proposed, nothing announced


# -------------------------------------------------------------------------- the graph


def graph_model(repo: Repo) -> ToolFakeModel:
    """One investigation model for the whole run: it plays the code investigator's turns and then the proposer's."""
    findings = CodeFindingsAnswer(findings=[CodeFindingDraft(summary="The gateway call has no timeout.", kind="suspect_code", locations=[CodeRef(path=PAYMENT, start_line=118, end_line=122)])])
    good = ChangeProposalAnswer(changes=[CodeChangeDraft(file_path=PAYMENT, operation="modify", explanation="Pass the timeout again.", old_code=OLD, new_code=NEW)])
    return ToolFakeModel(
        synthesis_answer(),
        script=[ai(call("list_files", "c1")), AIMessage(content="investigated"), AIMessage(content="proposed")],
        answers={CodeFindingsAnswer: findings, ChangeProposalAnswer: good},
    )


def gated_graph(repo: Repo) -> tuple[Any, InvestigationContext, FakeCode, Any]:
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code, investigation=graph_model(repo))
    ctx = dataclasses.replace(ctx, require_review=True)
    return build_graph(InMemorySaver()), ctx, code, {"configurable": {"thread_id": "t"}}


def start() -> InvestigationState:
    return new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope())


async def test_approval_leads_to_a_proposed_fix_against_the_pinned_commit(repo: Repo) -> None:
    graph, ctx, code, config = gated_graph(repo)
    paused = await graph.ainvoke(start(), config, context=ctx)
    assert "__interrupt__" in paused and code.snapshot_calls == []  # nothing is proposed before the engineer decides

    updates = [u async for u in graph.astream(Command(resume={"action": "approve", "note": None}), config, context=ctx, stream_mode="updates")]
    final = (await graph.aget_state(config)).values

    assert [c.file_path for c in final["proposed_changes"]] == [PAYMENT] and final["change_attempts"] == 1
    pinned = (ctx.tenant_id, "payment-service", final["repo"].sha)
    assert code.snapshot_calls == [pinned, pinned] and code.snapshots_open == 0  # one snapshot to propose, one to validate: both at the pinned commit
    events = [e for u in updates for e in events_from_update(u)]
    assert ("changes.proposed", {"count": 1, "files": [PAYMENT], "operations": ["modify"]}) in events


@pytest.mark.parametrize("action", ["stop"])
async def test_stop_proposes_nothing(repo: Repo, action: str) -> None:
    graph, ctx, code, config = gated_graph(repo)
    await graph.ainvoke(start(), config, context=ctx)
    final = await graph.ainvoke(Command(resume={"action": action, "note": None}), config, context=ctx)
    assert final["proposed_changes"] == [] and code.snapshot_calls == []


async def test_refine_proposes_nothing_until_the_engineer_finally_approves(repo: Repo) -> None:
    graph, ctx, code, config = gated_graph(repo)
    await graph.ainvoke(start(), config, context=ctx)
    again = await graph.ainvoke(Command(resume={"action": "refine", "note": "check the pool as well"}), config, context=ctx)
    assert "__interrupt__" in again and code.snapshot_calls == []
    final = await graph.ainvoke(Command(resume={"action": "approve", "note": None}), config, context=ctx)
    assert final["proposed_changes"] and len(code.snapshot_calls) == 2  # propose, then validate


async def test_without_the_gate_a_fix_is_proposed_automatically_when_code_is_available(repo: Repo) -> None:
    """The gate off means "no human in the loop", i.e. approved: otherwise the graph would end at the root cause and
    a default deployment would never propose a fix."""
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code, investigation=graph_model(repo))  # require_review stays False
    final = await build_graph().ainvoke(start(), context=ctx)
    assert final["review"] is not None and final["review"].action == "approve"
    assert final["proposed_changes"] != [] and code.snapshot_calls != []
    assert final["diff"] is not None
