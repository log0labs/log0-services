"""First graph node: gather the deterministic facts before any LLM runs.

No model is involved. It reads the authoritative incident, summarises the logs for its
fingerprint, and parses the stack trace into frames. What it learns goes into the
state as typed fields and as citable evidence; what it could not learn becomes a
*gap* instead of an error, so a ClickHouse hiccup never kills an investigation.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from langgraph.runtime import Runtime

from app.graph.state import InvestigationContext, InvestigationState
from app.integrations.clickhouse import ClickHouseError
from app.integrations.incident_service import IncidentNotFoundError
from app.parsing.stacktrace import ParsedStackTrace, parse_stack_trace
from app.schemas.domain import Evidence, make_evidence_id
from app.schemas.investigation import InvestigationStatus
from app.tools.incidents import IncidentContext, get_incident
from app.tools.logs import MAX_WINDOW, LogStats, get_log_stats, get_sample_stack_trace

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(minutes=30)  # look this far before the incident's first occurrence
MAX_CONTENT = 4000
_TOOL_CALLS = 3  # get_incident, get_log_stats, get_sample_stack_trace


def investigation_window(incident: IncidentContext) -> tuple[datetime, datetime]:
    """[first_seen - 30 min, last_seen + 1 min], never longer than the tools allow (7 days).

    The end is exclusive, so the extra minute keeps the incident's last event inside.
    """
    end = incident.last_seen_at + timedelta(minutes=1)
    start = incident.first_seen_at - LOOKBACK
    if end - start > MAX_WINDOW:
        start = end - MAX_WINDOW  # a long-running incident: keep its most recent week
    return start, end


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def incident_evidence(incident: IncidentContext) -> Evidence:
    ref = f"incident {incident.incident_id}"
    lines = [f"- {m}" for m in incident.top_messages]
    content = "Top messages:\n" + "\n".join(lines) if lines else ""
    if incident.ai_summary:
        content += f"\n\nEarlier AI summary:\n{incident.ai_summary}"
    return Evidence(
        id=make_evidence_id("incident", ref),
        kind="incident",
        source_ref=ref,
        summary=(
            f"{incident.severity} incident on {incident.service_name} ({incident.environment}), "
            f"status {incident.status}: {incident.occurrence_count} occurrences, "
            f"first seen {_stamp(incident.first_seen_at)} UTC, last seen {_stamp(incident.last_seen_at)} UTC"
        ),
        content=content.strip()[:MAX_CONTENT],
    )


def log_stats_evidence(stats: LogStats) -> Evidence:
    ref = f"logstats {stats.fingerprint[:12]} {stats.start:%Y%m%dT%H%MZ}..{stats.end:%Y%m%dT%H%MZ}"
    levels = ", ".join(f"{level} {n}" for level, n in stats.levels.items())
    services = "\n".join(f"{s.service_name}: {s.count}" for s in stats.services[:10])
    return Evidence(
        id=make_evidence_id("log", ref),
        kind="log",
        source_ref=ref,
        summary=(
            f"{stats.total} log events with this fingerprint across {len(stats.services)} service(s) "
            f"between {_stamp(stats.start)} and {_stamp(stats.end)} UTC; levels: {levels}"
        ),
        content=services[:MAX_CONTENT],
    )


def stack_trace_evidence(fingerprint: str, raw: str, parsed: ParsedStackTrace) -> Evidence:
    ref = f"stacktrace {fingerprint[:12]}"
    top = parsed.first_in_app_frame()
    where = f" at {top.file}:{top.line} in {top.function}" if top and top.has_location else (
        f" in {top.function}" if top else ""
    )
    error = ": ".join(x for x in (parsed.exception_type, parsed.exception_message) if x)
    return Evidence(
        id=make_evidence_id("log", ref),
        kind="log",
        source_ref=ref,
        summary=f"Stack trace ({parsed.language or 'unrecognised format'}) {error}{where}".strip(),
        content=raw[:MAX_CONTENT],
    )


async def load_context(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Fill ``incident``, ``stack_frames``, evidence and gaps. Fails only if the incident can't be read."""
    ctx = runtime.context
    try:
        incident = await get_incident(ctx.incidents, tenant_id=ctx.tenant_id, incident_id=UUID(state["incident_id"]))
    except IncidentNotFoundError:
        return {"status": InvestigationStatus.FAILED, "error": "Incident not found", "tool_calls": 1}
    except httpx.HTTPError as exc:
        logger.warning("incident-service unavailable: %s", exc)
        return {
            "status": InvestigationStatus.FAILED,
            "error": "Incident service is unavailable",
            "tool_calls": 1,
        }
    
    start, end = investigation_window(incident)
    evidence = [incident_evidence(incident)]
    gaps: list[str] = []
    
    stats, trace = await asyncio.gather(
        get_log_stats(ctx.logs, tenant_id=ctx.tenant_id, fingerprint=incident.fingerprint, start=start, end=end),
        get_sample_stack_trace(ctx.logs, tenant_id=ctx.tenant_id, fingerprint=incident.fingerprint, start=start, end=end),
        return_exceptions=True,  # one failing query must not hide the other's result
    )

    if isinstance(stats, ClickHouseError):
        logger.warning("log stats unavailable: %s", stats)
        gaps.append("Log statistics are unavailable (the log store could not be queried)")
    elif isinstance(stats, BaseException):
        raise stats  # a bug, not an outage: let it fail loudly
    elif stats.total == 0:
        gaps.append("No log events were found for this incident's fingerprint in the investigation window")
    else:
        evidence.append(log_stats_evidence(stats))
    
    frames = []
    if isinstance(trace, ClickHouseError):
        logger.warning("stack trace unavailable: %s", trace)
        gaps.append("The stack trace is unavailable (the log store could not be queried)")
    elif isinstance(trace, BaseException):
        raise trace
    elif trace is None:
        gaps.append("No stack trace was recorded for this incident")
    else:
        parsed = parse_stack_trace(trace)
        frames = parsed.frames
        evidence.append(stack_trace_evidence(incident.fingerprint, trace, parsed))
        if not frames:
            gaps.append("The stack trace could not be parsed into frames")
        elif parsed.first_in_app_frame() is None:
            gaps.append("The stack trace contains only library frames")

    return {
        "incident": incident,
        "stack_frames": frames,
        "evidence": evidence,
        "gaps": gaps,
        "tool_calls": _TOOL_CALLS,
    }
