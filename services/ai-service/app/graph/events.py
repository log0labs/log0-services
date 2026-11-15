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

from app.schemas.domain import (
    CodeChange,
    Evidence,
    Hypothesis,
    Recommendation,
    RootCause,
    Triage,
)

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
    payload: dict[str, Any] = {"id": item.id, "kind": item.kind, "summary": _cap(item.summary)}
    if item.redactions:
        payload["redacted"] = sum(item.redactions.values())
    return "evidence.added", payload


def _redaction_applied(items: list[Evidence]) -> EventPair | None:
    """One event per node update that hid something: how many values and of which kinds, never the values."""
    kinds: dict[str, int] = {}
    for item in items:
        for kind, count in item.redactions.items():
            kinds[kind] = kinds.get(kind, 0) + count
    if not kinds:
        return None
    return "redaction.applied", {"count": sum(kinds.values()), "kinds": dict(sorted(kinds.items()))}


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


def _changes_proposed(changes: list[CodeChange]) -> EventPair:
    """Which files the proposal touches and how: never the code itself (that stays in the checkpoint)."""
    return "changes.proposed", {
        "count": len(changes),
        "files": [c.file_path for c in changes],
        "operations": [c.operation for c in changes],
    }


def _changes_validated(delta: dict[str, Any]) -> EventPair:
    errors = delta.get("validation_errors") or []
    return "changes.validated", {"ok": bool(delta.get("diff")) and not errors, "issueCount": len(errors)}


def _diff_generated(delta: dict[str, Any]) -> EventPair:
    """The shape of the diff (files and line counts), never its text."""
    return "diff.generated", {
        "bytes": len(delta["diff"].encode("utf-8")),
        "files": [
            {"path": f.file_path, "operation": f.operation, "additions": f.additions, "deletions": f.deletions}
            for f in delta.get("diff_files") or []
        ],
    }


def _publish_events(node: str, delta: dict[str, Any]) -> list[EventPair]:
    """What the three write nodes did, as events: names and links only, never file content or tokens."""
    if node == "create_branch" and delta.get("branch") is not None:
        branch = delta["branch"]
        return [("branch.created", {"name": branch.name, "baseSha": branch.base_sha})]
    if node == "apply_patch" and delta.get("commit_sha"):
        return [("commit.created", {"sha": delta["commit_sha"]})]
    if node == "run_tests" and delta.get("ci_run") is not None:
        run = delta["ci_run"]
        checks = [{"name": c.name, "conclusion": c.conclusion or c.status} for c in run.checks]
        return [("tests.completed", {"outcome": run.outcome, "checks": checks})]
    if node == "open_draft_pr" and delta.get("pull_request") is not None:
        pr = delta["pull_request"]
        return [("pr.opened", {"number": pr.number, "url": pr.url, "branch": pr.branch, "draft": pr.draft})]
    return []


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
        added = delta.get("evidence") or []
        events.extend(_evidence_added(item) for item in added)
        if (hidden := _redaction_applied(added)) is not None:
            events.append(hidden)
        if node in _HYPOTHESIS_EVENTS:  # verify returns only the hypotheses it changed
            events.extend(_hypothesis_event(_HYPOTHESIS_EVENTS[node], h) for h in delta.get("hypotheses") or [])
        if node == "root_cause" and delta.get("root_cause") is not None:
            events.append(_root_cause_identified(delta["root_cause"], delta.get("recommendation"), delta.get("confidence_band")))
        if node == "propose_changes" and delta.get("proposed_changes"):
            events.append(_changes_proposed(delta["proposed_changes"]))
        if node == "validate_changes" and ("validation_errors" in delta or "diff" in delta):
            events.append(_changes_validated(delta))
            if delta.get("diff"):
                events.append(_diff_generated(delta))
        events.extend(_publish_events(node, delta))
        events.extend(("gap.recorded", {"message": _cap(gap)}) for gap in delta.get("gaps") or [])
    return events
