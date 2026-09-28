from uuid import UUID

import httpx
import respx

from app.config import get_settings
from app.integrations.incident_service import patch_ai_summary


@respx.mock
def test_patch_ai_summary_sends_camel_case_body():
    settings = get_settings()
    settings.incident_service_url = "http://incident.test"
    incident_id = UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f")
    route = respx.patch(
        f"http://incident.test/api/v1/incidents/{incident_id}/ai-summary"
    ).mock(return_value=httpx.Response(204))

    patch_ai_summary(settings, incident_id, "Summary: ok\nPossible Cause: x\nRecommended Actions:\n- y")

    assert route.called
    assert route.calls.last.request.content
    body = route.calls.last.request.content.decode()
    assert "aiSummary" in body
