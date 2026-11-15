"""``POST /pr/approve``, ``POST /pr/decline`` and ``GET /pull-request`` (real app, real Postgres)."""

import os
from typing import Any
from uuid import UUID

import pytest

from app.store import events as event_store
from tests.jwt_helpers import TENANT_A, TENANT_B
from tests.test_resume_api import BASE, Scene, headers, scene, sql

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

PARKED = "AWAITING_PR_APPROVAL"


@pytest.fixture(autouse=True)
def _no_repositories() -> Any:
    def wipe() -> None:
        for tenant in (TENANT_A, TENANT_B):
            sql("DELETE FROM ai.repository_connection WHERE tenant_id = :t", t=tenant)

    wipe()
    yield
    wipe()


def answer(s: Scene, action: str, **auth: Any) -> Any:
    return s.client.post(f"{BASE}/{s.id}/pr/{action}", headers=headers(**{"tenant_id": TENANT_A, "role": "ENGINEER"} | auth))


def decided(s: Scene) -> list[dict[str, Any]]:
    return [p for t, p in s.events() if t == "pr.decided"]


def add_pull_request(s: Scene, *, tenant: UUID = TENANT_A) -> None:
    repository_id = sql(
        "INSERT INTO ai.repository_connection (tenant_id, owner, name, token_ciphertext, key_id, created_by) "
        "VALUES (:t, 'acme', 'shop', 'x', 'k1', :u) RETURNING id",
        t=tenant,
        u=UUID(int=1),
    )[0][0]
    sql(
        "INSERT INTO ai.pull_request (tenant_id, investigation_id, repository_id, branch, base_sha, commit_sha, number, url, state, approved_by) "
        "VALUES (:t, :i, :r, 'log0/fix-a-b', :b, :c, 12, 'https://github.com/acme/shop/pull/12', 'open', :u)",
        t=tenant,
        i=s.id,
        r=repository_id,
        b="a" * 40,
        c="b" * 40,
        u=UUID(int=1),
    )


# ------------------------------------------------------------------------------ access


@pytest.mark.parametrize("action", ["approve", "decline"])
def test_answering_needs_a_signed_in_admin_or_engineer(action: str) -> None:
    with scene(status=PARKED) as s:
        assert s.client.post(f"{BASE}/{s.id}/pr/{action}").status_code == 401
        assert answer(s, action, role="VIEWER").status_code == 403
        assert s.status() == PARKED and s.executed == [] and decided(s) == []


@pytest.mark.parametrize("role", ["ADMIN", "ENGINEER"])
def test_admins_and_engineers_can_approve(role: str) -> None:
    with scene(status=PARKED) as s:
        assert answer(s, "approve", role=role).status_code == 202


@pytest.mark.parametrize("action", ["approve", "decline"])
def test_another_tenant_cannot_answer_or_tell_the_investigation_exists(action: str) -> None:
    with scene(status=PARKED) as s:
        assert answer(s, action, tenant_id=TENANT_B).status_code == 404
        assert s.status() == PARKED and decided(s) == []


# ------------------------------------------------------------------------------ answers


def test_approving_records_who_and_starts_publishing() -> None:
    with scene(status=PARKED) as s:
        response = answer(s, "approve", user_id=UUID(int=9))

        assert response.status_code == 202 and response.json() == {"investigationId": s.id, "status": "PUBLISHING"}
        assert decided(s) == [{"action": "approve", "decidedBy": str(UUID(int=9))}]
        assert s.executed == [UUID(s.id)]


def test_declining_goes_back_to_running_and_resumes_the_run() -> None:
    with scene(status=PARKED) as s:
        response = answer(s, "decline")

        assert response.status_code == 202 and response.json()["status"] == "RUNNING"
        assert decided(s)[0]["action"] == "decline" and s.executed == [UUID(s.id)]


def test_the_second_of_two_answers_is_refused_and_leaves_no_second_event() -> None:
    with scene(status=PARKED) as s:
        assert answer(s, "approve").status_code == 202
        assert answer(s, "approve").status_code == 409
        assert answer(s, "decline").status_code == 409
        assert [p["action"] for p in decided(s)] == ["approve"]


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "AWAITING_REVIEW", "PUBLISHING", "COMPLETED", "FAILED", "CANCELLED"])
def test_only_a_run_waiting_for_this_decision_can_be_answered(status: str) -> None:
    with scene(status=status) as s:
        response = answer(s, "approve")

        assert response.status_code == 409 and response.json()["detail"] == "Investigation is not waiting for a pull request decision"
        assert s.status() == status and s.executed == []


def test_the_review_gate_does_not_accept_a_pull_request_answer_and_the_reverse() -> None:
    with scene(status="AWAITING_REVIEW") as s:
        assert answer(s, "approve").status_code == 409
        assert s.client.post(f"{BASE}/{s.id}/resume", json={"action": "approve"}, headers=headers(tenant_id=TENANT_A, role="ENGINEER")).status_code == 202
    with scene(status=PARKED) as s:
        assert s.client.post(f"{BASE}/{s.id}/resume", json={"action": "approve"}, headers=headers(tenant_id=TENANT_A, role="ENGINEER")).status_code == 409


def test_when_the_answer_cannot_be_recorded_the_status_goes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database hiccup")

    with scene(status=PARKED) as s:
        monkeypatch.setattr(event_store, "append_event", broken)
        response = answer(s, "approve")
        assert response.status_code == 503
        assert s.status() == PARKED and s.executed == []
        monkeypatch.undo()
        assert answer(s, "approve").status_code == 202


# ------------------------------------------------------------------------- reading it


def test_the_pull_request_can_be_read_by_anyone_in_the_tenant() -> None:
    with scene(status="COMPLETED") as s:
        add_pull_request(s)

        for role in ("VIEWER", "ENGINEER", "ADMIN"):
            body = s.client.get(f"{BASE}/{s.id}/pull-request", headers=headers(tenant_id=TENANT_A, role=role)).json()
            assert body["state"] == "open" and body["repository"] == "acme/shop" and body["number"] == 12
            assert body["url"] == "https://github.com/acme/shop/pull/12" and body["branch"] == "log0/fix-a-b"
            assert body["commitSha"] == "b" * 40 and body["baseSha"] == "a" * 40


def test_an_investigation_without_a_pull_request_or_of_another_tenant_is_404() -> None:
    with scene(status="COMPLETED") as s:
        assert s.client.get(f"{BASE}/{s.id}/pull-request", headers=headers(tenant_id=TENANT_A)).status_code == 404
        add_pull_request(s)
        assert s.client.get(f"{BASE}/{s.id}/pull-request", headers=headers(tenant_id=TENANT_B)).status_code == 404
        assert s.client.get(f"{BASE}/{s.id}/pull-request").status_code == 401
