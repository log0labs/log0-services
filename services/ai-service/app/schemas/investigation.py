"""Investigation API/store models (camelCase JSON at the boundary)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.schemas.base import CamelModel


class InvestigationStatus(StrEnum):
    """Investigation status values."""
    
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InvestigationScope(CamelModel):
    """POST body scope flags"""

    logs: bool = True
    history: bool = True
    code: bool = True
    runbooks: bool = True
    docs: bool = True


class Investigation(CamelModel):
    """Row shape returned by the store"""

    id: UUID
    incident_id: UUID
    requested_by: UUID
    status: InvestigationStatus
    question: str | None = None
    scope: InvestigationScope
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    graph_version: str | None = None
    repo_sha: str | None = None
    root_cause: dict | None = None
    recommendation: dict | None = None
    diff: str | None = None
    confidence_band: str | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    tool_call_count: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CreateInvestigationRequest(CamelModel):
    """Request body for creating a new investigation."""
    
    incident_id: UUID
    question: str | None = None
    scope: InvestigationScope = Field(default_factory=InvestigationScope)


class CreateInvestigationResponse(CamelModel):
    """Response to a create investigation request."""
    
    investigation_id: UUID
    status: InvestigationStatus


class InvestigationEvent(CamelModel):
    """One row of the investigation event log (what the UI/SSE consume)."""

    investigation_id: UUID
    seq: int
    type: str
    payload: dict
    created_at: datetime
