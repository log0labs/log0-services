"""The code branch inside the investigation: gather_code, its budget, and the full graph with code."""

import dataclasses
import shutil
import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph.budget import Budget
from app.graph.builder import build_graph
from app.graph.code_access import CodeAccessError
from app.graph.events import events_from_update
from app.graph.nodes.gather_code import (
    MAX_CODE_CALLS,
    build_brief,
    gather_code,
    plan_limits,
)
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.graph.usage import UsageRecorder
from app.parsing.stacktrace import parse_stack_trace
from app.schemas.code_findings import CodeFindingDraft, CodeFindingsAnswer, CodeRef
from app.schemas.domain import Triage, make_evidence_id
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext
from tests.code_graph_support import (
    PAYMENT,
    TRACE,
    FakeCode,
    Repo,
    ToolFakeModel,
    ai,
    call,
    checkout_for,
    context_with_code,
)
from tests.graph_fakes import (
    FINGERPRINT,
    FIRST,
    INCIDENT_ID,
    LAST,
    make_context,
    synthesis_answer,
)

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]

FRAMES = parse_stack_trace(TRACE).frames
LIE = "The retry loop was rewritten."  # a model claim the repository does not support


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("gather") / "repo")


def incident(**over: Any) -> IncidentContext:
    data: dict[str, Any] = {
        "incident_id": INCIDENT_ID,
        "fingerprint": FINGERPRINT,
        "service_name": "payment-service",
        "environment": "production",
        "severity": "HIGH",
        "status": "NEW",
        "occurrence_count": 1500,
        "first_seen_at": FIRST,
        "last_seen_at": LAST,
        "resolved_at": None,
        "top_messages": ["Connection timeout after 30000ms"],
        "ai_summary": "EARLIER MODEL GUESS: the database is down",
    }
    data.update(over)
    return IncidentContext(**data)


def state_with(**over: Any) -> InvestigationState:
    state = new_state(investigation_id="i1", incident_id=str(INCIDENT_ID), scope=InvestigationScope(), question=over.pop("question", None))
    state["incident"] = incident()
    state["stack_frames"] = FRAMES
    state["triage"] = Triage(category="code_bug", summary="A call to the gateway fails.", gather=["logs", "code"], initial_hypotheses=[])
    state.update(over)  # type: ignore[typeddict-item]
    return state


async def run_node(state: InvestigationState, ctx: InvestigationContext, *, config: dict[str, Any] | None = None) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("gather_code", gather_code)
    builder.add_edge(START, "gather_code")
    builder.add_edge("gather_code", END)
    return await builder.compile().ainvoke(state, config, context=ctx)


def findings_answer(repo: Repo) -> CodeFindingsAnswer:
    return CodeFindingsAnswer(
        findings=[
            CodeFindingDraft(summary=f"The gateway call has no timeout. {LIE}", kind="suspect_code", locations=[CodeRef(path=PAYMENT, start_line=118, end_line=122)]),
            CodeFindingDraft(summary="The timeout was removed two days before the incident.", kind="recent_change", commits=[repo.c2[:8]]),
        ],
        unresolved=["Whether the gateway itself was slow"],
    )


def tool_model(repo: Repo, *, answer: CodeFindingsAnswer | Exception | None = None, script: list[Any] | None = None) -> ToolFakeModel:
    script = script if script is not None else [ai(call("show_commit", "c1", sha=repo.c2[:8])), AIMessage(content="done")]
    return ToolFakeModel(synthesis_answer(), script=script, answers={CodeFindingsAnswer: answer or findings_answer(repo)})


# --------------------------------------------------------------------------- evidence


async def test_findings_become_evidence_with_excerpts_read_from_the_repository(repo: Repo) -> None:
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code, investigation=tool_model(repo))

    final = await run_node(state_with(), ctx)

    by_kind = {e.kind: e for e in final["evidence"]}
    assert set(by_kind) == {"code", "commit"}
    code_ev, commit_ev = by_kind["code"], by_kind["commit"]

    assert code_ev.source_ref == f"{PAYMENT}:118-122@{repo.c2[:8]}"
    assert code_ev.id == make_evidence_id("code", code_ev.source_ref)
    assert "gateway.call(request); // no timeout set" in code_ev.content  # the repository's text, with line numbers
    assert "   120 |" in code_ev.content
    assert LIE in code_ev.summary and LIE not in code_ev.content  # the model's claim is only ever the summary

    assert commit_ev.source_ref == f"commit {repo.c2[:8]}"
    assert commit_ev.id == make_evidence_id("commit", commit_ev.source_ref)
    assert "payment: drop the gateway timeout" in commit_ev.content and "Alice Example" in commit_ev.content
    assert "-        response = gateway.call(request, TIMEOUT_MS);" in commit_ev.content  # the patch is real
    assert commit_ev.summary.startswith(f"Commit {repo.c2[:8]} (Alice Example, 2025-12-30)")


