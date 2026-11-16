"""Read-only access to log0's ``public.incident`` table (incident-service owns it).

Connects as the SELECT-only ``ai_incident_reader`` role. Every query filters on the 
caller's tenant; values are bound parameters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings

_RELATED_SQL = text(
    """
    SELECT incident_id, fingerprint, service_name, environment, severity, status,
           occurrence_count, first_seen_at, last_seen_at, resolved_at, ai_summary,
           (fingerprint = :fingerprint) AS same_fingerprint
    FROM public.incident
    WHERE tenant_id = :tenant_id
      AND incident_id <> :incident_id
      AND (fingerprint = :fingerprint
           OR (service_name = :service_name AND last_seen_at >= :since))
    ORDER BY same_fingerprint DESC, last_seen_at DESC
    LIMIT :max_rows
    """
)


class IncidentReader:
    """Tenant-scoped, read-only queries over incident history."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None

    def _get_engine(self) -> AsyncEngine:
        """Build the engine on first use: no password configured must not stop startup."""
        if self._engine is None:
            self._engine = create_async_engine(
                self._settings.incident_database_url_async, pool_pre_ping=True
            )
        return self._engine

    async def find_related(
        self,
        *,
        tenant_id: UUID,
        incident_id: UUID,
        fingerprint: str,
        service_name: str,
        since: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self._get_engine().connect() as conn:
            result = await conn.execute(
                _RELATED_SQL,
                {
                    "tenant_id": tenant_id,
                    "incident_id": incident_id,
                    "fingerprint": fingerprint,
                    "service_name": service_name,
                    "since": since,
                    "max_rows": limit,
                },
            )
            return [dict(row._mapping) for row in result]

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
