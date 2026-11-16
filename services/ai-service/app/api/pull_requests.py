"""``/api/v1/investigations/{id}/pr/...``: approve or decline the draft pull request, and read it.

ADMIN or ENGINEER of the owning tenant may answer (a VIEWER may only read). Approval is the one thing
that lets log0 write to a repository; the run is parked until it arrives. A second answer, or an answer
when nothing is waiting, is a 409. The pull request itself (branch, commit, number, link) is read from
its own row, tenant-scoped.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.gate_resume import answer_gate
from app.db.session import get_db
from app.graph.gates import PR_APPROVAL
from app.schemas.investigation import CreateInvestigationResponse
from app.schemas.pull_request import PullRequestOut
from app.security.jwt import CurrentUser, get_current_user, require_investigation_writer
from app.store import pull_requests
from app.store import repositories as repository_store

router = APIRouter(prefix="/api/v1/investigations", tags=["pull requests"])

_NOT_WAITING = "Investigation is not waiting for a pull request decision"


async def _decide(
    action: str,
    investigation_id: UUID,
    request: Request,
    user: CurrentUser,
    session: AsyncSession,
) -> CreateInvestigationResponse:
    return await answer_gate(
        gate=PR_APPROVAL,
        action=action,
        payload={},
        investigation_id=investigation_id,
        user=user,
        session=session,
        request=request,
        not_waiting=_NOT_WAITING,
    )


@router.post("/{investigation_id}/pr/approve", status_code=status.HTTP_202_ACCEPTED, response_model=CreateInvestigationResponse)
async def approve_pull_request(
    investigation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CreateInvestigationResponse:
    """Let log0 open a draft pull request for the validated diff."""
    return await _decide("approve", investigation_id, request, user, session)


@router.post("/{investigation_id}/pr/decline", status_code=status.HTTP_202_ACCEPTED, response_model=CreateInvestigationResponse)
async def decline_pull_request(
    investigation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CreateInvestigationResponse:
    """Do not open a pull request; the investigation finishes with its diff, as in V1."""
    return await _decide("decline", investigation_id, request, user, session)


@router.get("/{investigation_id}/pull-request", response_model=PullRequestOut)
async def get_pull_request(
    investigation_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PullRequestOut:
    row = await pull_requests.get_for_investigation(session, tenant_id=user.tenant_id, investigation_id=investigation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    repository = await repository_store.get(session, tenant_id=user.tenant_id, repository_id=row.repository_id)
    return PullRequestOut(
        state=row.state,
        repository=f"{repository.owner}/{repository.name}" if repository else None,
        branch=row.branch,
        base_sha=row.base_sha,
        commit_sha=row.commit_sha,
        number=row.number,
        url=row.url,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
