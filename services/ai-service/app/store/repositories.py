"""Persistence for ai.repository_connection and ai.service_repository.

Every function takes ``tenant_id`` and puts it in the WHERE clause. A service can only be mapped
to a repository of the same tenant: the check is made here (answering "not found") and the
composite foreign key from 3.1 enforces it in the database as the backstop.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import repository_connection, service_repository
from app.security.crypto import EncryptedSecret


class DuplicateRepositoryError(Exception):
    """This tenant already connected this repository."""


class RepositoryNotFoundError(Exception):
    """No such repository for this tenant (another tenant's repository looks exactly like a missing one)."""


@dataclass(frozen=True)
class RepositoryConnection:
    id: UUID
    tenant_id: UUID
    provider: str
    owner: str
    name: str
    default_branch: str
    token_ciphertext: str = field(repr=False)
    key_id: str
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True)
class ServiceRepository:
    tenant_id: UUID
    service_name: str
    repository_id: UUID
    path_prefix: str


def _to_connection(row) -> RepositoryConnection:
    m = row._mapping
    return RepositoryConnection(
        id=m["id"],
        tenant_id=m["tenant_id"],
        provider=m["provider"],
        owner=m["owner"],
        name=m["name"],
        default_branch=m["default_branch"],
        token_ciphertext=m["token_ciphertext"],
        key_id=m["key_id"],
        created_by=m["created_by"],
        created_at=m["created_at"],
    )


def _to_service(row) -> ServiceRepository:
    m = row._mapping
    return ServiceRepository(
        tenant_id=m["tenant_id"],
        service_name=m["service_name"],
        repository_id=m["repository_id"],
        path_prefix=m["path_prefix"],
    )


# ------------------------------------------------------------------------- connections


async def create(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    owner: str,
    name: str,
    default_branch: str,
    token: EncryptedSecret,
    created_by: UUID,
) -> RepositoryConnection:
    statement = (
        insert(repository_connection)
        .values(
            tenant_id=tenant_id,
            provider="github",
            owner=owner,
            name=name,
            default_branch=default_branch,
            token_ciphertext=token.ciphertext,
            key_id=token.key_id,
            created_by=created_by,
        )
        .returning(repository_connection)
    )
    try:
        result = await session.execute(statement)
        await session.commit()
    except IntegrityError:  # the only constraint a validated request can break is the unique repository
        await session.rollback()
        raise DuplicateRepositoryError from None
    return _to_connection(result.one())


async def list_for_tenant(session: AsyncSession, *, tenant_id: UUID) -> list[RepositoryConnection]:
    result = await session.execute(
        select(repository_connection)
        .where(repository_connection.c.tenant_id == tenant_id)
        .order_by(repository_connection.c.owner, repository_connection.c.name)
    )
    return [_to_connection(row) for row in result.all()]


async def get(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID) -> RepositoryConnection | None:
    result = await session.execute(
        select(repository_connection).where(
            repository_connection.c.tenant_id == tenant_id, repository_connection.c.id == repository_id
        )
    )
    row = result.first()
    return _to_connection(row) if row else None


async def delete_one(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID) -> bool:
    """True if removed. The service mappings that used it go with it (ON DELETE CASCADE)."""
    result = await session.execute(
        delete(repository_connection).where(
            repository_connection.c.tenant_id == tenant_id, repository_connection.c.id == repository_id
        )
    )
    await session.commit()
    return result.rowcount > 0


# ------------------------------------------------------------------- service mappings


async def list_services(session: AsyncSession, *, tenant_id: UUID) -> list[ServiceRepository]:
    result = await session.execute(
        select(service_repository)
        .where(service_repository.c.tenant_id == tenant_id)
        .order_by(service_repository.c.service_name)
    )
    return [_to_service(row) for row in result.all()]


async def get_service(session: AsyncSession, *, tenant_id: UUID, service_name: str) -> ServiceRepository | None:
    result = await session.execute(
        select(service_repository).where(
            service_repository.c.tenant_id == tenant_id, service_repository.c.service_name == service_name
        )
    )
    row = result.first()
    return _to_service(row) if row else None


async def upsert_service(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    service_name: str,
    repository_id: UUID,
    path_prefix: str,
) -> ServiceRepository:
    """Map a service to a repository of the same tenant (replacing any earlier mapping)."""
    if await get(session, tenant_id=tenant_id, repository_id=repository_id) is None:
        raise RepositoryNotFoundError
    values = {"repository_id": repository_id, "path_prefix": path_prefix}
    statement = (
        insert(service_repository)
        .values(tenant_id=tenant_id, service_name=service_name, **values)
        .on_conflict_do_update(
            index_elements=[service_repository.c.tenant_id, service_repository.c.service_name], set_=values
        )
        .returning(service_repository)
    )
    try:
        result = await session.execute(statement)
        await session.commit()
    except IntegrityError:  # the repository was disconnected between the check and the insert
        await session.rollback()
        raise RepositoryNotFoundError from None
    return _to_service(result.one())


async def delete_service(session: AsyncSession, *, tenant_id: UUID, service_name: str) -> bool:
    result = await session.execute(
        delete(service_repository).where(
            service_repository.c.tenant_id == tenant_id, service_repository.c.service_name == service_name
        )
    )
    await session.commit()
    return result.rowcount > 0
