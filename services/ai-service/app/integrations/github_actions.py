"""``DeployProvider`` for GitHub Actions: dispatch one workflow, find its run by the correlation id, follow it.

Uses the deploy-scoped installation token (``DEPLOY_PERMISSIONS``): it can run a workflow and read runs, and it cannot
write a commit. The customer's workflow must accept the three inputs (``sha``, ``environment``, ``correlation_id``) and
print the correlation id in its run name (``run-name: Deploy ${{ inputs.correlation_id }}``); that is how log0 finds the
run it started, because GitHub's dispatch answer carries no run id.
"""

import re
from typing import Any

from app.deploy.provider import DispatchRequest, RunInfo, status_of
from app.integrations.github import GitHubUnavailableError
from app.integrations.github_app import DEPLOY_PERMISSIONS
from app.integrations.github_writer import GitHubWriter, WriteTarget

RUNS_LOOKED_AT = 30
_CANCEL_ACCEPTED = (202, 409)  # 409: the run already finished, which is what we wanted


class GitHubActionsProvider:
    def __init__(self, writer: GitHubWriter) -> None:
        self._writer = writer

    async def dispatch(self, target: WriteTarget, request: DispatchRequest) -> None:
        """Start the workflow. ``WriteRejectedError`` when GitHub refuses (no such workflow, no ``workflow_dispatch`` trigger)."""
        repo = self._writer._repo(target)
        body = {"ref": request.ref, "inputs": request.inputs()}
        async with self._writer.session(target, DEPLOY_PERMISSIONS) as call:
            await call("POST", f"{repo}/actions/workflows/{request.workflow_file}/dispatches", json=body, expected=(204,))

    async def find_run(self, target: WriteTarget, request: DispatchRequest) -> RunInfo | None:
        """The run this request started, found by its correlation id in the run's name; ``None`` while GitHub has not made it."""
        repo = self._writer._repo(target)
        params = {"event": "workflow_dispatch", "per_page": str(RUNS_LOOKED_AT)}
        async with self._writer.session(target, DEPLOY_PERMISSIONS) as call:
            response = await call("GET", f"{repo}/actions/workflows/{request.workflow_file}/runs", params=params)
        marker = re.compile(re.escape(str(request.correlation_id)))
        try:
            for run in response.json()["workflow_runs"]:
                if marker.search(str(run.get("display_title") or "")) or marker.search(str(run.get("name") or "")):
                    return _info(run)
        except (KeyError, TypeError, ValueError):
            raise GitHubUnavailableError from None
        return None

    async def run_status(self, target: WriteTarget, run_id: int) -> RunInfo:
        repo = self._writer._repo(target)
        async with self._writer.session(target, DEPLOY_PERMISSIONS) as call:
            response = await call("GET", f"{repo}/actions/runs/{int(run_id)}")
        try:
            return _info(response.json())
        except (KeyError, TypeError, ValueError):
            raise GitHubUnavailableError from None

    async def cancel(self, target: WriteTarget, run_id: int) -> None:
        repo = self._writer._repo(target)
        async with self._writer.session(target, DEPLOY_PERMISSIONS) as call:
            await call("POST", f"{repo}/actions/runs/{int(run_id)}/cancel", expected=_CANCEL_ACCEPTED)


def _info(run: dict[str, Any]) -> RunInfo:
    return RunInfo(run_id=int(run["id"]), url=str(run.get("html_url") or ""), status=status_of(run))
