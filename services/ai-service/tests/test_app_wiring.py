"""How the lifespan builds the app: the graph executor only when the flag is on."""

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.graph.executor import GraphExecutor
from app.main import create_app
from app.runner import InvestigationRunner

INTEGRATION = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)


async def _no_recovery(self: InvestigationRunner) -> None:
    """Skip startup recovery: it would run any real QUEUED/RUNNING rows in the dev database."""


def test_the_stub_runs_when_the_graph_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(InvestigationRunner, "recover_on_startup", _no_recovery)
    with TestClient(create_app()) as client:  # conftest sets INVESTIGATION_GRAPH_ENABLED=false
        assert client.app.state.graph_executor is None


@INTEGRATION
def test_the_graph_executor_is_wired_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVESTIGATION_GRAPH_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(InvestigationRunner, "recover_on_startup", _no_recovery)
    with TestClient(create_app()) as client:  # opens the real Postgres checkpointer
        executor = client.app.state.graph_executor
        assert isinstance(executor, GraphExecutor)
        assert client.app.state.investigation_runner._execute == executor.run
