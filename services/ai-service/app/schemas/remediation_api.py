"""Response shapes of ``/api/v1/remediations``."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict

from app.schemas.base import CamelModel


class DeploymentOut(CamelModel):
    kind: str
    status: str
    sha: str
    run_url: str | None = None
    dispatched_at: datetime
    finished_at: datetime | None = None


class VerificationOut(CamelModel):
    kind: str  # "deploy", or "rollback" once the rollback was judged
    verdict: str
    numbers: dict[str, Any]
    window_end: datetime
    created_at: datetime


class DecisionIn(CamelModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["keep", "extend", "rollback"]


class RemediationOut(CamelModel):
    id: UUID
    pull_request_id: UUID
    investigation_id: UUID
    repository_id: UUID
    environment: str
    merge_sha: str
    rollback_target: str | None = None
    status: str
    baseline: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    wake_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    deployment: DeploymentOut | None = None
    rollback: DeploymentOut | None = None  # the pipeline run that put the previous commit back, once there is one
    verification: VerificationOut | None = None  # the latest verdict, with the numbers it compared
