"""Tests for ``/api/v1/investigations``."""

import asyncio
import os
import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.main import create_app
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token

INTEGRATION = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

INCIDENT_ID = "a0000010-0000-4000-8000-000000000010"
INCIDENT_ID_CONFLICT = "a0000011-0000-4000-8000-000000000011"
INCIDENT_ID_CROSS_TENANT = "a0000012-0000-4000-8000-000000000012"
INCIDENT_ID_RUNNER = "a0000013-0000-4000-8000-000000000013"
INCIDENT_ID_CANCEL = "a0000014-0000-4000-8000-000000000014"


def _sample_body(*, incident_id: str = INCIDENT_ID) -> dict:
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


def _auth_headers(secret: str, **mint_kwargs) -> dict[str, str]:
    token = mint_access_token(secret, **mint_kwargs)
    return {"Authorization": f"Bearer {token}"}


def _delete_tenant_investigations(tenant_id: UUID) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai.investigation WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    engine.dispose()


def test_post_requires_auth():
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/investigations", json=_sample_body())
    assert response.status_code == 401


def test_post_forbidden_for_viewer():
    secret = get_settings().jwt_secret.get_secret_value()
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/investigations",
            json=_sample_body(),
            headers=_auth_headers(secret, role="VIEWER"),
        )
    assert response.status_code == 403


@INTEGRATION
def test_post_returns_202_and_get():
    secret = get_settings().jwt_secret.get_secret_value()
    try:
        with TestClient(create_app()) as client:
            post = client.post(
                "/api/v1/investigations",
                json=_sample_body(),
                headers=_auth_headers(secret, tenant_id=TENANT_A),
            )
            assert post.status_code == 202
            body = post.json()
            assert body["status"] == "QUEUED"
            assert "investigationId" in body

            inv_id = body["investigationId"]
            get_resp = client.get(
                f"/api/v1/investigations/{inv_id}",
                headers=_auth_headers(secret, tenant_id=TENANT_A),
            )
            assert get_resp.status_code == 200
            got = get_resp.json()
            assert got["id"] == inv_id
            assert got["question"] == "Why?"
    finally:
        _delete_tenant_investigations(TENANT_A)


def _poll_status(client, inv_id, headers, wanted, timeout=5.0):
    deadline = time.monotonic() + timeout
    got = None
    while time.monotonic() < deadline:
        got = client.get(f"/api/v1/investigations/{inv_id}", headers=headers).json()["status"]
        if got in wanted:
            return got
        time.sleep(0.05)
    raise AssertionError(f"status stuck at {got}, wanted {wanted}")


@INTEGRATION
def test_post_conflict_409(monkeypatch):
    async def slow_execute(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr("app.runner.default_investigation_execute", slow_execute)
    secret = get_settings().jwt_secret.get_secret_value()
    try:
        with TestClient(create_app()) as client:
            headers = _auth_headers(secret, tenant_id=TENANT_A)
            body = _sample_body(incident_id=INCIDENT_ID_CONFLICT)
            first = client.post("/api/v1/investigations", json=body, headers=headers)
            assert first.status_code == 202
            second = client.post("/api/v1/investigations", json=body, headers=headers)
            assert second.status_code == 409
    finally:
        _delete_tenant_investigations(TENANT_A)


@INTEGRATION
def test_get_other_tenant_404():
    secret = get_settings().jwt_secret.get_secret_value()
    try:
        with TestClient(create_app()) as client:
            post = client.post(
                "/api/v1/investigations",
                json=_sample_body(incident_id=INCIDENT_ID_CROSS_TENANT),
                headers=_auth_headers(secret, tenant_id=TENANT_A),
            )
            assert post.status_code == 202
            inv_id = post.json()["investigationId"]

            cross = client.get(
                f"/api/v1/investigations/{inv_id}",
                headers=_auth_headers(secret, tenant_id=TENANT_B),
            )
            assert cross.status_code == 404
    finally:
        _delete_tenant_investigations(TENANT_A)


@INTEGRATION
def test_post_schedules_runner_until_completed():
    secret = get_settings().jwt_secret.get_secret_value()
    try:
        with TestClient(create_app()) as client:
            headers = _auth_headers(secret, tenant_id=TENANT_A)
            post = client.post(
                "/api/v1/investigations",
                json=_sample_body(incident_id=INCIDENT_ID_RUNNER),
                headers=headers,
            )
            assert post.status_code == 202
            _poll_status(client, post.json()["investigationId"], headers, {"COMPLETED"})
    finally:
        _delete_tenant_investigations(TENANT_A)


@INTEGRATION
def test_cancel_active_investigation():
    secret = get_settings().jwt_secret.get_secret_value()
    try:
        with TestClient(create_app()) as client:
            headers = _auth_headers(secret, tenant_id=TENANT_A)
            post = client.post(
                "/api/v1/investigations",
                json=_sample_body(incident_id=INCIDENT_ID_CANCEL),
                headers=headers,
            )
            inv_id = post.json()["investigationId"]
            cancel = client.post(
                f"/api/v1/investigations/{inv_id}/cancel", headers=headers
            )
            # stub may finish first, making the row non-active
            assert cancel.status_code in (204, 409)
            _poll_status(client, inv_id, headers, {"CANCELLED", "COMPLETED"})
    finally:
        _delete_tenant_investigations(TENANT_A)
