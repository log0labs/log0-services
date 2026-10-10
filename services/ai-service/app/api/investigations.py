"""``/api/v1/investigations`` — JWT-protected investigation CRUD"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.investigation import (
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    Investigation,
)
from app.security.jwt import CurrentUser, get_current_user, require_investigation_writer
from app.store import investigations as investigation_store
from app.store.exceptions import ActiveInvestigationError

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateInvestigationResponse,
)
async def create_investigation(
    body: CreateInvestigationRequest,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CreateInvestigationResponse:
    try:
        row = await investigation_store.create(
            session,
            tenant_id=user.tenant_id,
            incident_id=body.incident_id,
            requested_by=user.user_id,
            scope=body.scope,
            question=body.question,
        )
    except ActiveInvestigationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An investigation is already active for this incident",
        ) from None
    # Runner/graph: M1.8 — intentionally not started here.
    return CreateInvestigationResponse(
        investigation_id=row.id,
        status=row.status,
    )


@router.get("", response_model=list[Investigation])
async def list_investigations(
    incident_id: Annotated[UUID, Query(alias="incidentId")],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[Investigation]:
    return await investigation_store.list_by_incident(
        session,
        tenant_id=user.tenant_id,
        incident_id=incident_id,
    )


@router.get("/{investigation_id}", response_model=Investigation)
async def get_investigation(
    investigation_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Investigation:
    row = await investigation_store.get_by_id(
        session,
        tenant_id=user.tenant_id,
        investigation_id=investigation_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row
