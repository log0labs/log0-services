"""The deploy policy: may log0 start a deploy of this merged fix to this environment, and who may approve it? Pure functions.

Same shape as ``publishing/policy.py``: facts go in, a ``PolicyDecision`` with every reason comes out, and nothing here
reads a clock, a database or GitHub (the caller passes ``now`` and the facts). All the reasons are collected, not only the
first, so the person who has to act sees everything that stands in the way.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from app.deploy.target import EnvironmentRule, FreezeWindow
from app.schemas.remediation import PolicyDecision

if TYPE_CHECKING:
    from app.config import Settings

_MIGRATION_PATH = re.compile(
    r"(^|/)(migrations?|migrate|alembic|flyway|liquibase|prisma/migrations|db/migrate|schema/versions)(/|$)"
    r"|(^|/)schema\.prisma$|\.sql$",
    re.IGNORECASE,
)
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.MULTILINE)
_ROLE_RANK = {"VIEWER": 0, "ENGINEER": 1, "ADMIN": 2}


@dataclass(frozen=True)
class DeployFacts:
    """Everything the policy needs to know, gathered by the caller."""

    environment: str
    rule: EnvironmentRule | None
    server_enabled: bool
    tenant_enabled: bool
    changed_paths: tuple[str, ...]
    now: datetime
    deploys_last_hour: int
    max_per_hour: int
    ci_outcome: str | None  # passed, failed, none, timeout, or None when it was not read
    previous_environment: str | None = None  # the environment this one waits for, if its rule says so
    previous_verified: bool = False


def switches(settings: "Settings", tenant_id: UUID) -> tuple[bool, bool]:
    """The two kill switches the server owns: ``(server enabled, this workspace enabled)``. The third, per environment, is in its rule."""
    return settings.deploys_enabled, tenant_id not in settings.deploys_disabled_tenants


def paths_in_diff(diff: str | None) -> tuple[str, ...]:
    """The files a unified diff changes, in order, without duplicates."""
    return tuple(dict.fromkeys(_DIFF_FILE.findall(diff or "")))


def migration_paths(paths: Iterable[str]) -> list[str]:
    """The paths that look like a database migration or schema change (a rollback cannot undo those)."""
    return [p for p in paths if _MIGRATION_PATH.search(p.replace("\\", "/"))]


def in_freeze_window(windows: Iterable[FreezeWindow], now: datetime) -> bool:
    return any(now.weekday() in w.days and w.start_hour <= now.hour < w.end_hour for w in windows)


def can_approve(role: str, rule: EnvironmentRule) -> bool:
    """Whether a user of ``role`` may approve a deploy to the environment: at least the rule's role."""
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK[rule.approver]


def check_start(facts: DeployFacts) -> PolicyDecision:
    """May this deploy start? ``allowed`` is true only when no reason stands in the way."""
    reasons: list[str] = []
    if not facts.server_enabled:
        reasons.append("deploys are switched off on this server")
    if not facts.tenant_enabled:
        reasons.append("deploys are switched off for this workspace")
    rule = facts.rule
    if rule is None:
        reasons.append(f"'{facts.environment}' is not an environment of this repository's deploy target")
        return PolicyDecision(allowed=False, reasons=reasons)
    if not rule.enabled:
        reasons.append(f"deploys to '{facts.environment}' are switched off")
    if in_freeze_window(rule.freeze_windows, facts.now):
        reasons.append(f"'{facts.environment}' is in a freeze window right now")
    risky = migration_paths(facts.changed_paths)
    if risky:
        reasons.append(f"the change touches a migration or schema file ({', '.join(risky[:3])}); a rollback cannot undo data changes")
    if facts.ci_outcome in ("failed", "timeout"):
        reasons.append("the checks of the merge commit did not pass")
    if rule.requires_previous_verified and not facts.previous_verified:
        reasons.append(f"'{facts.previous_environment or 'the previous environment'}' has not verified this change yet")
    if facts.deploys_last_hour >= facts.max_per_hour:
        reasons.append(f"'{facts.environment}' already had {facts.deploys_last_hour} deploys in the last hour")
    return PolicyDecision(allowed=not reasons, reasons=reasons)
