"""``POST /api/v1/summaries``"""

from fastapi import APIRouter, BackgroundTasks, status
from fastapi.responses import Response

from app.schemas.summary import SummaryRequest
from app.summary.service import generate_and_store_summary

router = APIRouter(prefix="/api/v1/summaries", tags=["summaries"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_summary(
    request: SummaryRequest,
    background_tasks: BackgroundTasks,
) -> Response:
    """Accept incident context, queue summary generation, return immediately."""
    background_tasks.add_task(generate_and_store_summary, request)
    return Response(status_code=status.HTTP_202_ACCEPTED)
