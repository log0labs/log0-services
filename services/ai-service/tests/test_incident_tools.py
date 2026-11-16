"""Unit tests for the incident client and tools (no network, no database)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from app.config import Settings, get_settings
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient, IncidentNotFoundError
from app.tools.incidents import (
    SUMMARY_MAX_CHARS,
    IncidentContext,
    find_related_incidents,
    get_incident,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
INCIDENT_ID = UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _api_payload(**over: Any) -> dict[str, Any]:
    data = {
        "incidentId": str(INCIDENT_ID),
        "tenantId": str(TENANT),
        "fingerprint": "fp-1",
        "serviceName": "payment-service",
        "environment": "production",
        "severity": "HIGH",
        "status": "NEW",
        "occurrenceCount": 1500,
        "firstSeenAt": "2026-06-14T07:13:23.761518Z",
        "lastSeenAt": "2026-06-14T07:13:24.137120Z",
        "resolvedAt": None,
        "topMessages": ["timeout calling gateway"] * 8,
        "aiSummary": "x" * (SUMMARY_MAX_CHARS + 100),
    }
    data.update(over)
    return data


class FakeApi:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[UUID, UUID]] = []

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        self.calls.append((incident_id, tenant_id))
        return self.payload


class FakeSource:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    async def find_related(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.rows


def _context() -> IncidentContext:
    return IncidentContext(
        incident_id=INCIDENT_ID,
        fingerprint="fp-1",
        service_name="payment-service",
        environment="production",
        severity="HIGH",
        status="NEW",
        occurrence_count=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
        resolved_at=None,
        top_messages=[],
        ai_summary=None,
    )


# --------------------------------------------------------------- HTTP client


@respx.mock
@pytest.mark.asyncio
async def test_client_gets_incident_with_tenant_and_token() -> None:
    settings = get_settings()
    settings.incident_service_url = "http://incident.test"
    route = respx.get(f"http://incident.test/api/v1/incidents/{INCIDENT_ID}").mock(
        return_value=httpx.Response(200, json=_api_payload())
    )
    data = await IncidentApiClient(settings).get_incident(INCIDENT_ID, TENANT)
    assert data["fingerprint"] == "fp-1"
    request = route.calls.last.request
    assert request.url.params["tenantId"] == str(TENANT)
    assert request.headers["x-internal-token"] == "test-internal-token-at-least-16-characters-long"


@respx.mock
@pytest.mark.asyncio
async def test_client_maps_404_to_not_found() -> None:
    settings = get_settings()
    settings.incident_service_url = "http://incident.test"
    respx.get(f"http://incident.test/api/v1/incidents/{INCIDENT_ID}").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(IncidentNotFoundError):
        await IncidentApiClient(settings).get_incident(INCIDENT_ID, TENANT)


@respx.mock
@pytest.mark.asyncio
async def test_client_raises_on_server_error() -> None:
    settings = get_settings()
    settings.incident_service_url = "http://incident.test"
    respx.get(f"http://incident.test/api/v1/incidents/{INCIDENT_ID}").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await IncidentApiClient(settings).get_incident(INCIDENT_ID, TENANT)


# ------------------------------------------------------------- get_incident


@pytest.mark.asyncio
async def test_get_incident_maps_and_caps_fields() -> None:
    api = FakeApi(_api_payload())
    out = await get_incident(api, tenant_id=TENANT, incident_id=INCIDENT_ID)
    assert api.calls == [(INCIDENT_ID, TENANT)]
    assert out.service_name == "payment-service" and out.occurrence_count == 1500
    assert out.first_seen_at.tzinfo is not None and out.resolved_at is None
    assert len(out.top_messages) == 5  # capped
    assert out.ai_summary is not None and len(out.ai_summary) == SUMMARY_MAX_CHARS


@pytest.mark.asyncio
async def test_get_incident_rejects_other_tenant_even_if_api_leaks() -> None:
    api = FakeApi(_api_payload(tenantId=str(OTHER)))  # remote ignored our tenant filter
    with pytest.raises(IncidentNotFoundError):
        await get_incident(api, tenant_id=TENANT, incident_id=INCIDENT_ID)


@pytest.mark.asyncio
async def test_get_incident_handles_resolved_and_missing_optionals() -> None:
    api = FakeApi(
        _api_payload(resolvedAt="2026-06-15T00:00:00Z", topMessages=None, aiSummary=None)
    )
    out = await get_incident(api, tenant_id=TENANT, incident_id=INCIDENT_ID)
    assert out.resolved_at == datetime(2026, 6, 15, tzinfo=UTC)
    assert out.top_messages == [] and out.ai_summary is None


# ------------------------------------------------------ find_related_incidents


def _related_row(same: bool, summary: str | None = None) -> dict[str, Any]:
    return {
        "incident_id": UUID("33333333-3333-4333-8333-333333333333"),
        "fingerprint": "fp-1" if same else "fp-2",
        "service_name": "payment-service",
        "environment": "production",
        "severity": "HIGH",
        "status": "RESOLVED",
        "occurrence_count": 12,
        "first_seen_at": NOW - timedelta(days=3),
        "last_seen_at": NOW - timedelta(days=2),
        "resolved_at": NOW - timedelta(days=2),
        "ai_summary": summary,
        "same_fingerprint": same,
    }


@pytest.mark.asyncio
async def test_related_passes_incident_identity_and_window() -> None:
    source = FakeSource([_related_row(True), _related_row(False)])
    out = await find_related_incidents(
        source, tenant_id=TENANT, incident=_context(), lookback_days=10, limit=5
    )
    call = source.calls[0]
    assert call["tenant_id"] == TENANT and call["incident_id"] == INCIDENT_ID
    assert call["fingerprint"] == "fp-1" and call["service_name"] == "payment-service"
    assert call["limit"] == 5
    assert timedelta(days=9, hours=23) < datetime.now(UTC) - call["since"] < timedelta(days=10, minutes=1)
    assert [r.same_fingerprint for r in out] == [True, False]
    assert out[0].resolved_at is not None


@pytest.mark.asyncio
async def test_related_truncates_summaries() -> None:
    source = FakeSource([_related_row(True, summary="y" * (SUMMARY_MAX_CHARS + 5))])
    out = await find_related_incidents(source, tenant_id=TENANT, incident=_context())
    assert len(out[0].ai_summary or "") == SUMMARY_MAX_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"limit": 0}, "limit"), ({"limit": 21}, "limit"), ({"lookback_days": 0}, "lookback"), ({"lookback_days": 91}, "lookback")],
)
async def test_related_rejects_bad_bounds(kwargs: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        await find_related_incidents(FakeSource(), tenant_id=TENANT, incident=_context(), **kwargs)


# ------------------------------------------------------- reader credentials 


def test_reader_url_keeps_main_host_but_swaps_credentials() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://log0:log0@postgres:5432/log0",
        incident_db_user="reader",
        incident_db_password="p@ss/word",
    )
    url = settings.incident_database_url_async
    assert url.startswith("postgresql+psycopg_async://reader:")
    assert url.endswith("@postgres:5432/log0")
    assert "p%40ss%2Fword" in url  # special characters are escaped, not broken
    assert "log0:log0" not in url  # the admin credentials never leak into the reader URL


@pytest.mark.asyncio
async def test_missing_reader_password_fails_on_use_not_on_startup() -> None:
    settings = Settings(_env_file=None, incident_db_password=None)
    reader = IncidentReader(settings)  # constructing must not raise
    with pytest.raises(RuntimeError, match="INCIDENT_DB_PASSWORD"):
        await reader.find_related(
            tenant_id=TENANT,
            incident_id=INCIDENT_ID,
            fingerprint="fp",
            service_name="svc",
            since=NOW,
            limit=1,
        )
    await reader.close()  # closing a never-opened reader is a no-op
