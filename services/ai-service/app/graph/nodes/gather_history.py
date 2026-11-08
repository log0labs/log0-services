"""Gather branch: has this happened before? (earlier incidents, same error or same service).

Past incidents are the cheapest strong evidence there is: an engineer who resolved the same
fingerprint last month already knows the cause. Each earlier incident becomes one piece of
citable evidence; "nothing earlier" is evidence too.
"""

import logging
from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy.exc import SQLAlchemyError

from app.graph.state import InvestigationContext, InvestigationState
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
    """Add one evidence item per earlier incident. A database outage becomes a gap."""
    incident = state["incident"]
    if incident is None:
        return {}
    ctx = runtime.context
    try:
        related = await find_related_incidents(
            ctx.history,
            tenant_id=ctx.tenant_id,
            incident=incident,
            lookback_days=LOOKBACK_DAYS,
            limit=RELATED_LIMIT,
        )
    except (SQLAlchemyError, OSError) as exc:  # database down or unreachable; anything else is a bug
        logger.warning("incident history unavailable: %s", exc)
        return {
            "gaps": ["Earlier incidents could not be read (the incident database is unavailable)"],
            "tool_calls": _TOOL_CALLS,
        }
    evidence = [related_incident_evidence(r) for r in related] or [no_history_evidence(incident)]
    return {"evidence": evidence, "tool_calls": _TOOL_CALLS}

