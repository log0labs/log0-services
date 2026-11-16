"""What an environment runs now: the commit a rollback would go back to.

GitHub is asked first because it sees deployments log0 did not make; when it knows nothing (many pipelines never create a
GitHub deployment) the ledger, which holds log0's own deploys and earlier answers, is used. What GitHub reports is written
into the ledger so the next answer does not depend on GitHub being reachable.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.github_deploys import DeploysPort
from app.integrations.github_writer import WriteTarget
from app.store import releases


async def deployed_sha(
    session: AsyncSession,
    deploys: DeploysPort,
    target: WriteTarget,
    *,
    tenant_id: UUID,
    repository_id: UUID,
    environment: str,
) -> str | None:
    """The commit ``environment`` runs now, or ``None`` when neither GitHub nor the ledger knows.

    ``GitHubUnavailableError`` and ``GitHubAccessError`` propagate: the caller (preflight) turns them into a refusal,
    because a rollback target that might be out of date is worse than none.
    """
    reported = await deploys.last_successful_deployment(target, environment)
    if reported is not None:
        await releases.record(session, tenant_id=tenant_id, repository_id=repository_id, environment=environment, sha=reported, source="github")
        return reported
    known = await releases.current(session, tenant_id=tenant_id, repository_id=repository_id, environment=environment)
    return known.sha if known else None
