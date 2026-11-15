"""What a tenant tells log0 about deploying one repository: the workflow to ask for, and the rules of each environment.

Everything here is *data with a strict shape*. There are no commands, no free-text inputs, no URLs: a name is a short
lowercase token, a workflow is a file name in ``.github/workflows``, a number has a range. The shape is the security
control: whatever reaches the customer's pipeline was parsed by these models first.
"""

import re
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.base import CamelModel

ENVIRONMENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.ya?ml$")
MAX_ENVIRONMENTS = 5
PRODUCTION_HINT = "prod"


class Thresholds(CamelModel):
    """What counts as better or worse after the deploy. The defaults are conservative; verification (step 14) reads them."""

    model_config = ConfigDict(extra="forbid")

    improvement_margin: float = Field(default=0.5, gt=0, lt=1)  # healthy needs the rate at most (1 - margin) of the baseline
    min_traffic: int = Field(default=100, ge=1, le=10_000_000)  # fewer log lines than this in the window: too little to judge
    new_error_per_minute: float = Field(default=5.0, gt=0, le=100_000)  # a new fingerprint above this is a regression
    grace_seconds: int = Field(default=120, ge=0, le=1800)  # right after the deploy: restarts and warm-up are not judged


class FreezeWindow(CamelModel):
    """Hours (UTC) on some weekdays in which log0 does not deploy here, for example Friday afternoons."""

    model_config = ConfigDict(extra="forbid")

    days: list[int] = Field(min_length=1, max_length=7)  # 0 is Monday, 6 is Sunday
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)

    @model_validator(mode="after")
    def _a_window_is_within_one_day(self) -> Self:
        if self.start_hour >= self.end_hour:
            raise ValueError("a freeze window ends after it starts, within one day (use two windows to cross midnight)")
        if any(day not in range(7) for day in self.days):
            raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        return self


class EnvironmentRule(CamelModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = False  # the per-environment kill switch: off until an admin turns it on
    approver: Literal["ADMIN", "ENGINEER"] = "ADMIN"  # the lowest role that may approve a deploy here
    requires_previous_verified: bool = False  # the previous environment in the list must have verified this change
    soak_seconds: int = Field(default=900, ge=60, le=7200)
    deploy_timeout_seconds: int = Field(default=1200, ge=60, le=7200)
    auto_rollback: bool = False  # opt-in: roll back without asking when the verdict is degraded with enough traffic
    freeze_windows: list[FreezeWindow] = Field(default_factory=list, max_length=7)
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @field_validator("name")
    @classmethod
    def _name_is_a_token(cls, value: str) -> str:
        if not ENVIRONMENT_NAME.fullmatch(value):
            raise ValueError("an environment name is lowercase letters, digits and dashes, starting with a letter or digit, at most 63 characters")
        return value

    @model_validator(mode="after")
    def _production_needs_an_admin(self) -> Self:
        if PRODUCTION_HINT in self.name and self.approver != "ADMIN":
            raise ValueError("an environment that looks like production can only be approved by an ADMIN")
        return self


class DeployTargetConfig(CamelModel):
    model_config = ConfigDict(extra="forbid")

    workflow_file: str
    environments: list[EnvironmentRule] = Field(min_length=1, max_length=MAX_ENVIRONMENTS)

    @field_validator("workflow_file")
    @classmethod
    def _workflow_is_a_file_name(cls, value: str) -> str:
        if not WORKFLOW_FILE.fullmatch(value):
            raise ValueError("the workflow is a file name such as deploy.yml (no folders, no spaces)")
        return value

    @model_validator(mode="after")
    def _environments_are_a_sensible_list(self) -> Self:
        names = [environment.name for environment in self.environments]
        if len(set(names)) != len(names):
            raise ValueError("environment names must be unique")
        if self.environments[0].requires_previous_verified:
            raise ValueError("the first environment has no previous one to wait for")
        return self

    def rule_for(self, name: str) -> EnvironmentRule | None:
        return next((e for e in self.environments if e.name == name), None)

    def previous_of(self, name: str) -> EnvironmentRule | None:
        """The environment before ``name`` in the promotion order, or ``None`` for the first (or an unknown name)."""
        names = [e.name for e in self.environments]
        return self.environments[names.index(name) - 1] if name in names and names.index(name) > 0 else None
