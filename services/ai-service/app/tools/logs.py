"""Log evidence tools: typed, bounded, tenant-bound reads over ClickHouse.

Each tool is a plain async function that takes a ``LogBackend`` (``LogStore`` in
production, a fake in tests/evals) and the tenant from the verified JWT. SQL text
is built only from constant fragments; every caller-supplied value travels as a
bound parameter. Outputs are Pydantic models, capped in size, so one tool call
can never flood the model's context.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

Bucket = Literal["1m", "5m", "15m", "1h"]

_BUCKET_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
_BUCKET_SQL: dict[str, str] = {
    "1m": "INTERVAL 1 MINUTE",
    "5m": "INTERVAL 5 MINUTE",
    "15m": "INTERVAL 15 MINUTE",
    "1h": "INTERVAL 1 HOUR",
}
_LEVELS = frozenset({"DEBUG", "INFO", "WARN", "ERROR", "FATAL"})

MAX_TIMELINE_BUCKETS = 500
MAX_WINDOW = timedelta(days=7)
MAX_SEARCH_LIMIT = 200
MAX_COOCCURRING_LIMIT = 50
MESSAGE_MAX_CHARS = 500


class LogBackend(Protocol):
    """What the tools need from a log store (LogStore, or a fixture for evals)."""
    
    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        ...


class TimelineBucket(BaseModel):
    bucket_start: datetime
    count: int


class ErrorTimeline(BaseModel):
    fingerprint: str
    bucket: str
    start: datetime
    end: datetime
    buckets: list[TimelineBucket]  # zero-filled, oldest first
    total: int
    peak: TimelineBucket | None


class CooccurringError(BaseModel):
    service_name: str
    fingerprint: str
    message_template: str
    count: int
    first_seen: datetime
    last_seen: datetime


class LogLine(BaseModel):
    timestamp: datetime
    service_name: str
    level: str
    message: str
    message_truncated: bool
    fingerprint: str


class LogSearchResult(BaseModel):
    lines: list[LogLine]  # newest first
    limit_reached: bool  # True: more matching lines exist than were returned


def _utc(value: datetime) -> datetime:
    """Naive datetimes are taken as UTC; aware ones are converted to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _check_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > MAX_WINDOW:
        raise ValueError(f"time window must be at most {MAX_WINDOW.days} days")
    return start, end


def _floor(value: datetime, step_seconds: int) -> datetime:
    """Round down to a bucket boundary (matches ClickHouse toStartOfInterval)."""
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step_seconds, tz=UTC)


_TIMELINE_SQL = """
SELECT toStartOfInterval(timestamp, {interval}) AS bucket_start, count() AS n
FROM log_events
WHERE tenant_id = {{tenant_id:String}}
    AND fingerprint = {{fingerprint:String}}
    AND timestamp >= {{start:DateTime64(3)}}
    AND timestamp < {{end:DateTime64(3)}}
GROUP BY bucket_start
ORDER BY bucket_start
LIMIT {{max_rows:UInt32}}
"""


