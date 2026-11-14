"""``POST /api/v1/investigations/{id}/resume``: the engineer's decision at the review gate."""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.main import create_app
from app.runner import InvestigationRunner
from app.store import events as event_store
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

BASE = "/api/v1/investigations"
INCIDENT = "a0000050-0000-4000-8000-000000000050"


def headers(**kwargs: Any) -> dict[str, str]:
    secret = get_settings().jwt_secret.get_secret_value()
    return {"Authorization": f"Bearer {mint_access_token(secret, **kwargs)}"}


def sql(statement: str, **params: Any) -> list[Any]:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        result = conn.execute(text(statement), params)
        rows = result.fetchall() if result.returns_rows else []
    engine.dispose()
    return rows


def wipe() -> None:
    for tenant in (TENANT_A, TENANT_B):
        sql("DELETE FROM ai.investigation WHERE tenant_id = :t", t=tenant)


class Scene:
    def __init__(self, client: TestClient, investigation_id: str, executed: list[UUID]) -> None:
        self.client = client
        self.id = investigation_id
        self.executed = executed

    def post(self, body: dict[str, Any], **auth: Any) -> Any:
        return self.client.post(f"{BASE}/{self.id}/resume", json=body, headers=headers(**auth))

    def status(self) -> str:
        return sql("SELECT status FROM ai.investigation WHERE id = :i", i=self.id)[0][0]

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        rows = sql("SELECT type, payload FROM ai.investigation_event WHERE investigation_id = :i ORDER BY seq", i=self.id)
        return [(r[0], r[1]) for r in rows]

    def set_status(self, value: str) -> None:
        sql("UPDATE ai.investigation SET status = :s WHERE id = :i", s=value, i=self.id)


@contextmanager
def scene(*, status: str = "AWAITING_REVIEW") -> Iterator[Scene]:
    wipe()
    executed: list[UUID] = []

    async def record(_factory: Any, investigation_id: UUID, _tenant: UUID) -> None:
        executed.append(investigation_id)

    try:
        with TestClient(create_app()) as client:
            created = client.post(BASE, json={"incidentId": INCIDENT}, headers=headers(tenant_id=TENANT_A))
            assert created.status_code == 202
            current = Scene(client, created.json()["investigationId"], executed)
            for _ in range(100):  # the app's own stub runner finishes the new row: wait for it, then take over
                if current.status() == "COMPLETED":
                    break
                time.sleep(0.05)
            # the real runner, but its "execution" only records that a resume was scheduled
            client.app.state.investigation_runner = InvestigationRunner(client.app.state.async_session_factory, execute=record)  # type: ignore[attr-defined]
            current.set_status(status)
            yield current
    finally:
        wipe()


# ------------------------------------------------------------------------------ access


def test_resume_requires_a_signed_in_engineer() -> None:
    with scene() as s:
        assert s.client.post(f"{BASE}/{s.id}/resume", json={"action": "approve"}).status_code == 401
        assert s.post({"action": "approve"}, role="VIEWER").status_code == 403
        assert s.status() == "AWAITING_REVIEW" and s.executed == []


@pytest.mark.parametrize("role", ["ADMIN", "ENGINEER"])
def test_admins_and_engineers_can_decide(role: str) -> None:
    with scene() as s:
        assert s.post({"action": "approve"}, role=role).status_code == 202


def test_another_tenant_cannot_decide_and_cannot_tell_the_investigation_exists() -> None:
    with scene() as s:
        assert s.post({"action": "approve"}, tenant_id=TENANT_B).status_code == 404
        assert s.status() == "AWAITING_REVIEW" and not any(t == "review.decided" for t, _ in s.events())


def test_an_unknown_investigation_is_404() -> None:
    with scene() as s:
        response = s.client.post(f"{BASE}/{UUID(int=7)}/resume", json={"action": "approve"}, headers=headers(tenant_id=TENANT_A))
        assert response.status_code == 404


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "body",
    [{}, {"action": "delete"}, {"action": "refine"}, {"action": "refine", "note": "  "}, {"action": "refine", "note": "x" * 1001}],
)
def test_a_malformed_decision_is_422_and_changes_nothing(body: dict[str, Any]) -> None:
    with scene() as s:
        assert s.post(body).status_code == 422
        assert s.status() == "AWAITING_REVIEW" and not any(t == "review.decided" for t, _ in s.events())


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"])
def test_only_an_investigation_that_is_waiting_can_be_resumed(status: str) -> None:
    with scene(status=status) as s:
        response = s.post({"action": "approve"})
        assert response.status_code == 409 and response.json()["detail"] == "Investigation is not waiting for a review"
        assert s.status() == status and s.executed == []


# ------------------------------------------------------------------------------ success


def test_a_decision_is_recorded_and_the_run_is_resumed() -> None:
    with scene() as s:
        response = s.post({"action": "refine", "note": "  Look at the connection pool  "}, user_id=UUID(int=9))

        assert response.status_code == 202
        assert response.json() == {"investigationId": s.id, "status": "RUNNING"}
        decided = [p for t, p in s.events() if t == "review.decided"]
        assert decided == [{"action": "refine", "note": "Look at the connection pool", "decidedBy": str(UUID(int=9))}]
        assert s.executed == [UUID(s.id)]  # the runner was asked to resume exactly this investigation


def test_approve_and_stop_carry_no_note() -> None:
    with scene() as s:
        s.post({"action": "stop", "note": "ignored text"})
        assert [p["note"] for t, p in s.events() if t == "review.decided"] == [None]


def test_the_second_of_two_answers_is_refused_and_leaves_no_second_event() -> None:
    with scene() as s:
        assert s.post({"action": "approve"}).status_code == 202
        assert s.post({"action": "stop"}).status_code == 409  # RUNNING now: the stub runner never pauses it again
        assert [p["action"] for t, p in s.events() if t == "review.decided"] == ["approve"]


def test_when_the_decision_cannot_be_recorded_the_status_goes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database hiccup")

    with scene() as s:
        monkeypatch.setattr(event_store, "append_event", broken)
        response = s.post({"action": "approve"})
        assert response.status_code == 503 and "try again" in response.json()["detail"]
        assert s.status() == "AWAITING_REVIEW" and s.executed == []  # nothing resumed, nothing lost
        monkeypatch.undo()
        assert s.post({"action": "approve"}).status_code == 202  # and a retry works
