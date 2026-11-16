"""Response shape of ``/api/v1/deploy-targets``. The request body is ``DeployTargetConfig`` itself."""

from datetime import datetime
from uuid import UUID

from app.deploy.target import DeployTargetConfig
from app.schemas.base import CamelModel


class DeployTargetOut(CamelModel):
    repository_id: UUID
    config: DeployTargetConfig
    updated_at: datetime