async def get_error_timeline(backend: LogBackend, *, tenant_id: UUID, fingerprint: str, start: datetime, end: datetime, bucket: Bucket = "1m") -> ErrorTimeline:
    """Occurrences of one fingerprint per time bucket, with empty buckets as zeros."""
    if bucket not in _BUCKET_SECONDS:
        raise ValueError(f"bucket must be one of {sorted(_BUCKET_SECONDS)}")
    start, end = _check_window(start, end)
    step = _BUCKET_SECONDS[bucket]
    first = _floor(start, step)
    last = _floor(end, step)
    slots = int((last - first).total_seconds() // step) + 1
    if slots > MAX_TIMELINE_BUCKETS:
        raise ValueError(f"window gives {slots} buckets (max {MAX_TIMELINE_BUCKETS}); use a larger bucket")
    
    rows = await backend.query(
        _TIMELINE_SQL.format(interval=_BUCKET_SQL[bucket]),
        tenant_id=tenant_id,
        parameters={
            "fingerprint": fingerprint,
            "start": first,
            "end": last + timedelta(seconds=step),
            "max_rows": MAX_TIMELINE_BUCKETS,
        },
    )
    counts = {_utc(r["bucket_start"]): int(r["n"]) for r in rows}
    buckets = [
        TimelineBucket(
            bucket_start=first + timedelta(seconds=step * i),
            count=counts.get(first + timedelta(seconds=step * i), 0),
        )
        for i in range(slots)
    ]
    peak = max(buckets, key=lambda b: b.count) if any(b.count for b in buckets) else None
    return ErrorTimeline(
        fingerprint=fingerprint,
        bucket=bucket,
        start=first,
        end=last + timedelta(seconds=step),
        buckets=buckets,
        total=sum(b.count for b in buckets),
        peak=peak,
    )


_COOCCURRING_SQL = """
SELECT service_name, fingerprint, any(message_template) AS message_template,
       count() AS n, min(timestamp) AS first_ts, max(timestamp) AS last_ts
FROM log_events
WHERE tenant_id = {{tenant_id:String}}
  AND timestamp >= {{start:DateTime64(3)}}
  AND timestamp < {{end:DateTime64(3)}}
  AND level IN ('WARN', 'ERROR', 'FATAL')
{exclude}
GROUP BY service_name, fingerprint
ORDER BY n DESC
LIMIT {{max_rows:UInt32}}
"""
_EXCLUDE_FINGERPRINT = "  AND fingerprint != {exclude_fingerprint:String}"


async def get_cooccurring_errors(backend: LogBackend, *, tenant_id: UUID, start: datetime, end: datetime, exclude_fingerprint: str | None = None, limit: int = 20) -> list[CooccurringError]:
    """What else was failing in the window (WARN/ERROR/FATAL), across all services."""
    start, end = _check_window(start, end)
    if not 1 <= limit <= MAX_COOCCURRING_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_COOCCURRING_LIMIT}")
    parameters: dict[str, Any] = {"start": start, "end": end, "max_rows": limit}
    if exclude_fingerprint is not None:
        parameters["exclude_fingerprint"] = exclude_fingerprint
    rows = await backend.query(
        _COOCCURRING_SQL.format(exclude=_EXCLUDE_FINGERPRINT if exclude_fingerprint is not None else ""),
        tenant_id=tenant_id,
        parameters=parameters,
    )
    return [
        CooccurringError(
            service_name=r["service_name"],
            fingerprint=r["fingerprint"],
            message_template=r["message_template"],
            count=int(r["n"]),
            first_seen=_utc(r["first_ts"]),
            last_seen=_utc(r["last_ts"]),
        )
        for r in rows
    ]


_SEARCH_SQL = """
SELECT timestamp, service_name, level, message, fingerprint
FROM log_events
WHERE tenant_id = {{tenant_id:String}}
  AND timestamp >= {{start:DateTime64(3)}}
  AND timestamp < {{end:DateTime64(3)}}
{filters}
ORDER BY timestamp DESC
LIMIT {{max_rows:UInt32}}
"""


async def search_logs(
    backend: LogBackend,
    *,
    tenant_id: UUID,
    start: datetime,
    end: datetime,
    query: str | None = None,
    service: str | None = None,
    level: str | None = None,
    limit: int = 50,
) -> LogSearchResult:
    """Deterministic log filter (substring, service, level). Never semantic search."""
    start, end = _check_window(start, end)
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    parameters: dict[str, Any] = {"start": start, "end": end, "max_rows": limit + 1}
    filters: list[str] = []
    if query:
        filters.append("  AND positionCaseInsensitive(message, {query:String}) > 0")
        parameters["query"] = query
    if service:
        filters.append("  AND service_name = {service:String}")
        parameters["service"] = service
    if level:
        level = level.upper()
        if level not in _LEVELS:
            raise ValueError(f"level must be one of {sorted(_LEVELS)}")
        filters.append("  AND level = {level:String}")
        parameters["level"] = level

    rows = await backend.query(
        _SEARCH_SQL.format(filters="\n".join(filters)),
        tenant_id=tenant_id,
        parameters=parameters,
    )
    limit_reached = len(rows) > limit  # we asked for one extra row to detect this
    lines = [
        LogLine(
            timestamp=_utc(r["timestamp"]),
            service_name=r["service_name"],
            level=r["level"],
            message=r["message"][:MESSAGE_MAX_CHARS],
            message_truncated=len(r["message"]) > MESSAGE_MAX_CHARS,
            fingerprint=r["fingerprint"],
        )
        for r in rows[:limit]
    ]
    return LogSearchResult(lines=lines, limit_reached=limit_reached)