async def test_the_pinned_commit_and_the_budget_are_recorded(repo: Repo) -> None:
    code = FakeCode(checkout_for(repo, prefix="services/payment"))
    ctx, _ = context_with_code(code, investigation=tool_model(repo))

    final = await run_node(state_with(), ctx)

    assert (final["repo"].repo, final["repo"].branch, final["repo"].sha, final["repo"].path_prefix) == ("acme/shop", "main", repo.c2, "services/payment")
    # 1 to open + 3 seeding + 1 show_commit asked for + 2 excerpts read for the two pieces of evidence
    assert final["tool_calls"] == 1 + 3 + 1 + 2
    assert code.calls == [(ctx.tenant_id, "payment-service")]  # looked up by the incident's service, for the context's tenant


async def test_unresolved_questions_become_gaps(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo))
    final = await run_node(state_with(), ctx)
    assert final["gaps"] == ["Code investigation left open: Whether the gateway itself was slow"]


async def test_evidence_ids_are_stable_so_a_repeated_finding_is_one_piece_of_evidence(repo: Repo) -> None:
    twice = CodeFindingsAnswer(findings=[CodeFindingDraft(summary=f"first {i}", kind="suspect_code", locations=[CodeRef(path=PAYMENT, start_line=118, end_line=122)]) for i in range(2)])
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo, answer=twice))
    final = await run_node(state_with(), ctx)
    assert len(final["evidence"]) == 1  # the same file and lines: one id, kept once by the reducer
    again, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo, answer=twice))
    assert (await run_node(state_with(), again))["evidence"][0].id == final["evidence"][0].id


async def test_a_finding_falls_back_to_the_kind_it_can_cite(repo: Repo) -> None:
    answer = CodeFindingsAnswer(
        findings=[
            CodeFindingDraft(summary="a suspect with only a commit", kind="suspect_code", commits=[repo.c1]),
            CodeFindingDraft(summary="a recent change with only a place", kind="recent_change", locations=[CodeRef(path=PAYMENT, start_line=1, end_line=2)]),
        ]
    )
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo, answer=answer, script=[ai(call("show_commit", "c1", sha=repo.c1[:8])), ai(call("read_file", "c2", path=PAYMENT, start_line=1, end_line=2)), AIMessage(content="done")]))
    final = await run_node(state_with(), ctx)
    assert sorted(e.kind for e in final["evidence"]) == ["code", "commit"]


# -------------------------------------------------------------------------- gaps


async def test_without_code_access_configured_the_branch_records_a_gap(repo: Repo) -> None:
    ctx, _ = context_with_code(None)
    final = await run_node(state_with(), ctx)
    assert final["gaps"] == ["The code was not searched: code investigation is not available (this server has no repository access configured)"]
    assert final["repo"] is None and final["tool_calls"] == 0 and final["evidence"] == []


async def test_a_service_with_no_connected_repository_says_so(repo: Repo) -> None:
    ctx, models = context_with_code(FakeCode(None))
    final = await run_node(state_with(), ctx)
    assert final["gaps"] == ["The code was not examined: no repository is connected for the service 'payment-service' (connect one in the AI settings)"]
    assert final["repo"] is None and final["tool_calls"] == 1
    assert models.purposes == []  # no model was involved


async def test_a_repository_that_cannot_be_opened_is_a_gap_with_the_reason(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(CodeAccessError("the repository could not be fetched (git clone failed (128): not found)")))
    final = await run_node(state_with(), ctx)
    assert final["gaps"] == ["The code was not examined: the repository could not be fetched (git clone failed (128): not found)"]
    assert final["tool_calls"] == 1 and final["evidence"] == []


async def test_an_unexpected_error_opening_the_code_does_not_leak_its_text_or_end_the_run(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(RuntimeError("connection string postgresql://user:SECRET@db/x")))
    final = await run_node(state_with(), ctx)
    assert final["gaps"] == ["The code was not examined: the repository could not be opened"]
    assert "SECRET" not in str(final["gaps"])


