"""Persistence for ai.pull_request: the branch, commit and pull request log0 made for one investigation.

One row per investigation (unique key). Every function filters by ``tenant_id``. The row is created
when the branch is made and advances ``branch_created -> committed -> open``; the writes are
idempotent so a resumed run can call them again.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import pull_request, repository_connection


@dataclass(frozen=True)
class PullRequestRow:
    id: UUID
    tenant_id: UUID
    investigation_id: UUID
    repository_id: UUID
    branch: str
    base_sha: str
    commit_sha: str | None
    number: int | None
    url: str | None
    state: str
    approved_by: UUID
    created_at: datetime
    updated_at: datetime
    merge_commit_sha: str | None = None
    merged_at: datetime | None = None


def _to_row(row) -> PullRequestRow:
    m = row._mapping
    return PullRequestRow(**{name: m[name] for name in PullRequestRow.__dataclass_fields__})


async def start(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
    repository_id: UUID,
    branch: str,
    base_sha: str,
    approved_by: UUID,
) -> PullRequestRow:
    """Record the branch (or return the row an earlier attempt of this investigation already made)."""
    statement = (
        insert(pull_request)
        .values(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            repository_id=repository_id,
            branch=branch,
            base_sha=base_sha,
            approved_by=approved_by,
        )
        .on_conflict_do_nothing(index_elements=[pull_request.c.investigation_id])
        .returning(pull_request)
    )
    created = (await session.execute(statement)).first()
    await session.commit()
    if created is not None:
        return _to_row(created)
    existing = await get_for_investigation(session, tenant_id=tenant_id, investigation_id=investigation_id)
    assert existing is not None  # the conflicting row belongs to this tenant: the investigation id is the tenant's own
    return existing


async def get_for_investigation(session: AsyncSession, *, tenant_id: UUID, investigation_id: UUID) -> PullRequestRow | None:
    result = await session.execute(
        select(pull_request).where(pull_request.c.tenant_id == tenant_id, pull_request.c.investigation_id == investigation_id)
    )
    row = result.first()
    return _to_row(row) if row else None


async def mark_committed(session: AsyncSession, *, tenant_id: UUID, investigation_id: UUID, commit_sha: str) -> None:
    await _advance(session, tenant_id, investigation_id, state="committed", commit_sha=commit_sha)


async def mark_open(session: AsyncSession, *, tenant_id: UUID, investigation_id: UUID, number: int, url: str) -> None:
    await _advance(session, tenant_id, investigation_id, state="open", number=number, url=url)


async def _advance(session: AsyncSession, tenant_id: UUID, investigation_id: UUID, **values: object) -> None:
    await session.execute(
        update(pull_request)
        .where(pull_request.c.tenant_id == tenant_id, pull_request.c.investigation_id == investigation_id)
        .values(updated_at=func.now(), **values)
    )
    await session.commit()


async def count_since(session: AsyncSession, *, tenant_id: UUID, since: datetime, repository_id: UUID | None = None) -> int:
    """How many pull requests (any state) this tenant, or one of its repositories, started since ``since``."""
    query = select(func.count()).select_from(pull_request).where(pull_request.c.tenant_id == tenant_id, pull_request.c.created_at >= since)
    if repository_id is not None:
        query = query.where(pull_request.c.repository_id == repository_id)
    return int((await session.execute(query)).scalar_one())


async def sync_state(
    session: AsyncSession,
    *,
    installation_id: int,
    github_repository_id: int,
    number: int,
    state: str,
    merge_commit_sha: str | None = None,
) -> PullRequestRow | None:
    """A pull request log0 opened was merged, closed or reopened on GitHub: record it.

    The row is found by what GitHub tells us (installation, repository id, pull request number) joined to the
    tenant's own repository connection, so an event for a pull request log0 did not open changes nothing. A merge also
    records the commit it made (``merge_commit_sha``) and when, which is what a deploy would ship.
    """
    merged = {"merge_commit_sha": merge_commit_sha, "merged_at": func.now()} if state == "merged" and merge_commit_sha else {}
    result = await session.execute(
        update(pull_request)
        .where(
            pull_request.c.number == number,
            pull_request.c.repository_id.in_(
                select(repository_connection.c.id).where(
                    repository_connection.c.installation_id == installation_id,
                    repository_connection.c.github_repository_id == github_repository_id,
                )
            ),
        )
        .values(state=state, updated_at=func.now(), **merged)
        .returning(pull_request)
    )
    row = result.first()
    await session.commit()
    return _to_row(row) if row else None
