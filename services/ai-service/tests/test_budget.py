"""The budget check: three independent limits, each with its own reason."""

import time

import pytest

from app.graph.budget import Budget
from app.graph.state import InvestigationContext
from tests.graph_fakes import FakeApi, FakeHistory, FakeLogs


def _check(budget: Budget, *, elapsed: float = 0, tools: int = 0, rounds: int = 0) -> str | None:
    return budget.exhausted(elapsed_seconds=elapsed, tool_calls=tools, verify_rounds=rounds)


def test_the_defaults_are_the_limits_from_the_architecture() -> None:
    budget = Budget()
    assert (budget.max_wall_seconds, budget.max_tool_calls, budget.max_verify_rounds) == (300, 30, 2)


def test_there_is_room_below_every_limit() -> None:
    assert _check(Budget(), elapsed=299.9, tools=29, rounds=1) is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"elapsed": 300}, "time limit (300 seconds)"),
        ({"tools": 30}, "tool-call limit (30)"),
        ({"rounds": 2}, "verification round limit (2)"),
    ],
)
def test_each_limit_is_reported_by_name_once_reached(kwargs: dict[str, float], expected: str) -> None:
    reason = _check(Budget(), **kwargs)
    assert reason is not None and expected in reason and reason.endswith("was reached")


def test_when_several_limits_are_hit_the_time_limit_is_named_first() -> None:
    assert "time limit" in (_check(Budget(), elapsed=999, tools=99, rounds=9) or "")


def test_a_budget_is_immutable_and_each_context_gets_its_own_start_time() -> None:
    with pytest.raises(AttributeError):
        Budget().max_tool_calls = 99  # type: ignore[misc]

    def make() -> InvestigationContext:
        return InvestigationContext(tenant_id=None, logs=FakeLogs(), incidents=FakeApi(), history=FakeHistory())  # type: ignore[arg-type]

    first = make()
    time.sleep(0.01)
    assert make().started_at > first.started_at
