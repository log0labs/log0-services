"""validate_changes, finalize, the retry loop and the whole graph from approval to a validated diff."""

import dataclasses
import shutil
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.diff.builder import DiffResult, FileDiff
from app.graph import nodes
from app.graph.builder import build_graph
from app.graph.code_access import CodeAccessError
from app.graph.events import events_from_update
from app.graph.nodes.finalize import finalize
from app.graph.nodes.propose_changes import build_brief
from app.graph.nodes.validate_changes import MAX_ERRORS_SHOWN, validate_changes
from app.graph.result import result_for_row
from app.graph.routing import MAX_CHANGE_ATTEMPTS, after_propose, after_validate
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.schemas.change_proposal import ChangeProposalAnswer, CodeChangeDraft
from app.schemas.code_findings import CodeFindingDraft, CodeFindingsAnswer, CodeRef
from app.schemas.domain import CodeChange
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
from tests.test_propose_changes import approved_state

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("validate") / "repo")


GOOD = CodeChange(file_path=PAYMENT, operation="modify", explanation="Pass the timeout again.", old_code=OLD, new_code=NEW)
WRONG = CodeChange(file_path=PAYMENT, operation="modify", explanation="Pass the timeout.", old_code="response = gateway.call(request)  // no timeout", new_code=NEW)
SLOPPY = CodeChange(file_path=PAYMENT, operation="modify", explanation="x", old_code="response = gateway.call(request);   // no timeout set", new_code=NEW)


def proposed(source: Repo, changes: list[CodeChange], **over: Any) -> InvestigationState:
    fields: dict[str, Any] = {"proposed_changes": changes, "read_paths": [PAYMENT], "change_attempts": 1} | over
    return approved_state(source, **fields)


async def run_node(node: Any, name: str, state: InvestigationState, ctx: InvestigationContext) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node(name, node)
    builder.add_edge(START, name)
    builder.add_edge(name, END)
    return await builder.compile().ainvoke(state, context=ctx)


# ------------------------------------------------------------------ validate_changes


async def test_valid_changes_become_a_diff_that_git_accepts(repo: Repo) -> None:
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code)

    final = await run_node(validate_changes, "validate_changes", proposed(repo, [GOOD]), ctx)

    assert final["validation_errors"] == [] and final["diff"] is not None
    assert f"diff --git a/{PAYMENT} b/{PAYMENT}" in final["diff"] and f"+{NEW}" in final["diff"] and f"-{OLD}" in final["diff"]
    assert [(f.file_path, f.operation, f.additions, f.deletions) for f in final["diff_files"]] == [(PAYMENT, "modify", 1, 1)]
    assert final["tool_calls"] == 10 + 2  # the snapshot and git apply --check
    assert code.snapshot_calls == [(ctx.tenant_id, "payment-service", repo.c2)] and code.snapshots_open == 0
    assert final["proposed_changes"] == [GOOD]  # validation does not rewrite the proposal


