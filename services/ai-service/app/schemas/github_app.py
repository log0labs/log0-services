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


class PrPolicyIn(CamelModel):
    """A workspace's pull request rules. An omitted limit means "the server's value"."""

    model_config = ConfigDict(extra="forbid")

    only_on_green: bool = False
    max_files: int | None = Field(default=None, ge=1)
    max_changed_lines: int | None = Field(default=None, ge=1)
    max_per_repository_per_hour: int | None = Field(default=None, ge=1)
    max_per_tenant_per_hour: int | None = Field(default=None, ge=1)


class PrPolicyLimits(CamelModel):
    max_files: int
    max_changed_lines: int
    max_per_repository_per_hour: int
    max_per_tenant_per_hour: int


class PrPolicyOut(CamelModel):
    only_on_green: bool
    max_files: int | None
    max_changed_lines: int | None
    max_per_repository_per_hour: int | None
    max_per_tenant_per_hour: int | None
    server: PrPolicyLimits  # the ceilings: a workspace can only be stricter
    server_only_on_green: bool  # when true the workspace cannot switch it off
