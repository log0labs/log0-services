"""Persistence for ai.verification: what log0 concluded about a deploy, with the numbers it compared.

A remediation can have several (a soak that was extended is judged again). Every function filters by ``tenant_id``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import verification
from app.schemas.remediation import DeployKind, Verdict


@dataclass(frozen=True)
class VerificationRow:
    id: UUID
    tenant_id: UUID
    remediation_id: UUID
    kind: str
    verdict: str
    numbers: dict[str, Any]
    window_start: datetime
    window_end: datetime
    created_at: datetime


def _to_row(row) -> VerificationRow:
    m = row._mapping
    return VerificationRow(**{name: m[name] for name in VerificationRow.__dataclass_fields__})


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    remediation_id: UUID,
    kind: DeployKind,
    verdict: Verdict,
    numbers: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> VerificationRow:
    row = (
        await session.execute(
            verification.insert()
            .values(tenant_id=tenant_id, remediation_id=remediation_id, kind=kind, verdict=verdict, numbers=numbers, window_start=window_start, window_end=window_end)
            .returning(verification)
        )
    ).first()
    await session.commit()
    assert row is not None
    return _to_row(row)


async def latest(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID) -> VerificationRow | None:
    row = (
        await session.execute(
            select(verification)
            .where(verification.c.tenant_id == tenant_id, verification.c.remediation_id == remediation_id)
            .order_by(verification.c.created_at.desc(), verification.c.id.desc())
            .limit(1)
        )
    ).first()
    return _to_row(row) if row else None
