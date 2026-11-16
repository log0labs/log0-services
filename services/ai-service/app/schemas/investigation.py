"""Investigation API/store models (camelCase JSON at the boundary)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.base import CamelModel


class InvestigationStatus(StrEnum):
    """Investigation status values."""
    
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    AWAITING_PR_APPROVAL = "AWAITING_PR_APPROVAL"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_STATUSES = frozenset(
    {
        InvestigationStatus.QUEUED,
        InvestigationStatus.RUNNING,
        InvestigationStatus.AWAITING_REVIEW,
        InvestigationStatus.AWAITING_PR_APPROVAL,
        InvestigationStatus.PUBLISHING,
    }
)
"""Statuses that block a new investigation of the same incident (mirrors the partial unique index)."""

PAUSED_STATUSES = frozenset({InvestigationStatus.AWAITING_REVIEW, InvestigationStatus.AWAITING_PR_APPROVAL})
"""The run is parked at a human gate: nothing is executing and no concurrency slot is held."""


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


class ResumeInvestigationRequest(CamelModel):
    """The engineer's decision at the review gate."""

    action: Literal["approve", "refine", "stop"]
    note: str | None = Field(default=None, max_length=1000)  # required for refine: what to look at differently

    @model_validator(mode="after")
    def _refine_needs_guidance(self) -> "ResumeInvestigationRequest":
        self.note = (self.note or "").strip() or None
        if self.action == "refine" and (self.note is None or len(self.note) < 3):
            raise ValueError("refine needs a note saying what to look at differently")
        if self.action != "refine":
            self.note = None  # a note only means something when refining
        return self


class FeedbackRequest(CamelModel):
    """An engineer's opinion of a finished investigation's result: it feeds long-term memory."""

    verdict: Literal["helpful", "partly", "wrong"]
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _tidy_note(self) -> "FeedbackRequest":
        self.note = (self.note or "").strip() or None
        return self


class InvestigationEvent(CamelModel):
    """One row of the investigation event log (what the UI/SSE consume)."""

    investigation_id: UUID
    seq: int
    type: str
    payload: dict
    created_at: datetime


class LatestInvestigation(CamelModel):
    """The newest investigation of one incident, in the few fields a list needs (the incident list's AI column)."""

    incident_id: UUID
    investigation_id: UUID
    status: InvestigationStatus
    confidence_band: Literal["high", "medium", "low"] | None = None
    has_diff: bool = False
    has_pull_request: bool = False
    deploy_status: str | None = None  # the remediation status of the latest deploy that began, for a "deployed" or "rolled back" mark
    created_at: datetime