async def test_a_wrong_old_code_is_reported_in_words_the_proposer_can_act_on(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    final = await run_node(validate_changes, "validate_changes", proposed(repo, [WRONG]), ctx)

    assert final["diff"] is None and final["diff_files"] == []
    (error,) = final["validation_errors"]
    assert error.startswith(f"change 0 ({PAYMENT}) [old_code_missing]:") and "copy it exactly" in error
    assert final["tool_calls"] == 10 + 1  # no diff was built, so no git apply


async def test_a_whitespace_slip_comes_back_with_the_text_the_file_really_has(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    final = await run_node(validate_changes, "validate_changes", proposed(repo, [SLOPPY]), ctx)
    (error,) = final["validation_errors"]
    assert "[whitespace_mismatch]" in error and "The file actually has:" in error and "// no timeout set" in error


async def test_a_file_the_proposer_never_read_is_refused(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    final = await run_node(validate_changes, "validate_changes", proposed(repo, [GOOD], read_paths=["services/payment/other.py"]), ctx)
    assert "[not_read]" in final["validation_errors"][0] and final["diff"] is None


async def test_only_the_first_few_problems_are_reported(repo: Repo) -> None:
    changes = [WRONG.model_copy(update={"old_code": f"nothing like this {i}"}) for i in range(MAX_ERRORS_SHOWN + 4)]
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    final = await run_node(validate_changes, "validate_changes", proposed(repo, changes), ctx)
    assert len(final["validation_errors"]) == MAX_ERRORS_SHOWN


async def test_a_diff_that_git_refuses_is_a_validation_error(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(edits: Any, root: Any, *, git_binary: str = "git") -> DiffResult:
        return DiffResult(diff="x", files=[], applies=False, error="error: patch failed: app.py:3")

    monkeypatch.setattr(nodes.validate_changes, "build_checked_diff", refused)
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    final = await run_node(validate_changes, "validate_changes", proposed(repo, [GOOD]), ctx)
    assert final["diff"] is None and final["validation_errors"] == ["the diff built from the changes does not apply to the pinned commit: error: patch failed: app.py:3"]


async def test_infrastructure_trouble_is_a_gap_not_something_to_retry_the_model_for(repo: Repo) -> None:
    for result, expected in [
        (CodeAccessError("the pinned commit is no longer available (x)"), "could not be validated: the pinned commit is no longer available (x)"),
        (RuntimeError("path C:\\secret\\leak-777"), "could not be validated (the code at the pinned commit could not be read)"),
        (None, "the repository is no longer connected"),
    ]:
        ctx, _ = context_with_code(FakeCode(result))
        final = await run_node(validate_changes, "validate_changes", proposed(repo, [GOOD]), ctx)
        assert expected in final["gaps"][0] and "leak-777" not in str(final["gaps"])
        assert final["validation_errors"] == [] and final["diff"] is None
        assert after_validate(final) == "finalize"  # no loop back: the model is not at fault


async def test_nothing_to_validate_does_nothing(repo: Repo) -> None:
    ctx, _ = context_with_code(FakeCode(checkout_for(repo)))
    assert (await run_node(validate_changes, "validate_changes", proposed(repo, []), ctx))["diff"] is None
    ctx, _ = context_with_code(None)
    assert (await run_node(validate_changes, "validate_changes", proposed(repo, [GOOD]), ctx))["diff"] is None


# --------------------------------------------------------------------------- routing


def test_routing_after_propose_and_after_validate() -> None:
    state = new_state(investigation_id="i", incident_id="x", scope=InvestigationScope())
    assert after_propose(state) == "finalize"  # nothing proposed
    state["proposed_changes"] = [GOOD]
    assert after_propose(state) == "validate_changes"

    assert after_validate(state) == "finalize"  # no errors: done
    state["validation_errors"] = ["change 0: bad"]
    for attempts, expected in [(1, "propose_changes"), (MAX_CHANGE_ATTEMPTS - 1, "propose_changes"), (MAX_CHANGE_ATTEMPTS, "finalize"), (5, "finalize")]:
        state["change_attempts"] = attempts
        assert after_validate(state) == expected
    assert MAX_CHANGE_ATTEMPTS == 2


# --------------------------------------------------------------------------- finalize


async def test_finalize_attaches_validated_changes_to_the_recommendation(repo: Repo) -> None:
    ctx, _ = context_with_code(None)
    state = proposed(repo, [GOOD], diff="diff --git a/x b/x\n")
    final = await run_node(finalize, "finalize", state, ctx)
    assert final["recommendation"].changes == [GOOD] and final["recommendation"].summary == state["recommendation"].summary
    assert state["recommendation"].changes == []  # the original object was not mutated


async def test_finalize_explains_changes_that_never_validated(repo: Repo) -> None:
    ctx, _ = context_with_code(None)
    state = proposed(repo, [WRONG], validation_errors=["change 0: a", "change 1: b", "change 2: c", "change 3: d"], change_attempts=2)
    final = await run_node(finalize, "finalize", state, ctx)
    (gap,) = final["gaps"]
    assert "could not be validated after 2 attempt(s), so no fix is offered: change 0: a; change 1: b; change 2: c" in gap and "change 3" not in gap
    assert final["proposed_changes"] == [] and final["diff"] is None and final["recommendation"].changes == []  # nothing half-proposed is kept


async def test_finalize_without_a_proposal_changes_nothing(repo: Repo) -> None:
    ctx, _ = context_with_code(None)
    state = approved_state(repo)
    assert (await run_node(finalize, "finalize", state, ctx))["gaps"] == state["gaps"]
    assert (await run_node(finalize, "finalize", approved_state(repo, recommendation=None, diff="d", proposed_changes=[GOOD]), ctx))["recommendation"] is None


# ----------------------------------------------------------------- the row and events


async def test_the_diff_and_the_recommendation_changes_go_to_the_row(repo: Repo) -> None:
    state = proposed(repo, [GOOD], diff="diff --git a/x b/x\n")
    state["recommendation"] = state["recommendation"].model_copy(update={"changes": [GOOD]})  # type: ignore[union-attr]
    row = result_for_row(state)
    assert row["diff"] == "diff --git a/x b/x\n"
    assert row["recommendation"]["changes"][0]["oldCode"] == OLD and row["recommendation"]["changes"][0]["filePath"] == PAYMENT
    assert "diff" not in result_for_row(approved_state(repo))  # no validated diff, no column


async def test_validation_events_describe_the_outcome_without_the_code() -> None:
    files = [FileDiff(file_path=PAYMENT, operation="modify", additions=1, deletions=1)]
    ok = events_from_update({"validate_changes": {"validation_errors": [], "diff": "diff --git SECRET_TEXT\n", "diff_files": files, "tool_calls": 2}})
    assert ok == [
        ("changes.validated", {"ok": True, "issueCount": 0}),
        ("diff.generated", {"bytes": len("diff --git SECRET_TEXT\n"), "files": [{"path": PAYMENT, "operation": "modify", "additions": 1, "deletions": 1}]}),
    ]
    assert "SECRET_TEXT" not in str(ok)
    bad = events_from_update({"validate_changes": {"validation_errors": ["change 0: x", "change 1: y"], "diff": None, "diff_files": []}})
    assert bad == [("changes.validated", {"ok": False, "issueCount": 2})]
    assert events_from_update({"validate_changes": {"gaps": ["infrastructure"]}}) == [("gap.recorded", {"message": "infrastructure"})]


# --------------------------------------------------------------------- the retry brief


async def test_a_retry_brief_quotes_the_problems_and_the_previous_attempt(repo: Repo) -> None:
    state = proposed(repo, [WRONG], validation_errors=[f"change 0 ({PAYMENT}) [old_code_missing]: copy it exactly"])
    brief = build_brief(state)
    assert "checked against the code and rejected" in brief and "[old_code_missing]" in brief and "previous modify of" in brief
    assert "// no timeout" in brief
    assert "rejected" not in build_brief(proposed(repo, [GOOD]))  # the first attempt has no such section


# -------------------------------------------------------------------- the whole graph


def findings() -> CodeFindingsAnswer:
    return CodeFindingsAnswer(findings=[CodeFindingDraft(summary="The gateway call has no timeout.", kind="suspect_code", locations=[CodeRef(path=PAYMENT, start_line=118, end_line=122)])])


def proposal(change: CodeChange) -> ChangeProposalAnswer:
    return ChangeProposalAnswer(changes=[CodeChangeDraft(file_path=change.file_path, operation=change.operation, explanation=change.explanation, old_code=change.old_code, new_code=change.new_code)])


def graph_with(repo: Repo, proposals: list[ChangeProposalAnswer]) -> tuple[Any, InvestigationContext, FakeCode, ToolFakeModel, Any]:
    model = ToolFakeModel(
        synthesis_answer(),
        script=[ai(call("list_files", "c1")), AIMessage(content="investigated")],
        answers={CodeFindingsAnswer: findings()},
        sequences={ChangeProposalAnswer: proposals},
    )
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code, investigation=model)
    return build_graph(InMemorySaver()), dataclasses.replace(ctx, require_review=True), code, model, {"configurable": {"thread_id": "t"}}


def start() -> InvestigationState:
    return new_state(investigation_id="inv", incident_id=str(INCIDENT_ID), scope=InvestigationScope())


async def approve(graph: Any, config: Any, ctx: InvestigationContext) -> list[dict[str, Any]]:
    await graph.ainvoke(start(), config, context=ctx)
    return [u async for u in graph.astream(Command(resume={"action": "approve", "note": None}), config, context=ctx, stream_mode="updates")]


async def test_a_good_first_proposal_is_validated_and_ends_with_a_diff(repo: Repo) -> None:
    graph, ctx, _, _, config = graph_with(repo, [proposal(GOOD)])
    updates = await approve(graph, config, ctx)
    final = (await graph.aget_state(config)).values

    assert final["change_attempts"] == 1 and final["validation_errors"] == [] and final["diff"]
    assert final["recommendation"].changes == [GOOD]
    assert [next(iter(u)) for u in updates if next(iter(u)) in ("propose_changes", "validate_changes", "finalize")] == ["propose_changes", "validate_changes", "finalize"]
    events = [e for u in updates for e in events_from_update(u)]
    assert [t for t, _ in events if t in ("changes.proposed", "changes.validated", "diff.generated")] == ["changes.proposed", "changes.validated", "diff.generated"]
    assert result_for_row(final)["diff"] == final["diff"] and not any("could not be validated" in g for g in final["gaps"])


async def test_a_rejected_proposal_is_retried_with_the_errors_and_the_second_attempt_wins(repo: Repo) -> None:
    graph, ctx, code, model, config = graph_with(repo, [proposal(WRONG), proposal(GOOD)])
    updates = await approve(graph, config, ctx)
    final = (await graph.aget_state(config)).values

    assert final["change_attempts"] == 2 and final["diff"] and final["validation_errors"] == [] and final["recommendation"].changes == [GOOD]
    order = [next(iter(u)) for u in updates if next(iter(u)) in ("propose_changes", "validate_changes", "finalize")]
    assert order == ["propose_changes", "validate_changes", "propose_changes", "validate_changes", "finalize"]
    second_attempt_prompt = str([call for call in model.tool_prompts[-1]])
    assert "[old_code_missing]" in second_attempt_prompt and "checked against the code and rejected" in second_attempt_prompt  # the errors reached the model
    validated = [p for u in updates for t, p in events_from_update(u) if t == "changes.validated"]
    assert validated == [{"ok": False, "issueCount": 1}, {"ok": True, "issueCount": 0}]
    assert len(code.snapshot_calls) == 4 and code.snapshots_open == 0  # propose and validate each, twice, all released


async def test_two_rejected_proposals_end_with_an_explanation_and_no_third_attempt(repo: Repo) -> None:
    graph, ctx, _, _, config = graph_with(repo, [proposal(WRONG), proposal(SLOPPY)])
    await approve(graph, config, ctx)
    final = (await graph.aget_state(config)).values

    assert final["change_attempts"] == 2 and final["diff"] is None and final["proposed_changes"] == []
    assert final["recommendation"].changes == [] and final["recommendation"].summary  # the advice is still there
    assert any("could not be validated after 2 attempt(s), so no fix is offered" in g for g in final["gaps"])
    assert "diff" not in result_for_row(final)
    assert (await graph.aget_state(config)).next == ()


async def test_an_engineer_who_stops_gets_no_validation(repo: Repo) -> None:
    graph, ctx, code, _, config = graph_with(repo, [proposal(GOOD)])
    await graph.ainvoke(start(), config, context=ctx)
    final = await graph.ainvoke(Command(resume={"action": "stop", "note": None}), config, context=ctx)
    assert final["diff"] is None and final["change_attempts"] == 0 and code.snapshot_calls == []
