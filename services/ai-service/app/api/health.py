"""Liveness endpoint.

Served at both ``/actuator/health`` (same path as every Spring service in log0, so
ops scripts and compose healthchecks treat all services alike) and ``/health``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings, get_settings

router = APIRouter(tags=["health"])


async def _database_status(engine: AsyncEngine) -> str:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "UP"
    except SQLAlchemyError:
        return "DOWN"


@router.get("/actuator/health")
@router.get("/health")
async def health(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    """Return a simple JSON object indicating the service is alive."""
    engine: AsyncEngine = request.app.state.db_engine
    db_status = await _database_status(engine)
    overall = "UP" if db_status == "UP" else "DOWN"
    if overall == "DOWN":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": overall,
        "service_name": settings.service_name,
        "database": {"status": db_status},
    }
