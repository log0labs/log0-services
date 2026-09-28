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
