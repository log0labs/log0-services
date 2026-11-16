"""Run the log searches that ``verify`` asked for, so the next verify round has new evidence.

Each search text becomes one substring search over the incident's investigation window, in
every service of the tenant (a cause often sits in a different service than the symptom). The
result is citable evidence like any other, and so is an empty result: "no log line contains
X" can refute a hypothesis. As in the other gather nodes, a log-store outage is a gap.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from langgraph.runtime import Runtime

from app.graph.nodes.load_context import MAX_CONTENT, investigation_window
from app.graph.state import InvestigationContext, InvestigationState
from app.integrations.clickhouse import ClickHouseError
from app.schemas.domain import Evidence, make_evidence_id
from app.tools.logs import LogSearchResult, search_logs

logger = logging.getLogger(__name__)

FOLLOW_UP_LINES = 10


def search_evidence(term: str, start: datetime, end: datetime, result: LogSearchResult) -> Evidence:
    ref = f"search {term.lower()} {start:%Y%m%dT%H%MZ}..{end:%Y%m%dT%H%MZ}"
    window = f"between {start:%Y-%m-%d %H:%M} and {end:%Y-%m-%d %H:%M} UTC"
    if not result.lines:
        return Evidence(
            id=make_evidence_id("log", ref),
            kind="log",
            source_ref=ref,
            summary=f'No log line contains "{term}" {window}',
            content="",
        )
    lines = [
        f"{line.timestamp:%Y-%m-%d %H:%M} {line.service_name} {line.level} {' '.join(line.message.split())}"
        for line in result.lines
    ]
    more = " (more exist)" if result.limit_reached else ""
    return Evidence(
        id=make_evidence_id("log", ref),
        kind="log",
        source_ref=ref,
        summary=f'{len(result.lines)} most recent log line(s) contain "{term}" {window}{more}',
        content="\n".join(lines)[:MAX_CONTENT],
    )


async def gather_followup(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Search the logs for each text in ``follow_ups``, then clear the list (which ends the loop
    unless verify asks again)."""
    incident, terms = state["incident"], state["follow_ups"]
    if incident is None or not terms:
        return {"follow_ups": []}
    ctx = runtime.context
    start, end = investigation_window(incident)

    results = await asyncio.gather(
        *(
            search_logs(ctx.logs, tenant_id=ctx.tenant_id, start=start, end=end, query=term, limit=FOLLOW_UP_LINES)
            for term in terms
        ),
        return_exceptions=True,
    )
    evidence: list[Evidence] = []
    gaps: list[str] = []
    for term, result in zip(terms, results, strict=True):
        if isinstance(result, ClickHouseError):
            logger.warning("follow-up search %r unavailable: %s", term, result)
            gaps.append(f'The log search for "{term[:60]}" could not be run (the log store could not be queried)')
        elif isinstance(result, BaseException):
            raise result  # a bug, not an outage: fail loudly
        else:
            evidence.append(search_evidence(term, start, end, result))
    return {"evidence": evidence, "gaps": gaps, "tool_calls": len(terms), "follow_ups": []}
