"""The production ``Publisher``: GitHub through the App, bookkeeping in Postgres, every write audited.

``target_for`` is the single place that decides whether log0 may write to a repository: the global
kill switch, the repository's own switch, an active (not suspended) installation of the tenant, and a
GitHub repository id from linking. Every write re-resolves the target from the database, so nothing about
installations or tokens travels through the graph state, and a switch turned off between two steps is
honoured by the next one.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import GitHubAppNotConfiguredError
from app.integrations.github_writer import (
    BranchRefusedError,
    GitHubWriter,
    WriteRejectedError,
    WriteTarget,
)
from app.publishing.naming import branch_name
from app.publishing.policy import PrActivity
from app.publishing.port import PublishError, PublishFile, PublishTarget
from app.schemas.domain import BranchRef, CheckResult, CiRun, PullRequestRef
from app.store import github_installations as installations
from app.store import pull_requests, test_runs, write_audit
from app.store import repositories as repository_store

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ACCESS = {
    "bad_token": "GitHub rejected the log0 app's credentials",
    "forbidden": "GitHub refused access to the repository (is the log0 app still installed with write access?)",
    "not_found": "GitHub cannot find the repository or the installation (was the app uninstalled?)",
}


def _split(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


class GitHubPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        writer: GitHubWriter,
        *,
        enabled: bool,
        disabled_tenants: frozenset[UUID] = frozenset(),
    ) -> None:
        self._sessions = session_factory
        self._writer = writer
        self._enabled = enabled
        self._disabled_tenants = disabled_tenants

    async def target_for(self, *, tenant_id: UUID, repo: str) -> PublishTarget | None:
        if not self._enabled or tenant_id in self._disabled_tenants:
            return None
        owner, name = _split(repo)
        async with self._sessions() as session:
            row = await repository_store.get_by_name(session, tenant_id=tenant_id, owner=owner, name=name)
            if row is None:
                return None
            resolved = await self._write_target(session, tenant_id, row)
        return PublishTarget(row.id, f"{row.owner}/{row.name}", row.default_branch) if resolved else None

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
        write_target = await self._resolve(tenant_id, target)

        async def make() -> BranchRef:
            state = await self._writer.ensure_branch(write_target, name, base_sha, str(investigation_id))
            async with self._sessions() as session:
                await pull_requests.start(
                    session,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    repository_id=target.repository_id,
                    branch=name,
                    base_sha=base_sha,
                    approved_by=approved_by,
                )
            return BranchRef(name=state.name, base_sha=base_sha)

        return await self._audited(make, tenant_id, investigation_id, approved_by, "branch.create", target.repo, name)

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
        write_target = await self._resolve(tenant_id, target)

        async def make() -> str:
            head = await self._writer.head_of(write_target, branch.name)
            if head is not None and head != branch.base_sha:
                sha = head  # an earlier attempt already committed (ensure_branch checked the branch is ours)
            else:
                sha = await self._writer.commit_files(write_target, branch.name, branch.base_sha, files, message)
            async with self._sessions() as session:
                await pull_requests.mark_committed(session, tenant_id=tenant_id, investigation_id=investigation_id, commit_sha=sha)
            return sha

        return await self._audited(make, tenant_id, investigation_id, None, "commit.create", target.repo, branch.name)

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
        write_target = await self._resolve(tenant_id, target)

        async def make() -> PullRequestRef:
            opened = await self._writer.open_draft_pull_request(write_target, branch.name, title, body, tuple(labels))
            async with self._sessions() as session:
                await pull_requests.mark_open(session, tenant_id=tenant_id, investigation_id=investigation_id, number=opened.number, url=opened.url)
            return PullRequestRef(number=opened.number, url=opened.url, branch=branch.name, commit_sha=commit_sha)

        return await self._audited(make, tenant_id, investigation_id, None, "pull_request.open", target.repo, branch.name)

    async def activity(self, *, tenant_id: UUID, target: PublishTarget) -> PrActivity:
        since = datetime.now(UTC) - timedelta(hours=1)
        async with self._sessions() as session:
            return PrActivity(
                repository_last_hour=await pull_requests.count_since(session, tenant_id=tenant_id, since=since, repository_id=target.repository_id),
                tenant_last_hour=await pull_requests.count_since(session, tenant_id=tenant_id, since=since),
            )

    async def check_results(self, *, tenant_id: UUID, target: PublishTarget, commit_sha: str) -> list[CheckResult]:
        write_target = await self._resolve(tenant_id, target)
        try:
            return await self._writer.check_runs(write_target, commit_sha)
        except (GitHubAccessError, GitHubUnavailableError, GitHubAppNotConfiguredError) as exc:
            raise PublishError(self._safe_message(exc)) from None

    async def record_ci_run(self, *, tenant_id: UUID, investigation_id: UUID, commit_sha: str, run: CiRun) -> None:
        try:
            async with self._sessions() as session:
                await test_runs.record(session, tenant_id=tenant_id, investigation_id=investigation_id, commit_sha=commit_sha, run=run)
        except Exception:
            logger.exception("could not record the CI run of investigation %s", investigation_id)

    # ------------------------------------------------------------------ internals

    async def _write_target(self, session: AsyncSession, tenant_id: UUID, row: repository_store.RepositoryConnection) -> WriteTarget | None:
        if not (row.pr_writes_enabled and row.installation_id and row.github_repository_id):
            return None
        installation = await installations.get(session, tenant_id=tenant_id, installation_id=row.installation_id)
        if installation is None or installation.suspended:
            return None
        return WriteTarget(row.owner, row.name, row.installation_id, row.github_repository_id, row.default_branch)

    async def _resolve(self, tenant_id: UUID, target: PublishTarget) -> WriteTarget:
        """The write target for ``target``, re-checked now; ``PublishError`` if writing is no longer allowed."""
        if not self._enabled or tenant_id in self._disabled_tenants:
            raise PublishError("writing to repositories is switched off")
        async with self._sessions() as session:
            row = await repository_store.get(session, tenant_id=tenant_id, repository_id=target.repository_id)
            resolved = await self._write_target(session, tenant_id, row) if row else None
        if resolved is None:
            raise PublishError("writing to this repository is no longer allowed")
        return resolved

    async def _audited(
        self,
        operation: Callable[[], Awaitable[T]],
        tenant_id: UUID,
        investigation_id: UUID,
        actor_id: UUID | None,
        action: str,
        repo: str,
        ref: str,
    ) -> T:
        try:
            result = await operation()
        except (BranchRefusedError, WriteRejectedError, GitHubAccessError, GitHubUnavailableError, GitHubAppNotConfiguredError) as exc:
            error = self._safe_message(exc)
            await self._audit(tenant_id, investigation_id, actor_id, action, repo, ref, "error", error)
            raise PublishError(error) from None
        await self._audit(tenant_id, investigation_id, actor_id, action, repo, ref, "ok", "")
        return result

    @staticmethod
    def _safe_message(exc: Exception) -> str:
        if isinstance(exc, GitHubAccessError):
            return _ACCESS[exc.problem]
        if isinstance(exc, BranchRefusedError | WriteRejectedError):
            return str(exc)
        if isinstance(exc, GitHubAppNotConfiguredError):
            return "the GitHub App is not configured on this server"
        return "GitHub could not be reached"

    async def _audit(self, tenant_id: UUID, investigation_id: UUID, actor_id: UUID | None, action: str, repo: str, ref: str, result: str, detail: str) -> None:
        """The write already happened (or failed): a broken audit must not undo the run, but it is logged loudly."""
        try:
            async with self._sessions() as session:
                await write_audit.record(
                    session,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    actor_id=actor_id,
                    action=action,
                    repository=repo,
                    target=ref,
                    result=result,
                    detail=detail,
                )
        except Exception:
            logger.exception("could not record the audit of %s on %s", action, repo)
