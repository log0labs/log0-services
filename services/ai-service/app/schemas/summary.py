"""Summary API contract + structured LLM output."""


from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.base import CamelModel


class SummaryRequest(CamelModel):
    """Incident context sent by incident-service when a new incident is created."""

    incident_id: UUID
    tenant_id: UUID
    service_name: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    occurrence_count: int = Field(ge=0)
    first_seen_at: datetime
    last_seen_at: datetime
    top_messages: list[str]


class IncidentSummary(CamelModel):
    """Structured summary from the LLM (stored later in M1; formatted for callback now)."""

    summary: str = Field(description="One sentence: what is failing")
    possible_cause: str = Field(
        description="1-2 sentences with conditional language: may be, likely, possibly"
    )
    recommended_actions: list[str] = Field(
        min_length=1,
        max_length=5,
        description="Short actionable bullets",
    )


def format_incident_context(request: SummaryRequest) -> str:
    """User message body"""
    lines = "\n".join(f' - "{msg}"' for msg in request.top_messages)
    return (
        f"Service: {request.service_name} ({request.environment})\n"
        f"Severity: {request.severity}\n"
        f"Occurrences: {request.occurrence_count}\n"
        f"First seen: {request.first_seen_at.isoformat()}\n"
        f"Last seen: {request.last_seen_at.isoformat()}\n"
        f"Top error messages:\n{lines}"
    )


def format_legacy_summary_text(summary: IncidentSummary) -> str:
    """Text format incident-service and the console already expect."""
    bullets = "\n".join(f"- {action}" for action in summary.recommended_actions)
    return (
        f"Summary: {summary.summary}\n"
        f"Possible Cause: {summary.possible_cause}\n"
        f"Recommended Actions:\n{bullets}"
    )
