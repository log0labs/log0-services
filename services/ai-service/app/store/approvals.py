"""Persistence for ai.approval: what a human answered at a deploy gate, who they were, and about which commit.

The API writes the approval before the run is resumed, so the table is the durable hand-over between the click and the
graph (it survives a restart in between). Every function filters by ``tenant_id``.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import approval
from app.schemas.remediation import Decision, Gate


@dataclass(frozen=True)
class ApprovalRow:
    id: UUID
    tenant_id: UUID
    remediation_id: UUID
    gate: str
    decision: str
    user_id: UUID
    role: str
    environment: str
    sha: str
    created_at: datetime


def _to_approval(row) -> ApprovalRow:
    m = row._mapping
    return ApprovalRow(**{name: m[name] for name in ApprovalRow.__dataclass_fields__})


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    remediation_id: UUID,
    gate: Gate,
    decision: Decision,
    user_id: UUID,
    role: str,
    environment: str,
    sha: str,
) -> ApprovalRow:
    row = (
        await session.execute(
            approval.insert()
            .values(tenant_id=tenant_id, remediation_id=remediation_id, gate=gate, decision=decision, user_id=user_id, role=role, environment=environment, sha=sha)
            .returning(approval)
        )
    ).first()
    await session.commit()
    assert row is not None
    return _to_approval(row)


async def latest(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, gate: Gate) -> ApprovalRow | None:
    """The newest answer at ``gate``, or ``None`` while nobody has answered."""
    row = (
        await session.execute(
            select(approval)
            .where(approval.c.tenant_id == tenant_id, approval.c.remediation_id == remediation_id, approval.c.gate == gate)
            .order_by(approval.c.created_at.desc(), approval.c.id.desc())
            .limit(1)
        )
    ).first()
    return _to_approval(row) if row else None


async def count(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, gate: Gate, decision: Decision | None = None) -> int:
    """How many answers were given at ``gate`` (optionally only ``decision`` ones): the number of extensions already used, for example."""
    query = select(func.count()).select_from(approval).where(approval.c.tenant_id == tenant_id, approval.c.remediation_id == remediation_id, approval.c.gate == gate)
    if decision is not None:
        query = query.where(approval.c.decision == decision)
    return int((await session.execute(query)).scalar_one())


async def nth(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, gate: Gate, index: int) -> ApprovalRow | None:
    """The ``index``-th answer (from 0, oldest first) at a gate that is asked more than once: round ``index`` reads answer ``index``."""
    row = (
        await session.execute(
            select(approval)
            .where(approval.c.tenant_id == tenant_id, approval.c.remediation_id == remediation_id, approval.c.gate == gate)
            .order_by(approval.c.created_at, approval.c.id)
            .offset(index)
            .limit(1)
        )
    ).first()
    return _to_approval(row) if row else None
