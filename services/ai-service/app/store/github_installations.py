"""Persistence for ai.github_installation.

An installation belongs to one tenant for good: linking it again from the same tenant refreshes
it, linking it from another tenant is refused (and looks like a missing installation to them).
Only ``set_suspended_by_installation`` ignores the tenant: it serves GitHub's signed webhook,
which identifies an installation by GitHub's own id.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import github_installation


class InstallationTakenError(Exception):
    """The installation is already linked to another tenant."""


@dataclass(frozen=True)
class Installation:
    id: UUID
    tenant_id: UUID
    installation_id: int
    account_login: str
    account_type: str
    suspended_at: datetime | None
    created_by: UUID
    created_at: datetime

    @property
    def suspended(self) -> bool:
        return self.suspended_at is not None


def _to_installation(row) -> Installation:
    m = row._mapping
    return Installation(**{name: m[name] for name in Installation.__dataclass_fields__})


async def link(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    installation_id: int,
    account_login: str,
    account_type: str,
    suspended: bool,
    created_by: UUID,
) -> Installation:
    """Link (or refresh) an installation for ``tenant_id``; ``InstallationTakenError`` if another tenant has it."""
    suspended_at = datetime.now(UTC) if suspended else None
    refresh = {"account_login": account_login, "account_type": account_type, "suspended_at": suspended_at}
    statement = (
        insert(github_installation)
        .values(
            tenant_id=tenant_id,
            installation_id=installation_id,
            created_by=created_by,
            **refresh,
        )
        .on_conflict_do_update(
            index_elements=[github_installation.c.installation_id],
            set_=refresh,
            where=github_installation.c.tenant_id == tenant_id,
        )
        .returning(github_installation)
    )
    row = (await session.execute(statement)).first()
    await session.commit()
    if row is None:  # the conflicting row belongs to someone else, so nothing was updated
        raise InstallationTakenError
    return _to_installation(row)


async def list_for_tenant(session: AsyncSession, *, tenant_id: UUID) -> list[Installation]:
    result = await session.execute(
        select(github_installation).where(github_installation.c.tenant_id == tenant_id).order_by(github_installation.c.account_login)
    )
    return [_to_installation(row) for row in result.all()]


async def get(session: AsyncSession, *, tenant_id: UUID, installation_id: int) -> Installation | None:
    result = await session.execute(
        select(github_installation).where(
            github_installation.c.tenant_id == tenant_id, github_installation.c.installation_id == installation_id
        )
    )
    row = result.first()
    return _to_installation(row) if row else None


async def delete_one(session: AsyncSession, *, tenant_id: UUID, installation_id: int) -> bool:
    result = await session.execute(
        delete(github_installation).where(
            github_installation.c.tenant_id == tenant_id, github_installation.c.installation_id == installation_id
        )
    )
    await session.commit()
    return result.rowcount > 0


async def set_suspended_by_installation(session: AsyncSession, *, installation_id: int, suspended: bool) -> bool:
    """For GitHub's webhook (identified by GitHub's installation id, signature already verified)."""
    result = await session.execute(
        update(github_installation)
        .where(github_installation.c.installation_id == installation_id)
        .values(suspended_at=datetime.now(UTC) if suspended else None)
    )
    await session.commit()
    return result.rowcount > 0


async def delete_by_installation(session: AsyncSession, *, installation_id: int) -> bool:
    """For GitHub's webhook: the app was uninstalled."""
    result = await session.execute(delete(github_installation).where(github_installation.c.installation_id == installation_id))
    await session.commit()
    return result.rowcount > 0
