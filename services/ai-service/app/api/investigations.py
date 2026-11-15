"""``/api/v1/investigations`` - JWT-protected investigation CRUD"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.gate_resume import answer_gate
from app.config import Settings, get_settings
from app.db.session import get_db
from app.graph.gates import REVIEW
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.memory import Memory, NewMemory
from app.schemas.investigation import (
    ACTIVE_STATUSES,
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    FeedbackRequest,
    Investigation,
    InvestigationEvent,
    InvestigationStatus,
    LatestInvestigation,
    ResumeInvestigationRequest,
)
from app.security.jwt import CurrentUser, get_current_user, require_investigation_writer
from app.store import events as event_store
from app.store import investigations as investigation_store
from app.store.exceptions import ActiveInvestigationError

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])

_TERMINAL_STATUSES = frozenset(
    {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    }
)
_TERMINAL_EVENT_TYPES = frozenset(
    {
        "investigation.completed",
        "investigation.failed",
        "investigation.cancelled",
    }
)
_STREAM_BATCH = 100
_TERMINAL_GRACE_POLLS = 3


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateInvestigationResponse,
)
async def create_investigation(
    body: CreateInvestigationRequest,
    request: Request,
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
    runner = request.app.state.investigation_runner
    runner.schedule(row.id, tenant_id=user.tenant_id)
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


MAX_LATEST_INCIDENTS = 100


@router.get("/latest", response_model=list[LatestInvestigation])
async def latest_investigations(
    incident_ids: Annotated[str, Query(alias="incidentIds", description="Comma-separated incident ids, at most 100")],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[LatestInvestigation]:
    """The newest investigation of each listed incident, for a list view. Incidents with none are left out."""
    try:
        ids = list(dict.fromkeys(UUID(part) for part in incident_ids.split(",") if part.strip()))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="incidentIds must be UUIDs") from None
    if len(ids) > MAX_LATEST_INCIDENTS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"at most {MAX_LATEST_INCIDENTS} incident ids")
    rows = await investigation_store.latest_for_incidents(session, tenant_id=user.tenant_id, incident_ids=ids)
    return [LatestInvestigation(**row) for row in rows]


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


@router.post("/{investigation_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_investigation(
    investigation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    row = await investigation_store.get_by_id(
        session,
        tenant_id=user.tenant_id,
        investigation_id=investigation_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if row.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Investigation is not active",
        )
    runner = request.app.state.investigation_runner
    await runner.cancel(investigation_id, tenant_id=user.tenant_id)


@router.post(
    "/{investigation_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateInvestigationResponse,
)
async def resume_investigation(
    investigation_id: UUID,
    body: ResumeInvestigationRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CreateInvestigationResponse:
    """The engineer's decision at the review gate: approve, refine (with a note) or stop.

    The decision is written to the event log (``review.decided``) and the run is resumed from its
    checkpoint in the background; the response only says it was accepted. Two answers at once cannot
    both win: the status change AWAITING_REVIEW -> RUNNING is a single conditional UPDATE.
    """
    return await answer_gate(
        gate=REVIEW,
        action=body.action,
        payload={"note": body.note},
        investigation_id=investigation_id,
        user=user,
        session=session,
        request=request,
        not_waiting="Investigation is not waiting for a review",
    )


def get_memory(request: Request) -> Memory:
    return request.app.state.memory


@router.post("/{investigation_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def give_feedback(
    investigation_id: UUID,
    body: FeedbackRequest,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
    memory: Annotated[Memory, Depends(get_memory)],
) -> None:
    """Rate a finished investigation (helpful, partly, wrong). The rating decides how much later investigations
    trust what this one concluded: wrong ones are never recalled, helpful ones rank higher."""
    row = await investigation_store.get_by_id(session, tenant_id=user.tenant_id, investigation_id=investigation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    statement = (row.root_cause or {}).get("statement")
    if row.status != InvestigationStatus.COMPLETED or not statement:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Only a completed investigation with a root cause can be rated"
        )

    async def save() -> bool:
        return await memory.record_feedback(
            tenant_id=user.tenant_id,
            investigation_id=investigation_id,
            verdict=body.verdict,
            note=body.note,
            user_id=user.user_id,
        )

    if not await save():
        # Finished before memory existed (or the write failed then): store it now from the row, then rate it.
        recommendation = (row.recommendation or {}).get("summary")
        try:
            await memory.remember(
                NewMemory(
                    tenant_id=user.tenant_id,
                    investigation_id=investigation_id,
                    incident_id=row.incident_id,
                    service_name=None,
                    root_cause=statement,
                    recommendation=recommendation,
                    confidence_band=row.confidence_band,
                )
            )
        except (EmbeddingError, SQLAlchemyError, OSError):  # embedding or database trouble
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The feedback could not be saved, try again later"
            ) from None
        if not await save():
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The feedback could not be saved")


@router.get("/{investigation_id}/events", response_model=list[InvestigationEvent])
async def list_investigation_events(
    investigation_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[InvestigationEvent]:
    """List all events for an investigation, in order of increasing seq."""
    row = await investigation_store.get_by_id(
        session,
        tenant_id=user.tenant_id,
        investigation_id=investigation_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await event_store.list_after(
        session,
        tenant_id=user.tenant_id,
        investigation_id=investigation_id,
        after_seq=after,
        limit=limit,
    )


async def get_owned_investigation(
    investigation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> Investigation:
    """404 unless the investigation exists in the caller's tenant.

    Uses its own short session (not ``get_db``) so no pooled connection stays
    checked out for the whole lifetime of a stream.
    """
    factory = request.app.state.async_session_factory
    async with factory() as session:
        row = await investigation_store.get_by_id(
            session,
            tenant_id=user.tenant_id,
            investigation_id=investigation_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row


def _resume_cursor(last_event_id: str | None, after: int) -> int:
    """Last-Event-ID (sent by reconnecting clients) wins over ?after=."""
    if last_event_id is not None and last_event_id.isdigit():
        return int(last_event_id)
    return after


@router.get("/{investigation_id}/stream", response_class=EventSourceResponse)
async def stream_investigation_events(
    request: Request,
    investigation: Annotated[Investigation, Depends(get_owned_investigation)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> AsyncIterator[ServerSentEvent]:
    """SSE view of ai.investigation_event; the table is the source of truth.

    FastAPI adds a keep-alive comment every 15 s while we are idle.
    """
    factory = request.app.state.async_session_factory
    cursor = _resume_cursor(last_event_id, after)
    quiet_terminal_polls = 0
    while True:
        async with factory() as session:
            events = await event_store.list_after(
                session,
                tenant_id=user.tenant_id,
                investigation_id=investigation.id,
                after_seq=cursor,
                limit=_STREAM_BATCH,
            )
            current = (
                None if events 
                else await investigation_store.get_by_id(
                    session, 
                    tenant_id=user.tenant_id, 
                    investigation_id=investigation.id,
                )
            )
        for ev in events:
            cursor = ev.seq
            yield ServerSentEvent(data=ev, event=ev.type, id=str(ev.seq))
        if any(ev.type in _TERMINAL_EVENT_TYPES for ev in events):
            return
        if events:
            continue
        if current is not None and current.status in _TERMINAL_STATUSES:
            quiet_terminal_polls += 1
            if quiet_terminal_polls >= _TERMINAL_GRACE_POLLS:
                return
        else:
            quiet_terminal_polls = 0
        await asyncio.sleep(settings.investigation_stream_poll_seconds)
