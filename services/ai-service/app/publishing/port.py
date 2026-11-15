"""The graph's view of publishing: a small port, so nodes never see GitHub, databases or tokens.

``Publisher`` answers one question ("may log0 write to this repository, and where?") and performs
three writes (branch, commit, pull request). Production wires ``GitHubPublisher``; tests wire a
fake. Every method is safe to repeat: a resumed run calls them again and gets the earlier result.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.integrations.github_writer import FileWrite
from app.schemas.domain import BranchRef, CheckResult, CiRun, PullRequestRef

PublishFile = FileWrite
"""One file of the commit: path, whole new text (``None`` deletes it), executable bit."""


class PublishError(Exception):
    """A write could not be made. The message is written to be shown to the user inside a gap: it never
    contains a token, a URL with credentials or GitHub's raw answer."""


@dataclass(frozen=True)
class PublishTarget:
    """A repository log0 may open pull requests on. Opaque to the graph: ``repository_id`` is looked up
    again (tenant-scoped) by the publisher, so no installation or token detail travels through state."""

    repository_id: UUID
    repo: str  # "owner/name"
    default_branch: str


class Publisher(Protocol):
    async def target_for(self, *, tenant_id: UUID, repo: str) -> PublishTarget | None:
        """The target for ``owner/name``, or ``None`` when writing is not allowed (kill switch, writes off for
        the repository, no active installation, suspended)."""
        ...

    async def create_branch(
        self,
        *,
        tenant_id: UUID,
        target: PublishTarget,
        investigation_id: UUID,
        incident_id: UUID,
        base_sha: str,
        approved_by: UUID,
    ) -> BranchRef: ...

    async def commit(
        self,
        *,
        tenant_id: UUID,
        target: PublishTarget,
        investigation_id: UUID,
        branch: BranchRef,
        files: list[PublishFile],
        message: str,
    ) -> str:
        """One commit on the branch; returns its sha (the existing one when it was already made)."""
        ...

    async def open_pull_request(
        self,
        *,
        tenant_id: UUID,
        target: PublishTarget,
        investigation_id: UUID,
        branch: BranchRef,
        commit_sha: str,
        title: str,
        body: str,
        labels: list[str],
    ) -> PullRequestRef: ...

    async def check_results(self, *, tenant_id: UUID, target: PublishTarget, commit_sha: str) -> list[CheckResult]:
        """The CI checks GitHub has for the commit right now (empty: none have started or none exist)."""
        ...

    async def record_ci_run(self, *, tenant_id: UUID, investigation_id: UUID, commit_sha: str, run: CiRun) -> None:
        """Keep the outcome of the checks with the investigation (best effort: it is a record, not a decision)."""
        ...
