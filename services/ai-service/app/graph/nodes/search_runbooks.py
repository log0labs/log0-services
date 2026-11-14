"""Gather branch: what do the team's own runbooks say about this?

The search itself (retrieve, diversify, grade, rewrite once) lives in ``retrieval/runbooks.py``. This node
builds the query from facts already gathered, turns each relevant passage into citable evidence, and turns
every way the search can fail into a gap: a missing runbook search never fails the investigation.
"""

import logging
from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy.exc import SQLAlchemyError

from app.graph.state import InvestigationContext, InvestigationState
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.runbooks import RunbookPassage
from app.schemas.domain import Evidence, make_evidence_id

logger = logging.getLogger(__name__)

_TOOL_CALLS = 1
MAX_QUERY_CHARS = 500
MAX_CONTENT = 1500
SUMMARY_CHARS = 160


def build_query(state: InvestigationState) -> str:
    """What to look for: the engineer's question, the triage summary, the service and the top error message."""
    incident = state["incident"]
    triage = state["triage"]
    parts: list[str] = []
    if state["question"]:
        parts.append(state["question"])
    if triage is not None:
        parts.append(triage.summary)
    if incident is not None:
        parts.append(incident.service_name)
        parts.extend(incident.top_messages[:2])
    return " ".join(" ".join(parts).split())[:MAX_QUERY_CHARS]


def passage_evidence(passage: RunbookPassage) -> Evidence:
    ref = f'runbook "{passage.title}" #{passage.ordinal}'
    where = f' > {passage.heading_path}' if passage.heading_path else ""
    first_line = " ".join(passage.content.split())[:SUMMARY_CHARS]
    return Evidence(
        id=make_evidence_id("runbook", f"{passage.runbook_id}#{passage.ordinal}"),
        kind="runbook",
        source_ref=ref,
        summary=f'Runbook "{passage.title}"{where}: {first_line}',
        content=passage.content[:MAX_CONTENT],
    )


async def search_runbooks(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    if state["incident"] is None:
        return {}
    ctx = runtime.context
    if ctx.runbooks is None:
        return {"gaps": ["Runbooks were not searched: runbook search is not configured on this server"]}

    query = build_query(state)
    try:
        result = await ctx.runbooks.search(tenant_id=ctx.tenant_id, query=query, models=ctx.models)
    except EmbeddingError:
        logger.warning("runbook search: embedding service unavailable", exc_info=True)
        return {"gaps": ["Runbooks could not be searched (the embedding service is unavailable)"], "tool_calls": _TOOL_CALLS}
    except (SQLAlchemyError, OSError):
        logger.warning("runbook search: database unavailable", exc_info=True)
        return {"gaps": ["Runbooks could not be searched (the database is unavailable)"], "tool_calls": _TOOL_CALLS}

    if result.outcome == "no_runbooks":
        return {"gaps": ["No runbooks have been uploaded for this workspace"], "tool_calls": _TOOL_CALLS}
    if result.outcome == "none_relevant":
        return {"gaps": ["No runbook matched this incident"], "tool_calls": _TOOL_CALLS}

    update: dict[str, Any] = {
        "evidence": [passage_evidence(p) for p in result.passages],
        "tool_calls": _TOOL_CALLS,
    }
    if not result.graded:
        update["gaps"] = ["Runbook relevance was judged by similarity alone (the relevance check was unavailable)"]
    return update
