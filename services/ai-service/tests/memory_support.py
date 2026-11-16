"""Shared pieces for the memory tests: investigation rows to attach memories to, and cleanup."""

import json
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from app.config import get_settings
from tests.jwt_helpers import TENANT_A, TENANT_B


def make_investigation(
    tenant: UUID = TENANT_A,
    *,
    status: str = "COMPLETED",
    root_cause: str | None = "The connection pool was exhausted",
    incident_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Insert a row directly; returns (investigation id, incident id)."""
    incident = incident_id or uuid4()
    rc = json.dumps({"statement": root_cause}) if root_cause else None
    rec = json.dumps({"summary": "Raise the pool size"}) if root_cause else None
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        inv = conn.execute(
            text(
                "INSERT INTO ai.investigation (tenant_id, incident_id, requested_by, status, root_cause, recommendation, "
                "confidence_band) VALUES (:t, :inc, :by, :status, CAST(:rc AS jsonb), CAST(:rec AS jsonb), 'medium') "
                "RETURNING id"
            ),
            {"t": tenant, "inc": incident, "by": uuid4(), "status": status, "rc": rc, "rec": rec},
        ).scalar_one()
    engine.dispose()
    return inv, incident


def wipe_investigations() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        for tenant in (TENANT_A, TENANT_B):
            conn.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": tenant})  # memory cascades
    engine.dispose()


def query_one(sql: str, **params):
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).first()
    finally:
        engine.dispose()
