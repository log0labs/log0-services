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


def fan_out(state: InvestigationState) -> list[Send]:
    """One ``Send`` per branch triage chose, so the branches run in parallel.

    Returning an empty list (triage chose nothing, e.g. every source is off in the scope)
    ends the run here.
    """
    triage = state["triage"]
    if triage is None:
        return []
    return [Send(BRANCH_NODES[branch], state) for branch in triage.gather]
