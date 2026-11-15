"""The state and the run context of the remediation graph.

The state is plain JSON (strings, numbers, dicts): it is checkpointed after every step, and a resumed run must read
exactly what the run before it wrote. Everything with a lifecycle of its own (the status, the deployment, the approvals)
lives in the database and is read from there; the state only carries what the next node needs to decide.

The context is what a run *has*, not what it knows: the tenant, the backends, the clock. It is never checkpointed, so a
resumed run cannot be pointed at another tenant.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict
from uuid import UUID

from app.config import Settings
from app.deploy.provider import DeployProvider
from app.integrations.github_app_client import InstallationInfo
from app.integrations.github_deploys import DeploysPort
from app.integrations.github_writer import WriteTarget
from app.runner import SessionFactory
from app.schemas.domain import CheckResult
from app.tools.incidents import IncidentApi
from app.tools.logs import LogBackend

Outcome = str  # refused, declined, deployed, failed


class RemediationState(TypedDict, total=False):
    remediation_id: str
    investigation_id: str
    environment: str
    merge_sha: str
    rule: dict[str, Any]  # the environment's rule when preflight passed (approver, soak, timeouts)
    rollback_target: str | None
    baseline: dict[str, Any]
    fingerprint: str  # of the investigated error, kept from the baseline so the soak need not ask the incident service again
    service: str
    deployed_at: str  # when the pipeline finished: the soak counts from here
    soak_ends_at: str | None
    soak_extensions: int
    judgement: dict[str, Any] | None  # the verdict once decided: verdict, reasons, numbers, window
    decision: str | None
    approval: dict[str, Any]
    wake_at: str | None  # set while the run waits for a timer; the executor stores it for the sweeper
    outcome: Outcome | None
    gaps: list[str]


def new_state(*, remediation_id: UUID, investigation_id: UUID, environment: str, merge_sha: str) -> RemediationState:
    return RemediationState(
        remediation_id=str(remediation_id),
        investigation_id=str(investigation_id),
        environment=environment,
        merge_sha=merge_sha,
        rollback_target=None,
        wake_at=None,
        outcome=None,
        gaps=[],
    )


class CheckReader(Protocol):
    async def check_runs(self, target: WriteTarget, sha: str, permissions: Any = ...) -> list[CheckResult]: ...


class InstallationReader(Protocol):
    async def get_installation(self, installation_id: int) -> InstallationInfo: ...


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RemediationContext:
    """Read-only, per run. Nodes get it as ``runtime.context``."""

    tenant_id: UUID  # from the remediation row the runner verified, never from the request body
    sessions: SessionFactory
    provider: DeployProvider
    deploys: DeploysPort
    checks: CheckReader
    installations: InstallationReader
    logs: LogBackend
    incidents: IncidentApi
    settings: Settings
    now: Callable[[], datetime] = field(default=utcnow)
