"""Persistence for ai.release_ledger: which commit each environment of a repository ran, and since when.

Append-only by convention (rows are only inserted). The newest row of an environment is what it runs now; that is the
commit a rollback goes back to, so it must be recorded *before* a deploy replaces it. Every function filters by
``tenant_id``.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import release_ledger
from app.schemas.remediation import ReleaseSource


@dataclass(frozen=True)
class Release:
    id: int
    tenant_id: UUID
    repository_id: UUID
    environment: str
    sha: str
    source: str
    remediation_id: UUID | None
    recorded_at: datetime


def _to_release(row) -> Release:
    m = row._mapping
    return Release(**{name: m[name] for name in Release.__dataclass_fields__})


async def current(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID, environment: str) -> Release | None:
    """What ``environment`` runs now, as far as the ledger knows."""
    rows = await history(session, tenant_id=tenant_id, repository_id=repository_id, environment=environment, limit=1)
    return rows[0] if rows else None


async def history(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID, environment: str, limit: int = 20) -> list[Release]:
    """The newest releases of an environment first."""
    result = await session.execute(
        select(release_ledger)
        .where(release_ledger.c.tenant_id == tenant_id, release_ledger.c.repository_id == repository_id, release_ledger.c.environment == environment)
        .order_by(release_ledger.c.recorded_at.desc(), release_ledger.c.id.desc())
        .limit(limit)
    )
    return [_to_release(row) for row in result]


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    repository_id: UUID,
    environment: str,
    sha: str,
    source: ReleaseSource,
    remediation_id: UUID | None = None,
) -> Release | None:
    """Note that ``environment`` now runs ``sha``. Returns ``None`` (and writes nothing) when it was already the newest entry.

    Recording the same commit twice (a seed, then our own deploy of it) would make the "previous" release equal to the
    current one and a rollback a no-op, so a repeat is dropped.
    """
    latest = await current(session, tenant_id=tenant_id, repository_id=repository_id, environment=environment)
    if latest is not None and latest.sha == sha:
        return None
    row = (
        await session.execute(
            release_ledger.insert()
            .values(tenant_id=tenant_id, repository_id=repository_id, environment=environment, sha=sha, source=source, remediation_id=remediation_id)
            .returning(release_ledger)
        )
    ).first()
    await session.commit()
    assert row is not None
    return _to_release(row)
