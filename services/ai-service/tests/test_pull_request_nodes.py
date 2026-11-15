"""The V2 nodes (offer_pr, approve_pr, create_branch, apply_patch, open_draft_pr) and their routing, with a fake publisher."""

import dataclasses
import shutil
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graph.nodes.pull_request import (
    apply_patch,
    approve_pr,
    create_branch,
    offer_pr,
    open_draft_pr,
    read_pr_decision,
    run_tests,
)
from app.graph.routing import (
    after_apply_patch,
    after_create_branch,
    after_offer,
    after_pr_approval,
    after_validate,
)
from app.graph.state import InvestigationContext, InvestigationState
from app.publishing.port import PublishError
from app.schemas.domain import BranchRef, PrDecision
from tests.code_graph_support import PAYMENT, FakeCode, Repo
from tests.pull_request_support import (
    APPROVER,
    COMMIT_SHA,
    GOOD,
    FakePublisher,
    reviewed_state,
)
from tests.test_change_proposer import NEW, OLD

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]

APPROVE = {"action": "approve", "decided_by": str(APPROVER)}


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("pull-request") / "repo")


def pr_graph() -> Any:
    """offer_pr -> approve_pr -> create_branch -> apply_patch -> open_draft_pr, wired as in the real graph, with a checkpointer."""
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    for name, node in [("offer_pr", offer_pr), ("approve_pr", approve_pr), ("create_branch", create_branch), ("apply_patch", apply_patch), ("run_tests", run_tests), ("open_draft_pr", open_draft_pr)]:
        builder.add_node(name, node)
    builder.add_edge(START, "offer_pr")
    builder.add_conditional_edges("offer_pr", after_offer, {"approve_pr": "approve_pr", "finalize": END})
    builder.add_conditional_edges("approve_pr", after_pr_approval, {"create_branch": "create_branch", "finalize": END})
    builder.add_conditional_edges("create_branch", after_create_branch, {"apply_patch": "apply_patch", "finalize": END})
    builder.add_conditional_edges("apply_patch", after_apply_patch, {"run_tests": "run_tests", "finalize": END})
    builder.add_edge("run_tests", "open_draft_pr")
    builder.add_edge("open_draft_pr", END)
    return builder.compile(checkpointer=InMemorySaver())


async def start(repo: Repo, publisher: FakePublisher | None = None, thread: str = "t") -> tuple[Any, dict, InvestigationContext, FakePublisher, FakeCode, InvestigationState]:
    publisher = publisher or FakePublisher()
    state, ctx, code = await reviewed_state(repo, publisher)
    graph = pr_graph()
    config = {"configurable": {"thread_id": thread}}
    await graph.ainvoke(state, config, context=ctx)
    return graph, config, ctx, publisher, code, state


# ------------------------------------------------------------------------- the gate


async def test_a_valid_diff_parks_the_run_at_the_gate_and_shows_where_and_what(repo: Repo) -> None:
    graph, config, _, publisher, _, state = await start(repo)

    snapshot = await graph.aget_state(config)
    (waiting,) = [i for task in snapshot.tasks for i in task.interrupts]
    request = waiting.value

    assert request["gate"] == "pr_approval" and request["repository"] == "acme/shop" and request["defaultBranch"] == "main"
    assert request["branch"].startswith("log0/fix-") and request["baseSha"] == state["repo"].sha
    assert request["files"] == [{"path": PAYMENT, "operation": "modify", "additions": 1, "deletions": 1, "inRootCause": True}]
    assert publisher.names() == ["target_for", "activity"]  # nothing was written while waiting


@pytest.mark.parametrize("why", ["no_publisher", "writes_off", "no_diff"])
async def test_the_gate_lets_the_run_through_when_a_pull_request_cannot_be_offered(repo: Repo, why: str) -> None:
    publisher = FakePublisher(allowed=why != "writes_off")
    state, ctx, _ = await reviewed_state(repo, publisher)
    if why == "no_publisher":
        ctx = dataclasses.replace(ctx, publisher=None)
    if why == "no_diff":
        state = state | {"diff": None}

    final = await pr_graph().ainvoke(state, {"configurable": {"thread_id": why}}, context=ctx)

    assert "__interrupt__" not in final and final["pr_offer"] is None and final["pr_decision"] is None and final["branch"] is None
    assert set(publisher.names()) <= {"target_for"}


# ---------------------------------------------------------------------- the writes


