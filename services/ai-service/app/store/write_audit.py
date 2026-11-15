"""Persistence for ai.write_audit: an append-only record of every write log0 makes to a repository.

There is no update and no delete here on purpose. The table has no foreign key to the investigation, so
the record outlives it. Nothing secret is stored: the repository name, a ref or sha, and a short safe message.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import write_audit

MAX_DETAIL = 500


@dataclass(frozen=True)
class AuditEntry:
    id: int
    tenant_id: UUID
    investigation_id: UUID | None
    actor_id: UUID | None
    action: str
    repository: str
    target: str
    result: str
    detail: str
    created_at: datetime


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID | None,
    actor_id: UUID | None,
    action: str,
    repository: str,
    target: str = "",
    result: str,
    detail: str = "",
) -> None:
    await session.execute(
        insert(write_audit).values(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            actor_id=actor_id,
            action=action,
            repository=repository,
            target=target,
            result=result,
            detail=detail[:MAX_DETAIL],
        )
    )
    await session.commit()


async def list_for_tenant(session: AsyncSession, *, tenant_id: UUID, limit: int = 100) -> list[AuditEntry]:
    result = await session.execute(
        select(write_audit).where(write_audit.c.tenant_id == tenant_id).order_by(write_audit.c.id.desc()).limit(limit)
    )
    return [AuditEntry(**{name: row._mapping[name] for name in AuditEntry.__dataclass_fields__}) for row in result.all()]
