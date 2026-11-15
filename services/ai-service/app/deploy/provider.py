"""The deploy provider port: ask the customer's pipeline to deploy a commit, then follow the run.

log0 never deploys itself. A provider only *asks* (``dispatch``) and *looks* (``find_run``, ``run_status``,
``cancel``). The request is a small value whose fields are checked again here, at the last door before the pipeline:
a full commit sha, a plain workflow file name, an environment token and a correlation id log0 generated. Nothing else
can be sent, so no text of ours (or an attacker's) can reach a workflow input.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.deploy.target import ENVIRONMENT_NAME, WORKFLOW_FILE
from app.integrations.github_writer import WriteTarget
from app.schemas.remediation import DeploymentStatus

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class DispatchRequest:
    workflow_file: str
    ref: str  # the branch whose copy of the workflow runs (the default branch)
    sha: str  # the commit the workflow must deploy
    environment: str
    correlation_id: UUID  # printed in the run's name so log0 can find its own run again

    def __post_init__(self) -> None:
        if not _SHA.fullmatch(self.sha):
            raise ValueError("a deploy names a full commit sha")
        if not ENVIRONMENT_NAME.fullmatch(self.environment):
            raise ValueError("a deploy names an environment token")
        if not WORKFLOW_FILE.fullmatch(self.workflow_file):
            raise ValueError("a deploy runs a workflow file name")

    def inputs(self) -> dict[str, str]:
        """The only inputs a workflow ever receives from log0."""
        return {"sha": self.sha, "environment": self.environment, "correlation_id": str(self.correlation_id)}


@dataclass(frozen=True)
class RunInfo:
    run_id: int
    url: str
    status: DeploymentStatus


class DeployProvider(Protocol):
    async def dispatch(self, target: WriteTarget, request: DispatchRequest) -> None: ...

    async def find_run(self, target: WriteTarget, request: DispatchRequest) -> RunInfo | None: ...

    async def run_status(self, target: WriteTarget, run_id: int) -> RunInfo: ...

    async def cancel(self, target: WriteTarget, run_id: int) -> None: ...


def status_of(run: dict[str, Any]) -> DeploymentStatus:
    """GitHub's run (``status``, ``conclusion``) as log0's deployment status.

    A run that ended without deploying (skipped, neutral) counts as failed: nothing is running the new commit.
    """
    if run.get("status") != "completed":
        return "running" if run.get("status") in ("in_progress", "waiting", "pending") else "dispatched"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "succeeded"
    if conclusion == "cancelled":
        return "cancelled"
    return "failed"
