"""Incident evidence tools: the incident itself, and its history.

``get_incident`` reads the authoritative record through incident-service's API;
``find_related_incidents`` reads incident history (same fingerprint first, then
same service) from Postgres. Both take the tenant from the verified JWT and
return small, typed, capped objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.integrations.incident_service import IncidentNotFoundError

MAX_RELATED_LIMIT = 20
MAX_LOOKBACK_DAYS = 90
MAX_TOP_MESSAGES = 5
TEXT_MAX_CHARS = 500
SUMMARY_MAX_CHARS = 1000


class IncidentApi(Protocol):
    """incident-service client (or a fixture)."""

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict[str, Any]: ...


class RelatedIncidentSource(Protocol):
    """incident history reader (or a fixture)."""

    async def find_related(
        self,
        *,
        tenant_id: UUID,
        incident_id: UUID,
        fingerprint: str,
        service_name: str,
        since: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class IncidentContext(BaseModel):
    incident_id: UUID
    fingerprint: str
    service_name: str
    environment: str
    severity: str
    status: str
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    top_messages: list[str]
    ai_summary: str | None


class RelatedIncident(BaseModel):
    incident_id: UUID
    fingerprint: str
    service_name: str
    environment: str
    severity: str
    status: str
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    same_fingerprint: bool  # True: this exact error happened before (recurrence)
    ai_summary: str | None


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _cut(value: str | None, limit: int) -> str | None:
    return None if value is None else value[:limit]


async def get_incident(
    api: IncidentApi,
    *,
    tenant_id: UUID,
    incident_id: UUID,
) -> IncidentContext:
    """The authoritative incident. Raises IncidentNotFoundError for a wrong tenant too."""
    data = await api.get_incident(incident_id, tenant_id)
    # Defense in depth: never trust the remote service to have applied our tenant filter.
    if str(data.get("tenantId")) != str(tenant_id):
        raise IncidentNotFoundError(f"incident {incident_id} not found")
    resolved = data.get("resolvedAt")
    return IncidentContext(
        incident_id=UUID(str(data["incidentId"])),
        fingerprint=data["fingerprint"],
        service_name=data["serviceName"],
        environment=data["environment"],
        severity=data["severity"],
        status=data["status"],
        occurrence_count=int(data["occurrenceCount"]),
        first_seen_at=_parse_time(data["firstSeenAt"]),
        last_seen_at=_parse_time(data["lastSeenAt"]),
        resolved_at=_parse_time(resolved) if resolved else None,
        top_messages=[
            m[:TEXT_MAX_CHARS] for m in (data.get("topMessages") or [])[:MAX_TOP_MESSAGES]
        ],
        ai_summary=_cut(data.get("aiSummary"), SUMMARY_MAX_CHARS),
    )


async def find_related_incidents(
    source: RelatedIncidentSource,
    *,
    tenant_id: UUID,
    incident: IncidentContext,
    lookback_days: int = 30,
    limit: int = 10,
) -> list[RelatedIncident]:
    """Past occurrences of this exact error first, then same-service incidents."""
    if not 1 <= limit <= MAX_RELATED_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_RELATED_LIMIT}")
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}")
    rows = await source.find_related(
        tenant_id=tenant_id,
        incident_id=incident.incident_id,
        fingerprint=incident.fingerprint,
        service_name=incident.service_name,
        since=datetime.now(UTC) - timedelta(days=lookback_days),
        limit=limit,
    )
    return [
        RelatedIncident(
            incident_id=r["incident_id"],
            fingerprint=r["fingerprint"],
            service_name=r["service_name"],
            environment=r["environment"],
            severity=r["severity"],
            status=r["status"],
            occurrence_count=int(r["occurrence_count"]),
            first_seen_at=_parse_time(r["first_seen_at"]),
            last_seen_at=_parse_time(r["last_seen_at"]),
            resolved_at=_parse_time(r["resolved_at"]) if r["resolved_at"] else None,
            same_fingerprint=bool(r["same_fingerprint"]),
            ai_summary=_cut(r["ai_summary"], SUMMARY_MAX_CHARS),
        )
        for r in rows
    ]
