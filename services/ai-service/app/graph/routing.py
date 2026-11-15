"""Routing functions: the decisions between nodes, as plain testable Python.

A routing function reads the state and returns where to go next. Keeping them out of the
builder means each rule has a name, a docstring and a unit test.
"""

from typing import Literal

from langgraph.graph import END
from langgraph.types import Send

from app.graph.state import InvestigationState
from app.schemas.domain import GatherBranch
from app.schemas.investigation import InvestigationStatus

# Which node implements each triage branch. M3/M5 change the values here, nothing else.
BRANCH_NODES: dict[GatherBranch, str] = {
    "logs": "gather_logs",
    "history": "gather_history",
    "code": "gather_code",
    "runbooks": "search_runbooks",
}


def after_load_context(state: InvestigationState) -> Literal["triage", "__end__"]:
    """Stop right after load_context when it could not read the incident: nothing to triage."""
    if state["status"] == InvestigationStatus.FAILED or state["incident"] is None:
        return END
    return "triage"


def after_verify(state: InvestigationState) -> Literal["gather_followup", "root_cause"]:
    """Loop back for more evidence only when verify left searches to run, otherwise conclude.
    Verify already checked the budget before asking, so this is a plain read of ``follow_ups``."""
    return "gather_followup" if state["follow_ups"] else "root_cause"


def after_review(state: InvestigationState) -> Literal["synthesize", "propose_changes", "__end__"]:
    """What the engineer decided at the review gate (or nothing: the gate was off or had no root cause).

    refine goes back to ``synthesize`` for another round; approve goes on to propose a fix (a
    placeholder until step 4.2); stop, or no decision at all, ends the investigation.
    """
    review = state["review"]
    if review is None or review.action == "stop":
        return END
    return "synthesize" if review.action == "refine" else "propose_changes"


def after_propose(state: InvestigationState) -> Literal["validate_changes", "finalize"]:
    """Validate what was proposed; with nothing proposed there is nothing to validate."""
    return "validate_changes" if state["proposed_changes"] else "finalize"


MAX_CHANGE_ATTEMPTS = 2


def after_validate(state: InvestigationState) -> Literal["propose_changes", "offer_pr", "finalize"]:
    """Failed validation goes back to the proposer with the errors, at most ``MAX_CHANGE_ATTEMPTS`` times in all;
    a valid diff goes to ``offer_pr`` (which lets it pass when log0 may not write to the repository);
    the last failed attempt, or nothing to publish, goes on to finalize."""
    if state["validation_errors"]:
        return "propose_changes" if state["change_attempts"] < MAX_CHANGE_ATTEMPTS else "finalize"
    return "offer_pr" if state["diff"] else "finalize"


def after_offer(state: InvestigationState) -> Literal["approve_pr", "finalize"]:
    """Ask the engineer only when there is an offer to make."""
    return "approve_pr" if state["pr_offer"] is not None else "finalize"


def after_pr_approval(state: InvestigationState) -> Literal["create_branch", "finalize"]:
    """Only an explicit approval from an engineer leads to a write."""
    decision = state["pr_decision"]
    return "create_branch" if decision is not None and decision.action == "approve" else "finalize"


def after_create_branch(state: InvestigationState) -> Literal["apply_patch", "finalize"]:
    return "apply_patch" if state["branch"] is not None else "finalize"


def after_apply_patch(state: InvestigationState) -> Literal["open_draft_pr", "finalize"]:
    return "open_draft_pr" if state["commit_sha"] is not None else "finalize"


def fan_out(state: InvestigationState) -> list[Send]:
    """One ``Send`` per branch triage chose, so the branches run in parallel.

    Returning an empty list (triage chose nothing, e.g. every source is off in the scope)
    ends the run here.
    """
    triage = state["triage"]
    if triage is None:
        return []
    return [Send(BRANCH_NODES[branch], state) for branch in triage.gather]
