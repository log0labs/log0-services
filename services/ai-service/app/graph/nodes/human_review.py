"""The review gate: after the root cause, the investigation waits for an engineer.

This is LangGraph's *human in the loop*. The node calls ``interrupt(payload)``: the graph stops
right there, its whole state is already in the checkpoint, and the run ends without finishing. Hours
later (or after a restart) the engineer's decision arrives and the graph is resumed from that
checkpoint with ``Command(resume=decision)``; ``interrupt()`` then *returns* the decision and the node
carries on. Nothing is held in memory while it waits: no task, no connection, no concurrency slot.

Three decisions (``ReviewDecision``):

* **approve**: the root cause is right; move on (to proposing a fix, from step 4.2),
* **refine** + a note: look again with the engineer's guidance; the graph goes back to
  ``synthesize`` and runs hypotheses, verification and root cause once more, then asks again.
  At most ``MAX_REFINES`` times,
* **stop**: enough; end the investigation with what it found.

The gate is **off unless the server turns it on** (``INVESTIGATION_REVIEW_GATE``): an investigation
that waits for a human is only useful where a human can answer (the console review panel is
step 4.10), and a waiting investigation blocks a new one for the same incident. A run without a
root cause is never held either: there is nothing to review.

LangGraph re-runs a node from its first line when it resumes, so everything before
``interrupt()`` must be safe to repeat. Here it only reads the state.
"""

import logging
from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from app.graph.state import InvestigationContext, InvestigationState
from app.schemas.domain import ReviewDecision

logger = logging.getLogger(__name__)

MAX_REFINES = 2
MAX_TEXT = 600
MAX_ITEMS = 5


def review_request(state: InvestigationState) -> dict[str, Any]:
    """What the engineer is shown while the investigation waits: plain JSON, no evidence content.

    It is stored in the checkpoint and copied (capped) into the ``review.requested`` event.
    """
    cause = state["root_cause"]
    recommendation = state["recommendation"]
    assert cause is not None
    return {
        "round": state["review_round"] + 1,
        "refinesLeft": max(0, MAX_REFINES - state["review_round"]),
        "rootCause": cause.statement[:MAX_TEXT],
        "confidenceBand": state["confidence_band"],
        "evidenceIds": list(cause.evidence_ids),
        "contributingFactors": [f[:MAX_TEXT] for f in cause.contributing_factors[:MAX_ITEMS]],
        "unresolvedQuestions": [q[:MAX_TEXT] for q in cause.unresolved_questions[:MAX_ITEMS]],
        "recommendation": recommendation.summary[:MAX_TEXT] if recommendation else None,
        "gapCount": len(state["gaps"]),
        "pinnedCommit": state["repo"].sha if state["repo"] else None,
    }


def read_decision(raw: Any) -> ReviewDecision | None:
    """The resume value as a decision, or ``None`` when it is not one."""
    try:
        decision = ReviewDecision.model_validate(raw)
    except ValidationError:
        return None
    if decision.action == "refine" and not (decision.note or "").strip():
        return None
    return decision


async def human_review(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Pause for the engineer's decision (when the gate is on and there is a root cause)."""
    if state["root_cause"] is None:
        return {"review": None}  # nothing to review; None also clears a decision left over from an earlier round
    if not runtime.context.require_review:
        # No human in the loop: the gate being off means "approved", so a fix is proposed when the server can read
        # code. Without this the graph would end at the root cause and never propose a fix (the review decision is
        # what routes to propose_changes). A server with no code access ends here, as before: there is nothing to propose.
        approved = ReviewDecision(action="approve", note=None) if runtime.context.code is not None else None
        return {"review": approved}

    raw = interrupt(review_request(state))  # the graph stops here; resuming returns the decision

    decision = read_decision(raw)
    if decision is None:
        logger.warning("human_review: the resume value was not a valid decision, ending the investigation")
        return {
            "review": ReviewDecision(action="stop"),
            "gaps": ["The review decision could not be read, so the investigation was ended"],
        }
    if decision.action == "refine" and state["review_round"] >= MAX_REFINES:
        return {
            "review": ReviewDecision(action="stop", note=decision.note),
            "gaps": [f"The investigation was ended because the refinement limit ({MAX_REFINES}) was reached"],
        }
    update: dict[str, Any] = {"review": decision}
    if decision.action == "refine":
        # A fresh analysis round: verification gets its own rounds, follow-ups start empty, and the earlier
        # conclusion is cleared, so a round that cannot reach one is not mistaken for the old answer.
        update |= {
            "review_round": state["review_round"] + 1,
            "verify_round": 0,
            "follow_ups": [],
            "root_cause": None,
            "recommendation": None,
            "confidence_band": None,
        }
    return update
