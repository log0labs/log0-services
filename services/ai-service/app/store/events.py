"""Persistence for ai.investigation_event (append-only, ordered by seq)."""

from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import investigation, investigation_event
from app.schemas.investigation import InvestigationEvent

_MAX_APPEND_ATTEMPTS = 3


def _row_to_event(row) -> InvestigationEvent:
    """Convert a row from the investigation_event table to an InvestigationEvent schema object."""
    m = row._mapping
    return InvestigationEvent(
        investigation_id=m["investigation_id"],
        seq=m["seq"],
        type=m["type"],
        payload=m["payload"],
        created_at=m["created_at"],
    )


async def append_event(
    session: AsyncSession,
    *,
    investigation_id: UUID,
    type: str,
    payload: dict | None = None,
) -> InvestigationEvent:
    """Insert the next event; ``seq`` = current max + 1, computed inside the INSERT.

    Two concurrent writers for the same investigation can compute the same seq;
    the composite PK rejects the loser, which retries with a fresh max.
    """
    ev = investigation_event
    next_seq = (
        select(func.coalesce(func.max(ev.c.seq), 0) + 1)
        .where(ev.c.investigation_id == investigation_id)
        .scalar_subquery()
    )
    stmt = (
        insert(ev)
        .values(
            investigation_id=investigation_id,
            seq=next_seq,
            type=type,
            payload=payload or {},
        )
        .returning(ev)
    )
    for attempt in range(_MAX_APPEND_ATTEMPTS):
        try:
            result = await session.execute(stmt)
            await session.commit()
            return _row_to_event(result.one())
        except IntegrityError:
            await session.rollback()
            if attempt == _MAX_APPEND_ATTEMPTS - 1:
                raise
    raise RuntimeError("unreachable")


async def list_after(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
    after_seq: int = 0,
    limit: int = 500,
) -> list[InvestigationEvent]:
    """Events with seq > after_seq, oldest first. Tenant-scoped via the parent row."""
    ev = investigation_event
    stmt = (
        select(ev)
        .join(investigation, investigation.c.id == ev.c.investigation_id)
        .where(
            ev.c.investigation_id == investigation_id,
            investigation.c.tenant_id == tenant_id,
            ev.c.seq > after_seq,
        )
        .order_by(ev.c.seq.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [_row_to_event(row) for row in result.all()]
