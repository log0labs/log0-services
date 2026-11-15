"""The human gates of the graph, described once: the review of the root cause and the pull-request approval.

A gate is a node that calls ``interrupt()``. The executor, which only sees a paused graph and the
interrupt's payload, needs to know three things about it: which status the investigation has while it
waits, which events announce the request and carry the answer, and how to turn the answer into the value
the node resumes with. Adding a gate is adding one entry here.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.investigation import InvestigationStatus

MAX_TEXT = 300


@dataclass(frozen=True)
class Gate:
    name: str
    paused: InvestigationStatus  # the status while the run waits
    requested_event: str  # written when the run parks
    decided_event: str  # written by the API with the engineer's answer
    announce: Callable[[dict[str, Any]], dict[str, Any]]  # interrupt payload -> event payload (summaries only)
    resume_value: Callable[[dict[str, Any]], dict[str, Any]]  # decided-event payload -> what the node resumes with
    resumed: Callable[[str | None], InvestigationStatus]  # status after an answer, given its action


def _review_announcement(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "round": request.get("round"),
        "refinesLeft": request.get("refinesLeft"),
        "rootCause": str(request.get("rootCause", ""))[:MAX_TEXT],
        "confidenceBand": request.get("confidenceBand"),
        "evidenceIds": list(request.get("evidenceIds") or []),
        "recommendation": str(request["recommendation"])[:MAX_TEXT] if request.get("recommendation") else None,
    }


def _pr_announcement(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": request.get("repository"),
        "branch": request.get("branch"),
        "defaultBranch": request.get("defaultBranch"),
        "baseSha": request.get("baseSha"),
        "files": list(request.get("files") or []),
        "rootCause": str(request.get("rootCause", ""))[:MAX_TEXT],
        "confidenceBand": request.get("confidenceBand"),
    }


REVIEW = Gate(
    name="review",
    paused=InvestigationStatus.AWAITING_REVIEW,
    requested_event="review.requested",
    decided_event="review.decided",
    announce=_review_announcement,
    resume_value=lambda payload: {"action": payload.get("action"), "note": payload.get("note")},
    resumed=lambda _action: InvestigationStatus.RUNNING,
)

PR_APPROVAL = Gate(
    name="pr_approval",
    paused=InvestigationStatus.AWAITING_PR_APPROVAL,
    requested_event="pr.approval_requested",
    decided_event="pr.decided",
    announce=_pr_announcement,
    resume_value=lambda payload: {"action": payload.get("action"), "decided_by": payload.get("decidedBy")},
    resumed=lambda action: InvestigationStatus.PUBLISHING if action == "approve" else InvestigationStatus.RUNNING,
)

GATES: dict[str, Gate] = {gate.name: gate for gate in (REVIEW, PR_APPROVAL)}


def gate_for(request: Any) -> Gate:
    """The gate a paused graph is waiting at, from its interrupt payload (older checkpoints have no name: the review)."""
    name = request.get("gate") if isinstance(request, dict) else None
    return GATES.get(name or "review", REVIEW)
