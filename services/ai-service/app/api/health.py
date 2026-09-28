"""Liveness endpoint.

Served at both ``/actuator/health`` (same path as every Spring service in log0, so
ops scripts and compose healthchecks treat all services alike) and ``/health``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings

router = APIRouter(tags=["health"])

@router.get("/actuator/health")
@router.get("/health")
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    """Return a simple JSON object indicating the service is alive."""
    return {"status": "UP", "service_name": settings.service_name}
