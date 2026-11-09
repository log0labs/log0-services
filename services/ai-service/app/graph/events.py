"""Turn what the graph just did into the events the UI shows.

``graph.astream(..., stream_mode="updates")`` yields one chunk each time a node finishes:
``{"<node name>": {<the keys that node returned>}}``. This module reads those chunks and
returns ``(event_type, payload)`` pairs. It is pure (no database, no network), so the
mapping is easy to test and the graph itself never knows events exist.

Events are observable activity only: facts the nodes found and short summaries they wrote.
They are never prompts, never model reasoning, and never raw log content (the evidence
``content`` stays in the checkpoint; the event carries only the one-line ``summary``).
Payload keys are camelCase, like the rest of the JSON the browser sees.
"""

from typing import Any

from app.schemas.domain import Evidence, Hypothesis, Recommendation, RootCause, Triage

MAX_TEXT = 300  # cap on any free text copied into an event

EventPair = tuple[str, dict[str, Any]]

# the nodes that write hypotheses, and what each one's write means
_HYPOTHESIS_EVENTS = {"synthesize": "hypothesis.proposed", "verify": "hypothesis.updated"}


def _cap(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _context_loaded(delta: dict[str, Any]) -> EventPair:
    incident = delta["incident"]
    return "context.loaded", {
        "service": incident.service_name,
        "environment": incident.environment,
        "severity": incident.severity,
        "occurrenceCount": incident.occurrence_count,
        "stackFrames": len(delta.get("stack_frames") or []),
    }


def _triage_completed(triage: Triage) -> EventPair:
    return "triage.completed", {
        "category": triage.category,
        "summary": _cap(triage.summary),
        "gather": list(triage.gather),
        "hypotheses": [_cap(h) for h in triage.initial_hypotheses],
    }


def _evidence_added(item: Evidence) -> EventPair:
    return "evidence.added", {"id": item.id, "kind": item.kind, "summary": _cap(item.summary)}


def _hypothesis_event(event_type: str, item: Hypothesis) -> EventPair:
    return event_type, {
        "id": item.id,
        "statement": _cap(item.statement),
        "status": item.status,
        "supportingEvidence": list(item.supporting_evidence),
        "contradictingEvidence": list(item.contradicting_evidence),
    }


def _root_cause_identified(cause: RootCause, recommendation: Recommendation | None, band: str | None) -> EventPair:
    return "root_cause.identified", {
        "statement": _cap(cause.statement),
        "evidenceIds": list(cause.evidence_ids),
        "hypothesisId": cause.hypothesis_id,
        "confidenceBand": band,
        "contributingFactors": [_cap(f) for f in cause.contributing_factors],
        "unresolvedQuestions": [_cap(q) for q in cause.unresolved_questions],
        "recommendation": _cap(recommendation.summary) if recommendation else None,
    }


def events_from_update(update: dict[str, Any]) -> list[EventPair]:
    """Events for one ``updates`` chunk, in a stable order: the node's own event, then its
    evidence, then its hypotheses, then its root cause, then its gaps."""
    events: list[EventPair] = []
    for node, delta in update.items():
        if not isinstance(delta, dict):  # e.g. an "__interrupt__" marker: nothing to report
            continue
        if node == "load_context" and delta.get("incident") is not None:
            events.append(_context_loaded(delta))
        elif node == "triage" and delta.get("triage") is not None:
            events.append(_triage_completed(delta["triage"]))
        events.extend(_evidence_added(item) for item in delta.get("evidence") or [])
        if node in _HYPOTHESIS_EVENTS:  # verify returns only the hypotheses it changed
            events.extend(_hypothesis_event(_HYPOTHESIS_EVENTS[node], h) for h in delta.get("hypotheses") or [])
        if node == "root_cause" and delta.get("root_cause") is not None:
            events.append(_root_cause_identified(delta["root_cause"], delta.get("recommendation"), delta.get("confidence_band")))
        events.extend(("gap.recorded", {"message": _cap(gap)}) for gap in delta.get("gaps") or [])
    return events
