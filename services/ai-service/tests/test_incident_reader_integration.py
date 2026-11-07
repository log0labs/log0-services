"""IncidentReader against the real Postgres (needs ai_incident_reader role)."""

import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.integrations.incident_db import IncidentReader
from app.tools.incidents import IncidentContext, find_related_incidents

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

NOW = datetime.now(UTC)
INSERT = text(
    """
    INSERT INTO public.incident
        (incident_id, tenant_id, fingerprint, service_name, environment, severity,
         status, occurrence_count, first_seen_at, last_seen_at, resolved_at, ai_summary, created_at, updated_at)
    VALUES
        (:id, :tenant, :fp, :svc, 'prod', 'HIGH', :status, 5, :seen, :seen, :resolved, 'summary',
         now(), now())
    """
)


def _row(tenant: UUID, fp: str, svc: str, status: str, age_days: int) -> dict:
    seen = NOW - timedelta(days=age_days)
    return {
        "id": uuid4(), "tenant": tenant, "fp": fp, "svc": svc, "status": status,
        "seen": seen, "resolved": seen if status == "RESOLVED" else None,
    }


@pytest.fixture
def seeded() -> Generator[dict]:
    """Tenant A: current incident + same-fp resolved + same-service recent + same-service old
    + other-service. Tenant B: a same-fingerprint incident that must never appear."""
    a, b = uuid4(), uuid4()
    fp = f"fp-{uuid4().hex[:8]}"
    rows = {
        "current": _row(a, fp, "payment", "NEW", 0),
        "recurrence": _row(a, fp, "payment", "RESOLVED", 20),
        "same_service_recent": _row(a, "fp-other", "payment", "RESOLVED", 5),
        "same_service_old": _row(a, "fp-old", "payment", "RESOLVED", 60),
        "other_service": _row(a, "fp-x", "billing", "RESOLVED", 2),
        "other_tenant": _row(b, fp, "payment", "RESOLVED", 1),
    }
    engine = create_engine(get_settings().database_url)  # admin writes; the reader is read-only
    with engine.begin() as conn:
        for r in rows.values():
            conn.execute(INSERT, r)
    try:
        yield {"tenant": a, "other": b, "fp": fp, **rows}
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM public.incident WHERE tenant_id = ANY(:t)"), {"t": [a, b]}
            )
        engine.dispose()


def _context(seed: dict) -> IncidentContext:
    cur = seed["current"]
    return IncidentContext(
        incident_id=cur["id"], fingerprint=seed["fp"], service_name="payment",
        environment="prod", severity="HIGH", status="NEW", occurrence_count=5,
        first_seen_at=cur["seen"], last_seen_at=cur["seen"], resolved_at=None,
        top_messages=[], ai_summary=None,
    )


@pytest_asyncio.fixture
async def reader():
    r = IncidentReader(get_settings())
    yield r
    await r.close()


async def test_related_orders_recurrence_first_and_scopes_tenant(reader, seeded) -> None:
    out = await find_related_incidents(
        reader, tenant_id=seeded["tenant"], incident=_context(seeded), lookback_days=30
    )
    ids = [r.incident_id for r in out]
    assert ids == [seeded["recurrence"]["id"], seeded["same_service_recent"]["id"]]
    assert [r.same_fingerprint for r in out] == [True, False]
    assert seeded["current"]["id"] not in ids  # never itself
    assert seeded["other_tenant"]["id"] not in ids  # never another tenant
    assert seeded["same_service_old"]["id"] not in ids  # outside the 30-day window
    assert seeded["other_service"]["id"] not in ids  # different service, different fingerprint


async def test_related_longer_lookback_includes_old_incident(reader, seeded) -> None:
    out = await find_related_incidents(
        reader, tenant_id=seeded["tenant"], incident=_context(seeded), lookback_days=90
    )
    assert seeded["same_service_old"]["id"] in [r.incident_id for r in out]


async def test_related_other_tenant_sees_only_its_own(reader, seeded) -> None:
    out = await find_related_incidents(reader, tenant_id=seeded["other"], incident=_context(seeded))
    ids = [r.incident_id for r in out]
    assert ids == [seeded["other_tenant"]["id"]]  # tenant B gets B's row, never A's
    assert seeded["recurrence"]["id"] not in ids


async def test_related_limit_is_applied(reader, seeded) -> None:
    out = await find_related_incidents(
        reader, tenant_id=seeded["tenant"], incident=_context(seeded), limit=1
    )
    assert [r.incident_id for r in out] == [seeded["recurrence"]["id"]]
