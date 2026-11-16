from fastapi.testclient import TestClient

from app.main import create_app

INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token-at-least-16-characters-long"}


def test_post_summaries_returns_202_and_runs_background_task(
    monkeypatch, summary_request_json
):
    ran: list = []

    def fake_generate(request):
        ran.append(request.incident_id)

    monkeypatch.setattr(
        "app.api.summaries.generate_and_store_summary",
        fake_generate,
    )

    client = TestClient(create_app())
    response = client.post("/api/v1/summaries", json=summary_request_json, headers=INTERNAL_HEADERS,)

    assert response.status_code == 202
    assert len(ran) == 1


def test_post_summaries_rejects_invalid_body():
    client = TestClient(create_app())
    response = client.post("/api/v1/summaries", json={"incidentId": "not-a-uuid"}, headers=INTERNAL_HEADERS,)
    assert response.status_code == 422


def test_post_summaries_requires_internal_token(summary_request_json):
    client = TestClient(create_app())
    assert client.post("/api/v1/summaries", json=summary_request_json).status_code == 401
    bad = client.post("/api/v1/summaries", json=summary_request_json, headers={"X-Internal-Token": "wrong"},)
    assert bad.status_code == 401
