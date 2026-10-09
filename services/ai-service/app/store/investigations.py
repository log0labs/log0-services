"""Persistence for ai.investigation."""

from uuid import UUID

from sqlalchemy import insert, select
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
