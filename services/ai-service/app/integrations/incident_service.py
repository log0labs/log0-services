"""HTTP client for incident-service (summary callback)."""

from uuid import UUID

import httpx

from app.config import Settings


def ai_summary_patch_url(settings: Settings, incident_id: UUID) -> str:
    base = settings.incident_service_url.rstrip("/")
    return f"{base}/api/v1/incidents/{incident_id}/ai-summary"


def patch_ai_summary(settings: Settings, incident_id: UUID, ai_summary: str) -> None:
    """Write generated summary to incident-service. Raises on non-2xx or network error."""
    url = ai_summary_patch_url(settings, incident_id)
    headers: dict[str, str] = {}
    if settings.internal_service_token is not None:
        headers["X-Internal-Token"] = settings.internal_service_token.get_secret_value()

    with httpx.Client(timeout=30.0) as client:
        response = client.patch(url, json={"aiSummary": ai_summary}, headers=headers)
        response.raise_for_status()


class IncidentNotFoundError(Exception):
    """No such incident for this tenant (a wrong tenant looks exactly like a missing id)."""


class IncidentApiClient:
    """Async read client for incident-service (the authoritative incident record)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict:
        """GET one incident, scoped to ``tenant_id``. Raises IncidentNotFoundError on 404."""
        base = self._settings.incident_service_url.rstrip("/")
        headers: dict[str, str] = {}
        if self._settings.internal_service_token is not None:
            headers["X-Internal-Token"] = self._settings.internal_service_token.get_secret_value()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{base}/api/v1/incidents/{incident_id}",
                params={"tenantId": str(tenant_id)},
                headers=headers,
            )
        if response.status_code == 404:
            raise IncidentNotFoundError(f"incident {incident_id} not found")
        response.raise_for_status()
        return response.json()