async def test_an_approval_creates_the_branch_commits_the_reviewed_change_and_opens_a_draft(repo: Repo) -> None:
    graph, config, ctx, publisher, code, state = await start(repo)

    final = await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    assert publisher.names().count("create_branch") == publisher.names().count("commit") == publisher.names().count("open_pull_request") == 1
    assert publisher.arguments("create_branch") == {"approved_by": APPROVER, "base_sha": state["repo"].sha}
    (file,) = publisher.arguments("commit")["files"]
    assert file.path == PAYMENT and file.content is not None and NEW in file.content and OLD not in file.content and file.executable is False
    assert "log0-investigation: " + state["investigation_id"] in publisher.arguments("commit")["message"]
    assert final["branch"].name.startswith("log0/fix-") and final["commit_sha"] == COMMIT_SHA
    assert final["pull_request"].number == 12 and final["pull_request"].draft is True
    assert code.snapshots_open == 0
    assert final["tool_calls"] == state["tool_calls"] + 1 + 2 + 1


async def test_the_pull_request_text_carries_the_root_cause_and_files_and_no_secret(repo: Repo) -> None:
    graph, config, ctx, publisher, _, state = await start(repo)
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    state_with_secret = state["root_cause"].model_copy(update={"statement": f"The gateway call has no timeout (token {secret})."})
    # the same state, but the root cause text now contains a secret that must never be published
    await graph.aupdate_state(config, {"root_cause": state_with_secret})

    await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    opened = publisher.arguments("open_pull_request")
    assert opened["title"].startswith("Fix: The gateway call has no timeout")
    assert "## Root cause" in opened["body"] and f"`{PAYMENT}`" in opened["body"] and "never merges" in opened["body"]
    assert secret not in opened["title"] + opened["body"] + publisher.arguments("commit")["message"]


async def test_a_decline_writes_nothing(repo: Repo) -> None:
    graph, config, ctx, publisher, _, _ = await start(repo)

    final = await graph.ainvoke(Command(resume={"action": "decline"}), config, context=ctx)

    assert final["pr_decision"].action == "decline" and final["branch"] is None and final["pull_request"] is None
    assert publisher.names() == ["target_for", "activity"]  # the offer was decided once, before waiting


@pytest.mark.parametrize("answer", [{"action": "approve"}, {"action": "approve", "decided_by": "not-a-uuid"}, {"action": "maybe"}, "yes"])
async def test_an_answer_that_is_not_a_clear_decision_is_a_decline_with_a_gap(repo: Repo, answer: Any) -> None:
    graph, config, ctx, publisher, _, _ = await start(repo)

    final = await graph.ainvoke(Command(resume=answer), config, context=ctx)

    assert final["pr_decision"].action == "decline" and final["pull_request"] is None
    assert any("could not be read" in gap for gap in final["gaps"])
    assert "create_branch" not in publisher.names()


async def test_an_approval_needs_a_user_id_so_it_can_be_audited() -> None:
    assert read_pr_decision({"action": "approve", "decided_by": str(APPROVER)}) == PrDecision(action="approve", decided_by=str(APPROVER))
    assert read_pr_decision({"action": "approve"}) is None
    assert read_pr_decision({"action": "decline"}) == PrDecision(action="decline")


@pytest.mark.parametrize("step", ["create_branch", "commit", "open_pull_request"])
async def test_a_failed_write_is_a_gap_and_stops_the_chain_without_failing_the_run(repo: Repo, step: str) -> None:
    publisher = FakePublisher()
    publisher.fail[step] = PublishError("GitHub could not be reached")
    graph, config, ctx, publisher, _, _ = await start(repo, publisher, thread=step)

    final = await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    assert final["pull_request"] is None
    assert any("No pull request was opened: GitHub could not be reached" in gap for gap in final["gaps"])
    later = {"create_branch": ["commit", "open_pull_request"], "commit": ["open_pull_request"], "open_pull_request": []}[step]
    assert not set(later) & set(publisher.names())


async def test_writing_turned_off_after_approval_stops_before_any_write(repo: Repo) -> None:
    graph, config, ctx, publisher, _, _ = await start(repo)
    publisher.allowed = False  # the admin switched the repository off while the engineer decided

    final = await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    assert "create_branch" not in publisher.names() and final["pull_request"] is None
    assert any("no longer allowed" in gap for gap in final["gaps"])


