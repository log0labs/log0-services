"""Request and response shapes of ``/api/v1/github``."""

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from app.schemas.base import CamelModel


class GitHubAppOut(CamelModel):
    configured: bool
    writes_enabled: bool  # the global kill switch (PR_WRITES_ENABLED)
    install_url: str | None = None  # where an admin installs the app; carries a signed, short-lived state


class InstallationIn(CamelModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: int = Field(gt=0)
    state: str = Field(min_length=1, max_length=2048)


class InstallationOut(CamelModel):
    installation_id: int
    account_login: str
    account_type: str
    suspended: bool
    created_at: datetime


class InstalledRepositoryOut(CamelModel):
    github_id: int
    owner: str
    name: str
    default_branch: str
    private: bool
    repository_id: UUID | None = None  # set when the tenant also connected it in Settings, Repositories
    writes_enabled: bool = False


class WritesIn(CamelModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AuditEntryOut(CamelModel):
    id: int
    investigation_id: UUID | None
    actor_id: UUID | None
    action: str
    repository: str
    target: str
    result: str
    detail: str
    created_at: datetime
