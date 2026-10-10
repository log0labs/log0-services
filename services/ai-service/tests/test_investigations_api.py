"""Tests for ``/api/v1/investigations``."""

import os
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


@INTEGRATION
def test_post_conflict_409():
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
