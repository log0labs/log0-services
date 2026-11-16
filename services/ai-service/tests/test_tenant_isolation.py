"""Tenant-isolation regression tests: tenant B must never see or affect tenant A.

Every ai.* read and write is scoped by tenant_id taken from the JWT. These tests
pin that behaviour at three layers (store, runner, HTTP) so a future query that
forgets ``tenant_id`` fails loudly. Wrong-tenant access answers 404, never 403,
so existence is not revealed.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from app.main import create_app
from app.runner import InvestigationRunner, SessionFactory
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.store import events as event_store
from app.store import investigations as store
from tests.jwt_helpers import TENANT_A, TENANT_B, USER_ID, mint_access_token

INTEGRATION = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

INCIDENT_ID = "a0000040-0000-4000-8000-000000000040"
INCIDENT_ID_RUNNER = UUID("a0000041-0000-4000-8000-000000000041")
INCIDENT_ID_STORE = UUID("a0000042-0000-4000-8000-000000000042")
BASE = "/api/v1/investigations"


def _body(incident_id: str = INCIDENT_ID) -> dict:
    return {
        "incidentId": incident_id,
        "question": "Why?",
        "scope": {
            "logs": True,
            "history": True,
            "code": False,
            "runbooks": False,
            "docs": False,
        },
    }


def _headers(tenant_id: UUID) -> dict[str, str]:
    secret = get_settings().jwt_secret.get_secret_value()
    return {"Authorization": f"Bearer {mint_access_token(secret, tenant_id=tenant_id)}"}


def _purge() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai.investigation WHERE tenant_id = ANY(:tids)"),
            {"tids": [TENANT_A, TENANT_B]},
        )
    engine.dispose()


@dataclass
class Victim:
    client: TestClient
    investigation_id: str


@pytest.fixture
def victim(monkeypatch: pytest.MonkeyPatch) -> Generator[Victim]:
    """Tenant A owns one ACTIVE investigation (kept RUNNING by a slow execute)."""

    async def slow_execute(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr("app.runner.default_investigation_execute", slow_execute)
    _purge()
    try:
        with TestClient(create_app()) as client:
            post = client.post(BASE, json=_body(), headers=_headers(TENANT_A))
            assert post.status_code == 202
            yield Victim(client, post.json()["investigationId"])
    finally:
        _purge()


# ---------------------------------------------------------------- HTTP layer


@INTEGRATION
def test_other_tenant_cannot_get(victim: Victim) -> None:
    r = victim.client.get(f"{BASE}/{victim.investigation_id}", headers=_headers(TENANT_B))
    assert r.status_code == 404


@INTEGRATION
def test_other_tenant_list_is_empty(victim: Victim) -> None:
    r = victim.client.get(
        BASE, params={"incidentId": INCIDENT_ID}, headers=_headers(TENANT_B)
    )
    assert r.status_code == 200
    assert r.json() == []
    own = victim.client.get(
        BASE, params={"incidentId": INCIDENT_ID}, headers=_headers(TENANT_A)
    )
    assert len(own.json()) == 1


@INTEGRATION
def test_other_tenant_cannot_read_events(victim: Victim) -> None:
    r = victim.client.get(
        f"{BASE}/{victim.investigation_id}/events", headers=_headers(TENANT_B)
    )
    assert r.status_code == 404


@INTEGRATION
def test_other_tenant_cannot_stream(victim: Victim) -> None:
    r = victim.client.get(
        f"{BASE}/{victim.investigation_id}/stream", headers=_headers(TENANT_B)
    )
    assert r.status_code == 404


@INTEGRATION
def test_other_tenant_cannot_cancel(victim: Victim) -> None:
    r = victim.client.post(
        f"{BASE}/{victim.investigation_id}/cancel", headers=_headers(TENANT_B)
    )
    assert r.status_code == 404
    mine = victim.client.get(
        f"{BASE}/{victim.investigation_id}", headers=_headers(TENANT_A)
    )
    assert mine.json()["status"] in ("QUEUED", "RUNNING")


@INTEGRATION
def test_same_incident_id_is_independent_per_tenant(victim: Victim) -> None:
    """B posting A's incident id must not hit A's 'one active' slot (no 409 oracle)."""
    r = victim.client.post(BASE, json=_body(), headers=_headers(TENANT_B))
    assert r.status_code == 202
    assert r.json()["investigationId"] != victim.investigation_id
    a_list = victim.client.get(
        BASE, params={"incidentId": INCIDENT_ID}, headers=_headers(TENANT_A)
    ).json()
    b_list = victim.client.get(
        BASE, params={"incidentId": INCIDENT_ID}, headers=_headers(TENANT_B)
    ).json()
    assert [x["id"] for x in a_list] == [victim.investigation_id]
    assert [x["id"] for x in b_list] == [r.json()["investigationId"]]


# ------------------------------------------------- runner + store layers (async)


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)
    _purge()
    try:
        yield factory
    finally:
        _purge()
        await engine.dispose()


@INTEGRATION
@pytest.mark.asyncio
async def test_runner_cancel_ignores_other_tenant(session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        row = await store.create(
            session,
            tenant_id=TENANT_A,
            incident_id=INCIDENT_ID_RUNNER,
            requested_by=USER_ID,
            scope=InvestigationScope(),
        )
    started = asyncio.Event()

    async def hang(_factory, _investigation_id, _tenant_id) -> None:
        started.set()
        await asyncio.Event().wait()

    runner = InvestigationRunner(session_factory, execute=hang)
    runner.schedule(row.id, tenant_id=TENANT_A)
    await asyncio.wait_for(started.wait(), timeout=5)

    await runner.cancel(row.id, tenant_id=TENANT_B)  # wrong tenant: must be a no-op

    assert runner.is_running(row.id)
    async with session_factory() as session:
        current = await store.get_by_id(
            session, tenant_id=TENANT_A, investigation_id=row.id
        )
    assert current is not None
    assert current.status == InvestigationStatus.RUNNING
    await runner.shutdown()


@INTEGRATION
@pytest.mark.asyncio
async def test_store_writes_and_reads_are_tenant_scoped(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        row = await store.create(
            session,
            tenant_id=TENANT_A,
            incident_id=INCIDENT_ID_STORE,
            requested_by=USER_ID,
            scope=InvestigationScope(),
        )
        await event_store.append_event(session, investigation_id=row.id, type="a")

    async with session_factory() as session:
        assert not await store.mark_running(
            session, investigation_id=row.id, tenant_id=TENANT_B
        )
        await store.transition_status(
            session,
            investigation_id=row.id,
            tenant_id=TENANT_B,
            status=InvestigationStatus.CANCELLED,
            set_completed=True,
        )
        assert (
            await store.get_by_id(session, tenant_id=TENANT_B, investigation_id=row.id)
            is None
        )
        assert (
            await store.list_by_incident(
                session, tenant_id=TENANT_B, incident_id=INCIDENT_ID_STORE
            )
            == []
        )
        assert (
            await event_store.list_after(
                session, tenant_id=TENANT_B, investigation_id=row.id
            )
            == []
        )
        mine = await store.get_by_id(session, tenant_id=TENANT_A, investigation_id=row.id)

    assert mine is not None
    assert mine.status == InvestigationStatus.QUEUED  # B's transition changed nothing
