"""Gather branch: what the logs show around the incident (timeline, neighbours, samples).

Runs in parallel with the other gather branches. It reads ClickHouse three ways and turns
each answer into citable evidence:

* a **timeline** of this fingerprint (when did it start, did it spike, is it still going),
* the **co-occurring errors** (what else was failing at the same time, in any service),
* a few **recent ERROR lines** from the incident's own service.

As in ``load_context``, a ClickHouse outage becomes a gap, never an error.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from langgraph.runtime import Runtime

"""Gather branch: what the logs show around the incident (timeline, neighbours, samples).

Runs in parallel with the other gather branches. It reads ClickHouse three ways and turns
each answer into citable evidence:

* a **timeline** of this fingerprint (when did it start, did it spike, is it still going),
* the **co-occurring errors** (what else was failing at the same time, in any service),
* a few **recent ERROR lines** from the incident's own service.

As in ``load_context``, a ClickHouse outage becomes a gap, never an error.
"""



from app.graph.nodes.load_context import MAX_CONTENT, investigation_window
from app.graph.state import InvestigationContext, InvestigationState
from app.integrations.clickhouse import ClickHouseError
from app.schemas.domain import Evidence, make_evidence_id
from app.tools.incidents import IncidentContext
from app.tools.logs import (
    Bucket,
    CooccurringError,
    ErrorTimeline,
    LogSearchResult,
    get_cooccurring_errors,
    get_error_timeline,
    search_logs,
)

logger = logging.getLogger(__name__)

_TOOL_CALLS = 3  # get_error_timeline, get_cooccurring_errors, search_logs
COOCCURRING_LIMIT = 10
SAMPLE_LINES = 15
MAX_TIMELINE_LINES = 60
MAX_SLOTS = 120  # a timeline longer than this is too long to read, so use bigger buckets

# (bucket name, seconds): smallest first
_BUCKETS: tuple[tuple[Bucket, int], ...] = (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600))


def pick_bucket(start: datetime, end: datetime) -> Bucket:
    """The finest bucket that keeps the timeline at most ``MAX_SLOTS`` long."""
    seconds = (end - start).total_seconds()
    for name, step in _BUCKETS:
        if seconds / step + 1 <= MAX_SLOTS:
            return name
    return "1h"


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def timeline_evidence(timeline: ErrorTimeline) -> Evidence:
    ref = f"timeline {timeline.fingerprint[:12]} {timeline.bucket} {timeline.start:%Y%m%dT%H%MZ}..{timeline.end:%Y%m%dT%H%MZ}"
    active = [b for b in timeline.buckets if b.count]
    peak = timeline.peak
    lines = [f"{_stamp(b.bucket_start)}  {b.count}" for b in active[:MAX_TIMELINE_LINES]]
    if len(active) > MAX_TIMELINE_LINES:
        lines.append(f"... {len(active) - MAX_TIMELINE_LINES} more buckets with events")
    return Evidence(
        id=make_evidence_id("timeline", ref),
        kind="timeline",
        source_ref=ref,
        summary=(
            f"{timeline.total} occurrences in {len(active)} of {len(timeline.buckets)} "
            f"{timeline.bucket} buckets between {_stamp(timeline.start)} and {_stamp(timeline.end)} UTC; "
            f"first events {_stamp(active[0].bucket_start)}, last {_stamp(active[-1].bucket_start)}"
            + (f"; peak {peak.count} at {_stamp(peak.bucket_start)}" if peak else "")
        ),
        content="\n".join(lines)[:MAX_CONTENT],
    )


def cooccurring_evidence(incident: IncidentContext, start: datetime, end: datetime, errors: list[CooccurringError]) -> Evidence:
    ref = f"cooccurring {incident.fingerprint[:12]} {start:%Y%m%dT%H%MZ}..{end:%Y%m%dT%H%MZ}"
    window = f"between {_stamp(start)} and {_stamp(end)} UTC"
    if not errors:
        # Absence is a finding too: it points at an isolated failure, not a wider outage.
        return Evidence(
            id=make_evidence_id("log", ref),
            kind="log",
            source_ref=ref,
            summary=f"No other WARN/ERROR/FATAL patterns were logged by any service {window}",
            content="",
        )
    top = errors[0]
    lines = [
        f"{e.service_name}: {e.message_template} ({e.count}x, {_hhmm(e.first_seen)}-{_hhmm(e.last_seen)})"
        for e in errors
    ]
    return Evidence(
        id=make_evidence_id("log", ref),
        kind="log",
        source_ref=ref,
        summary=(
            f"{len(errors)} other failing pattern(s) {window}; "
            f"most frequent: {top.service_name}: {top.message_template[:120]} ({top.count}x)"
        ),
        content="\n".join(lines)[:MAX_CONTENT],
    )


def sample_lines_evidence(incident: IncidentContext, start: datetime, end: datetime, result: LogSearchResult) -> Evidence:
    ref = f"samples {incident.service_name} ERROR {start:%Y%m%dT%H%MZ}..{end:%Y%m%dT%H%MZ}"
    window = f"between {_stamp(start)} and {_stamp(end)} UTC"
    if not result.lines:
        return Evidence(
            id=make_evidence_id("log", ref),
            kind="log",
            source_ref=ref,
            summary=f"{incident.service_name} logged no ERROR lines {window}",
            content="",
        )
    lines = [
        f"{_stamp(line.timestamp)} {line.level} "
        f"{'[this incident] ' if line.fingerprint == incident.fingerprint else ''}{' '.join(line.message.split())}"
        for line in result.lines
    ]
    more = " (more exist)" if result.limit_reached else ""
    return Evidence(
        id=make_evidence_id("log", ref),
        kind="log",
        source_ref=ref,
        summary=f"{len(result.lines)} most recent ERROR lines from {incident.service_name} {window}{more}",
        content="\n".join(lines)[:MAX_CONTENT],
    )


async def gather_logs(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Add timeline, co-occurring-error and sample-line evidence. Never fails on a log-store outage."""
    incident = state["incident"]
    if incident is None:
        return {}
    ctx = runtime.context
    start, end = investigation_window(incident)
    
    timeline, neighbours, samples = await asyncio.gather(
        get_error_timeline(
            ctx.logs,
            tenant_id=ctx.tenant_id,
            fingerprint=incident.fingerprint,
            start=start,
            end=end,
            bucket=pick_bucket(start, end),
        ),
        get_cooccurring_errors(
            ctx.logs,
            tenant_id=ctx.tenant_id,
            start=start,
            end=end,
            exclude_fingerprint=incident.fingerprint,
            limit=COOCCURRING_LIMIT,
        ),
        search_logs(
            ctx.logs,
            tenant_id=ctx.tenant_id,
            start=start,
            end=end,
            service=incident.service_name,
            level="ERROR",
            limit=SAMPLE_LINES,
        ),
        return_exceptions=True,  # one failing query must not hide the others' results
    )

    evidence: list[Evidence] = []
    gaps: list[str] = []
    for label, result in (("timeline", timeline), ("co-occurring errors", neighbours), ("sample log lines", samples)):
        if isinstance(result, ClickHouseError):
            logger.warning("%s unvailable: %s", label, result)
            gaps.append(f"The {label} could not be read (the log store could not be queried)")
        elif isinstance(result, BaseException):
            raise result # a bug, not an outage: fail loudly
    
    if isinstance(timeline, ErrorTimeline) and timeline.total > 0:
        evidence.append(timeline_evidence(timeline))
    if isinstance(neighbours, list):
        evidence.append(cooccurring_evidence(incident, start, end, neighbours))
    if isinstance(samples, LogSearchResult):
        evidence.append(sample_lines_evidence(incident, start, end, samples))

    return {"evidence": evidence, "gaps": gaps, "tool_calls": _TOOL_CALLS}
