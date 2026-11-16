"""The last node: put the validated changes where the result is read, or say why there are none.

Everything the run produced is already in the state; this node only assembles it:

* a validated diff (``diff`` set): the changes become part of the ``Recommendation``
  (``recommendation.changes``), so the stored recommendation, the API and the console show the fix
  next to the explanation. The diff itself is stored in its own column by the executor,
* changes that were proposed but never validated: a gap says so, with the first problems, and the
  recommendation stays as it was (the engineer still gets the root cause and the advice),
* nothing proposed (the gate was off, the engineer stopped, no fix could be made): nothing to do.

It makes no model call and cannot fail the run. Persisting the result, the token and cost totals and
the closing event are the executor's and the runner's job (they have been since 2.14).
"""

from typing import Any

from langgraph.runtime import Runtime

from app.graph.state import InvestigationContext, InvestigationState

MAX_ERRORS_IN_GAP = 3


async def finalize(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    if state["diff"]:
        recommendation = state["recommendation"]
        if recommendation is None:
            return {}
        return {"recommendation": recommendation.model_copy(update={"changes": list(state["proposed_changes"])})}
    if state["validation_errors"]:
        shown = "; ".join(state["validation_errors"][:MAX_ERRORS_IN_GAP])
        return {
            "gaps": [f"The proposed changes could not be validated after {state['change_attempts']} attempt(s), so no fix is offered: {shown}"],
            "proposed_changes": [],
        }
    return {}
