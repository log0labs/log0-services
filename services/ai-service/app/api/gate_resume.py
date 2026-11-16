"""Answering a human gate: record the decision, move the status, resume the run in the background.

Shared by the review endpoint and the pull-request endpoints. Two answers at once cannot both win: the status
change out of the gate's waiting status is a single conditional UPDATE. The decision is written to the event
log before the run is resumed, so it survives a restart between the click and the resume; if it cannot be
written the status is put back, otherwise the run would be lost.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.gates import Gate
from app.schemas.investigation import CreateInvestigationResponse
from app.security.jwt import CurrentUser
from app.store import events as event_store
from app.store import investigations as investigation_store


async def answer_gate(
    *,
    gate: Gate,
    action: str,
    payload: dict[str, Any],
    investigation_id: UUID,
    user: CurrentUser,
    session: AsyncSession,
    request: Request,
    not_waiting: str,
) -> CreateInvestigationResponse:
    row = await investigation_store.get_by_id(session, tenant_id=user.tenant_id, investigation_id=investigation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    resumed = gate.resumed(action)
    won = row.status == gate.paused and await investigation_store.transition_status_if(
        session, investigation_id=investigation_id, tenant_id=user.tenant_id, from_status=gate.paused, to_status=resumed
    )
    if not won:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=not_waiting)
    try:
        await event_store.append_event(
            session,
            investigation_id=investigation_id,
            type=gate.decided_event,
            payload={"action": action, **payload, "decidedBy": str(user.user_id)},
        )
    except Exception:  # noqa: BLE001 - whatever went wrong, the status must be put back
        await session.rollback()
        await investigation_store.transition_status_if(
            session, investigation_id=investigation_id, tenant_id=user.tenant_id, from_status=resumed, to_status=gate.paused
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The decision could not be recorded; try again") from None
    runner = request.app.state.investigation_runner
    await runner.wait_idle(investigation_id)
    runner.schedule(investigation_id, tenant_id=user.tenant_id)
    return CreateInvestigationResponse(investigation_id=investigation_id, status=resumed)