async def test_what_is_committed_must_equal_the_diff_that_was_reviewed(repo: Repo) -> None:
    graph, config, ctx, publisher, _, _ = await start(repo)
    await graph.aupdate_state(config, {"diff": "diff --git a/other b/other\n--- a/other\n+++ b/other\n@@ -1 +1 @@\n-a\n+b\n"})

    final = await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    assert "commit" not in publisher.names() and final["pull_request"] is None
    assert any("differs from the reviewed diff" in gap for gap in final["gaps"])


async def test_a_change_that_no_longer_matches_the_code_is_not_committed(repo: Repo) -> None:
    graph, config, ctx, publisher, _, _ = await start(repo)
    stale = GOOD.model_copy(update={"old_code": "this text is not in the file"})
    await graph.aupdate_state(config, {"proposed_changes": [stale]})

    final = await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    assert "commit" not in publisher.names()
    assert any("no longer validates" in gap for gap in final["gaps"])


async def test_the_repository_disappearing_is_a_gap(repo: Repo) -> None:
    graph, config, ctx, publisher, code, _ = await start(repo)
    code.result = None

    final = await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)

    assert "commit" not in publisher.names() and any("no longer connected" in gap for gap in final["gaps"])


# ------------------------------------------------------------------ idempotency


async def test_nodes_that_already_did_their_work_do_not_repeat_it(repo: Repo) -> None:
    graph, config, ctx, publisher, _, _ = await start(repo)
    await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)
    done = await graph.aget_state(config)
    values = done.values
    calls_before = list(publisher.names())

    for node in (create_branch, apply_patch, open_draft_pr):
        runtime = type("R", (), {"context": ctx})()
        assert await node(values, runtime) == {}

    assert publisher.names() == calls_before


async def test_a_run_that_stopped_after_the_branch_continues_with_the_commit(repo: Repo) -> None:
    publisher = FakePublisher()
    publisher.fail["commit"] = PublishError("GitHub could not be reached")
    graph, config, ctx, publisher, _, _ = await start(repo, publisher, thread="resume")
    await graph.ainvoke(Command(resume=APPROVE), config, context=ctx)
    state = (await graph.aget_state(config)).values
    assert state["branch"] is not None and state["commit_sha"] is None

    publisher.fail.clear()
    runtime = type("R", (), {"context": ctx})()
    after_commit = await apply_patch(state, runtime)
    assert after_commit["commit_sha"] == COMMIT_SHA
    assert publisher.names().count("create_branch") == 1  # the branch was not created again


# ---------------------------------------------------------------------- routing


async def test_routing_sends_only_a_valid_diff_to_the_gate_and_only_an_approval_to_a_write(repo: Repo) -> None:
    state, _, _ = await reviewed_state(repo)

    assert after_validate(state) == "offer_pr"
    assert after_offer(state | {"pr_offer": {"gate": "pr_approval"}}) == "approve_pr"
    assert after_offer(state | {"pr_offer": None}) == "finalize"
    assert after_validate(state | {"diff": None}) == "finalize"
    assert after_validate(state | {"diff": None, "validation_errors": ["x"], "change_attempts": 1}) == "propose_changes"
    assert after_validate(state | {"diff": None, "validation_errors": ["x"], "change_attempts": 2}) == "finalize"
    assert after_pr_approval(state | {"pr_decision": PrDecision(action="approve", decided_by=str(APPROVER))}) == "create_branch"
    assert after_pr_approval(state | {"pr_decision": PrDecision(action="decline")}) == "finalize"
    assert after_pr_approval(state | {"pr_decision": None}) == "finalize"
    assert after_create_branch(state | {"branch": BranchRef(name="log0/x", base_sha="a")}) == "apply_patch"
    assert after_create_branch(state) == "finalize"
    assert after_apply_patch(state | {"commit_sha": COMMIT_SHA}) == "run_tests"
    assert after_apply_patch(state) == "finalize"


async def test_the_offer_marks_a_file_the_root_cause_does_not_name(repo: Repo) -> None:
    from app.diff.builder import FileDiff

    state, ctx, _ = await reviewed_state(repo, FakePublisher())
    extra = FileDiff(file_path="services/payment/src/main/java/com/log0/payment/Seed.java", operation="modify", additions=1, deletions=0)
    state = state | {"diff_files": [*state["diff_files"], extra]}

    offer = (await offer_pr(state, type("R", (), {"context": ctx})()))["pr_offer"]

    assert {f["path"].rsplit("/", 1)[-1]: f["inRootCause"] for f in offer["files"]} == {"PaymentProcessor.java": True, "Seed.java": False}
