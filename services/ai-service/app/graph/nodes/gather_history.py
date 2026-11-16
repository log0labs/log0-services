"""Gather branch: has this happened before? (earlier incidents, same error or same service).

Past incidents are the cheapest strong evidence there is: an engineer who resolved the same
fingerprint last month already knows the cause. Each earlier incident becomes one piece of
citable evidence; "nothing earlier" is evidence too. When long-term memory is configured, the conclusions
of earlier *investigations* of similar incidents are added as leads (kind "memory"): fuzzy matches, not proof.
"""

import logging
from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy.exc import SQLAlchemyError

from app.graph.nodes.search_runbooks import build_query
from app.graph.state import InvestigationContext, InvestigationState
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.memory import RecalledInvestigation
from app.schemas.domain import Evidence, make_evidence_id
from app.tools.incidents import IncidentContext, RelatedIncident, find_related_incidents

logger = logging.getLogger(__name__)

_TOOL_CALLS = 1
LOOKBACK_DAYS = 30
RELATED_LIMIT = 8
MAX_CONTENT = 1000


def _day(value: Any) -> str:
    return value.strftime("%Y-%m-%d")


def related_incident_evidence(incident: RelatedIncident) -> Evidence:
    ref = f"incident {incident.incident_id}"
    kind = "same error" if incident.same_fingerprint else f"same service ({incident.service_name})"
    outcome = f"resolved {_day(incident.resolved_at)}" if incident.resolved_at else f"status {incident.status}"
    return Evidence(
        id=make_evidence_id("incident", ref),
        kind="incident",
        source_ref=ref,
        summary=(
            f"Earlier incident, {kind}: {incident.severity}, {incident.occurrence_count} occurrences, "
            f"{_day(incident.first_seen_at)} to {_day(incident.last_seen_at)}, {outcome}"
        ),
        content=(incident.ai_summary or "")[:MAX_CONTENT],
    )


def memory_evidence(past: RecalledInvestigation) -> Evidence:
    ref = f"past investigation {past.investigation_id}"
    where = f"{past.service_name}, " if past.service_name else ""
    band = f", {past.confidence_band} confidence" if past.confidence_band else ""
    feedback = f", engineers said: {past.feedback_verdict}" if past.feedback_verdict else ""
    lines = [
        "A lead from an earlier investigation, not proof for this incident.",
        f"Root cause then: {past.root_cause}",
    ]
    if past.recommendation:
        lines.append(f"Recommendation then: {past.recommendation}")
    if past.feedback_note:
        lines.append(f"Engineer's note: {past.feedback_note}")
    return Evidence(
        id=make_evidence_id("memory", ref),
        kind="memory",
        source_ref=ref,
        summary=f"Past investigation ({where}{_day(past.created_at)}{band}{feedback}): {past.root_cause[:200]}",
        content="\n".join(lines)[:MAX_CONTENT],
    )


def no_history_evidence(incident: IncidentContext) -> Evidence:
    ref = f"history {incident.fingerprint[:12]}"
    return Evidence(
        id=make_evidence_id("incident", ref),
        kind="incident",
        source_ref=ref,
        summary=(
            f"No earlier incident with this fingerprint, and none from {incident.service_name}, "
            f"in the last {LOOKBACK_DAYS} days"
        ),
        content="",
    )


async def gather_history(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Add one evidence item per earlier incident, and per similar past investigation when memory is on.
    A database outage becomes a gap; one source failing never hides what the other found."""
    incident = state["incident"]
    if incident is None:
        return {}
    ctx = runtime.context
    evidence: list[Evidence] = []
    gaps: list[str] = []
    tool_calls = _TOOL_CALLS
    try:
        related = await find_related_incidents(
            ctx.history,
            tenant_id=ctx.tenant_id,
            incident=incident,
            lookback_days=LOOKBACK_DAYS,
            limit=RELATED_LIMIT,
        )
        evidence = [related_incident_evidence(r) for r in related] or [no_history_evidence(incident)]
    except (SQLAlchemyError, OSError) as exc:  # database down or unreachable; anything else is a bug
        logger.warning("incident history unavailable: %s", exc)
        gaps.append("Earlier incidents could not be read (the incident database is unavailable)")

    if ctx.memory is not None:
        tool_calls += 1
        try:
            recalled = await ctx.memory.recall(
                tenant_id=ctx.tenant_id, query=build_query(state), exclude_incident_id=incident.incident_id
            )
            evidence += [memory_evidence(r) for r in recalled]
        except (EmbeddingError, SQLAlchemyError, OSError):
            logger.warning("memory recall unavailable", exc_info=True)
            gaps.append("Past investigations could not be recalled (memory is unavailable)")

    update: dict[str, Any] = {"tool_calls": tool_calls}
    if evidence:
        update["evidence"] = evidence
    if gaps:
        update["gaps"] = gaps
    return update