async def test_findings_that_are_empty_are_reported_as_a_gap_not_as_evidence(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo, answer=CodeFindingsAnswer(findings=[])))
    final = await run_node(state_with(), ctx)
    assert final["evidence"] == []
    assert final["gaps"] == ["The code was examined but nothing in it was found relevant to this incident"]
    assert final["repo"] is not None  # the commit was still pinned: the code was read


async def test_a_model_that_is_down_leaves_a_gap_and_the_pinned_commit(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo, script=[RuntimeError("503")]))
    final = await run_node(state_with(), ctx)
    assert final["evidence"] == [] and any("could not be investigated" in g for g in final["gaps"])
    assert final["repo"].sha == repo.c2 and final["tool_calls"] == 1 + 3


async def test_a_state_without_an_incident_does_nothing(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    state = state_with()
    state["incident"] = None
    final = await run_node(state, ctx)
    assert final["gaps"] == [] and final["repo"] is None


# ------------------------------------------------------------------------- budget


async def test_the_code_investigation_gets_the_default_share_of_a_fresh_budget() -> None:
    limits, reason = plan_limits(new_state(investigation_id="i", incident_id="x", scope=InvestigationScope()), make_context())
    assert reason is None and limits is not None
    assert limits.max_tool_calls == MAX_CODE_CALLS and limits.max_seconds == 90.0


@pytest.mark.parametrize(("used", "expected"), [(3, 16), (13, 8), (17, 4), (18, None), (29, None)])
async def test_calls_already_spent_shrink_the_share(used: int, expected: int | None) -> None:
    state = new_state(investigation_id="i", incident_id="x", scope=InvestigationScope())
    state["tool_calls"] = used
    limits, reason = plan_limits(state, make_context())  # budget 30: minus used, minus 1 to open, minus 8 kept back
    assert (limits.max_tool_calls if limits else None) == expected
    assert (reason is None) == (expected is not None)
    assert reason is None or "tool-call budget" in reason


async def test_time_already_spent_shrinks_the_share_and_too_little_skips_it() -> None:
    state = new_state(investigation_id="i", incident_id="x", scope=InvestigationScope())
    late = dataclasses.replace(make_context(), started_at=time.monotonic() - 150)  # 300 - 150 - 120 reserved = 30 s
    limits, _ = plan_limits(state, late)
    assert limits is not None and 25 < limits.max_seconds <= 30
    too_late = dataclasses.replace(make_context(), started_at=time.monotonic() - 250)
    assert plan_limits(state, too_late) == (None, "time budget left was too small")
    custom = dataclasses.replace(make_context(), budget=Budget(max_wall_seconds=1000, max_tool_calls=100))
    limits, _ = plan_limits(state, custom)
    assert limits is not None and limits.max_tool_calls == 16 and limits.max_seconds == 90.0  # never more than the cap


async def test_with_too_little_budget_the_code_is_pinned_but_not_investigated(repo: Repo) -> None:
    ctx, models = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo))
    state = state_with(tool_calls=25)
    final = await run_node(state, ctx)
    assert final["gaps"] == ["The code was not examined because the tool-call budget left was too small"]
    assert final["repo"].sha == repo.c2 and final["evidence"] == [] and models.purposes == []


async def test_the_investigation_limits_follow_the_parents_budget(repo: Repo) -> None:
    model = tool_model(repo, script=[ai(*(call("list_files", f"g{i}") for i in range(30))), ai(*(call("list_files", f"h{i}") for i in range(30)))])
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=model)
    final = await run_node(state_with(tool_calls=13), ctx)  # budget 30: 30 - 13 - 1 - 8 = 8 calls for the investigator
    # 8 for the investigator (3 seeding + 5 asked), plus 1 to open, plus excerpts for the evidence it found
    assert final["tool_calls"] - 13 >= 1 + 8
    assert any("tool-call limit (8)" in g for g in final["gaps"])


# --------------------------------------------------------------------------- brief


async def test_the_brief_describes_the_incident_without_the_earlier_ai_summary(repo: Repo) -> None:
    model = tool_model(repo, script=[AIMessage(content="done")])
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=model)
    await run_node(state_with(question="Why did it start at noon?"), ctx)

    first_turn = model.tool_prompts[0]
    brief = str(first_turn[1].content)
    assert "payment-service (production)" in brief and "1500 occurrences" in brief
    assert "began at 2026-01-01T12:00Z" in brief and "Connection timeout after 30000ms" in brief
    assert "First assessment: A call to the gateway fails." in brief and "Why did it start at noon?" in brief
    assert "EARLIER MODEL GUESS" not in brief  # a previous model's guess must not anchor this analysis


