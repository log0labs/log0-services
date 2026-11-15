"""The pull request log0 opened for an investigation, as the API returns it."""

from datetime import datetime
from typing import Literal

from app.schemas.base import CamelModel


class PullRequestOut(CamelModel):
    state: Literal["branch_created", "committed", "open", "merged", "closed"]
    repository: str | None  # "owner/name"; None if the repository was disconnected since
    branch: str
    base_sha: str
    commit_sha: str | None = None
    number: int | None = None
    url: str | None = None
    created_at: datetime
    updated_at: datetime
