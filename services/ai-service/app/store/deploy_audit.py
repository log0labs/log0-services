"""Persistence for ai.deploy_audit: one row for every approval, dispatch and outcome of a remediation.

Insert and read only. The table itself rejects updates, deletes and truncates (a trigger from the migration), so what is
written here is what the history says. The detail is redacted before it is stored: an audit must not become a place where
a token or a secret survives.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import deploy_audit
from app.security.redaction import redact_text

MAX_DETAIL = 500


@dataclass(frozen=True)
class AuditRow:
    id: int
    tenant_id: UUID
    remediation_id: UUID | None
    actor_id: UUID | None
    action: str
    environment: str
    sha: str
    result: str
    detail: str
    created_at: datetime


async def append(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    remediation_id: UUID | None,
    action: str,
    result: str,
    actor_id: UUID | None = None,
    environment: str = "",
    sha: str = "",
    detail: str = "",
) -> None:
    await session.execute(
        deploy_audit.insert().values(
            tenant_id=tenant_id,
            remediation_id=remediation_id,
            actor_id=actor_id,
            action=action,
            environment=environment,
            sha=sha,
            result=result,
            detail=redact_text(detail)[:MAX_DETAIL],
        )
    )
    await session.commit()


async def list_for_remediation(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID) -> list[AuditRow]:
    result = await session.execute(
        select(deploy_audit).where(deploy_audit.c.tenant_id == tenant_id, deploy_audit.c.remediation_id == remediation_id).order_by(deploy_audit.c.id)
    )
    return [AuditRow(**{name: row._mapping[name] for name in AuditRow.__dataclass_fields__}) for row in result]
