"""``POST /api/v1/investigations/{id}/retry``: run a finished investigation again from one step."""

from typing import Any
from uuid import UUID

import pytest

from tests.jwt_helpers import TENANT_A, TENANT_B
from tests.test_resume_api import BASE, Scene, headers, pytestmark, scene  # noqa: F401


class StubExecutor:
    def __init__(self, reaches: tuple[str, ...] = ("root_cause", "propose_changes")) -> None:
        self.reaches, self.rewound = reaches, []

    async def can_rewind(self, _id: UUID, node: str) -> bool:
        return node in self.reaches

    async def rewind(self, investigation_id: UUID, node: str) -> bool:
        self.rewound.append((investigation_id, node))
        return True


def retry(s: Scene, step: str = "root_cause", **auth: Any) -> Any:
    return s.client.post(f"{BASE}/{s.id}/retry", json={"from": step}, headers=headers(**{"tenant_id": TENANT_A, **auth}))


def test_a_finished_investigation_is_taken_back_and_scheduled_again() -> None:
    with scene(status="COMPLETED") as s:
        executor = s.client.app.state.graph_executor = StubExecutor()  # type: ignore[attr-defined]

        response = retry(s)

        assert response.status_code == 202 and response.json()["status"] == "QUEUED"
        assert executor.rewound == [(UUID(s.id), "root_cause")]
        assert ("investigation.retried", {"from": "root_cause"}) in s.events()


def test_only_an_engineer_of_the_same_workspace_can_retry() -> None:
    with scene(status="COMPLETED") as s:
        s.client.app.state.graph_executor = StubExecutor()  # type: ignore[attr-defined]
        assert retry(s, role="VIEWER").status_code == 403
        assert retry(s, tenant_id=TENANT_B).status_code == 404
        assert s.status() == "COMPLETED"


@pytest.mark.parametrize("status", ["RUNNING", "AWAITING_REVIEW", "QUEUED"])
def test_a_run_that_is_not_finished_cannot_be_retried(status: str) -> None:
    with scene(status=status) as s:
        executor = s.client.app.state.graph_executor = StubExecutor()  # type: ignore[attr-defined]
        assert retry(s).status_code == 409
        assert s.status() == status and executor.rewound == []


def test_a_step_the_run_never_reached_and_an_unknown_step_are_refused() -> None:
    with scene(status="COMPLETED") as s:
        s.client.app.state.graph_executor = StubExecutor(reaches=("root_cause",))  # type: ignore[attr-defined]
        assert retry(s, "propose_changes").status_code == 409
        assert retry(s, "synthesize").status_code == 422
        assert s.status() == "COMPLETED"
