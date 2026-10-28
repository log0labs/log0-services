"""Persistence for ai.investigation."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import investigation
from app.schemas.investigation import (
    Investigation,
    InvestigationScope,
    InvestigationStatus,
)
from app.store.exceptions import ActiveInvestigationError


def _row_to_investigation(row) -> Investigation:
    m = row._mapping
    scope_raw = m["scope"]
    scope = (
        InvestigationScope.model_validate(scope_raw)
        if isinstance(scope_raw, dict)
        else InvestigationScope()
    )
    return Investigation(
        id=m["id"],
        incident_id=m["incident_id"],
        requested_by=m["requested_by"],
        status=InvestigationStatus(m["status"]),
        question=m["question"],
        scope=scope,
        provider=m["provider"],
        model=m["model"],
        prompt_version=m["prompt_version"],
        graph_version=m["graph_version"],
        repo_sha=m["repo_sha"],
        root_cause=m["root_cause"],
        recommendation=m["recommendation"],
        diff=m["diff"],
        confidence_band=m["confidence_band"],
        error=m["error"],
        input_tokens=m["input_tokens"],
        output_tokens=m["output_tokens"],
        cost_usd=m["cost_usd"],
        tool_call_count=m["tool_call_count"],
        started_at=m["started_at"],
        completed_at=m["completed_at"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


async def create(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    incident_id: UUID,
    requested_by: UUID,
    scope: InvestigationScope,
    question: str | None = None,
) -> Investigation:
    stmt = (
        insert(investigation)
        .values(
            tenant_id=tenant_id,
            incident_id=incident_id,
            requested_by=requested_by,
            status=InvestigationStatus.QUEUED.value,
            question=question,
            scope=scope.model_dump(mode="json", by_alias=True),
        )
        .returning(investigation)
    )
    try:
        result = await session.execute(stmt)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ActiveInvestigationError from exc
    return _row_to_investigation(result.one())


async def get_by_id(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
) -> Investigation | None:
    stmt = select(investigation).where(
        investigation.c.id == investigation_id,
        investigation.c.tenant_id == tenant_id,
    )
    result = await session.execute(stmt)
    row = result.first()
    return _row_to_investigation(row) if row else None


async def list_by_incident(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    incident_id: UUID,
) -> list[Investigation]:
    stmt = (
        select(investigation)
        .where(
            investigation.c.tenant_id == tenant_id,
            investigation.c.incident_id == incident_id,
        )
        .order_by(investigation.c.created_at.desc())
    )
    result = await session.execute(stmt)
    return [_row_to_investigation(row) for row in result.all()]


async def list_pending_for_runner(session: AsyncSession) -> list[tuple[UUID, UUID]]:
    """Investigation ids to schedule on startup (QUEUED or RUNNING)."""
    active = (InvestigationStatus.QUEUED.value, InvestigationStatus.RUNNING.value)
    stmt = select(investigation.c.id, investigation.c.tenant_id).where(investigation.c.status.in_(active))
    result = await session.execute(stmt)
    return [(row.id, row.tenant_id) for row in result.all()]


async def mark_running(session: AsyncSession, *, investigation_id: UUID, tenant_id: UUID) -> bool:
    """QUEUED or RUNNING → RUNNING; set started_at if unset."""
    now = datetime.now(tz=UTC)
    stmt = (
        update(investigation)
        .where(
            investigation.c.id == investigation_id,
            investigation.c.tenant_id == tenant_id,
            investigation.c.status.in_((InvestigationStatus.QUEUED.value, InvestigationStatus.RUNNING.value)),
        )
        .values(status=InvestigationStatus.RUNNING.value, started_at=func.coalesce(investigation.c.started_at, now), updated_at=now)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0



async def transition_status(session: AsyncSession, *, investigation_id: UUID, tenant_id: UUID, status: InvestigationStatus, error: str | None = None, set_completed: bool = False) -> None:
    now = datetime.now(tz=UTC)
    values: dict = {
        "status": status.value,
        "updated_at": now,
    }
    if error is not None:
        values["error"] = error
    if set_completed:
        values["completed_at"] = now
        
    stmt = (
        update(investigation)
        .where(
            investigation.c.id == investigation_id,
            investigation.c.tenant_id == tenant_id,
        )
        .values(**values)
    )
    await session.execute(stmt)
    await session.commit()
    