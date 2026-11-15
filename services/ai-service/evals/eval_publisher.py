"""A ``Publisher`` for the live pull request evaluation: real GitHub writes, no database.

The production publisher looks a repository up in Postgres (switches, installations, audit). The evaluation writes to
one throwaway repository whose installation is known, so this one has a fixed target and the same behaviour where it
matters: a branch under ``log0/`` created from the pinned commit, one commit per attempt built on the branch, a draft
pull request against the case's base branch, the repository's own checks read back, and a failed check's log
fetched for a repair. It uses the very same ``GitHubWriter`` as production, so what the evaluation measures is what
production would do.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar
from uuid import UUID, uuid4

from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import GitHubAppNotConfiguredError
from app.integrations.github_writer import (
    BranchRefusedError,
    GitHubWriter,
    WriteRejectedError,
    WriteTarget,
)
from app.publishing.github_publisher import describe_failure, failed_check_logs
from app.publishing.naming import branch_name
from app.publishing.policy import PrActivity
from app.publishing.port import PublishError, PublishFile, PublishTarget
from app.schemas.domain import BranchRef, CheckResult, CiRun, PullRequestRef

T = TypeVar("T")
_GITHUB_ERRORS = (BranchRefusedError, WriteRejectedError, GitHubAccessError, GitHubUnavailableError, GitHubAppNotConfiguredError)


class EvalPublisher:
    def __init__(self, writer: GitHubWriter, *, owner: str, name: str, installation_id: int, github_repository_id: int, base_branch: str) -> None:
        self._writer = writer
        self._target = WriteTarget(owner, name, installation_id, github_repository_id, base_branch)
        self._publish_target = PublishTarget(uuid4(), f"{owner}/{name}", base_branch)
        self.ci_runs: list[CiRun] = []  # every outcome the checks gave, in order: the first and the last matter

    async def _guard(self, work: Awaitable[T]) -> T:
        try:
            return await work
        except _GITHUB_ERRORS as exc:
            raise PublishError(describe_failure(exc)) from None

    async def target_for(self, *, tenant_id: UUID, repo: str) -> PublishTarget | None:
        return self._publish_target

    async def activity(self, *, tenant_id: UUID, target: PublishTarget) -> PrActivity:
        return PrActivity()

    async def create_branch(
        self,
        *,
        tenant_id: UUID,
        target: PublishTarget,
        investigation_id: UUID,
        incident_id: UUID,
        base_sha: str,
        approved_by: UUID,
    ) -> BranchRef:
        name = branch_name(incident_id, investigation_id)
        state = await self._guard(self._writer.ensure_branch(self._target, name, base_sha, str(investigation_id)))
        return BranchRef(name=state.name, base_sha=base_sha)

    async def commit(
        self,
        *,
        tenant_id: UUID,
        target: PublishTarget,
        investigation_id: UUID,
        branch: BranchRef,
        files: list[PublishFile],
        message: str,
        parent_sha: str | None = None,
    ) -> str:
        parent = parent_sha or branch.base_sha
        head = await self._guard(self._writer.head_of(self._target, branch.name))
        if head is not None and head != parent:
            return head
        return await self._guard(self._writer.commit_files(self._target, branch.name, branch.base_sha, files, message, parent_sha=parent))

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
    ) -> PullRequestRef:
        opened = await self._guard(self._writer.open_draft_pull_request(self._target, branch.name, title, body, tuple(labels)))
        return PullRequestRef(number=opened.number, url=opened.url, branch=branch.name, commit_sha=commit_sha)

    async def check_results(self, *, tenant_id: UUID, target: PublishTarget, commit_sha: str) -> list[CheckResult]:
        return await self._guard(self._writer.check_runs(self._target, commit_sha))

    async def ci_failure_log(self, *, tenant_id: UUID, target: PublishTarget, checks: list[CheckResult]) -> str:
        return await failed_check_logs(self._writer, self._target, checks)

    async def record_ci_run(self, *, tenant_id: UUID, investigation_id: UUID, commit_sha: str, run: CiRun) -> None:
        self.ci_runs.append(run)

