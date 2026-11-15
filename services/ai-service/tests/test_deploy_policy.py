"""The deploy policy: every rule on its own, all reasons together, and the approver roles. No I/O."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.deploy.policy import (
    DeployFacts,
    can_approve,
    check_start,
    in_freeze_window,
    migration_paths,
    paths_in_diff,
    switches,
)
from app.deploy.target import EnvironmentRule, FreezeWindow

MONDAY_NOON = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)  # a Monday
FRIDAY_16H = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)


def _rule(**overrides) -> EnvironmentRule:
    return EnvironmentRule.model_validate({"name": "production", "enabled": True} | overrides)


def _facts(**overrides) -> DeployFacts:
    base = DeployFacts(
        environment="production",
        rule=_rule(),
        server_enabled=True,
        tenant_enabled=True,
        changed_paths=("services/api/src/db.js",),
        now=MONDAY_NOON,
        deploys_last_hour=0,
        max_per_hour=3,
        ci_outcome="passed",
    )
    return replace(base, **overrides)


def test_an_ordinary_deploy_is_allowed() -> None:
    decision = check_start(_facts())

    assert decision.allowed and decision.reasons == []


@pytest.mark.parametrize("ci", ["passed", "none"])
def test_a_repository_without_checks_is_not_blocked_but_a_failing_one_is(ci: str) -> None:
    assert check_start(_facts(ci_outcome=ci)).allowed
    assert check_start(_facts(ci_outcome=None)).allowed  # not read: the preflight node decides whether that is acceptable


@pytest.mark.parametrize("ci", ["failed", "timeout"])
def test_checks_that_did_not_pass_block(ci: str) -> None:
    decision = check_start(_facts(ci_outcome=ci))

    assert not decision.allowed and "checks" in decision.reasons[0]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"server_enabled": False}, "this server"),
        ({"tenant_enabled": False}, "this workspace"),
        ({"rule": _rule(enabled=False)}, "switched off"),
        ({"rule": None}, "not an environment"),
        ({"deploys_last_hour": 3}, "3 deploys in the last hour"),
        ({"changed_paths": ("db/migrate/001_users.rb",)}, "migration"),
    ],
)
def test_each_rule_blocks_and_says_why(overrides: dict, fragment: str) -> None:
    decision = check_start(_facts(**overrides))

    assert not decision.allowed
    assert any(fragment in reason for reason in decision.reasons)


def test_every_reason_is_listed_not_only_the_first() -> None:
    decision = check_start(_facts(server_enabled=False, tenant_enabled=False, ci_outcome="failed", deploys_last_hour=9))

    assert len(decision.reasons) == 4


def test_an_unknown_environment_stops_early_with_one_clear_reason() -> None:
    decision = check_start(_facts(rule=None, environment="qa"))

    assert decision.reasons == ["'qa' is not an environment of this repository's deploy target"]


def test_the_previous_environment_must_have_verified_when_the_rule_says_so() -> None:
    needs = _rule(requiresPreviousVerified=True)

    blocked = check_start(_facts(rule=needs, previous_environment="staging", previous_verified=False))
    allowed = check_start(_facts(rule=needs, previous_environment="staging", previous_verified=True))
    free = check_start(_facts(previous_verified=False))  # the rule does not ask for it

    assert not blocked.allowed and "'staging' has not verified" in blocked.reasons[0]
    assert allowed.allowed and free.allowed


def test_a_freeze_window_blocks_only_inside_its_hours_and_days() -> None:
    friday = _rule(freezeWindows=[{"days": [4], "startHour": 15, "endHour": 24}])

    assert not check_start(_facts(rule=friday, now=FRIDAY_16H)).allowed
    assert check_start(_facts(rule=friday, now=datetime(2026, 11, 20, 14, 59, tzinfo=UTC))).allowed  # before it starts
    assert check_start(_facts(rule=friday, now=MONDAY_NOON)).allowed  # another day
    assert not in_freeze_window(friday.freeze_windows, datetime(2026, 11, 21, 3, 0, tzinfo=UTC))  # Saturday morning is not Friday


@pytest.mark.parametrize("window", [{"days": [], "startHour": 1, "endHour": 2}, {"days": [7], "startHour": 1, "endHour": 2}, {"days": [1], "startHour": 5, "endHour": 5}, {"days": [1], "startHour": 22, "endHour": 6}, {"days": [1], "startHour": 0, "endHour": 25}])
def test_a_freeze_window_must_make_sense(window: dict) -> None:
    with pytest.raises(ValidationError):
        FreezeWindow.model_validate(window)


@pytest.mark.parametrize(
    ("role", "approver", "allowed"),
    [("ADMIN", "ADMIN", True), ("ENGINEER", "ADMIN", False), ("VIEWER", "ADMIN", False), ("ADMIN", "ENGINEER", True), ("ENGINEER", "ENGINEER", True), ("VIEWER", "ENGINEER", False), ("GUEST", "ENGINEER", False), ("", "ENGINEER", False)],
)
def test_who_may_approve(role: str, approver: str, allowed: bool) -> None:
    rule = EnvironmentRule.model_validate({"name": "staging", "approver": approver})

    assert can_approve(role, rule) is allowed


@pytest.mark.parametrize(
    ("path", "risky"),
    [
        ("db/migrate/20260101_add_users.rb", True),
        ("services/api/migrations/001_init.js", True),
        ("alembic/versions/a1_users.py", True),
        ("prisma/schema.prisma", True),
        ("schema.prisma", True),
        ("src/seed.sql", True),
        (r"db\migrate\x.rb", True),
        ("src/migrate-helper.js", False),
        ("src/db.js", False),
        ("docs/migrations-guide.md", False),
        ("src/transmigration/a.js", False),
    ],
)
def test_migration_paths(path: str, risky: bool) -> None:
    assert bool(migration_paths([path])) is risky


def test_the_files_of_a_diff_are_read_in_order_without_duplicates() -> None:
    diff = "--- a/x.js\n+++ b/x.js\n@@ -1 +1 @@\n-a\n+b\n--- a/y/z.go\n+++ b/y/z.go\n@@ -1 +1 @@\n-a\n+b\n--- a/x.js\n+++ b/x.js\n"

    assert paths_in_diff(diff) == ("x.js", "y/z.go")
    assert paths_in_diff("") == () and paths_in_diff(None) == ()


def test_the_server_switches_default_to_off_and_a_workspace_can_be_listed_off() -> None:
    from uuid import uuid4

    tenant, other = uuid4(), uuid4()

    assert switches(Settings(_env_file=None), tenant) == (False, True)  # type: ignore[call-arg]  # deploys are opt-in on a server
    on = Settings(_env_file=None, deploys_enabled=True, deploys_disabled_tenants={other})  # type: ignore[call-arg]
    assert switches(on, tenant) == (True, True)
    assert switches(on, other) == (True, False)