async def test_the_brief_is_bounded() -> None:
    long = incident(top_messages=["x" * 900, "a", "b", "c", "d"])
    text = build_brief(long, state_with(question="q" * 900))
    assert text.count("\n- ") == 3 and "x" * 201 not in text and "q" * 301 not in text


# -------------------------------------------------------- recording and checkpointing


async def test_the_subgraphs_model_calls_are_recorded_with_the_rest_of_the_run(repo: Repo) -> None:
    from tests.test_code_investigator import (  # a real LangChain chat model, so callbacks fire
        ScriptedChat,
        ScriptedModels,
    )

    recorder = UsageRecorder()
    chat = ScriptedChat(script=[ai(call("list_files", "u1")), AIMessage(content="done")], answer=findings_answer(repo), calls=[], structured_calls=[], bound=[])
    ctx = dataclasses.replace(make_context(models=ScriptedModels(chat)), code=FakeCode(checkout_for(repo)))
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("gather_code", gather_code)
    builder.add_edge(START, "gather_code")
    builder.add_edge("gather_code", END)
    await builder.compile().ainvoke(state_with(), {"callbacks": [recorder]}, context=ctx)

    assert {"gather_code", "seed", "agent", "tools", "finalize"} <= {n.node for n in recorder.nodes}
    assert [c.node for c in recorder.calls] == ["agent", "agent"]  # both tool-calling turns of the investigator


async def test_the_subgraph_does_not_write_its_conversation_into_the_parents_checkpoint(repo: Repo) -> None:
    saver = InMemorySaver()
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo))
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("gather_code", gather_code)
    builder.add_edge(START, "gather_code")
    builder.add_edge("gather_code", END)
    graph = builder.compile(checkpointer=saver)

    await graph.ainvoke(state_with(), {"configurable": {"thread_id": "t1"}}, context=ctx)

    assert set(saver.storage["t1"]) == {""}  # one checkpoint namespace: the parent's; none for the subgraph


# ----------------------------------------------------------------- the whole graph


async def test_the_whole_graph_with_code_evidence_reaches_a_root_cause(repo: Repo) -> None:
    investigation = tool_model(repo)
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=investigation)
    state = new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope())

    updates = [u async for u in build_graph().astream(state, context=ctx, stream_mode="updates")]

    code_update = next(u["gather_code"] for u in updates if "gather_code" in u)
    assert sorted(e.kind for e in code_update["evidence"]) == ["code", "commit"]
    events = [e for u in updates if "gather_code" in u for e in events_from_update(u)]
    assert [(t, p["kind"]) for t, p in events if t == "evidence.added"] == [("evidence.added", "code"), ("evidence.added", "commit")]
    assert any(t == "gap.recorded" and "left open" in p["message"] for t, p in events)
    assert "gather_code" in {name for u in updates for name in u} and any("root_cause" in u for u in updates)
    # synthesize, verify and root_cause read the code evidence like any other
    assert "The gateway call has no timeout." in str(investigation.messages)


async def test_the_whole_graph_state_has_the_pinned_commit_and_a_shared_budget(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)), investigation=tool_model(repo))
    final = await build_graph().ainvoke(new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope()), context=ctx)

    assert final["repo"].sha == repo.c2
    assert len({e.id for e in final["evidence"]}) == len(final["evidence"])  # parallel branches never collide
    assert {e.kind for e in final["evidence"]} >= {"log", "timeline", "incident", "code", "commit"}
    # load_context 3, gather_logs 3, gather_history 1, gather_code: 1 to open + seeding 4 (the first frame: list, read,
    # commits; the second frame, Dispatcher.java, is not in the repository: one listing) + 1 show_commit + 2 excerpts
    assert final["tool_calls"] == 3 + 3 + 1 + (1 + 4 + 1 + 2)
    assert final["root_cause"] is not None and final["status"].value == "RUNNING"


async def test_a_run_without_the_code_branch_never_touches_the_code(repo: Repo) -> None:
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code, gather=["logs", "history"])
    final = await build_graph().ainvoke(new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope()), context=ctx)
    assert code.calls == [] and final["repo"] is None
    assert not any(e.kind in ("code", "commit") for e in final["evidence"])


async def test_the_context_has_no_code_access_unless_the_server_configures_one() -> None:
    assert make_context().code is None
