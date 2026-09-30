"""``POST /api/v1/summaries``"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import Response

from app.schemas.summary import SummaryRequest
from app.security.internal_token import require_internal_service_token
from app.summary.service import generate_and_store_summary

router = APIRouter(prefix="/api/v1/summaries", tags=["summaries"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_summary(
    request: SummaryRequest,
    background_tasks: BackgroundTasks,
    _: Annotated[None, Depends(require_internal_service_token)],
) -> Response:
    """Accept incident context, summary generation by background task, return immediately."""
    background_tasks.add_task(generate_and_store_summary, request)
    return Response(status_code=status.HTTP_202_ACCEPTED)
