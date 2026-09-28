"""Contract for ``POST /api/v1/summaries``.

Field-for-field compatible with the Java ``SummaryRequest`` that incident-service's
``AiSummarizer`` sends, so incident-service needs no change.
"""


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
